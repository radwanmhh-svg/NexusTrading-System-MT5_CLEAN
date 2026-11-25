"""D-01 unit tests: single source of truth for trades (Remediation Directive
v1.0, Phase 1).

These tests would have caught the original bugs:
- a closed trade missing from the DuckDB trades table (SELL @3787.07 +$897),
- two views showing different P&L for the same position (-767.71 vs -787.71),
because the builder below derives trades from MT5 deal history (the truth)
and the Journal view now maps from the same DuckDB rows.

Run on the project machine:
    .venv\\Scripts\\python -m pytest tests/test_d01_single_source.py -q
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ej = importlib.import_module("services.nexus_data.execution_journal")
ev = importlib.import_module("shared.events")

MAP = importlib.import_module("services.nexus_api.routers.intelligence")
map_db_trade_to_memory_record = MAP.map_db_trade_to_memory_record


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _deals_sample() -> list[dict]:
    """Mimics execution_journal_deals rows (IN/OUT per position).

    pos 1001: BUY closed  @ 3669.00 → exit 3652.00, 1.00 lot,
              profit -767.71 + commission -10.00 + swap -10.00 = -787.71 net
    pos 1002: SELL closed @ 3787.07 → exit 3700.00, 0.50 lot,
              profit +897.00 + commission -5.00 + swap 0.00 = +892.00 net
              (the trade that was missing from History)
    pos 1003: BUY still open (IN only) — must NOT become a trade row.
    """
    return [
        {"deal_ticket": 9001, "position_id": 1001, "symbol": "XAUUSDS", "side": "BUY",
         "entry": "IN", "volume": 1.0, "price": 3669.00, "commission": -5.0,
         "swap": -5.0, "profit": 0.0, "comment": "nexus:automl", "time": _ts(1700000000000)},
        {"deal_ticket": 9002, "position_id": 1001, "symbol": "XAUUSDS", "side": "SELL",
         "entry": "OUT", "volume": 1.0, "price": 3652.00, "commission": -5.0,
         "swap": -5.0, "profit": -767.71, "comment": "", "time": _ts(1700000300000)},
        {"deal_ticket": 9003, "position_id": 1002, "symbol": "XAUUSDS", "side": "SELL",
         "entry": "IN", "volume": 0.5, "price": 3787.07, "commission": -5.0,
         "swap": 0.0, "profit": 0.0, "comment": "nexus:genai", "time": _ts(1700001000000)},
        {"deal_ticket": 9004, "position_id": 1002, "symbol": "XAUUSDS", "side": "BUY",
         "entry": "OUT", "volume": 0.5, "price": 3700.00, "commission": 0.0,
         "swap": 0.0, "profit": 897.00, "comment": "", "time": _ts(1700002000000)},
        {"deal_ticket": 9005, "position_id": 1003, "symbol": "XAUUSDS", "side": "BUY",
         "entry": "IN", "volume": 0.2, "price": 3700.00, "commission": 0.0,
         "swap": 0.0, "profit": 0.0, "comment": "nexus:automl", "time": _ts(1700003000000)},
    ]


def test_builder_excludes_open_positions_and_keeps_closed():
    built = ej.positions_to_trades(_deals_sample())
    ids = [t["id"] for t in built]
    assert ids == ["1001", "1002"]          # 1003 still open → excluded
    assert all(t["symbol"] == "XAUUSDS" for t in built)


def test_net_pnl_is_profit_plus_commission_plus_swap():
    built = {t["id"]: t for t in ej.positions_to_trades(_deals_sample())}
    assert abs(built["1001"]["pnl_usd"] - (-787.71)) < 0.01
    assert abs(built["1001"]["commission"] - (-10.0)) < 0.01
    assert abs(built["1001"]["swap"] - (-10.0)) < 0.01
    assert abs(built["1002"]["pnl_usd"] - 892.00) < 0.01


def test_pnl_pips_sign_follows_side():
    built = {t["id"]: t for t in ej.positions_to_trades(_deals_sample())}
    # BUY closed lower → negative pips
    assert built["1001"]["pnl_pips"] < 0
    # SELL from 3787.07 down to 3700.00 → positive pips (+870.7)
    assert built["1002"]["pnl_pips"] > 800
    assert abs(built["1002"]["entry_price"] - 3787.07) < 0.01


def test_volume_and_deal_ticket_come_from_out_deals():
    built = {t["id"]: t for t in ej.positions_to_trades(_deals_sample())}
    assert built["1002"]["volume"] == 0.5
    assert built["1002"]["deal_ticket"] == 9004
    assert built["1002"]["position_id"] == 1002


def test_builder_ids_are_stable_idempotency_keys():
    # Re-running the backfill must not duplicate: same id per position.
    a = ej.positions_to_trades(_deals_sample())
    b = ej.positions_to_trades(_deals_sample())
    assert [t["id"] for t in a] == [t["id"] for t in b]


def test_db_trade_maps_to_journal_record_shape():
    row = {
        "id": "1002", "symbol": "XAUUSDS", "side": "SELL",
        "entry_price": 3787.07, "exit_price": 3700.00,
        "pnl_pips": 870.7, "pnl_usd": 892.00, "session": "ASIA",
        "strategy": "genai", "conviction": 0.72,
        "closed_at": "2026-11-24T10:35:00+00:00",
    }
    rec = map_db_trade_to_memory_record(row)
    assert rec["trade_id"] == "1002"
    assert rec["is_win"] is True
    assert rec["regime"] == "genai"
    assert rec["_hour_bucket"] == "H10"
    assert rec["source"] == "historical"
    assert abs(rec["pnl_usd"] - 892.00) < 0.01


def test_trade_close_event_accepts_real_deal_fields():
    e = ev.TradeCloseEvent(
        trade_id="1002", symbol="XAUUSDS", side="SELL",
        entry_price=3787.07, exit_price=3700.00,
        pnl_pips=870.7, pnl_usd=892.0,
        volume=0.5, commission=-5.0, swap=0.0,
        deal_ticket=9004, position_id=1002, magic=777,
        comment="nexus:genai",
    )
    assert e.deal_ticket == 9004
    assert abs(e.commission - (-5.0)) < 1e-9
    assert e.position_id == 1002
