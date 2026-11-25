"""Unit tests for the D-05 + D-04 + R-02 batch (Remediation Directive v1.0).

- D-05: max_drawdown on a per-trade curve — [100,110,99,120] → 10.0%.
- D-04: ONE pip definition (shared.asset_specs) + expectancy consistency.
- R-02: min R:R gate rejects R:R 1.4 / 0.0 before the order is sent.

Run on the project machine:
    .venv\\Scripts\\python -m pytest tests/test_d05_d04_r02_batch.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

met = importlib.import_module("shared.metrics")
specs = importlib.import_module("shared.asset_specs")

max_drawdown = met.max_drawdown
max_drawdown_pct = met.max_drawdown_pct
per_trade_equity_curve = met.per_trade_equity_curve
expectancy_consistency = met.expectancy_consistency
check_min_rr = met.check_min_rr


# ── D-05 ─────────────────────────────────────────────────────────────────────

def test_directive_acceptance_curve():
    # [100, 110, 99, 120] → peak 110, trough 99 → 10.0%
    assert max_drawdown([100, 110, 99, 120]) == 0.10
    assert max_drawdown_pct([100, 110, 99, 120]) == 10.0


def test_flat_and_short_curves_are_zero():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100]) == 0.0
    assert max_drawdown([100, 100, 100]) == 0.0
    assert max_drawdown([100, 120, 130]) == 0.0      # pure rise → no DD


def test_per_trade_curve_from_stored_pnls():
    curve = per_trade_equity_curve(100000.0, [666.52, 497.8, -787.71])
    assert curve[0] == 100000.0
    assert abs(curve[-1] - (100000 + 666.52 + 497.8 - 787.71)) < 0.01


# ── D-04 ─────────────────────────────────────────────────────────────────────

def test_single_pip_definition_xauusd():
    # The ONE definition lives in shared.asset_specs (config-backed).
    assert specs.get_pip_size("XAUUSDs") == 0.01
    assert specs.get_pip_value_per_lot("XAUUSDs") == 1.0
    # 17.0 price distance = 1700 pips under this definition — everywhere.
    assert abs(specs.price_to_pips("XAUUSDs", 17.0) - 1700.0) < 1e-6


def test_expectancy_consistency_formula():
    # Directive example shape: expectancy 405 pips × n × pip_value × lot ≈ return
    r = expectancy_consistency(405, 13_515, 1.0, 0.01, 54_735.75)
    assert r["implied_usd"] == 54_735.75          # 405*13515*1.0*0.01
    assert r["within_5pct"] is True
    bad = expectancy_consistency(405, 13_515, 1.0, 0.01, 20_000.0)
    assert bad["within_5pct"] is False


# ── R-02 ─────────────────────────────────────────────────────────────────────

def test_rr_14_is_rejected():
    g = check_min_rr(3700.0, sl=3690.0, tp=3714.0, side="BUY", min_rr=1.5)
    assert g["ok"] is False                       # tp 14 / sl 10 = 1.4
    assert "1.40" in g["reason"] or "1.4" in g["reason"]


def test_rr_15_exactly_passes():
    g = check_min_rr(3700.0, sl=3690.0, tp=3715.0, side="BUY", min_rr=1.5)
    assert g["ok"] is True and g["rr"] == 1.5


def test_rr_zero_missing_tp_is_rejected():
    g = check_min_rr(3700.0, sl=3690.0, tp=0.0, side="SELL", min_rr=1.5)
    assert g["ok"] is False                       # R:R 0.0 trades must not pass
    assert "MIN_RR" in g["reason"]


def test_rr_0_passed_before_is_now_blocked_at_12():
    # The historical leak: R:R 1.2 executed despite 1.5 setting.
    g = check_min_rr(3787.07, sl=3797.07, tp=3799.07, side="SELL", min_rr=1.5)
    assert g["ok"] is False and g["rr"] == 1.2
