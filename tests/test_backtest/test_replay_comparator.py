"""B-01 — replay comparator logic (unit-tested with synthetic data).

The live 7-day replay itself is a CLI run (`scripts/run_backtest.py --replay`);
here we validate the comparator: direction multiset match, count ±2, entry
proximity and the window filter.
"""
from __future__ import annotations

import pandas as pd
import pytest

from services.nexus_backtest.engine import BacktestTrade
from services.nexus_backtest.replay import compare_trades

T0 = pd.Timestamp("2026-11-18 00:00", tz="UTC")


def _live(side, opened_at, entry=3600.0):
    return {"symbol": "XAUUSDs", "side": side, "opened_at": opened_at, "entry_price": entry}


def _replay(side, entry=3600.0, i=0):
    return BacktestTrade(
        side=side,
        signal_time=T0 + pd.Timedelta(minutes=5 * i),
        entry_time=T0 + pd.Timedelta(minutes=5 * i + 5),
        entry_price=entry, exit_time=T0, exit_price=entry,
        sl=0, tp=0, lot=0.1, gross_pnl=0, costs=0, net_pnl=0,
        exit_reason="TP", signal_prob=0.9,
    )


def test_direction_and_count_match_passes():
    live = [_live("BUY", "2026-11-19T10:00"), _live("SELL", "2026-11-20T10:00")]
    replay = [_replay("BUY", 3600.0, 1), _replay("SELL", 3601.0, 2)]
    rep = compare_trades(live, replay, T0)
    assert rep.passed


def test_direction_mismatch_fails():
    live = [_live("BUY", "2026-11-19T10:00")]
    replay = [_replay("SELL", 3600.0, 1)]
    rep = compare_trades(live, replay, T0)
    assert not rep.direction_match
    assert not rep.passed


def test_count_tolerance_2():
    live = [_live("BUY", "2026-11-19T10:00"), _live("BUY", "2026-11-19T11:00"),
            _live("SELL", "2026-11-19T12:00"), _live("SELL", "2026-11-19T13:00")]
    replay = [_replay("BUY", 3600.0, 1), _replay("BUY", 3600.5, 2)]
    rep = compare_trades(live, replay, T0)
    assert rep.direction_match  # replay directions never contradict live
    assert rep.count_within_tolerance  # |4-2| = 2 ≤ 2
    assert rep.passed


def test_count_beyond_tolerance_fails():
    live = [_live("BUY", f"2026-11-19T{h:02d}:00") for h in range(10)]
    replay = []
    rep = compare_trades(live, replay, T0)
    assert not rep.count_within_tolerance
    assert not rep.passed


def test_window_filter_excludes_old_trades():
    live = [_live("BUY", "2026-11-10T10:00"), _live("SELL", "2026-11-19T10:00")]
    replay = [_replay("SELL", 3600.0, 1)]
    rep = compare_trades(live, replay, T0)
    assert rep.live_trades == 1
    assert rep.live_directions == {"SELL": 1}
    assert rep.passed


def test_entry_proximity_reported():
    live = [_live("BUY", "2026-11-19T10:00", entry=3620.0)]
    replay = [_replay("BUY", 3620.3, 1)]
    rep = compare_trades(live, replay, T0)
    m = rep.entry_matches[0]
    assert m["side_match"] and m["within_tol"]
    assert m["entry_diff_pips"] == pytest.approx(30.0)


def test_entry_far_beyond_tol_flagged():
    live = [_live("BUY", "2026-11-19T10:00", entry=3620.0)]
    replay = [_replay("BUY", 3621.5, 1)]
    rep = compare_trades(live, replay, T0)
    m = rep.entry_matches[0]
    assert m["side_match"] and not m["within_tol"]
    assert m["entry_diff_pips"] == pytest.approx(150.0)
