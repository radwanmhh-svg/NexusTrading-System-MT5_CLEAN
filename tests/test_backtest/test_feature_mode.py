"""B-01 — default feature mode is the FULL live vector.

lean=True (diagnostics only) is NOT decision-equivalent:
RuleBasedSignalGenerator consumes trend_direction / trend_strength /
vol_regime / atr_normalized, so the engine defaults to lean=False — the
exact 300-bar live publisher window with the full feature dict.
"""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture(scope="module")
def snapshot():
    from services.nexus_backtest.dataset import load_snapshot
    return load_snapshot()


def test_default_run_uses_full_live_feature_vector(snapshot):
    """Default engine runs must compute the full live vector (trend trio,
    vol_regime, session included) — verified via the on_bar hook."""
    from services.nexus_backtest.engine import BacktestEngine

    df = snapshot
    end = int(df["timestamp"].searchsorted(pd.Timestamp("2026-10-20", tz="UTC")))
    start = end - 40
    seen: dict = {}
    eng = BacktestEngine(initial_balance=100_000)
    eng.run(df, start_idx=start, end_idx=end,
            on_bar=lambda i, feats: seen.update(feats))
    for key in ("trend_direction", "trend_strength", "trend_score",
                "vol_regime", "atr_normalized", "session"):
        assert key in seen, f"full vector missing {key}"


def test_dead_regime_stub_executes_live_adx_fallback(snapshot):
    """The engine's dead-model stub routes regime_from_features into its own
    ADX fallback — identical to live's effective behaviour (43≠41)."""
    from services.nexus_decision import regime_from_features
    from services.nexus_backtest.engine import BacktestEngine

    BacktestEngine(initial_balance=100_000)._pin_dead_regime_stub()
    assert regime_from_features({"adx": 30.0}) == "TRENDING"
    assert regime_from_features({"adx": 10.0}) == "RANGING"
