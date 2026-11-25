"""
System router — health dashboard, kill-switch, auto-trade toggle, mode switch.
"""
from __future__ import annotations

from inspect import isawaitable
from typing import Any

import structlog
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, StrictBool

from shared.constants import ServiceName, TradingMode
from shared.trade_controls import EntryBlocked, read_controls, set_auto, set_manual_halt, set_mode
from shared.models import SystemHealth
from shared.redis_client import RedisClient
from shared.service_urls import data_service_url as _dsvc

from ..deps import get_redis
from ..middleware.auth import verify_pin

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


# ── Request / response models ────────────────────────────────────────────────

class KillSwitchRequest(BaseModel):
    pin: str
    reason: str = "manual kill-switch"


class ToggleAutoTradeRequest(BaseModel):
    enabled: StrictBool
    expected_mode: TradingMode | None = None


class SwitchModeRequest(BaseModel):
    mode: TradingMode
    pin: str | None = None


class ModeResponse(BaseModel):
    mode: TradingMode
    auto_trade: bool
    auto_requested: bool
    manual_halt: bool
    risk_halt: bool
    entry_block: str | None = None
    # D-03: set when reconcile() reports |diff| > $0.01 — honest surfacing.
    reconcile_paused: str | None = None
    reconcile_diff: float | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────

async def _require_pin(pin: str) -> None:
    # Delegate to the server verifier, never a browser value or fallback PIN.
    if not pin or not pin.strip():
        raise HTTPException(403, "Gateway PIN required")
    verified = verify_pin(pin)
    if isawaitable(verified):
        verified = await verified
    # Existing raise-on-denial verifiers may return None on success.
    if verified is not None and verified is not True:
        raise HTTPException(403, "Invalid gateway PIN")


@router.get("/health", response_model=list[SystemHealth])
async def system_health(
    redis: RedisClient = Depends(get_redis),
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for svc in ServiceName:
        cached = await redis.cache_get(f"health:{svc}")
        if cached and isinstance(cached, dict):
            results.append(cached)
        else:
            results.append(
                SystemHealth(service_name=svc, status="unknown").model_dump(mode="json"),
            )
    return results


@router.post("/kill-switch", status_code=status.HTTP_200_OK)
async def kill_switch(
    body: KillSwitchRequest,
    redis: RedisClient = Depends(get_redis),
) -> dict[str, str]:
    """Latch subsequent new entries; do not liquidate or recall in-flight orders."""
    await _require_pin(body.pin)
    await set_manual_halt(redis, True)
    # The latch is authoritative; a notification failure does not undo it.
    try:
        await redis.publish_channel("system:commands", {"command": "kill_switch", "reason": body.reason})
    except Exception:
        logger.warning("system.kill_notification_unavailable")
    await logger.awarning("KILL_SWITCH activated", reason=body.reason)
    return {"status": "kill_switch_activated", "reason": body.reason}


@router.post("/kill-switch/release")
async def release_manual_halt(body: KillSwitchRequest, redis: RedisClient = Depends(get_redis)) -> dict:
    """Only release the operator latch. AUTO stays off and risk holds are retained."""
    await _require_pin(body.pin)
    await set_manual_halt(redis, False)
    return {"status": "manual_halt_released", "auto_trade": False}


@router.post("/toggle-auto-trade")
async def toggle_auto_trade(
    body: ToggleAutoTradeRequest,
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    try:
        await set_auto(redis, body.enabled, body.expected_mode)
    except EntryBlocked as exc:
        raise HTTPException(503 if str(exc) == "controls_unavailable" else 409, str(exc)) from exc
    state = "enabled" if body.enabled else "disabled"
    await logger.ainfo("auto_trade.toggled", enabled=body.enabled)
    return {"auto_trade": body.enabled, "message": f"Auto-trade {state}"}


@router.get("/mode", response_model=ModeResponse)
async def get_mode(
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    try:
        snapshot = await read_controls(redis)
    except EntryBlocked as exc:
        raise HTTPException(503, "controls_unavailable") from exc
    payload = snapshot.payload()
    if snapshot.pause:
        try:
            payload["reconcile_diff"] = float(snapshot.pause.split("=", 1)[1])
        except (ValueError, IndexError):
            pass
    return payload


@router.post("/mode", response_model=ModeResponse)
async def switch_mode(
    body: SwitchModeRequest,
    redis: RedisClient = Depends(get_redis),
) -> dict[str, Any]:
    if body.mode == TradingMode.LIVE:
        if not body.pin:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "PIN required to switch to live mode",
            )
        await _require_pin(body.pin)

    try:
        await set_mode(redis, body.mode)
    except EntryBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        await redis.publish_channel("system:commands", {"command": "mode_switch", "mode": body.mode.value})
    except Exception:
        logger.warning("system.mode_notification_unavailable")
    await logger.ainfo("mode.switched", mode=body.mode.value)
    return await get_mode(redis)


@router.get("/reconcile")
async def reconcile_report() -> dict[str, Any]:
    """D-03 banner source: proxy the full reconcile report from nexus-data
    (the DuckDB owner). Falls back to an honest unavailable state.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_dsvc()}/api/data/reconcile")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("system.reconcile_proxy_failed", error=str(exc))
        return {"status": "unavailable", "error": str(exc)}
