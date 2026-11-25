"""
MetaTrader 5 real-time data connector.

IMPORTANT (Windows): The MetaTrader5 C-extension uses COM/ATL under the
hood. COM requires that every call happens on the SAME OS thread that called
CoInitialize(). Using the default asyncio ThreadPoolExecutor (which rotates
worker threads) causes STATUS_HEAP_CORRUPTION (exit code 0xC0000374).

Fix: every MT5 call is dispatched through a single dedicated worker thread
(_MT5_EXECUTOR with max_workers=1). The thread is created once at import
time and is reused for every MT5 call for the lifetime of the process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import structlog

from shared.models import Tick, OHLCV

log = structlog.get_logger("nexus_data.mt5")
ISOLATED_MT5 = os.getenv("NEXUS_MT5_ISOLATED", "1").lower() in ("1", "true", "yes")

# ── Single-thread executor — ALL MT5 calls MUST go through this ──────────────
_MT5_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="mt5-com",
)

try:
    if ISOLATED_MT5:
        mt5 = None  # type: ignore[assignment]
    else:
        import MetaTrader5 as mt5  # type: ignore[import-untyped]
except ImportError:
    mt5 = None  # type: ignore[assignment]

_TF_MAP: dict[str, int] = {}
if mt5 is not None:
    _TF_MAP = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }

RECONNECT_DELAY_SEC = 5
MAX_RECONNECT_ATTEMPTS = 10

# I-022: persistent tick session — one long-lived mt5_worker subprocess per
# broker symbol instead of a fresh spawn + terminal login every 0.5s poll.
TICK_SESSION_TIMEOUT_SEC = 10.0
TICK_SESSION_INIT_TIMEOUT_SEC = 45.0
_SESSION_STDERR_KEEP = 8  # stderr lines kept for crash diagnostics


def parse_mt5_symbol_aliases() -> dict[str, str]:
    raw = os.getenv("MT5_SYMBOL_ALIASES", "").strip()
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            key = k.strip().upper().replace("/", "")
            val = v.strip()
            if key and val:
                out[key] = val
    return out


async def _mt5(fn: Callable, *args, **kwargs):
    """Run *fn* on the dedicated MT5 COM thread and return the result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_MT5_EXECUTOR, lambda: fn(*args, **kwargs))


