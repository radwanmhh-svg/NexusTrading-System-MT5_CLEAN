"""B-01 — Live-faithful feature computation for the backtest engine.

Replicates the live feature pipeline (`services/nexus_features/main.py`
`_handle_feature_event`) on a trailing closed-bar window:

- window length = NEXUS_OHLCV_BAR_COUNT (default 300) — the exact window the
  live publisher feeds to the indicator engine.
- indicators come from `services.nexus_features.indicators.calculate_all`
  (the same production module — no duplicated math, Directive rule #5).
- session/trend/regime/tick-imbalance come from the same production modules.

Known honest limitations (documented in the B-01 report):
- live-only context features (cross-asset DXY/US10Y) are absent historically
  (no point-in-time historical store — A-05 pending) → reported as missing
  (0.0), exactly like the live fallback when the cache is empty.
- economic-calendar features ARE reproduced point-in-time from the local
  calendar (leakage-safe `nexus_economic.build_features(now=bar_time)`).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from shared.logger import get_logger

logger = get_logger("backtest.features")

ROOT = Path(__file__).resolve().parents[2]

FEATURE_WINDOW = int(os.getenv("NEXUS_OHLCV_BAR_COUNT", "300"))

_ECONOMIC_EVENTS: list | None = None


def _economic_events():
    global _ECONOMIC_EVENTS
    if _ECONOMIC_EVENTS is None:
        try:
            from services.nexus_economic import load_events
            _ECONOMIC_EVENTS = load_events()
        except Exception:
            logger.warning("backtest.features economic calendar unavailable — gate stays neutral")
            _ECONOMIC_EVENTS = []
    return _ECONOMIC_EVENTS


MIN_BARS_FOR_FEATURES = 50  # same threshold as the live publisher (features.insufficient_data)


def compute_features_at(
    df: pd.DataFrame, i: int, window: int = FEATURE_WINDOW, lean: bool = False,
) -> dict | None:
    """Compute the live feature vector using only closed bars [0..i].

    Mirrors nexus_features.main._handle_feature_event on the trailing
    `window`-bar window (identical to the live publisher's window).
    Returns None exactly like the live publisher when there is
    insufficient data (< 50 bars) — no features, no signal.
    """
    lo = max(0, i - window + 1)
    w = df.iloc[lo : i + 1].copy()
    if len(w) < MIN_BARS_FOR_FEATURES:
        return None

    from services.nexus_features.indicators import calculate_all
    from services.nexus_features.main import _extract_features
    from services.nexus_features.session_tagger import tag_dataframe
    from services.nexus_features.trend_analyzer import detect_trend, calculate_trend_score
    from services.nexus_features.volatility import calculate_atr_normalized, detect_regime

    ind = calculate_all(w)

    if lean:
        # Decision-equivalent reduction (verified by test): the current live
        # decision chain (rule-based signal + decide() gates) consumes only
        # indicator columns + adx + atr + tick-imbalance + economic features.
        # trend trio / vol_regime / atr_normalized / session are computed by
        # live but do NOT change any decision in this path; skipping them is
        # proven trade-identical in tests/test_backtest.
        features = _extract_features(ind)
    else:
        ind = tag_dataframe(ind)
        features = _extract_features(ind)

        trend = detect_trend(ind)
        features["trend_direction"] = trend.direction.value
        features["trend_strength"] = trend.strength
        features["trend_score"] = calculate_trend_score(ind)

        atr_n = calculate_atr_normalized(ind)
        features["atr_normalized"] = _safe_float(atr_n.iloc[-1])
        features["vol_regime"] = str(detect_regime(ind).iloc[-1])

        # session from the BAR's time (live uses wall clock; backtest uses the bar)
        features["session"] = str(ind["session"].iloc[-1])
        features["session_overlap"] = bool(ind.get("session_overlap", pd.Series(False)).iloc[-1])

    features["close"] = _safe_float(ind["close"].iloc[-1])
    features["atr_14"] = _safe_float(ind["atr"].iloc[-1]) if "atr" in ind else 0.0

    from services.nexus_features.main import _compute_tick_imbalance
    features["tick_imbalance_20"] = _compute_tick_imbalance(ind, window=20)
    features["tick_imbalance_5"] = _compute_tick_imbalance(ind, window=5)

    # economic calendar — point-in-time, leakage-safe
    try:
        from services.nexus_economic import build_features
        bar_ts = pd.Timestamp(df["timestamp"].iloc[i]).to_pydatetime()
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        features.update(build_features(_economic_events(), now=bar_ts))
    except Exception:
        pass

    return features


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return round(f, 6) if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None
