"""D-03 unit tests: reconcile() (Remediation Directive v1.0, Phase 1).

Would have caught the original bug: displayed P&L (+$40.49) never reconciled
with the account balance movement (−$210) and nothing flagged it.

Run on the project machine:
    .venv\\Scripts\\python -m pytest tests/test_d03_reconcile.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

rc = importlib.import_module("shared.reconcile")
compute_diff = rc.compute_diff
trade_mismatches = rc.trade_mismatches


def test_perfect_reconciliation_is_ok():
    # MT5 truth: Σ deals net = 958.70 → balance 100,958.70 − 100,000 − 958.70 = 0
    r = compute_diff(100958.70, 100000.0, 958.70)
    assert r["ok"] is True
    assert r["diff"] == 0.0


def test_injected_mismatch_is_flagged():
    # Real data from rd66 (2026-11-24): stored legacy pnl ≠ MT5 deals.
    r = compute_diff(100958.70, 100000.0, 903.94)
    assert r["ok"] is False
    assert r["diff"] == 54.76          # exactly the sum of legacy diffs


def test_tolerance_boundary():
    ok = compute_diff(1000.0, 900.0, 100.01)
    assert ok["ok"] is True            # |diff| = 0.01 → still within tolerance
    bad = compute_diff(1000.0, 900.0, 100.02)
    assert bad["ok"] is False
    assert bad["diff"] == -0.02


def test_trade_mismatches_reports_every_leg():
    stored = {45501648: -192.5, 45488575: -130.32, 45318943: -787.71,
              44762856: 863.45}
    mt5 = {45501648: -210.5, 45488575: -138.12, 45318943: -787.71,
           44762856: 863.45, 44741876: 632.7}
    m = {x["position_id"]: x for x in trade_mismatches(stored, mt5)}
    assert m[45501648]["status"] == "mismatch"
    assert m[45501648]["diff"] == -18.0
    assert m[45488575]["status"] == "mismatch"
    assert m[45488575]["diff"] == -7.8
    # matching trades must NOT be reported
    assert 45318943 not in m
    assert 44762856 not in m
    # MT5-only position → missing_in_db (the old D-01 bug, now visible)
    assert m[44741876]["status"] == "missing_in_db"


def test_mismatch_report_never_rewrites_values():
    # The function must be read-only: same inputs → same output, no mutation.
    stored = {1: 10.0}
    mt5 = {1: 20.0}
    a = trade_mismatches(stored, mt5)
    b = trade_mismatches(stored, mt5)
    assert a == b
    assert stored == {1: 10.0} and mt5 == {1: 20.0}
