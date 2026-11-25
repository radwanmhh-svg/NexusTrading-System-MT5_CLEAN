"""
Shared quantitative metrics — one definition per number (D-04/D-05).

Pure functions only (no IO, no clock) so every service and the unit tests
compute the exact same values.

D-04 (one pip definition): the ONLY pip definition in the system is
`shared.asset_specs` (config/assets.yaml: XAUUSD pip_size=0.01,
pip_value_per_lot=1.0 USD/pip/lot). All conversions must go through
`price_to_pips` / `pips_to_price` / `get_pip_value_per_lot` — never local
multipliers.

D-05 (max drawdown): per-trade equity curve, not per-day.
"""
from __future__ import annotations

from typing import Any, Sequence


# ── D-05: drawdown ───────────────────────────────────────────────────────────

def per_trade_equity_curve(
    initial_balance: float,
    pnls: Sequence[float],
) -> list[float]:
    """Equity after each closed trade, in chronological order."""
    curve: list[float] = [float(initial_balance)]
    for p in pnls:
        curve.append(curve[-1] + float(p or 0.0))
    return curve


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown as a POSITIVE fraction.

    Directive acceptance: [100, 110, 99, 120] → 0.10 (i.e. 10.0%).
    Empty / flat / single-point curves → 0.0.
    """
    curve = [float(x) for x in (equity_curve or [])]
    if len(curve) < 2:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for x in curve:
        if x > peak:
            peak = x
        elif peak > 0:
            dd = (peak - x) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def max_drawdown_pct(equity_curve: Sequence[float]) -> float:
    """Same as `max_drawdown`, expressed in percent (10.0 = 10%)."""
    return round(max_drawdown(equity_curve) * 100.0, 2)


# ── D-04: pip consistency (directive acceptance formula) ─────────────────────

def expectancy_consistency(
    expectancy_pips: float,
    n_trades: int,
    pip_value_per_lot: float,
    avg_lot: float,
    total_return_usd: float,
) -> dict[str, Any]:
    """Directive D-04 acceptance:
        expectancy_pips × trades × pip_value × avg_lot ≈ total_return (±5%)

    `pip_value_per_lot` = USD value of 1 pip for 1.0 lot (from
    shared.asset_specs — the single pip definition). Returns the implied
    USD return and whether it matches `total_return_usd` within ±5%.
    """
    implied = float(expectancy_pips) * float(n_trades) * \
        float(pip_value_per_lot) * float(avg_lot)
    target = abs(float(total_return_usd))
    ratio = (implied / target) if target > 0 else None
    return {
        "implied_usd": round(implied, 2),
        "total_return_usd": round(float(total_return_usd), 2),
        "ratio": round(ratio, 3) if ratio is not None else None,
        "within_5pct": ratio is not None and abs(ratio - 1.0) <= 0.05,
    }


# ── R-02: minimum R:R gate (pure gate used by the executor) ──────────────────

def check_min_rr(
    entry_price: float,
    sl: float,
    tp: float,
    side: str,
    min_rr: float = 1.5,
) -> dict[str, Any]:
    """Enforce tp_distance / sl_distance ≥ min_rr inside the executor,
    BEFORE sending to MT5. Returns {"ok": bool, "rr": float, "reason": str}.
    A trade without SL or TP cannot be gated → rejected with an explicit
    reason (never sent silently unprotected).
    """
    side_u = str(side or "").upper()
    if entry_price <= 0 or sl <= 0 or tp <= 0:
        return {"ok": False, "rr": 0.0,
                "reason": "MIN_RR: missing entry/SL/TP — order not sent unprotected"}
    sl_distance = abs(entry_price - sl)
    tp_distance = abs(tp - entry_price)
    if sl_distance <= 0:
        return {"ok": False, "rr": 0.0, "reason": "MIN_RR: zero SL distance"}
    rr = tp_distance / sl_distance
    if rr < min_rr:
        return {
            "ok": False,
            "rr": round(rr, 3),
            "reason": f"MIN_RR: R:R {rr:.2f} < required {min_rr}",
        }
    return {"ok": True, "rr": round(rr, 3), "reason": ""}
