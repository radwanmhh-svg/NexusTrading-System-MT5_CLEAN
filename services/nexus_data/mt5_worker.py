"""
Isolated MetaTrader 5 worker for nexus-data.

The MetaTrader5 Python extension can crash the host process on Windows
(STATUS_HEAP_CORRUPTION / 0xC0000374). nexus-data calls this helper in a
short-lived subprocess so a native crash kills only the worker, not the
FastAPI service.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


def _fail(message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": message}), flush=True)
    raise SystemExit(code)


def _drop_forming_bar(rates, tf_minutes: int) -> list:
    """B-03: drop the FORMING (not-yet-closed) bar.

    copy_rates_from_pos(pos=0) includes the current bar, whose OHLC is still
    changing. Features/labels must use CLOSED candles only (directive B-03:
    'كل feature تُحسب على bars[:-1]'). A bar is closed when
    open_time + timeframe_seconds <= now.
    """
    if rates is None or len(rates) == 0:
        return rates
    tf_sec = int(tf_minutes) * 60
    now_ts = datetime.now(timezone.utc).timestamp()
    try:
        last_open = float(rates[-1]["time"])
    except Exception:
        return rates
    if last_open + tf_sec > now_ts:
        return rates[:-1]
    return rates


def _timeframes(mt5) -> dict[str, int]:
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }


def _initialise(mt5, payload: dict[str, Any]) -> None:
    # Credentials come from the environment, never the stdin payload. The
    # payload is the arg to asyncio's communicate(), whose internal `input`
    # local is dumped by rich tracebacks on any subprocess error — sending
    # the password there printed it in clear text in the logs. Env vars are
    # not shown in tracebacks. (Payload is still read as a fallback so an
    # older caller keeps working.)
    kwargs: dict[str, Any] = {
        "login": int(os.getenv("MT5_LOGIN") or payload.get("login") or 0),
        "password": os.getenv("MT5_PASSWORD") or payload.get("password") or "",
        "server": os.getenv("MT5_SERVER") or payload.get("server") or "",
    }
    path = payload.get("path") or ""
    if path:
        kwargs["path"] = path
    if not mt5.initialize(**kwargs):
        code, msg = mt5.last_error()
        _fail(f"MT5 initialize failed: [{code}] {msg}", 2)


def _session_dispatch(mt5, symbol: str, broker_symbol: str, req: dict[str, Any]) -> dict[str, Any]:
    """Handle one cheap op inside a persistent session.

    Errors are reported per-request, never fatal — the connector decides when
    the session is no longer worth keeping (I-022).
    """
    op = req.get("op")
    if op == "tick":
        tick = mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            return {"ok": False, "error": f"MT5 returned no tick for {broker_symbol!r}"}
        return {
            "ok": True,
            "tick": {
                "symbol": symbol,
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "timestamp": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
            },
        }
    if op == "ping":
        info = mt5.account_info()
        return {
            "ok": True,
            "connected": info is not None,
            "account": info._asdict() if info is not None else None,
        }
    if op == "close":
        return {"ok": True, "closed": True}
    return {"ok": False, "error": f"unsupported session op: {op}"}


def _run_session(mt5, symbol: str, broker_symbol: str) -> None:
    """Persistent-session mode (I-022).

    The one-shot mode below spawns a fresh subprocess + full mt5.initialize()
    login for every call — at a 0.5s tick poll that meant ~2 spawns and ~2
    terminal logins per second. In session mode the terminal is initialised
    ONCE, then requests arrive as JSON lines on stdin and responses leave as
    JSON lines on stdout until EOF or an explicit {"op":"close"}.
    """
    sys.stdout.write(json.dumps({"ok": True, "session": True, "symbol": symbol, "broker_symbol": broker_symbol}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            sys.stdout.write(json.dumps({"ok": False, "error": f"bad session request: {exc}"}) + "\n")
            sys.stdout.flush()
            continue
        resp = _session_dispatch(mt5, symbol, broker_symbol, req)
        sys.stdout.write(json.dumps(resp, default=str) + "\n")
        sys.stdout.flush()
        if req.get("op") == "close":
            break


def _argv_value(flag: str) -> str:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


def main() -> None:
    if "--session" in sys.argv:
        # I-022: session mode takes NO stdin payload — stdin stays open and
        # carries one JSON request per line after init. Credentials come from
        # the inherited environment only; the symbol arrives via CLI args
        # because stdin is reserved for the request loop.
        payload: dict[str, Any] = {}
        symbol = _argv_value("--symbol").upper().replace("/", "")
        broker_symbol = _argv_value("--broker-symbol") or symbol
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except Exception as exc:
            _fail(f"bad worker payload: {exc}")

    try:
        import MetaTrader5 as mt5  # type: ignore[import-untyped]
    except Exception as exc:
        _fail(f"MetaTrader5 import failed: {exc}", 2)

    op = payload.get("op")
    if "--session" not in sys.argv:
        symbol = str(payload.get("symbol") or "").upper().replace("/", "")
        broker_symbol = str(payload.get("broker_symbol") or symbol)

    try:
        _initialise(mt5, payload)
        if symbol and not mt5.symbol_select(broker_symbol, True):
            _fail(f"MT5 symbol_select failed for {broker_symbol!r}", 3)

        # I-022: persistent session — read ops line-by-line after init.
        if op == "session" or "--session" in sys.argv:
            try:
                _run_session(mt5, symbol, broker_symbol)
            finally:
                try:
                    mt5.shutdown()
                except Exception:
                    pass
            return

        if op == "ping":
            info = mt5.account_info()
            out = {
                "ok": True,
                "connected": info is not None,
                "account": info._asdict() if info is not None else None,
            }
        elif op == "tick":
            tick = mt5.symbol_info_tick(broker_symbol)
            if tick is None:
                _fail(f"MT5 returned no tick for {broker_symbol!r}", 4)
            out = {
                "ok": True,
                "tick": {
                    "symbol": symbol,
                    "bid": float(tick.bid),
                    "ask": float(tick.ask),
                    "timestamp": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
                },
            }
        elif op == "ohlcv":
            tf = _timeframes(mt5).get(str(payload.get("timeframe") or "M15"))
            if tf is None:
                _fail(f"unsupported timeframe: {payload.get('timeframe')}", 5)
            count = int(payload.get("count") or 500)
            rates = mt5.copy_rates_from_pos(broker_symbol, tf, 0, count)
            if rates is None or len(rates) == 0:
                _fail(f"MT5 returned no OHLCV for {broker_symbol!r}", 6)
            # B-03: closed candles only (the newest rate at pos 0 is the
            # still-forming bar). Consumers (features/labels/training) must
            # never see a partial bar.
            if bool(payload.get("closed_only", True)):
                rates = rates[:-1] if len(rates) > 1 else rates
            if len(rates) == 0:
                _fail(f"no closed OHLCV bars for {broker_symbol!r}", 6)
            out = {
                "ok": True,
                "bars": [
                    {
                        "symbol": symbol,
                        "timeframe": payload.get("timeframe") or "M15",
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["tick_volume"]),
                        "timestamp": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                    }
                    for r in rates
                ],
            }
        elif op == "ohlcv_range":
            tf = _timeframes(mt5).get(str(payload.get("timeframe") or "M15"))
            if tf is None:
                _fail(f"unsupported timeframe: {payload.get('timeframe')}", 5)
            date_from = datetime.fromisoformat(str(payload.get("date_from")).replace("Z", "+00:00"))
            date_to = datetime.fromisoformat(str(payload.get("date_to")).replace("Z", "+00:00"))
            rates = mt5.copy_rates_range(broker_symbol, tf, date_from, date_to)
            if rates is None or len(rates) == 0:
                _fail(f"MT5 returned no OHLCV for {broker_symbol!r} in range", 6)
            # B-03: the newest bar in a range that reaches 'now' may still be
            # forming — same closed-only rule as the ohlcv op.
            rates = _drop_forming_bar(rates, int(tf))
            if rates is None or len(rates) == 0:
                _fail(f"no closed OHLCV bars for {broker_symbol!r} in range", 6)
            out = {
                "ok": True,
                "bars": [
                    {
                        "symbol": symbol,
                        "timeframe": payload.get("timeframe") or "M15",
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["tick_volume"]),
                        "timestamp": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                    }
                    for r in rates
                ],
            }
        elif op == "ticks_range":
            date_from = datetime.fromisoformat(str(payload.get("date_from")).replace("Z", "+00:00"))
            date_to = datetime.fromisoformat(str(payload.get("date_to")).replace("Z", "+00:00"))
            ticks = mt5.copy_ticks_range(broker_symbol, date_from, date_to, mt5.COPY_TICKS_INFO)
            if ticks is None or len(ticks) == 0:
                _fail(f"MT5 returned no ticks for {broker_symbol!r} in range", 10)
            out = {
                "ok": True,
                "count": len(ticks),
                "first": datetime.fromtimestamp(int(ticks[0]["time_msc"]) / 1000, tz=timezone.utc).isoformat(),
                "last": datetime.fromtimestamp(int(ticks[-1]["time_msc"]) / 1000, tz=timezone.utc).isoformat(),
                "sample": [
                    {
                        "time": datetime.fromtimestamp(int(t["time_msc"]) / 1000, tz=timezone.utc).isoformat(),
                        "bid": float(t["bid"]),
                        "ask": float(t["ask"]),
                        "last": float(t["last"]),
                        "volume": int(t["volume"]),
                        "flags": int(t["flags"]),
                    }
                    for t in ticks[:5]
                ],
            }
        elif op == "execution_history":
            from_days = int(payload.get("days") or 7)
            now = datetime.now(timezone.utc)
            date_from = now - timedelta(days=from_days)
            # MetaTrader5 API: first two positional args are DATES; symbol
            # selection is by group wildcard. Passing the symbol name as a
            # date silently returned an empty history (D-01 bug).
            group = f"*{broker_symbol}*"
            orders = mt5.history_orders_get(date_from, now, group=group)
            deals = mt5.history_deals_get(date_from, now, group=group)
            out = {
                "ok": True,
                "orders": [o._asdict() for o in (orders or [])],
                "deals": [d._asdict() for d in (deals or [])],
            }
        elif op == "symbol_spec":
            info = mt5.symbol_info(broker_symbol)
            if info is None:
                _fail(f"MT5 symbol_info returned None for {broker_symbol!r}", 9)
            props = info._asdict()
            out = {"ok": True, "spec": props}
        elif op == "account":
            info = mt5.account_info()
            if info is None:
                _fail("MT5 account_info returned None", 7)
            out = {"ok": True, "account": info._asdict()}
        else:
            _fail(f"unsupported op: {op}", 8)
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass

    print(json.dumps(out, default=str), flush=True)


if __name__ == "__main__":
    main()
