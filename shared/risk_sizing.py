"""
Shared position sizing — one source of truth for lot calculation.

Uses the broker's REAL symbol specification (from ``mt5.symbol_info`` via the
isolated worker), never generic gold assumptions:

- value-per-point from SYMBOL_TRADE_TICK_VALUE / SYMBOL_TRADE_TICK_SIZE
- lot rounding to SYMBOL_VOLUME_STEP, clamped to [VOLUME_MIN, VOLUME_MAX]
- SL/TP validity against SYMBOL_TRADE_STOPS_LEVEL / FREEZE_LEVEL
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SizingResult:
    lot: float
    risk_amount: float
    sl_distance: float          # price distance
    point_value_per_lot: float  # account currency per 1.0 point move at 1 lot
    rounded_to_step: bool
    clamped: str | None         # None | "min" | "max"


def _num(spec: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(spec.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def point_value_per_lot(spec: dict[str, Any]) -> float:
    """Account-currency value of a ONE-POINT (not tick) move for 1.0 lot."""
    tick_value = _num(spec, "trade_tick_value") or _num(spec, "volume_min") * 0  # guard
    tick_size = _num(spec, "trade_tick_size")
    point = _num(spec, "point")
    if tick_value <= 0 or tick_size <= 0:
        # Fallback: contract_size × point (accurate when profit currency == account currency)
        contract = _num(spec, "trade_contract_size")
        return contract * point if contract > 0 and point > 0 else 0.0
    if point > 0 and tick_size > 0:
        return tick_value * (point / tick_size)
    return tick_value


def calc_lot(
    spec: dict[str, Any],
    *,
    balance: float,
    risk_pct: float,
    entry: float,
    sl: float,
) -> SizingResult:
    """Position size so that SL hit ≈ risk_pct% of balance.

    risk_pct is a fraction (0.01 = 1%). Raises ValueError on invalid inputs.
    """
    if balance <= 0 or risk_pct <= 0:
        raise ValueError("balance and risk_pct must be positive")
    distance = abs(entry - sl)
    if distance <= 0:
        raise ValueError("entry and sl must differ")

    risk_amount = balance * risk_pct
    pv = point_value_per_lot(spec)
    if pv <= 0:
        raise ValueError("symbol spec lacks tick value / contract size")

    raw_lot = risk_amount / (distance / _num(spec, "point", 1.0) * pv) if _num(spec, "point") > 0 else risk_amount / (distance * pv)

    vol_min = _num(spec, "volume_min") or 0.01
    vol_max = _num(spec, "volume_max") or 100.0
    step = _num(spec, "volume_step") or 0.01

    clamped: str | None = None
    lot = raw_lot
    if lot < vol_min:
        lot, clamped = vol_min, "min"
    elif lot > vol_max:
        lot, clamped = vol_max, "max"

    steps = round((lot - vol_min) / step)
    rounded_lot = round(vol_min + steps * step, 8)
    rounded = abs(rounded_lot - raw_lot) > 1e-9

    return SizingResult(
        lot=rounded_lot,
        risk_amount=risk_amount,
        sl_distance=distance,
        point_value_per_lot=pv,
        rounded_to_step=rounded or clamped is not None,
        clamped=clamped,
    )


def validate_stop_levels(
    spec: dict[str, Any], *, side: str, entry: float, sl: float, tp: float,
) -> list[str]:
    """Check SL/TP against the broker's stops & freeze levels. Returns problems."""
    problems: list[str] = []
    stops_level = _num(spec, "trade_stops_level") * _num(spec, "point", 1.0)
    freeze = _num(spec, "trade_freeze_level") * _num(spec, "point", 1.0)
    min_dist = max(stops_level, freeze)

    side = side.upper()
    if side == "BUY":
        if sl >= entry:
            problems.append("BUY SL must be below entry")
        if tp <= entry:
            problems.append("BUY TP must be above entry")
    elif side == "SELL":
        if sl <= entry:
            problems.append("SELL SL must be above entry")
        if tp >= entry:
            problems.append("SELL TP must be below entry")
    else:
        problems.append(f"unknown side {side}")

    if min_dist > 0 and side in ("BUY", "SELL"):
        if abs(entry - sl) < min_dist:
            problems.append(f"SL distance {abs(entry - sl)} < broker minimum {min_dist}")
        if abs(tp - entry) < min_dist:
            problems.append(f"TP distance {abs(tp - entry)} < broker minimum {min_dist}")
    return problems
