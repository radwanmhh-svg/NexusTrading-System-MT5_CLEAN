"""R-01 unit tests: server-side risk caps (Remediation Directive v1.0, Phase 0).

These tests would have caught the original bug: dashboard-editable limits of
daily 20% / weekly 25% / per-trade 1-5% with no server-side cap.

Run on the project machine:
    .venv\\Scripts\\python -m pytest tests/test_r01_risk_caps.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

rs = importlib.import_module("shared.runtime_settings")
re_mod = importlib.import_module("services.nexus_risk.rules_engine")


def test_defaults_are_r01_values():
    assert rs.DEFAULTS["max_daily_loss_pct"] == 2.0
    assert rs.DEFAULTS["max_weekly_dd_pct"] == 6.0
    assert rs.DEFAULTS["risk_per_trade_pct"] == 0.5


def test_bounds_enforce_hard_caps():
    # MAX_DAILY_RISK = 3% : anything above is clamped server-side
    assert rs._coerce("max_daily_loss_pct", 20.0) <= 3.0
    assert rs._coerce("max_daily_loss_pct", 50.0) == 3.0
    assert rs._coerce("max_weekly_dd_pct", 25.0) == 6.0
    assert rs._coerce("max_weekly_dd_pct", 60.0) == 6.0
    assert rs._coerce("risk_per_trade_pct", 5.0) == 0.5
    # sane lower bounds
    assert rs._coerce("max_daily_loss_pct", 0.01) >= 0.1


def test_merge_with_stored_20pct_gets_clamped():
    # Redis may still hold the pre-R-01 20% value; reads must clamp it.
    merged = rs.merge_with_defaults({"max_daily_loss_pct": 20.0, "max_weekly_dd_pct": 25.0})
    assert merged["max_daily_loss_pct"] <= 3.0
    assert merged["max_weekly_dd_pct"] <= 6.0


def test_rules_engine_defaults_respect_r01():
    cfg = re_mod.RulesEngineConfig()
    assert cfg.max_daily_loss_pct <= 3.0
    assert cfg.max_weekly_drawdown_pct <= 6.0
    assert cfg.max_risk_per_trade_pct <= 0.5


def test_rules_engine_cannot_be_loosened_by_runtime_overrides():
    eng = re_mod.RulesEngine()
    eng.apply_runtime_overrides(
        {"max_daily_loss_pct": 20.0, "max_weekly_dd_pct": 25.0, "max_risk_per_trade_pct": 5.0}
    )
    assert eng._cfg.max_daily_loss_pct <= 3.0
    assert eng._cfg.max_weekly_drawdown_pct <= 6.0
    assert eng._cfg.max_risk_per_trade_pct <= 0.5


def test_check_daily_loss_trips_at_2pct():
    eng = re_mod.RulesEngine()
    v = eng.check_daily_loss(2.0)
    assert v is not None, "daily loss 2.0% must trip the 2.0% limit"
