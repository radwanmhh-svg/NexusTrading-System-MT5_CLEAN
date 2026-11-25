"""B-01 — chunked checkpointed runs must equal a single continuous run."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _synthetic(n: int = 1400, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-01-05 00:00", periods=n, freq="5min", tz="UTC")
    close = 3600 + 0.04 * np.arange(n) + np.cumsum(rng.normal(0, 0.35, n))
    o = close + rng.normal(0, 0.1, n)
    hi = np.maximum(o, close) + np.abs(rng.normal(0, 0.15, n)) + 0.05
    lo = np.minimum(o, close) - np.abs(rng.normal(0, 0.15, n)) - 0.05
    return pd.DataFrame({
        "timestamp": ts, "open": o, "high": hi, "low": lo,
        "close": close, "volume": rng.integers(50, 500, n).astype(float),
    })


def test_chunked_run_equals_continuous_run():
    from services.nexus_backtest.engine import BacktestEngine

    df = _synthetic()
    start, end = 400, 1200
    chunk = 300

    # continuous
    eng1 = BacktestEngine(initial_balance=100_000)
    r1 = eng1.run(df, start_idx=start, end_idx=end)

    # chunked with resume
    eng2 = BacktestEngine(initial_balance=100_000)
    state = None
    pos = None
    leb = -(10**9)
    s = start
    while s < end:
        e = min(s + chunk, end)
        r2 = eng2.run(df, start_idx=s, end_idx=e, resume_state=state,
                      close_at_end=(e >= end))
        state = eng2.state(r2, getattr(r2, "_open_pos", None), r2.last_entry_bar)
        s = e

    assert len(r1.trades) == len(state["trades"])
    assert round(eng1.balance, 2) == round(eng2.balance, 2)
    for a, b in zip(r1.trades, eng2.state(r2, None, 0)["trades"]):
        assert a.side == b["side"]
        assert a.entry_price == b["entry_price"]
        assert a.net_pnl == b["net_pnl"]
        assert a.exit_reason == b["exit_reason"]
