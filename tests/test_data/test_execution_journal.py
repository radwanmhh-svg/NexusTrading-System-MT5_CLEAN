"""Tests for the execution journal reconciliation (Order/Deal/Position kept separate)."""
from services.nexus_data.execution_journal import reconcile_history

import pytest

ORDERS = [
    {
        "ticket": 1001, "position_id": 5001, "symbol": "XAUUSD",
        "type": 0, "volume_initial": 0.2, "volume_current": 0.2,
        "price_open": 2000.00, "state": 4, "ret_code": 10009,
        "comment": "nexus:cycle=abc", "time_setup_msc": 1731700000000,
        "time_done_msc": 1731700001200,
    },
]
DEALS = [
    {   # entry IN — filled at 2000.05 → BUY slippage +0.05
        "ticket": 7001, "order": 1001, "position_id": 5001, "symbol": "XAUUSD",
        "type": 0, "entry": 0, "volume": 0.2, "price": 2000.05,
        "commission": -0.5, "swap": 0.0, "profit": 0.0,
        "comment": "", "time_msc": 1731700001300,
    },
    {   # exit OUT — profit +50, swap -1.0
        "ticket": 7002, "order": 1002, "position_id": 5001, "symbol": "XAUUSD",
        "type": 1, "entry": 1, "volume": 0.2, "price": 2025.05,
        "commission": -0.5, "swap": -1.0, "profit": 50.0,
        "comment": "tp", "time_msc": 1731708000000,
    },
]


def test_orders_normalized():
    out = reconcile_history(ORDERS, DEALS, symbols={"XAUUSD"})
    assert len(out["orders"]) == 1
    o = out["orders"][0]
    assert o["order_ticket"] == 1001 and o["position_id"] == 5001
    assert o["side"] == "BUY" and o["volume_filled"] == 0.2
    assert o["retcode"] == 10009
    assert "cycle=abc" in o["comment"]


def test_deals_in_out_kept_separate():
    out = reconcile_history(ORDERS, DEALS, symbols={"XAUUSD"})
    deals = out["deals"]
    assert [d["entry"] for d in deals] == ["IN", "OUT"]
    assert deals[0]["side"] == "BUY" and deals[1]["side"] == "SELL"
    assert deals[1]["profit"] == 50.0 and deals[1]["swap"] == -1.0


def test_slippage_buy_in():
    out = reconcile_history(ORDERS, DEALS, symbols={"XAUUSD"})
    s = out["slippage"]
    assert len(s) == 1
    # BUY filled above intended → positive (costly) slippage
    assert s[0]["intended_price"] == 2000.00
    assert s[0]["filled_price"] == 2000.05
    assert s[0]["slippage"] == pytest.approx(0.05, abs=1e-6)
    assert s[0]["slippage_points"] == pytest.approx(5.0, abs=1e-6)


def test_symbol_filter():
    out = reconcile_history(ORDERS, DEALS, symbols={"BTCUSD"})
    assert out["orders"] == [] and out["deals"] == [] and out["slippage"] == []
