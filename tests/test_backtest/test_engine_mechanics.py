"""B-01 — engine mechanics: fills on next open, costs, SL-first, sizing.

Windows shorter than 50 bars produce NO features (same as the live publisher's
`features.insufficient_data` behaviour). Every test prepends WARMUP realistic
closed bars (live window = 300) and appends crafted bars CONTINUING from the
last warm-up price (no artificial price jump — that would distort ATR).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.nexus_backtest import engine as eng_mod
from services.nexus_backtest.engine import BacktestEngine, PIP_SIZE

WARMUP = 320


@pytest.fixture()
def trending_regime(monkeypatch):
    """Force the deterministic ADX fallback inside decide() (no LGBM)."""
    monkeypatch.setattr(eng_mod, "load_spec", lambda symbol="XAUUSD": {
        "point": 0.01, "trade_tick_value": 1, "trade_tick_size": 0.01,
        "volume_min": 0.01, "volume_max": 35, "volume_step": 0.01,
        "trade_stops_level": 0, "trade_freeze_level": 0, "spread_points": 7,
    })
    import services.nexus_decision as nd
    monkeypatch.setattr(nd, "_REGIME_BOOSTER", None)
    # mechanics tests: neutral economic calendar (no blackout windows)
    import services.nexus_backtest.features as btf
    monkeypatch.setattr(btf, "_ECONOMIC_EVENTS", [])


def _bars(rows: list[tuple]) -> tuple[pd.DataFrame, float]:
    """Crafted rows (open,high,low,close,volume) appended after WARMUP bars.

    Crafted opens/closes are ABSOLUTE but the caller passes values relative
    to P = last warm-up close; returns (df, P). Signal bar = index WARMUP.
    """
    rng = np.random.default_rng(11)
    n = WARMUP
    ts = pd.date_range("2026-03-02 00:00", periods=n + len(rows), freq="5min", tz="UTC")
    close = 3600 + 0.08 * np.arange(n) + np.cumsum(rng.normal(0, 0.2, n))
    o = close + rng.normal(0, 0.05, n)
    hi = np.maximum(o, close) + np.abs(rng.normal(0, 0.08, n)) + 0.05
    lo = np.minimum(o, close) - np.abs(rng.normal(0, 0.08, n)) - 0.05
    warm = pd.DataFrame({"open": o, "high": hi, "low": lo, "close": close,
                         "volume": rng.integers(50, 500, n).astype(float)})
    P = float(close[-1])
    craft = pd.DataFrame(
        [[r[0] + P, r[1] + P, r[2] + P, r[3] + P, r[4]] for r in rows],
        columns=["open", "high", "low", "close", "volume"],
    )
    df = pd.concat([warm, craft], ignore_index=True)
    df.insert(0, "timestamp", ts)
    return df, P


def _force_signal(direction, fire_close: float):
    """Fires ONCE on the bar whose close == fire_close (the crafted signal bar);
    NO_TRADE otherwise — so warm-up bars never trade."""
    state = {"fired": False}

    def fn(features):
        if state["fired"]:
            return "NO_TRADE", 0.0
        if abs(float(features.get("close") or 0.0) - fire_close) < 1e-4:
            state["fired"] = True
            return direction, 0.95
        return "NO_TRADE", 0.0

    return fn


def test_buy_fill_at_next_open_plus_costs(trending_regime):
    df, P = _bars([
        (0, 0.5, -0.5, 0.05, 100),       # signal bar (index WARMUP): close = P+0.05
        (0, 1, -1, 0.5, 100),            # fill bar: open = P
        (1, 2, 0.5, 1.5, 100),
        (1.5, 2, 1, 1.8, 100),
        (1.8, 2, 1.5, 1.7, 100),
        (1.7, 2, 1, 1.9, 100),
        (1.9, 2.2, 1.5, 2, 100),
        (2, 2.5, 1.8, 2.3, 100),
        (2.3, 3, 2, 2.8, 100),
        (2.8, 3.5, 2.5, 3.2, 100),
        (3.2, 4, 3, 3.8, 100),
        (3.8, 4.2, 3.5, 4, 100),
    ])
    engine = BacktestEngine(
        signal_fn=_force_signal("BUY", P + 0.05),
        spread_pips=25, slippage_pips=1, commission_per_lot=0.0,
        initial_balance=100_000,
    )
    res = engine.run(df, start_idx=0, end_idx=len(df) - 1)
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t.side == "BUY"
    assert t.entry_price == pytest.approx(P + 25 * PIP_SIZE + 1 * PIP_SIZE)
    assert t.entry_time == df["timestamp"].iloc[WARMUP + 1]


def test_sell_exit_pays_full_spread(trending_regime):
    # Crafted gap bar guarantees the SELL stop is hit regardless of exact ATR.
    df, P = _bars([
        (0, 0.5, -0.5, 0.05, 100),       # signal bar: close = P+0.05
        (0, 1, -1, 0.5, 100),            # fill bar (open = P)
        (400, 402, 399, 401, 100),       # massive gap up → SELL stop hit for sure
        (401, 401.6, 400.5, 401.2, 100),
        (401.2, 401.5, 400.8, 401, 100),
        (401, 401.2, 400.5, 400.8, 100),
        (400.8, 401, 400.2, 400.4, 100),
        (400.4, 400.6, 399.8, 400, 100),
        (400, 400.3, 399.5, 400, 100),
        (400, 400.3, 399.5, 400.1, 100),
        (400.1, 400.4, 399.8, 400.2, 100),
        (400.2, 400.5, 399.9, 400.3, 100),
    ])
    engine = BacktestEngine(
        signal_fn=_force_signal("SELL", P + 0.05),
        spread_pips=25, slippage_pips=1, commission_per_lot=0.0,
        initial_balance=100_000,
    )
    res = engine.run(df, start_idx=0, end_idx=len(df) - 1)
    t = res.trades[0]
    assert t.side == "SELL"
    assert t.entry_price == pytest.approx(P - 1 * PIP_SIZE)
    assert t.exit_reason == "SL"
    # SELL SL = entry + 1×ATR (trigger on bid); exit ask = trigger + spread.
    # The fill bar's range exceeds 1×ATR → SL hits on the fill bar itself:
    # exit bid = max(open, sl) = sl (open below the stop) → ask = sl + spread.
    assert t.exit_price == pytest.approx(t.sl + 25 * PIP_SIZE)
    expected_net = ((t.entry_price - t.exit_price) / PIP_SIZE) * 1.0 * t.lot
    assert t.net_pnl == pytest.approx(expected_net, abs=0.02)
    assert t.costs == pytest.approx(25 * 1.0 * t.lot)  # full spread paid once


def test_sl_priority_when_both_touched(trending_regime):
    """Bar touching SL and TP simultaneously → conservative SL-first."""
    df, P = _bars([
        (0, 0.5, -0.5, 0.05, 100),       # signal bar: close = P+0.05
        (0.06, 0.06, -0.04, 0.03, 100),  # fill bar — inside stops (no hit)
        (1, 400, -400, 1, 100),          # hi ≥ tp AND lo ≤ sl → SL first
        (1, 1.2, 0.8, 1, 100),           # trailing bar (position already closed)
    ])
    engine = BacktestEngine(
        signal_fn=_force_signal("BUY", P + 0.05),
        spread_pips=25, slippage_pips=1, commission_per_lot=0.0,
        initial_balance=100_000,
    )
    res = engine.run(df, start_idx=0, end_idx=len(df) - 1)
    t = res.trades[0]
    assert t.exit_reason == "SL"


def test_commission_charged_per_lot(trending_regime):
    df, P = _bars([
        (0, 0.5, -0.5, 0.05, 100),
        (0, 1, -1, 0.5, 100),
        (1, 2, 0.5, 1.5, 100),
        (1.5, 2, 1, 1.8, 100),
        (1.8, 2, 1.5, 1.7, 100),
        (1.7, 2, 1, 1.9, 100),
        (1.9, 2.2, 1.5, 2, 100),
        (2, 2.5, 1.8, 2.3, 100),
        (2.3, 3, 2, 2.8, 100),
        (2.8, 3.5, 2.5, 3.2, 100),
        (3.2, 4, 3, 3.8, 100),
        (3.8, 4.2, 3.5, 4, 100),
    ])
    engine = BacktestEngine(
        signal_fn=_force_signal("BUY", P + 0.05),
        spread_pips=25, slippage_pips=1, commission_per_lot=7.0,
        initial_balance=100_000,
    )
    res = engine.run(df, start_idx=0, end_idx=len(df) - 1)
    t = res.trades[0]
    assert t.costs == pytest.approx(25 * t.lot + 7.0 * t.lot)


def test_end_of_data_close_marks_final_position(trending_regime):
    # Tiny crafted bars so the 1×ATR SL/TP are never touched after the fill;
    # the position must then be marked-to-market closed at end of data.
    df, P = _bars([
        (0, 0.05, -0.05, 0.05, 100),   # signal bar: close = P+0.05
        (0.02, 0.08, -0.04, 0.05, 100),  # fill bar (open = P) — range < 1×ATR
        (0.05, 0.09, 0.0, 0.06, 100),    # last bar — still inside stops
    ])
    engine = BacktestEngine(
        signal_fn=_force_signal("BUY", P + 0.05),
        spread_pips=25, slippage_pips=1, commission_per_lot=0.0,
        initial_balance=100_000,
    )
    res = engine.run(df, start_idx=0, end_idx=len(df) - 1)
    assert res.trades[-1].exit_reason == "end_of_data"


def test_insufficient_window_produces_no_features_and_no_signal(trending_regime):
    """Shorter than 50 closed bars → no features (live parity), no trades."""
    from services.nexus_backtest.features import compute_features_at
    df, _ = _bars([(0, 0.5, -0.5, 0.05, 100)])
    assert compute_features_at(df.head(40), 39) is None
    engine = BacktestEngine(signal_fn=_force_signal("BUY", 1.05), initial_balance=100_000)
    res = engine.run(df.head(60), start_idx=0, end_idx=59)
    assert res.signals == 0