_SECRET_KEYS = frozenset({"password", "pass", "token", "api_key", "secret"})


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy of *payload* with credential values masked."""
    return {
        k: ("***REDACTED***" if k.lower() in _SECRET_KEYS else v)
        for k, v in payload.items()
    }


async def _mt5_worker(payload: dict[str, Any], timeout_sec: float = 20.0) -> dict[str, Any]:
    """Run an MT5 call in a short-lived subprocess.

    This isolates native MetaTrader5 crashes from the nexus-data process.
    """
    worker = Path(__file__).with_name("mt5_worker.py")

    # SECURITY: credentials reach the worker only through the inherited process
    # environment, never the stdin payload. The payload is the argument to
    # asyncio's communicate(), whose internal `input` local is dumped verbatim
    # by rich tracebacks on any subprocess error/timeout — that is what printed
    # the broker password in clear text in the logs. Env vars live in
    # os.environ (a module global), which tracebacks never dump as a frame
    # local. We promote any creds that still arrive in the payload into
    # os.environ so the short-lived worker inherits them (subprocess with no
    # env= copies the parent environment), then redact the payload before it is
    # ever serialized — so neither a frame local nor the stdin bytes carry the
    # secret. Do NOT bind the credentials to a local here (e.g. a child_env
    # dict): such a local is exactly what the rich traceback would print.
    for src_key, env_key in (
        ("login", "MT5_LOGIN"),
        ("password", "MT5_PASSWORD"),
        ("server", "MT5_SERVER"),
    ):
        if payload.get(src_key) and not os.environ.get(env_key):
            os.environ[env_key] = str(payload[src_key])
    payload = _redact(payload)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(worker),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, default=str).encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError("MT5 worker timed out")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        out = stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MT5 worker failed ({proc.returncode}): {out or err}")

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MT5 worker returned invalid JSON: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "MT5 worker failed"))
    return data


class MT5Connector:
    """Async wrapper around the MetaTrader5 terminal library."""

    def __init__(self) -> None:
        self._connected: bool = False
        self._login: int = 0
        self._password: str = ""
        self._server: str = ""
        self._path: str = ""
        self._reconnect_attempts: int = 0
        # I-022: persistent tick sessions (one subprocess per broker symbol)
        self._sessions: dict[str, asyncio.subprocess.Process] = {}
        self._session_stderr: dict[str, list[str]] = {}
        self._session_stderr_tasks: dict[str, asyncio.Task] = {}
        # T15.8: تتبّع الانقطاع للشاشة — عدد الانقطاعات/آخر انقطاع/آخر استرداد
        self.disconnect_count: int = 0
        self.last_disconnect_ts: str = ""
        self.last_recovered_ts: str = ""

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, login: int, password: str, server: str, path: str = "") -> None:
        if mt5 is None and not ISOLATED_MT5:
            raise RuntimeError("MetaTrader5 package is not installed (Windows-only)")
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        await self._init_terminal()

    async def _init_terminal(self) -> None:
        if ISOLATED_MT5:
            await _mt5_worker({
                "op": "ping",
                "login": self._login,
                "password": self._password,
                "server": self._server,
                "path": self._path,
            })
            if self.last_disconnect_ts and self._connected is False:
                from datetime import datetime, timezone
                self.last_recovered_ts = datetime.now(timezone.utc).isoformat()
            was_disconnected = not self._connected
            self._connected = True
            self._reconnect_attempts = 0
            if was_disconnected and self.last_disconnect_ts:
                from datetime import datetime, timezone
                self.last_recovered_ts = datetime.now(timezone.utc).isoformat()
            await log.ainfo(
                "mt5.connected_isolated",
                login=self._login,
                server=self._server,
            )
            return

        kwargs: dict[str, Any] = {
            "login": self._login,
            "password": self._password,
            "server": self._server,
        }
        if self._path:
            kwargs["path"] = self._path

        def _do_init():
            assert mt5 is not None
            return mt5.initialize(**kwargs)

        ok = await _mt5(_do_init)
        if not ok:
            def _last_err():
                assert mt5 is not None
                return mt5.last_error()
            code, msg = await _mt5(_last_err)
            self._connected = False
            raise ConnectionError(f"MT5 initialize failed: [{code}] {msg}")

        self._connected = True
        self._reconnect_attempts = 0
        await log.ainfo("mt5.connected", login=self._login, server=self._server)

    async def _reconnect(self) -> bool:
        while self._reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
            self._reconnect_attempts += 1
            await log.awarning(
                "mt5.reconnecting",
                attempt=self._reconnect_attempts,
                max_attempts=MAX_RECONNECT_ATTEMPTS,
            )
            try:
                await self._init_terminal()
                return True
            except ConnectionError:
                await asyncio.sleep(RECONNECT_DELAY_SEC)
        await log.aerror("mt5.reconnect_exhausted")
        return False

    def _mark_disconnected(self) -> None:
        from datetime import datetime, timezone
        self._connected = False
        self.disconnect_count += 1
        self.last_disconnect_ts = datetime.now(timezone.utc).isoformat()

    # ── I-022: persistent tick session ──────────────────────────────────

    async def _drain_session_stderr(self, broker_symbol: str, proc) -> None:
        """Keep the stderr pipe from filling (which would deadlock the worker)
        while retaining the last few lines for crash diagnostics."""
        buf = self._session_stderr.setdefault(broker_symbol, [])
        try:
            while proc.returncode is None:
                line = await proc.stderr.readline()
                if not line:
                    break
                buf.append(line.decode("utf-8", errors="replace").rstrip())
                del buf[:-_SESSION_STDERR_KEEP]
        except Exception:
            pass

    async def _start_session(self, broker_symbol: str) -> None:
        """Spawn a persistent mt5_worker session for *broker_symbol*."""
        self._stop_session(broker_symbol)
        logical = broker_symbol.upper().replace("/", "")
        worker = Path(__file__).with_name("mt5_worker.py")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker),
                "--session",
                "--symbol", logical,
                "--broker-symbol", broker_symbol,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            self._mark_disconnected()
            raise
        self._sessions[broker_symbol] = proc
        self._session_stderr[broker_symbol] = []
        self._session_stderr_tasks[broker_symbol] = asyncio.create_task(
            self._drain_session_stderr(broker_symbol, proc)
        )
        # The worker reads creds from the inherited environment — same pattern
        # as the one-shot path (never via stdin, which tracebacks would dump).
        for env_key, value in (
            ("MT5_LOGIN", str(self._login or 0)),
            ("MT5_PASSWORD", self._password or ""),
            ("MT5_SERVER", self._server or ""),
        ):
            if value and not os.environ.get(env_key):
                os.environ[env_key] = value
        try:
            # The worker writes a single banner line once mt5.initialize() +
            # symbol_select() have completed — read it before any request so
            # the request/response protocol stays aligned.
            banner_raw = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=TICK_SESSION_INIT_TIMEOUT_SEC,
            )
            banner = json.loads(banner_raw.decode("utf-8", errors="replace"))
            if not banner.get("ok"):
                raise RuntimeError(str(banner.get("error") or "session init failed"))
            await self._session_call(
                broker_symbol,
                {"op": "ping"},
                timeout=TICK_SESSION_INIT_TIMEOUT_SEC,
            )
        except Exception:
            self._mark_disconnected()
            self._stop_session(broker_symbol)
            raise
        # A live session means MT5 is initialised and reachable — restore the
        # connection state here (no separate _reconnect() needed anywhere).
        self._connected = True
        self._reconnect_attempts = 0
        await log.ainfo("mt5.tick_session_started", broker_symbol=broker_symbol)

    def _stop_session(self, broker_symbol: str) -> None:
        proc = self._sessions.pop(broker_symbol, None)
        task = self._session_stderr_tasks.pop(broker_symbol, None)
        if task is not None:
            task.cancel()
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.write(b'{"op":"close"}\n')
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _stop_all_sessions(self) -> None:
        for broker_symbol in list(self._sessions):
            self._stop_session(broker_symbol)

    async def _session_call(
        self,
        broker_symbol: str,
        req: dict[str, Any],
        *,
        timeout: float = TICK_SESSION_TIMEOUT_SEC,
    ) -> dict[str, Any]:
        """One JSON-line request/response against the persistent session.

        A single in-flight request at a time (the tick stream loop is the only
        caller). On timeout the session is torn down — a cancelled readline
        may leave a partial line in the pipe, so the process is not reusable.
        """
        proc = self._sessions.get(broker_symbol)
        if proc is None or proc.returncode is not None:
            raise RuntimeError(f"mt5 tick session not running for {broker_symbol}")
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps(req, default=str).encode("utf-8") + b"\n")
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            stderr_tail = " | ".join(self._session_stderr.get(broker_symbol, [])[-3:])
            await log.aerror(
                "mt5.tick_session_timeout",
                broker_symbol=broker_symbol,
                op=req.get("op"),
                stderr=stderr_tail,
            )
            self._stop_session(broker_symbol)
            raise TimeoutError(f"mt5 tick session timed out for {broker_symbol}")
        if proc.returncode is not None:
            raise RuntimeError(
                f"mt5 tick session died (code={proc.returncode}): "
                f"{' | '.join(self._session_stderr.get(broker_symbol, [])[-3:])}"
            )
        try:
            data = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            self._stop_session(broker_symbol)
            raise RuntimeError(f"mt5 tick session bad JSON: {exc}") from exc
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "mt5 tick session op failed"))
        return data

    async def _get_tick_via_session(self, symbol: str) -> Tick:
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        proc = self._sessions.get(resolved)
        if proc is None or proc.returncode is not None:
            await self._start_session(resolved)
        data = await self._session_call(resolved, {
            "op": "tick",
            "symbol": logical,
            "broker_symbol": resolved,
        })
        tick = data["tick"]
        return Tick(
            symbol=logical,
            bid=float(tick["bid"]),
            ask=float(tick["ask"]),
            timestamp=datetime.fromisoformat(tick["timestamp"]),
        )

    async def disconnect(self) -> None:
        self._stop_all_sessions()
        if ISOLATED_MT5:
            self._mark_disconnected()
            await log.ainfo("mt5.disconnected_isolated")
            return
        if mt5 is not None and self._connected:
            await _mt5(mt5.shutdown)
            self._connected = False
            await log.ainfo("mt5.disconnected")

    def resolve_symbol(self, logical: str) -> str:
        s = logical.upper().replace("/", "")
        aliases = parse_mt5_symbol_aliases()
        if s in aliases:
            return aliases[s]
        # case-insensitive fallback: "XAUUSDS" (uppercased by a caller)
        # must still resolve to the broker symbol "XAUUSDs".
        for k, v in aliases.items():
            if v.upper() == s:
                return v
        return s

    async def _ensure_symbol_selected(self, broker_symbol: str) -> None:
        self._ensure_connected()
        if ISOLATED_MT5:
            return

        def _sel():
            assert mt5 is not None
            if not mt5.symbol_select(broker_symbol, True):
                raise ValueError(
                    f"MT5 symbol_select failed for {broker_symbol!r} — add it in Market Watch "
                    f"or set MT5_SYMBOL_ALIASES (e.g. XAUUSD=XAUUSDm)."
                )

        await _mt5(_sel)

    async def get_tick(self, symbol: str) -> Tick:
        self._ensure_connected()
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        if ISOLATED_MT5:
            data = await _mt5_worker({
                "op": "tick",
                "login": self._login,
                "password": self._password,
                "server": self._server,
                "path": self._path,
                "symbol": logical,
                "broker_symbol": resolved,
            }, timeout_sec=10.0)
            tick = data["tick"]
            return Tick(
                symbol=logical,
                bid=float(tick["bid"]),
                ask=float(tick["ask"]),
                timestamp=datetime.fromisoformat(tick["timestamp"]),
            )
        await self._ensure_symbol_selected(resolved)

        def _fetch():
            assert mt5 is not None
            return mt5.symbol_info_tick(resolved)

        info = await _mt5(_fetch)
        if info is None:
            raise ValueError(f"MT5 returned no tick for {resolved} (logical={logical})")
        return Tick(
            symbol=logical,
            bid=info.bid,
            ask=info.ask,
            timestamp=datetime.fromtimestamp(info.time, tz=timezone.utc),
        )

    async def get_ohlcv(self, symbol: str, timeframe: str, count: int = 500) -> list[OHLCV]:
        self._ensure_connected()
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        tf = _TF_MAP.get(timeframe)
        if not ISOLATED_MT5 and tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if ISOLATED_MT5:
            data = await _mt5_worker({
                "op": "ohlcv",
                "login": self._login,
                "password": self._password,
                "server": self._server,
                "path": self._path,
                "symbol": logical,
                "broker_symbol": resolved,
                "timeframe": timeframe,
                "count": count,
            }, timeout_sec=max(20.0, min(240.0, count / 100.0)))
            return [
                OHLCV(
                    symbol=logical,
                    timeframe=timeframe,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["volume"]),
                    timestamp=datetime.fromisoformat(r["timestamp"]),
                )
                for r in data["bars"]
            ]

        await self._ensure_symbol_selected(resolved)

        def _fetch():
            assert mt5 is not None
            return mt5.copy_rates_from_pos(resolved, tf, 0, count)

        rates = await _mt5(_fetch)
        if rates is None or len(rates) == 0:
            raise ValueError(
                f"MT5 returned no OHLCV for {resolved}/{timeframe} (logical={logical})"
            )
        return [
            OHLCV(
                symbol=logical,
                timeframe=timeframe,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["tick_volume"]),
                timestamp=datetime.fromtimestamp(r["time"], tz=timezone.utc),
            )
            for r in rates
        ]

    async def get_ohlcv_range(self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime) -> list[OHLCV]:
        """Fetch OHLCV bars for an explicit UTC window (incremental backfill).

        Uses ``copy_rates_range`` in the isolated worker with a generous
        timeout, because a multi-month window takes far longer than a
        count-based pull and must not be retried as a fresh 20s subprocess.
        """
        self._ensure_connected()
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        if not ISOLATED_MT5:
            raise ValueError("ohlcv_range requires the isolated MT5 worker")
        data = await _mt5_worker({
            "op": "ohlcv_range",
            "login": self._login,
            "password": self._password,
            "server": self._server,
            "path": self._path,
            "symbol": logical,
            "broker_symbol": resolved,
            "timeframe": timeframe,
            "date_from": date_from.astimezone(timezone.utc).isoformat(),
            "date_to": date_to.astimezone(timezone.utc).isoformat(),
        }, timeout_sec=240.0)
        return [
            OHLCV(
                symbol=logical,
                timeframe=timeframe,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                timestamp=datetime.fromisoformat(r["timestamp"]),
            )
            for r in data["bars"]
        ]

    async def get_execution_history(self, symbol: str, days: int = 7) -> dict[str, list]:
        """Fetch broker order+deal history for reconciliation (execution journal)."""
        self._ensure_connected()
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        if ISOLATED_MT5:
            data = await _mt5_worker({
                "op": "execution_history",
                "login": self._login,
                "password": self._password,
                "server": self._server,
                "path": self._path,
                "symbol": logical,
                "broker_symbol": resolved,
                "days": days,
            }, timeout_sec=60.0)
            return {"orders": data["orders"], "deals": data["deals"]}
        assert mt5 is not None
        def _fetch():
            date_to = datetime.now(timezone.utc)
            date_from = date_to - timedelta(days=days)
            orders = mt5.history_orders_get(logical, date_from, date_to) or []
            deals = mt5.history_deals_get(logical, date_from, date_to) or []
            return ([o._asdict() if hasattr(o, "_asdict") else dict(o) for o in orders],
                    [d._asdict() if hasattr(d, "_asdict") else dict(d) for d in deals])
        orders, deals = await _mt5(_fetch)
        return {"orders": orders, "deals": deals}

    async def get_symbol_spec(self, symbol: str) -> dict[str, Any]:
        """Read the broker's REAL contract specification for a symbol.

        Returns the full ``symbol_info`` payload from the isolated worker so
        risk sizing uses OneRoyal's actual tick value / contract size / volume
        constraints instead of generic assumptions.
        """
        self._ensure_connected()
        logical = symbol.upper().replace("/", "")
        resolved = self.resolve_symbol(logical)
        data = await _mt5_worker({
            "op": "symbol_spec",
            "login": self._login,
            "password": self._password,
            "server": self._server,
            "path": self._path,
            "symbol": logical,
            "broker_symbol": resolved,
        }, timeout_sec=30.0)
        return data["spec"]

    async def get_account_info(self) -> dict[str, Any]:
        self._ensure_connected()
        if ISOLATED_MT5:
            data = await _mt5_worker({
                "op": "account",
                "login": self._login,
                "password": self._password,
                "server": self._server,
                "path": self._path,
            }, timeout_sec=10.0)
            return data["account"]

        def _fetch():
            assert mt5 is not None
            info = mt5.account_info()
            if info is None:
                raise RuntimeError("MT5 account_info returned None")
            return info._asdict()

        return await _mt5(_fetch)

    async def stream_ticks(
        self,
        symbols: list[str],
        callback: Callable[[Tick], Awaitable[None]],
        poll_interval: float = 0.5,
    ) -> None:
        last_times: dict[str, int] = {}
        _polls = 0  # diag: poll counter for tick_poll heartbeat
        consecutive_errors = 0
        # I-022 (live bridge-close test finding): `while self._connected` + a
        # finite _reconnect() meant one MT5 outage killed this loop FOREVER —
        # the same pattern as the 2026-11-23 publisher freeze. The loop now
        # never exits: sessions respawn and re-init MT5 on every poll, with a
        # bounded backoff when nothing works.
        while True:
            got_data = False
            for symbol in symbols:
                try:
                    logical = symbol.upper().replace("/", "")
                    # I-022: persistent session instead of a fresh subprocess
                    # + full terminal login on every 0.5s poll.
                    tick = await self._get_tick_via_session(logical)
                    ts_key = int(tick.timestamp.timestamp() * 1000)
                    if ts_key != last_times.get(symbol):
                        last_times[symbol] = ts_key
                        await callback(tick)
                    consecutive_errors = 0
                    got_data = True
                    _polls += 1
                    if _polls == 1 or _polls % 120 == 0:
                        await log.ainfo(
                            "mt5.tick_poll",
                            symbol=symbol, bid=tick.bid, ask=tick.ask,
                            ts=str(tick.timestamp), n=_polls,
                        )
                except Exception:
                    consecutive_errors += 1
                    await log.aexception(
                        "mt5.stream_tick_error", symbol=symbol,
                        consecutive_errors=consecutive_errors,
                    )
                    # Session dead or stale — force a clean respawn next poll.
                    # No _reconnect() here: the session spawn itself performs
                    # the MT5 init, so a closed terminal just retries with the
                    # bounded backoff below instead of killing the loop.
                    resolved = self.resolve_symbol(symbol.upper().replace("/", ""))
                    self._stop_session(resolved)
            if not got_data and consecutive_errors > 0:
                # I-022: back off so a dead terminal does not produce a hot
                # spawn/error loop at the poll interval.
                backoff = min(5.0, poll_interval * (1 + consecutive_errors))
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(poll_interval)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MT5Connector is not connected")
