"""B-01 — Historical M5 dataset (closed candles) from the single live store.

Reads OHLCV through the live nexus-data API (the same DuckDB+WAL state the
live services see). This avoids copying the locked `nexus.duckdb` file and
touches nothing: pure reads.

The resulting snapshot (Parquet) is the shared historical dataset for
B-01 (backtest engine) and M-01 (honest retraining) per the Remediation
Directive.

All bars are CLOSED candles: the store was fixed by B-03 to only persist
closed bars (closed_only worker) and the imported OneRoyal history contains
only completed candles by construction.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from shared.bar_contract import validate_ohlcv_bar
from shared.logger import get_logger

logger = get_logger("backtest.dataset")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT = ROOT / "data" / "backtest" / "m5_xauusd.parquet"
NEXUS_DATA_URL = os.getenv("NEXUS_DATA_URL", "http://localhost:9001")
PAGE_LIMIT = 5000          # API hard limit (le=5000)


@dataclass
class SnapshotMeta:
    symbol: str
    timeframe: str
    bars: int
    start: str
    end: str
    fetched_at: str
    source: str = "nexus-data:/api/data/ohlcv (live store, closed bars only)"


def _parse_ts(v: str) -> pd.Timestamp:
    ts = pd.Timestamp(v)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.tz_convert(timezone.utc)


def _fetch_page(symbol: str, timeframe: str, end: str, limit: int) -> list[dict]:
    import urllib.parse
    import urllib.request

    url = (
        f"{NEXUS_DATA_URL}/api/data/ohlcv/{symbol}/{timeframe}"
        f"?limit={limit}&end={urllib.parse.quote(end, safe='') }"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.load(resp)
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected payload from ohlcv API: {type(payload)}")
    return payload


def build_snapshot(
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    months: float = 13.0,
    out_path: Path = DEFAULT_SNAPSHOT,
) -> tuple[pd.DataFrame, SnapshotMeta]:
    """Pull `months` of closed candles via paged API reads and save a Parquet snapshot.

    Pages backwards in time using the `end` cursor (the endpoint returns the
    newest bars before `end`, descending) until the requested horizon is
    covered, then returns bars in ascending order.
    """
    end = datetime.now(timezone.utc)
    start_target = end - timedelta(days=int(months * 30.44) + 3)
    rows: list[dict] = []
    cursor = end.isoformat()
    page = 0
    while True:
        page += 1
        batch = _fetch_page(symbol, timeframe, cursor, PAGE_LIMIT)
        if not batch:
            break
        rows.extend(batch)
        oldest = _parse_ts(batch[-1]["timestamp"])
        cursor = batch[-1]["timestamp"]
        logger.info(
            "backtest.dataset page=%d got=%d oldest=%s total=%d",
            page, len(batch), oldest, len(rows),
        )
        if oldest <= pd.Timestamp(start_target) or len(batch) < PAGE_LIMIT:
            break
        time.sleep(0.05)

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("no OHLCV rows returned by the live store")
    df["timestamp"] = df["timestamp"].map(_parse_ts)
    df = df.drop_duplicates(subset="timestamp", keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    meta = SnapshotMeta(
        symbol=symbol, timeframe=timeframe, bars=len(df),
        start=df["timestamp"].iloc[0].isoformat(),
        end=df["timestamp"].iloc[-1].isoformat(),
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    (out_path.with_suffix(".meta.json")).write_text(json.dumps(meta.__dict__, indent=2))
    logger.info(
        "backtest.dataset saved %s bars %s..%s -> %s",
        meta.bars, meta.start, meta.end, out_path,
    )
    return df, meta


def validate_snapshot(df: pd.DataFrame, min_months: float = 12.0) -> list[str]:
    """Honest data checks. Returns a list of problems (empty = clean)."""
    problems: list[str] = []
    if df.empty:
        return ["empty snapshot"]
    if not df["timestamp"].is_monotonic_increasing:
        problems.append("timestamps not ascending")
    if df["timestamp"].duplicated().any():
        problems.append("duplicate timestamps present")
    # OHLC sanity on a sample + first/last (full loop is fine too, it is cheap)
    n = len(df)
    sample = list(range(0, n, max(1, n // 2000))) + [n - 1]
    for i in sample:
        row = df.iloc[i]
        try:
            validate_ohlcv_bar(
                {
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "timestamp": row["timestamp"].isoformat(),
                },
                context=f"snapshot row {i}",
            )
        except ValueError as exc:
            problems.append(f"invalid bar at row {i}: {exc}")
            break
    span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 86400
    if span_days < min_months * 30.44:
        problems.append(
            f"coverage {span_days:.1f} days < required {min_months * 30.44:.1f} days"
        )
    # weekend gaps are expected for XAUUSD; flag only in-week gaps > 30 min
    gaps = df["timestamp"].diff().dt.total_seconds()
    weekday = df["timestamp"].dt.weekday
    big = gaps[(gaps > 1800) & (weekday > 0)]  # Mon..Fri
    # Friday->Monday transitions show up as Mon gaps; filter those out
    big = big[~((weekday.loc[big.index] == 0) & (big > 3600 * 24))]
    if len(big) > 0:
        problems.append(f"{len(big)} unexpected intraweek gaps > 30min (max {big.max()/60:.0f} min)")
    return problems


def load_snapshot(path: Path = DEFAULT_SNAPSHOT) -> pd.DataFrame:
    df = pd.read_parquet(path)
    return df
