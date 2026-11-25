"""D-02 unit tests: MT5 field backfill on existing trades (Remediation
Directive v1.0, Phase 1).

These tests would have caught the original bug: closed trades persisted with
volume/deal_ticket/commission/swap empty ("—" in the dashboard VOL/SOURCE
columns). The fill function below must ONLY write empty fields and NEVER
rewrite a stored value (rule #1).

Run on the project machine:
    .venv\\Scripts\\python -m pytest tests/test_d02_mt5_fields.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ej = importlib.import_module("services.nexus_data.execution_journal")
fill_missing_fields = ej.fill_missing_fields


MT5 = {
    "volume": 0.35,
    "commission": 0.0,          # broker really charges 0 → no-op, must be skipped
    "swap": -0.42,
    "deal_ticket": 34790453,
    "position_id": 44762856,
    "magic": 777,
    "comment": "nexus:automl:scalping",
}


def test_fills_every_empty_numeric_and_string_field():
    existing = {"volume": 0.0, "commission": 0.0, "swap": 0.0,
                "deal_ticket": None, "position_id": None, "magic": None,
                "comment": ""}
    fill = fill_missing_fields(existing, MT5)
    assert fill["volume"] == 0.35
    assert fill["deal_ticket"] == 34790453
    assert fill["position_id"] == 44762856
    assert fill["magic"] == 777
    assert fill["swap"] == -0.42
    assert fill["comment"] == "nexus:automl:scalping"


def test_never_overwrites_a_populated_value():
    existing = {"volume": 0.5, "commission": 0.0, "swap": -0.42,
                "deal_ticket": 111, "position_id": 222, "magic": 333,
                "comment": "nexus:genai"}
    fill = fill_missing_fields(existing, MT5)
    assert "volume" not in fill          # 0.5 already stored → untouched
    assert "deal_ticket" not in fill     # 111 already stored → untouched
    assert "position_id" not in fill
    assert "magic" not in fill
    assert "comment" not in fill
    assert "swap" not in fill            # non-zero already → untouched
    # commission stays 0.0 == 0.0 → no-op write must be skipped
    assert "commission" not in fill
    assert fill == {}


def test_empty_mt5_data_returns_nothing():
    assert fill_missing_fields({"volume": 0.0}, None) == {}
    assert fill_missing_fields({"volume": 0.0}, {}) == {}


def test_idempotent_second_pass_is_empty():
    existing = {"volume": 0.0, "swap": 0.0, "deal_ticket": None,
                "position_id": None, "magic": None, "comment": ""}
    once = fill_missing_fields(existing, MT5)
    after = dict(existing)
    after.update(once)
    twice = fill_missing_fields(after, MT5)
    assert twice == {}                   # running twice writes nothing new


def test_journal_builder_carries_magic():
    deals = [
        {"deal_ticket": 1, "position_id": 10, "symbol": "XAUUSDS", "side": "SELL",
         "entry": "IN", "volume": 0.1, "price": 3700.0, "commission": 0.0,
         "swap": 0.0, "profit": 0.0, "comment": "nexus:automl", "time": 1700000000,
         "magic": 777},
        {"deal_ticket": 2, "position_id": 10, "symbol": "XAUUSDS", "side": "BUY",
         "entry": "OUT", "volume": 0.1, "price": 3690.0, "commission": 0.0,
         "swap": 0.0, "profit": 95.0, "comment": "", "time": 1700000300,
         "magic": 777},
    ]
    built = ej.positions_to_trades(deals)
    assert len(built) == 1
    assert built[0]["magic"] == 777
