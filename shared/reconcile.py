"""
D-03 reconciliation — pure, unit-testable math (no IO, no clock).

Directive: reconcile() hourly + on every close:
    diff = MT5.balance − initial_balance − Σ(trades.net_pnl)
    |diff| ≤ $0.01 → ok; otherwise red banner + AUTO=false.
Rule #1: mismatches are REPORTED, never silently fixed by rewriting history.
"""
from __future__ import annotations

from typing import Any

TOLERANCE_USD = 0.01


def compute_diff(
    mt5_balance: float,
    initial_balance: float,
    stored_net_pnl: float,
) -> dict[str, Any]:
    """The core D-03 identity. `stored_net_pnl` = Σ(pnl_usd) of the trades
    table (the single source of truth populated by the executor's real MT5
    deal numbers — see D-01/D-02)."""
    diff = round(float(mt5_balance) - float(initial_balance) - float(stored_net_pnl), 2)
    return {
        "mt5_balance": round(float(mt5_balance), 2),
        "initial_balance": round(float(initial_balance), 2),
        "stored_net_pnl": round(float(stored_net_pnl), 2),
        "diff": diff,
        "ok": abs(diff) <= TOLERANCE_USD,
        "tolerance_usd": TOLERANCE_USD,
    }


def trade_mismatches(
    stored: dict[int, float],
    mt5: dict[int, float],
    tol: float = TOLERANCE_USD,
) -> list[dict[str, Any]]:
    """Per-trade comparison (position_id → net pnl) in both directions.

    Returns every position that differs by more than `tol`, plus positions
    present on one side only (missing_in_db / missing_in_mt5). Never mutates
    anything — reporting only.
    """
    out: list[dict[str, Any]] = []
    for pid in sorted(set(stored) | set(mt5)):
        s = stored.get(pid)
        m = mt5.get(pid)
        if s is None:
            out.append({"position_id": pid, "status": "missing_in_db", "mt5": m})
            continue
        if m is None:
            out.append({"position_id": pid, "status": "missing_in_mt5", "stored": s})
            continue
        d = round(m - s, 2)
        if abs(d) > tol:
            out.append({
                "position_id": pid,
                "status": "mismatch",
                "stored": s,
                "mt5": m,
                "diff": d,
            })
    return out
