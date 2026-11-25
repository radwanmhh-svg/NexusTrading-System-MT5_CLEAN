"""Executor receipts for the ML submitter. All HTTP is in-process MockTransport."""
import httpx
import pytest

from services.nexus_ml.order_receipt import submit_entry


@pytest.mark.asyncio
@pytest.mark.parametrize("code,body,outcome", [
    (200, {"status": "ENTRY_BLOCKED"}, "rejected"), (200, {"status": "MIN_RR_REJECTED"}, "rejected"),
    (200, {"status": "PENDING", "order_id": "t1"}, "pending"), (200, {"status": "PENDING"}, "unconfirmed"),
    (200, {"status": "REJECTED"}, "rejected"), (200, {"status": "ERROR"}, "unconfirmed"),
    (200, {"status": "CANCELLED"}, "cancelled"), (200, {"status": "APPROVE"}, "unconfirmed"),
    (200, {"status": "FILLED", "order_id": "t1", "fill_price": 2000.0, "size": 0.01}, "filled"),
    (200, {"status": "PARTIAL", "order_id": "t1", "fill_price": 2000.0, "size": 0.005}, "partial"),
    (200, {"status": "FILLED", "order_id": "t1", "fill_price": 2000.0, "size": 0}, "unconfirmed"),
    (200, {"status": "FILLED", "order_id": None, "fill_price": 2000.0, "size": 1}, "unconfirmed"),
    (200, {"status": "FILLED", "order_id": "t1", "fill_price": "NaN", "size": 1}, "unconfirmed"),
    (200, [], "unconfirmed"), (200, "bad-json", "unconfirmed"),
    (502, {"status": "FILLED", "order_id": "t1", "fill_price": 2000.0, "size": 1}, "unconfirmed"),
    (None, {}, "unconfirmed"),
])
async def test_submit_once(code, body, outcome):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if code is None:
            raise httpx.ReadTimeout("fixture lost acknowledgement")
        if body == "bad-json":
            return httpx.Response(code, content=b"{")
        return httpx.Response(code, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await submit_entry(client, "http://fixture.invalid", {"symbol": "XAUUSD"})
    assert calls == ["/api/executor/order"]
    assert receipt["outcome"] == outcome and receipt["http_status"] == code
    assert (receipt["filled_size"] is not None) == (outcome in {"partial", "filled"})


@pytest.mark.asyncio
async def test_ccxt_entry_does_not_invent_price_or_quantity():
    from unittest.mock import AsyncMock
    from services.nexus_executor.broker_adapter import OrderRequest, OrderSide
    from services.nexus_executor.ccxt_executor import CCXTExecutor
    from shared.execution_receipt import receipt_state

    adapter = CCXTExecutor(sandbox=True)
    adapter._exchange = AsyncMock()
    adapter._exchange.create_order.return_value = {"status": "closed", "id": "t", "price": 2000.0}
    order = OrderRequest(symbol="XAUUSD", side=OrderSide.BUY, size=0.1)
    result = await adapter.send_order(order)
    assert result.size == result.fill_price == 0
    assert receipt_state(vars(result)) == "unconfirmed"
    adapter._exchange.create_order.return_value = {"status": "open", "id": "t", "average": 1999.8, "filled": 0.04}
    partial = await adapter.send_order(order)
    assert receipt_state(vars(partial)) == "partial" and partial.size == 0.04
