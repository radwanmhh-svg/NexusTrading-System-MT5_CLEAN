"""Final dispatch boundary shared by the REST and stream entry paths."""
from shared.execution_receipt import receipt_state
from shared.trade_controls import EntryBlocked, read_controls
from shared.redis_client import RedisClient

from .broker_adapter import BrokerAdapter, OrderRequest, OrderResponse, OrderStatus


async def dispatch_entry(redis: RedisClient, broker: BrokerAdapter, order: OrderRequest) -> OrderResponse:
    # No name-based guess, client-supplied flag, or fallback to a real account.
    # A non-attesting/private adapter remains compatible, but cannot open entries.
    try:
        account = await broker.get_account_info()
        confirmed = getattr(account, "execution_mode", None)
    except Exception as exc:
        raise EntryBlocked("unverified_account") from exc
    # Last awaited operation before entering the adapter; a broker call already
    # underway is not retractable by a later stop. Never apply this to exits.
    controls = await read_controls(redis)
    if controls.block:
        raise EntryBlocked(controls.block)
    if confirmed != controls.mode.value:
        raise EntryBlocked("account_mode_unverified_or_mismatched")
    result = await broker.send_order(order)
    if result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.PENDING):
        if receipt_state(vars(result)) == "unconfirmed":
            result.status = OrderStatus.UNCONFIRMED
            result.error = "Incomplete adapter receipt; reconcile before resubmitting"
    return result
