"""B-01 — backtest CLI.

Usage:
  python scripts/run_backtest.py                      # full snapshot run
  python scripts/run_backtest.py --replay             # last 7 days vs live trades
  python scripts/run_backtest.py --start 2026-06-01 --end 2026-09-01

Writes a JSON report into data/backtest/reports/. Read-only on live data.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.logger import get_logger  # noqa: E402

logger = get_logger("backtest.cli")

# bulk runs: per-bar diagnostic noise off (signals still computed identically)
logging.getLogger("services.nexus_ml.rule_based_signal").setLevel(logging.WARNING)
try:
    import lightgbm.basic
    lightgbm.basic._LOGGER.setLevel(logging.CRITICAL)
except Exception:
    pass

REPORTS_DIR = ROOT / "data" / "backtest" / "reports"


def _run_window(df, start: int, end: int, label: str, extra: dict | None = None) -> dict:
    from services.nexus_backtest.engine import BacktestEngine

    # The trained LGBM regime model is dead in live too (43≠41 after B-03) —
    # live effectively executes the deterministic ADX fallback every bar.
    # Pin the same effective behaviour instead of raising thousands of
    # LightGBM fatal logs; the fallback is inside decide() itself.
    import services.nexus_decision as _nd
    _nd._REGIME_BOOSTER = None

    eng = BacktestEngine()
    t0 = time.time()
    res = eng.run(df, start_idx=start, end_idx=end)
    summary = res.summary()
    summary["elapsed_s"] = round(time.time() - t0, 1)
    summary["label"] = label
    if extra:
        summary.update(extra)
    summary["trades"] = [
        {
            "side": t.side,
            "signal_time": t.signal_time.isoformat(),
            "entry_time": t.entry_time.isoformat(),
            "entry_price": t.entry_price,
            "exit_time": t.exit_time.isoformat(),
            "exit_price": t.exit_price,
            "sl": t.sl,
            "tp": t.tp,
            "lot": t.lot,
            "net_pnl": t.net_pnl,
            "costs": t.costs,
            "exit_reason": t.exit_reason,
        }
        for t in res.trades
    ]
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="7-day replay vs live trades")
    ap.add_argument("--start", default=None, help="ISO start (UTC) inside the snapshot")
    ap.add_argument("--end", default=None, help="ISO end (UTC) inside the snapshot")
    ap.add_argument("--days", type=float, default=None, help="last N days of the snapshot")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from services.nexus_backtest.dataset import load_snapshot, validate_snapshot

    df = load_snapshot()
    problems = validate_snapshot(df)
    if problems:
        logger.warning("backtest.dataset problems: %s", problems)

    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "snapshot_bars": len(df)}

    if args.replay:
        from services.nexus_backtest.replay import compare_trades, fetch_live_trades

        end_ts = df["timestamp"].iloc[-1]
        window_start = end_ts - timedelta(days=7)
        warmup_start_idx = max(0, df["timestamp"].searchsorted(window_start) - 400)
        summary = _run_window(df, warmup_start_idx, len(df), "replay_7d")
        # only trades signalled inside the 7-day window count for the comparison
        replay_trades = [
            t for t in _rebuild_trades(summary)
            if pd.Timestamp(t.signal_time) >= window_start
        ]
        live = fetch_live_trades()
        rep = compare_trades(live, replay_trades, window_start)
        report["replay"] = rep.to_dict()
        report["replay"]["engine_summary"] = {
            k: v for k, v in summary.items() if k != "trades"
        }
        logger.info("replay passed=%s live=%d replay=%d", rep.passed, rep.live_trades, rep.replay_trades)
    else:
        start_idx, end_idx = 0, len(df)
        extra = {}
        if args.start or args.days:
            if args.days:
                end_ts = df["timestamp"].iloc[-1]
                start_ts = end_ts - timedelta(days=args.days)
            else:
                start_ts = pd.Timestamp(args.start, tz="UTC")
            start_idx = int(df["timestamp"].searchsorted(start_ts))
            extra["requested_start"] = str(start_ts)
        if args.end:
            end_ts = pd.Timestamp(args.end, tz="UTC")
            end_idx = int(df["timestamp"].searchsorted(end_ts))
            extra["requested_end"] = str(end_ts)
        label = args.days and f"last_{args.days}d" or (args.start and "custom_window" or "full_snapshot")
        report["run"] = _run_window(df, max(0, start_idx - 400), end_idx, label, extra)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORTS_DIR / f"b01_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in report.items() if k != "run" or args.replay}, indent=2, ensure_ascii=False)[:2000])
    logger.info("report saved: %s", out)


def _rebuild_trades(summary: dict) -> list[dict]:
    # engine summary already carries dicts; replay.py expects attribute access,
    # so wrap into light objects
    class _T:
        def __init__(self, d):
            self.side = d["side"]
            self.entry_price = d["entry_price"]
            self.signal_time = d["signal_time"]
            self.entry_time = d["entry_time"]

    return [_T(d) for d in summary["trades"]]


if __name__ == "__main__":
    main()
