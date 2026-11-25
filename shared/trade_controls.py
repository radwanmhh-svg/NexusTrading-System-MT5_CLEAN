"""Redis interlocks for NEW order entries (not liquidation or cancellation).

A0's session schedule is not an execution mode. Risk owns its cooldown; the
operator owns a separate, non-expiring latch. Changing mode or releasing the
latch always turns AUTO off, even if it was on before the stop.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from redis.exceptions import WatchError

from .constants import REDIS_KEY_PREFIX, TradingMode
from .redis_client import RedisClient

MANUAL = REDIS_KEY_PREFIX + "system:operator_halt"
RISK = REDIS_KEY_PREFIX + "system:kill_switch"  # existing risk/legacy halt
PAUSE = REDIS_KEY_PREFIX + "reconcile:auto_paused"
AUTO = REDIS_KEY_PREFIX + "system:auto_trade"
MODE = REDIS_KEY_PREFIX + "system:execution_mode"
LEASE_SECONDS = 86400


class EntryBlocked(RuntimeError):
    """An entry must not be sent. The message is a stable reason code."""


def _process_mode() -> str:
    return os.getenv("NEXUS_TRADING_MODE", "").strip().lower()


@dataclass(frozen=True)
class Controls:
    mode: TradingMode
    requested: bool
    manual: bool
    risk: bool
    pause: str | None

    @property
    def interlock(self) -> str | None:
        if self.manual:
            return "operator_halt"
        if self.risk:
            return "risk_halt"
        if self.pause is not None:
            return "reconciliation_pause"
        if self.mode not in (TradingMode.PAPER, TradingMode.LIVE):
            return self.mode.value
        if self.mode.value != _process_mode():
            return "process_mode_mismatch"
        return None

    @property
    def block(self) -> str | None:
        return self.interlock or (None if self.requested else "auto_off")

    def payload(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "auto_trade": self.block is None,
                "auto_requested": self.requested, "manual_halt": self.manual,
                "risk_halt": self.risk, "reconcile_paused": self.pause,
                "entry_block": self.block}


def _decode(manual, risk, pause, auto, mode) -> Controls:
    requested = False if auto is None else json.loads(auto)
    if type(requested) is not bool:
        raise ValueError("invalid AUTO flag")
    mode = TradingMode.SHADOW if mode is None else TradingMode(json.loads(mode))
    if isinstance(pause, bytes):
        pause = pause.decode()
    return Controls(mode, requested, bool(manual), bool(risk), pause)


async def read_controls(redis: RedisClient) -> Controls:
    """One consistent Redis snapshot. Errors and malformed values never authorize."""
    try:
        async with redis.r.pipeline(transaction=True) as tx:
            tx.exists(MANUAL).exists(RISK).get(PAUSE).get(AUTO).get(MODE)
            return _decode(*await tx.execute())
    except Exception as exc:
        raise EntryBlocked("controls_unavailable") from exc


async def halted(redis: RedisClient) -> bool:
    return bool(await redis.r.exists(MANUAL, RISK))


async def set_manual_halt(redis: RedisClient, active: bool) -> None:
    async with redis.r.pipeline(transaction=True) as tx:
        tx.set(AUTO, "false")
        if active:
            tx.set(MANUAL, "true")
        else:
            tx.delete(MANUAL)  # never release risk or reconciliation here
        await tx.execute()


async def set_mode(redis: RedisClient, mode: TradingMode) -> None:
    if mode == TradingMode.LIVE and _process_mode() != mode.value:
        raise EntryBlocked("process_mode_mismatch")
    async with redis.r.pipeline(transaction=True) as tx:
        tx.set(AUTO, "false").set(MODE, json.dumps(mode.value), ex=LEASE_SECONDS)
        await tx.execute()


async def set_auto(redis: RedisClient, enabled: bool, expected_mode: TradingMode | None = None) -> None:
    if not enabled:
        await redis.r.set(AUTO, "false")
        return
    # The read and guarded write use the same connection. A concurrent halt,
    # mode change, or key expiry invalidates the decision (no automatic retry).
    try:
        async with redis.r.pipeline(transaction=True) as tx:
            await tx.watch(MANUAL, RISK, PAUSE, AUTO, MODE)
            current = _decode(await tx.exists(MANUAL), await tx.exists(RISK),
                              await tx.get(PAUSE), await tx.get(AUTO), await tx.get(MODE))
            if current.interlock:
                raise EntryBlocked(current.interlock)
            if expected_mode is None or current.mode != expected_mode:
                raise EntryBlocked("mode_changed_or_unspecified")
            tx.multi()
            tx.set(AUTO, "true", ex=LEASE_SECONDS)
            await tx.execute()
    except WatchError as exc:
        raise EntryBlocked("controls_changed") from exc
    except EntryBlocked:
        raise
    except Exception as exc:
        raise EntryBlocked("controls_unavailable") from exc
