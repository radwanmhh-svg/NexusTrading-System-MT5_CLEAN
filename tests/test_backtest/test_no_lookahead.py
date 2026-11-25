"""B-01 — no-lookahead tests: future bars must never influence past signals."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.nexus_backtest.dataset import validate_snapshot
from services.nexus_backtest.engine import BacktestEngine


def _synthetic(n: int = 1200, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-01-05 00:00", periods=n, freq="5min", tz="UTC")
    close = 3600 + np.cumsum(rng.normal(0, 0.4, n))
    o = close + rng.normal(0, 0.1, n)
    hi = np.maximum(o, close) + np.abs(rng.normal(0, 0.2, n)) + 0.1
    lo = np.minimum(o, close) - np.abs(rng.normal(0, 0.2, n)) - 0.1
    vol = rng.integers(50, 500, n).astype(float)
    return pd.DataFrame({"timestamp": ts, "open": o, "high": hi, "low": lo, "close": close, "volume": vol})


def test_features_immune_to_future_noise():
    """Perturbing all bars AFTER index i must not change features at i (B-03/B-01)."""
    from services.nexus_backtest.features import compute_features_at

    df = _synthetic()
    i = 600
    base = compute_features_at(df, i)

    noisy = df.copy()
    rng = np.random.default_rng(123)
    noisy.loc[i + 1 :, ["open", "high", "low", "close"]] += rng.normal(0, 3.0, (len(df) - i - 1, 4))
    after = compute_features_at(noisy, i)

    numeric_keys = [k for k, v in base.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    diffs = [k for k in numeric_keys if abs(float(base[k]) - float(after[k])) > 1e-9]
    assert diffs == [], f"future noise leaked into features at bar {i}: {diffs}"


def test_engine_trades_immune_to_future_noise():
    """Running the engine over [0, E) must give identical trades even if all
    bars after E are corrupted — proves fills/decisions never see the future."""
    df = _synthetic()
    E = 900
    eng = BacktestEngine(spread_pips=25, slippage_pips=1, initial_balance=10_000)
    r1 = eng.run(df.copy(), start_idx=300, end_idx=E)

    corrupted = df.copy()
    corrupted.loc[E:, ["open", "high", "low", "close"]] = 9999.0
    eng2 = BacktestEngine(spread_pips=25, slippage_pips=1, initial_balance=10_000)
    r2 = eng2.run(corrupted, start_idx=300, end_idx=E)

    assert r1.signals == r2.signals
    assert len(r1.trades) == len(r2.trades)
    for a, b in zip(r1.trades, r2.trades):
        assert a.entry_price == b.entry_price
        assert a.exit_price == b.exit_price
        assert a.net_pnl == b.net_pnl
        assert a.exit_reason == b.exit_reason


def test_engine_deterministic_rerun():
    df = _synthetic()
    eng1 = BacktestEngine(initial_balance=10_000)
    eng2 = BacktestEngine(initial_balance=10_000)
    r1 = eng1.run(df, start_idx=300, end_idx=1100)
    r2 = eng2.run(df, start_idx=300, end_idx=1100)
    s1, s2 = r1.summary(), r2.summary()
    s1.pop("rejected_by_gate"); s2.pop("rejected_by_gate")
    assert s1 == s2


@pytest.mark.parametrize("bad,expect_substr", [
    ("duplicate", "duplicate"),
    ("descending", "ascending"),
    ("short_span", "coverage"),
])
def test_validate_snapshot_flags(bad, expect_substr):
    df = _synthetic(600)
    if bad == "duplicate":
        df = pd.concat([df, df.iloc[[-1]]], ignore_index=True)
    elif bad == "descending":
        df = df.iloc[::-1].reset_index(drop=True)
    elif bad == "short_span":
        df = df.head(10)
    problems = validate_snapshot(df)
    assert any(expect_substr in p for p in problems), (bad, problems)
