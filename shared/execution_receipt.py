"""Read the executor's *reported* outcome. HTTP success is not a trade."""
from __future__ import annotations

import math

_PRE_SUBMISSION_VETOES = {"ENTRY_BLOCKED", "MIN_RR_REJECTED", "STACKING_BLOCKED", "DEDUP_SKIPPED"}


def _positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        return False
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def receipt_state(body: object) -> str:
    if not isinstance(body, dict):
        return "unconfirmed"
    status = str(body.get("status") or "").upper()
    if status in _PRE_SUBMISSION_VETOES or status == "REJECTED":
        return "rejected"
    if status == "CANCELLED":
        return "cancelled"  # does not establish that the executed quantity is zero
    if body.get("error"):
        return "unconfirmed"  # a transport/adapter error can follow submission
    ticket = body.get("order_id")
    if isinstance(ticket, bool) or not isinstance(ticket, (int, str)):
        return "unconfirmed"
    if str(ticket).strip().lower() in {"", "0", "none", "null"}:
        return "unconfirmed"
    if status in {"PENDING", "PLACED"}:
        return "pending"
    if status in {"FILLED", "PARTIAL"} and _positive(body.get("fill_price")) and _positive(body.get("size")):
        return "filled" if status == "FILLED" else "partial"
    return "unconfirmed"


def execution_veto(data: dict, key: str) -> bool:
    """Nested Redis Stream booleans arrive as strings. Unknown values veto.

    Legacy producers may omit an optional veto; an explicitly malformed one
    (including null) must not grant permission to replay an order.
    """
    if key not in data:
        return False
    value = data[key]
    return not (value is False or (isinstance(value, str) and value.lower() == "false"))
