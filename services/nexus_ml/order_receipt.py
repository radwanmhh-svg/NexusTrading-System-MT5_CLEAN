"""Submit once to the internal executor; do not replay ambiguous transport outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from shared.execution_receipt import receipt_state


async def submit_entry(client: httpx.AsyncClient, executor_url: str, payload: dict) -> dict[str, Any]:
    body: Any = None
    http_status = None
    try:
        response = await client.post(f"{executor_url}/api/executor/order", json=payload)
        http_status = response.status_code
        if response.is_success:
            body = response.json()
    except (httpx.RequestError, ValueError):
        # A timeout or truncated acknowledgement is not a broker rejection.
        pass
    outcome = receipt_state(body)
    report = body if isinstance(body, dict) else {}
    return {
        "symbol": payload["symbol"], "outcome": outcome, "http_status": http_status,
        "order_id": report.get("order_id"), "executor_status": report.get("status"),
        "fill_price": report.get("fill_price") if outcome in {"filled", "partial"} else None,
        "filled_size": report.get("size") if outcome in {"filled", "partial"} else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
