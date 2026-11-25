"""No connection/lifespan/MT5 startup: physical sends are replaced by an in-memory log."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI, HTTPException

from services.nexus_executor import main as executor
from services.nexus_executor.broker_adapter import AccountInfo, OrderResponse, OrderStatus
from shared import trade_controls as c
from shared.constants import TradingMode
from shared.events import RiskDecisionEvent, parse_event
from shared.redis_client import RedisClient

ORDER = dict(symbol="XAUUSD", side="BUY", size=0.01, price=2000.0, sl=1999.0, tp=2002.0)


@pytest_asyncio.fixture
async def controls(monkeypatch):
    monkeypatch.setenv("NEXUS_TRADING_MODE", "paper")
    monkeypatch.delenv("NEXUS_FIXED_LOTS", raising=False)
    monkeypatch.setenv("NEXUS_MIN_RR", "1.5")
    redis = RedisClient()
    redis._redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(executor, "redis", redis)
    try:
        yield redis
    finally:
        await redis.disconnect()


class RecordingAdapter:
    def __init__(self):
        self.mode, self.calls = "paper", []
        self.inventory = AsyncMock(return_value=[])
        self.before_account = None
        self.status = OrderStatus.FILLED
        self.fill_price = 2000.0

    async def get_positions(self):
        return await self.inventory()

    async def get_account_info(self):
        if self.before_account:
            await self.before_account()
        return AccountInfo(equity=10000, execution_mode=self.mode)

    async def send_order(self, order):
        self.calls.append(order)
        return OrderResponse(order_id="test-ticket", symbol=order.symbol, side=order.side,
                             order_type=order.order_type, size=order.size, fill_price=self.fill_price, status=self.status)


@pytest.fixture
def adapter(monkeypatch):
    broker = RecordingAdapter()
    monkeypatch.setattr(executor, "_get_broker", AsyncMock(return_value=broker))
    return broker


async def arm_paper(redis):
    await c.set_mode(redis, TradingMode.PAPER)
    await c.set_auto(redis, True, TradingMode.PAPER)


async def send_via(path, redis, monkeypatch, *, flags=None, order=None):
    order = ORDER if order is None else order
    if path == "rest":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=executor.app), base_url="http://test") as client:
            res = await client.post("/api/executor/order", json=order)
        assert res.status_code == 200
        return res.json()
    event = RiskDecisionEvent(source="test", decision="APPROVE", proposal_id="test-proposal", approved_size=order["size"],
                              data=dict(order, entry=order["price"], **(flags or {"skip_execution": False, "already_executed": False})))

    async def stream(*_):
        # The real wire conversion matters: nested False comes back as the string 'False'.
        yield parse_event(event.to_stream_dict())

    monkeypatch.setattr(redis, "subscribe_stream", stream)
    await executor._execution_stream_consumer()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["rest", "stream"])
@pytest.mark.parametrize("case", ["operator_halt", "risk_halt", "reconciliation_pause", "auto_off", "shadow", "backtest",
                                  "missing_mode", "bad_flag", "wrong_key_type", "connection_lost", "unknown_account", "real_account", "process_mismatch"])
async def test_blocked_orders_never_reach_adapter(path, case, controls, adapter, monkeypatch):
    await arm_paper(controls)
    if case == "operator_halt":
        await c.set_manual_halt(controls, True)
    elif case == "risk_halt":
        await controls.cache_set("system:kill_switch", {"reason": "anti_tilt"})
    elif case == "reconciliation_pause":
        await controls.r.set(c.PAUSE, "diff=12.34")
    elif case == "auto_off":
        await controls.r.delete(c.AUTO)
    elif case in {"shadow", "backtest"}:
        await c.set_mode(controls, TradingMode(case))
    elif case == "missing_mode":
        await controls.r.delete(c.MODE)
    elif case == "bad_flag":
        await controls.r.set(c.AUTO, json.dumps("true"))
    elif case == "wrong_key_type":
        await controls.r.delete(c.MODE)
        await controls.r.lpush(c.MODE, '"paper"')
    elif case == "connection_lost":
        monkeypatch.setattr(controls.r, "pipeline", lambda **k: (_ for _ in ()).throw(ConnectionError("offline")))
    elif case == "unknown_account":
        adapter.mode = None
    elif case == "real_account":
        adapter.mode = "live"
    elif case == "process_mismatch":
        monkeypatch.delenv("NEXUS_TRADING_MODE")
    out = await send_via(path, controls, monkeypatch)
    if path == "rest":
        assert out["status"] == "ENTRY_BLOCKED"
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["rest", "stream"])
async def test_confirmed_paper_allows_one_handoff(path, controls, adapter, monkeypatch):
    await arm_paper(controls)
    await controls.cache_set("session:state", {"mode": "wind_down"})  # schedule is not an execution-mode writer
    out = await send_via(path, controls, monkeypatch)
    assert len(adapter.calls) == 1
    if path == "rest":
        assert (out["status"], out["order_id"], out["fill_price"]) == ("FILLED", "test-ticket", 2000.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["rest", "stream"])
async def test_stop_between_approval_and_dispatch(path, controls, adapter, monkeypatch):
    await arm_paper(controls)
    at_boundary, proceed = asyncio.Event(), asyncio.Event()

    async def wait_at_boundary():
        at_boundary.set()
        await asyncio.wait_for(proceed.wait(), timeout=2)

    monkeypatch.setattr(executor, "_clamp_lot_size", AsyncMock(return_value=0.01))
    adapter.before_account = wait_at_boundary
    task = asyncio.create_task(send_via(path, controls, monkeypatch))
    await asyncio.wait_for(at_boundary.wait(), timeout=2)
    await c.set_manual_halt(controls, True)
    proceed.set()
    out = await asyncio.wait_for(task, timeout=2)
    if path == "rest":
        assert out["error"] == "operator_halt"
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, "bad", None])
async def test_no_replay_of_skipped_or_ambiguous_agent_attempt(flag, controls, adapter, monkeypatch):
    await arm_paper(controls)
    await send_via("stream", controls, monkeypatch, flags={"skip_execution": flag, "already_executed": False})
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["rest", "stream"])
async def test_no_inverted_bracket(path, controls, adapter, monkeypatch):
    await arm_paper(controls)
    await send_via(path, controls, monkeypatch, order=dict(ORDER, sl=2001.0, tp=1998.0))
    assert not adapter.calls


@pytest.mark.asyncio
async def test_incomplete_fill_not_published_as_filled(controls, adapter, monkeypatch):
    await arm_paper(controls)
    adapter.fill_price = 0  # adapter said FILLED but cannot show a fill price
    out = await send_via("rest", controls, monkeypatch)
    assert out["status"] == "UNCONFIRMED"
    assert not await controls.r.exists("nexus:trade_meta:test-ticket")
    assert await controls.r.exists("nexus:recent_orders:XAUUSD:BUY")


@pytest.mark.asyncio
async def test_shadow_record_failure_does_not_send(controls, adapter, monkeypatch):
    from services.nexus_agents import shadow_trades
    recorder = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    monkeypatch.setattr(shadow_trades, "record_shadow_trade", recorder)
    await c.set_mode(controls, TradingMode.SHADOW)
    await send_via("stream", controls, monkeypatch)
    recorder.assert_awaited_once()
    assert not adapter.calls


@pytest.mark.asyncio
async def test_operator_release_does_not_release_risk_or_rearm(controls):
    await arm_paper(controls)
    await c.set_manual_halt(controls, True)
    assert await controls.r.ttl(c.MANUAL) == -1
    await controls.cache_set("system:kill_switch", {"reason": "anti_tilt"}, ttl_sec=1800)
    await controls.r.set(c.PAUSE, "diff=3.25")
    await controls.cache_delete("system:kill_switch")  # the risk day reset must not lift the operator halt
    assert await c.halted(controls)
    await controls.cache_set("system:kill_switch", {"reason": "anti_tilt"}, ttl_sec=1800)
    await c.set_manual_halt(controls, False)
    state = await c.read_controls(controls)
    assert (state.manual, state.requested, state.risk, state.pause) == (False, False, True, "diff=3.25")
    with pytest.raises(c.EntryBlocked):
        await c.set_auto(controls, True, TradingMode.PAPER)
    await controls.r.delete(c.RISK, c.PAUSE)
    assert (await c.read_controls(controls)).block == "auto_off"


@pytest.mark.asyncio
async def test_late_auto_enable_cannot_overwrite_stop(controls, monkeypatch):
    await c.set_mode(controls, TradingMode.PAPER)
    create_pipe = controls.r.pipeline

    def pipeline(*args, **kwargs):
        pipe = create_pipe(*args, **kwargs)
        execute = pipe.execute

        async def late_stop(*a, **k):
            # Another client's atomic write between WATCH and EXEC.
            async with create_pipe(transaction=True) as other:
                other.set(c.MANUAL, "true").set(c.AUTO, "false")
                await other.execute()
            return await execute(*a, **k)

        pipe.execute = late_stop
        return pipe

    with monkeypatch.context() as patch:
        patch.setattr(controls.r, "pipeline", pipeline)
        with pytest.raises(c.EntryBlocked, match="controls_changed"):
            await c.set_auto(controls, True, TradingMode.PAPER)
    assert (await c.read_controls(controls)).manual
    assert not (await c.read_controls(controls)).requested


@pytest.mark.asyncio
async def test_protective_rest_routes_separate(controls, adapter):
    await c.set_manual_halt(controls, True)
    adapter.close_position = AsyncMock(return_value=OrderResponse(order_id="t", status=OrderStatus.PENDING))
    adapter.modify_order = AsyncMock(return_value=OrderResponse(order_id="t", status=OrderStatus.PENDING))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=executor.app), base_url="http://test") as client:
        assert (await client.delete("/api/executor/position/t")).status_code == 200
        assert (await client.put("/api/executor/position/t", json={"sl": 1999})).status_code == 200
    adapter.close_position.assert_awaited_once()
    adapter.modify_order.assert_awaited_once()
    assert not adapter.calls


@pytest.mark.asyncio
async def test_gateway_readback_and_server_pin(controls, monkeypatch):
    # The source-only export excludes private auth. Load the same router under
    # a TEST-ONLY module name with a strict verifier. No production import or
    # global router instance is patched to permit trading.
    private = ModuleType("services.nexus_api.middleware.auth")

    def verify(pin):
        if pin != "fixture-only":
            raise HTTPException(403, "Invalid test PIN")

    private.verify_pin = verify
    monkeypatch.setitem(sys.modules, private.__name__, private)
    path = Path(__file__).resolve().parents[2] / "services/nexus_api/routers/system.py"
    spec = importlib.util.spec_from_file_location("services.nexus_api.routers._controls_test_router", path)
    router = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, router)
    spec.loader.exec_module(router)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_redis] = lambda: controls
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/system/") as client:
        empty = (await client.get("mode")).json()
        assert empty["mode"] == "shadow" and not empty["auto_trade"]
        assert (await client.post("mode", json={"mode": "paper"})).status_code == 200
        assert (await client.post("toggle-auto-trade", json={"enabled": True})).status_code == 409
        assert (await client.post("toggle-auto-trade", json={"enabled": True, "expected_mode": "live"})).status_code == 409
        assert (await client.post("toggle-auto-trade", json={"enabled": True, "expected_mode": "paper"})).status_code == 200
        assert (await client.post("kill-switch", json={"pin": "wrong"})).status_code == 403
        assert (await client.get("mode")).json()["auto_trade"]
        assert (await client.post("kill-switch", json={"pin": "fixture-only"})).status_code == 200
        assert (await client.post("kill-switch/release", json={"pin": "wrong"})).status_code == 403
        assert (await client.post("toggle-auto-trade", json={"enabled": True, "expected_mode": "paper"})).status_code == 409
        assert (await client.post("kill-switch/release", json={"pin": "fixture-only"})).status_code == 200
        off = (await client.get("mode")).json()
        assert not off["manual_halt"] and off["entry_block"] == "auto_off"
        assert (await client.post("mode", json={"mode": "live", "pin": "fixture-only"})).status_code == 409
        await controls.r.set(c.AUTO, "broken-json")
        assert (await client.get("mode")).status_code == 503


@pytest.mark.asyncio
async def test_preference_writes_do_not_authorize_entries(controls):
    from services.nexus_api.routers import config
    for field, value in (("trading_mode", "live"), ("auto_trade_enabled", True)):
        with pytest.raises(HTTPException) as exc:
            await config.update_settings(config.UpdateSettingsRequest(settings={field: value}), redis=controls)
        assert exc.value.status_code == 409
    assert (await c.read_controls(controls)).mode == TradingMode.SHADOW


@pytest.mark.asyncio
async def test_inventory_error_blocks_rest(controls, adapter, monkeypatch):
    await arm_paper(controls)
    adapter.inventory.side_effect = RuntimeError("no inventory")
    out = await send_via("rest", controls, monkeypatch)
    assert out["error"] == "position_inventory_unavailable" and not adapter.calls


@pytest.mark.asyncio
async def test_revalidate_routed_price(controls, adapter, monkeypatch):
    await arm_paper(controls)
    # A plausible requested price must not validate an inverted, later quote.
    out = await send_via("rest", controls, monkeypatch, order=dict(ORDER, bid=1997.0, ask=1998.0))
    assert out["status"] == "MIN_RR_REJECTED" and not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,expected", [({"status": "PENDING", "order_id": "t"}, "pending"),
                                            ({"status": "MIN_RR_REJECTED"}, "rejected"),
                                            ({"status": "timeout"}, "unconfirmed"),
                                            ({"status": "PARTIAL", "order_id": "t", "size": 0.004, "fill_price": 2000.0}, "partial")])
async def test_a9_attempt_not_replayed_on_stream(reply, expected, controls, adapter, monkeypatch, make_agent_state):
    from types import SimpleNamespace
    from services.nexus_agents import main as agents
    from services.nexus_agents.agents import trade_executor as a9
    from services.nexus_risk import main as risk
    from services import nexus_decision, nexus_economic
    from shared.constants import RedisStream as S

    await arm_paper(controls)
    agent = a9.TradeExecutorAgent(redis_client=controls)
    monkeypatch.setattr(agent, "_get_llm", lambda: None)  # offline; no model requests
    monkeypatch.setattr(agent, "_publish_message", AsyncMock())
    monkeypatch.setattr(agent, "_log_reasoning", AsyncMock())
    monkeypatch.setattr(a9, "get_indicators", AsyncMock(return_value={"atr_14": 2.0}))
    monkeypatch.setattr(a9, "get_price", AsyncMock(return_value={"bid": 2000.0, "ask": 2000.1}))
    monkeypatch.setattr(a9, "send_order", AsyncMock(return_value=reply))
    monkeypatch.setattr(a9, "record_trade", AsyncMock())
    final = await agent.invoke(make_agent_state(symbol="XAUUSD", verdict="BUY", risk_decision="APPROVE", position_size=0.01, verdict_conviction=80))
    assert final["order"]["status"] == expected
    a9.send_order.assert_awaited_once()
    monkeypatch.setattr(agents, "redis_client", controls)
    monkeypatch.setattr(agents, "_researcher", None)
    await agents._publish_results(final, "fixture-cycle")

    async def stream(stream_name, *_):
        for _, fields in await controls.r.xrange(str(stream_name)):
            yield parse_event(fields)

    monkeypatch.setattr(controls, "subscribe_stream", stream)
    monkeypatch.setattr(risk, "redis", controls)
    monkeypatch.setattr(nexus_economic, "load_events", lambda: [])
    monkeypatch.setattr(risk, "_count_open_positions", AsyncMock(return_value=0))
    monkeypatch.setattr(risk, "_get_nexus_daily_pnl_pct", AsyncMock(return_value=0.0))
    # Deliberately force an independent approval to exercise the replay veto,
    # not to test or bypass any production risk policy.
    monkeypatch.setattr(nexus_decision, "decide", lambda **k: SimpleNamespace(action="BUY", trace={}))
    monkeypatch.setattr(risk, "_evaluate_proposal", AsyncMock(return_value={"decision": "APPROVE", "approved_size": 0.01}))
    await risk._risk_stream_consumer()
    [(msg_id, fields)] = await controls.r.xrange(str(S.RISK_DECISION))
    assert fields["data.skip_execution"] == "True"
    assert fields["data.already_executed"] == str(expected in {"filled", "partial"})
    await executor._execution_stream_consumer()
    assert not adapter.calls
