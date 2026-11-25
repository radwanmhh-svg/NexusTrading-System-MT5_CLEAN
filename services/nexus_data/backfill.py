"""
Nexus Data — incremental OHLCV backfill engine.

The "smart import" layer:

1.  Every (symbol, timeframe) pair has a persistent registry record in Redis
    (hash ``nexus:data:import:registry:{symbol}``) storing the timestamp of the
    last imported candle, bars imported and last update time.
2.  On every run the engine only downloads candles NEWER than the registered
    ``last_ts`` — it never re-downloads the same history twice.
3.  Runs can be triggered from the dashboard (POST /api/data/import/run), from
    the CLI, or automatically at service startup (NEXUS_IMPORT_AUTO=1).
4.  On graceful shutdown the registry is flushed with a final checkpoint so the
    next start resumes exactly where the previous session stopped.

Candles land in DuckDB (``ohlcv`` table).

FIX (2026-11-22): H1 candles are NO longer merged into
``nexus:features:history:{symbol}``. That key is the training history fed to
ML/DL/RL and must hold a single feature schema: the live M5 features computed
by nexus-features and appended by nexus-ml's ``_accumulate_training_history``.
Mixing H1 ``compute_features`` records (24 features) with live M5 records
(41 features) poisoned the schema — RL logged "feature mismatch: got 41,
fitted on 24" and abstained.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from shared.redis_client import RedisClient

logger = structlog.get_logger("nexus_data.backfill")

REGISTRY_KEY_TPL = "nexus:data:import:registry:{symbol}"
PROGRESS_KEY_TPL = "nexus:data:import:progress:{symbol}"
SHUTDOWN_CHECKPOINT_KEY = "nexus:data:import:last_shutdown"
FEATURE_HISTORY_KEY_TPL = "nexus:features:history:{symbol}"

_TF_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

# Backfill configuration (env-driven, documented in .env.example)
IMPORT_SYMBOLS = os.getenv("NEXUS_IMPORT_SYMBOLS", "XAUUSD,BTCUSD")
IMPORT_TIMEFRAMES = os.getenv(
    "NEXUS_IMPORT_TIMEFRAMES", os.getenv("NEXUS_OHLCV_TIMEFRAME", "M5,M15,H1,H4,D1")
)
IMPORT_START_DATE = os.getenv("NEXUS_IMPORT_START_DATE", "2024-01-01")
IMPORT_MAX_BARS = int(os.getenv("NEXUS_IMPORT_MAX_BARS", "20000"))
IMPORT_AUTO = os.getenv("NEXUS_IMPORT_AUTO", "1") not in ("0", "false", "no")


def _parse_start(value: str | None) -> datetime:
    if not value:
        return datetime(2024, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime(2024, 1, 1, tzinfo=timezone.utc)


async def registry_status(
    redis_client: RedisClient, symbol: str,
    *, db_stats: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Read the import registry for one symbol.

    T15.6: يُدمَج دائماً مع إحصاء القاعدة الفعلية (صفوف/آخر شمعة لكل فريم) —
    السجل في Redis وحده كان يعرض "لم يُستورد" رغم 1.03M صف مخزّنة،
    ويُظهر الخطأ الأخير إن وُجد بدل الصمت الكامل.
    """
    raw = await redis_client.r.hgetall(REGISTRY_KEY_TPL.format(symbol=symbol.upper()))
    out: dict[str, Any] = {"symbol": symbol.upper(), "timeframes": {}}
    for tf, blob in (raw or {}).items():
        try:
            out["timeframes"][tf] = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue

    # دمج القاعدة الفعلية (المصدر الحقيقي) — يفوق السجل دائماً في العرض
    for tf, stats in (db_stats or {}).items():
        rec = out["timeframes"].setdefault(tf, {})
        rec["db_rows"] = stats.get("rows", 0)
        rec["db_last_ts"] = stats.get("last_ts", "")
        rec.setdefault("updated_at", rec["db_last_ts"])

    out["updated_at"] = max(
        (rec.get("updated_at", "") for rec in out["timeframes"].values()),
        default="",
    )
    return out


async def db_ohlcv_stats(data_store, symbol: str) -> dict[str, dict]:
    """T15.6: صفوف وآخر شمعة لكل فريم من القاعدة الفعلية (قراءة فقط)."""
    stats: dict[str, dict] = {}
    try:
        con = data_store._conn
        rows = con.execute(
            "SELECT timeframe, COUNT(*) AS n, MAX(timestamp) AS last_ts "
            "FROM ohlcv WHERE symbol=? GROUP BY timeframe",
            [symbol.upper()],
        ).fetchall()
        for tf, n, last_ts in rows:
            stats[tf] = {"rows": int(n), "last_ts": str(last_ts)}
    except Exception:
        pass
    return stats


async def _set_progress(
    redis_client: RedisClient, symbol: str, payload: dict[str, Any]
) -> None:
    await redis_client.cache_set(
        PROGRESS_KEY_TPL.format(symbol=symbol.upper()), payload, ttl_sec=86400
    )


async def _get_progress(redis_client: RedisClient, symbol: str) -> dict[str, Any] | None:
    return await redis_client.cache_get(PROGRESS_KEY_TPL.format(symbol=symbol.upper()))


async def shutdown_checkpoint(redis_client: RedisClient) -> None:
    """Flush a final registry checkpoint on graceful shutdown."""
    try:
        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "registry": {
                sym.upper(): await registry_status(redis_client, sym)
                for sym in IMPORT_SYMBOLS.split(",")
            },
        }
        await redis_client.cache_set(
            SHUTDOWN_CHECKPOINT_KEY, snapshot, ttl_sec=0 or 60 * 60 * 24 * 30
        )
        await logger.ainfo("backfill.shutdown_checkpoint_saved")
    except Exception:
        await logger.aexception("backfill.shutdown_checkpoint_failed")


async def _merge_h1_features(
    redis_client: RedisClient, symbol: str, candles: list[dict[str, Any]]
) -> int:
    """Compute ML features from new H1 candles and merge into training history."""
    if not candles:
        return 0
    try:
        from bootstrap_ensemble import compute_features  # type: ignore
    except Exception:
        await logger.awarning("backfill.features_module_unavailable")
        return 0

    feats = compute_features(candles)
    if not feats:
        return 0

    key = FEATURE_HISTORY_KEY_TPL.format(symbol=symbol.upper())
    existing = await redis_client.r.lrange(key, 0, -1)
    old_list = [json.loads(o) for o in existing] if existing else []
    new_ts = {f["timestamp"] for f in feats}
    merged = [f for f in old_list if f.get("timestamp") not in new_ts] + feats
    merged.sort(key=lambda f: f["timestamp"])
    await redis_client.r.delete(key)
    if merged:
        await redis_client.r.rpush(key, *[json.dumps(f) for f in merged])
    return len(feats)


async def run_backfill(
    redis_client: RedisClient,
    data_store: Any,
    mt5_connector: Any,
    symbol: str,
    timeframes: list[str] | None = None,
    start_date: str | None = None,
    max_bars: int | None = None,
) -> dict[str, Any]:
    """Incrementally import candles for one symbol across the given timeframes.

    Only candles newer than the registry ``last_ts`` are fetched and stored.
    """
    symbol = symbol.upper()
    tfs = timeframes or [t.strip().upper() for t in IMPORT_TIMEFRAMES.split(",")]
    max_bars = max_bars or IMPORT_MAX_BARS
    started = datetime.now(timezone.utc)

    await _set_progress(redis_client, symbol, {
        "symbol": symbol,
        "status": "running",
        "started_at": started.isoformat(),
        "timeframes": tfs,
        "done": [],
        "current": None,
        "error": None,
    })

    results: dict[str, Any] = {}
    for tf in tfs:
        tf = tf.strip().upper()
        if tf not in _TF_SECONDS:
            results[tf] = {"skipped": f"unsupported timeframe {tf}"}
            continue

        await _set_progress(redis_client, symbol, {
            "symbol": symbol,
            "status": "running",
            "started_at": started.isoformat(),
            "timeframes": tfs,
            "current": tf,
        })

        registry = await registry_status(redis_client, symbol)
        rec = registry["timeframes"].get(tf)
        last_ts: datetime
        if rec and rec.get("last_ts"):
            last_ts = _parse_start(rec["last_ts"])
            mode = "incremental"
        else:
            last_ts = _parse_start(start_date or IMPORT_START_DATE)
            mode = "full"

        now = datetime.now(timezone.utc)
        span_sec = max(0.0, (now - last_ts).total_seconds())
        needed = int(span_sec / _TF_SECONDS[tf]) + 60
        fetch_count = min(max_bars, max(needed, 500))
        bars: list[Any] = []
        range_error: str | None = None
        last_fetch_error: Exception | None = None
        for attempt in range(3):
            try:
                bars = await mt5_connector.get_ohlcv_range(symbol, tf, last_ts, now)
                break
            except Exception as exc:
                # Many brokers only expose the most recent N bars per timeframe,
                # so copy_rates_range can legitimately return nothing. Fall
                # back to a count-based pull of the most recent bars — the
                # incremental registry then simply starts from the oldest bar
                # the broker has.
                range_error = str(exc)
                last_fetch_error = exc
                try:
                    bars = await mt5_connector.get_ohlcv(symbol, tf, fetch_count)
                    break
                except Exception as exc2:
                    last_fetch_error = exc2
                    # Transient disconnects happen while MT5 reconnects in the
                    # background; give it a moment and try the window again.
                    await asyncio.sleep(10 * (attempt + 1))
        if last_fetch_error is not None and not bars:
            await logger.aexception("backfill.fetch_failed", symbol=symbol, tf=tf)
            results[tf] = {"error": str(last_fetch_error), "mode": mode}
            await _set_progress(redis_client, symbol, {
                "symbol": symbol,
                "status": "running",
                "started_at": started.isoformat(),
                "timeframes": tfs,
                "done": list(results.keys()),
                "current": None,
                "last_error": f"{tf}: {last_fetch_error}",
            })
            continue
        fetch_count = len(bars)

        new_bars = [b for b in bars if b.timestamp.replace(tzinfo=timezone.utc) > last_ts]
        if max_bars and len(new_bars) > max_bars:
            new_bars = new_bars[-max_bars:]
        new_bars.sort(key=lambda b: b.timestamp)

        inserted = 0
        if new_bars:
            try:
                await data_store.insert_batch(new_bars)
                inserted = len(new_bars)
            except Exception:
                await logger.aexception("backfill.store_failed", symbol=symbol, tf=tf)
                results[tf] = {"error": "duckdb insert failed", "mode": mode}
                continue

        latest_ts = new_bars[-1].timestamp if new_bars else last_ts
        record = {
            "last_ts": latest_ts.isoformat(),
            "bars_total": int((rec or {}).get("bars_total", 0)) + inserted,
            "last_run_new": inserted,
            "mode": mode,
            "fetched_count": fetch_count,
            "updated_at": now.isoformat(),
        }
        await redis_client.r.hset(
            REGISTRY_KEY_TPL.format(symbol=symbol), tf, json.dumps(record)
        )
        results[tf] = record

        # FIX (2026-11-22): توحيد فريم التدريب — لا دمج لسموع H1 في
        # nexus:features:history بعد اليوم. المفتاح مخطط تدريب موحّد (M5 حي)
        # تُكمّله nexus-ml تلقائيًا عند كل شمعة M5 عبر _accumulate_training_history.
        # الدمج القديم (24 ميزة H1) كان يفسد المخطط أمام ميزات التشغيل الحي (41).

        await logger.ainfo(
            "backfill.tf_done",
            symbol=symbol,
            tf=tf,
            mode=mode,
            new_bars=inserted,
            last_ts=record["last_ts"],
        )

        await _set_progress(redis_client, symbol, {
            "symbol": symbol,
            "status": "running",
            "started_at": started.isoformat(),
            "timeframes": tfs,
            "done": [tf for tf in tfs if tf in results],
            "current": None,
        })

    finished = datetime.now(timezone.utc)
    progress = {
        "symbol": symbol,
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "timeframes": tfs,
        "results": {k: v for k, v in results.items()},
        "error": None,
    }
    await _set_progress(redis_client, symbol, progress)
    await logger.ainfo("backfill.run_complete", symbol=symbol, timeframes=tfs)
    return progress


class BackfillRunner:
    """Serialises backfill runs per symbol and auto-imports at startup."""

    def __init__(self, redis_client: RedisClient, data_store: Any, mt5_connector: Any):
        self.redis = redis_client
        self.data_store = data_store
        self.mt5 = mt5_connector
        self._tasks: dict[str, asyncio.Task] = {}

    async def auto_import_on_startup(self) -> None:
        """Scheduled at service startup: one incremental run per configured symbol.

        Waits for the MT5 feed task to establish the broker connection first —
        MT5 connects asynchronously in the background, and starting before it
        is ready used to fail every fetch with 'MT5Connector is not connected'.
        """
        if not IMPORT_AUTO:
            await logger.ainfo("backfill.auto_disabled")
            return
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            if self.mt5.connected:
                break
            await asyncio.sleep(3)
        else:
            await logger.awarning(
                "backfill.auto_skipped_no_mt5", timeout_sec=180
            )
            return
        for sym in [s.strip() for s in IMPORT_SYMBOLS.split(",") if s.strip()]:
            try:
                await self.start(sym)
            except Exception:
                await logger.aexception("backfill.auto_start_failed", symbol=sym)

    async def start(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
        start_date: str | None = None,
        max_bars: int | None = None,
    ) -> dict[str, Any]:
        """Kick off a background backfill run for one symbol."""
        symbol = symbol.upper()
        existing = self._tasks.get(symbol)
        if existing and not existing.done():
            return {"status": "already_running", "symbol": symbol}

        task = asyncio.create_task(
            run_backfill(
                self.redis,
                self.data_store,
                self.mt5,
                symbol,
                timeframes=timeframes,
                start_date=start_date,
                max_bars=max_bars,
            )
        )
        self._tasks[symbol] = task
        return {"status": "started", "symbol": symbol, "timeframes": timeframes or []}

    async def progress(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        prog = await _get_progress(self.redis, symbol) or {}
        # T15.6: دمج إحصاء القاعدة الفعلية (صفوف/آخر شمعة لكل فريم)
        db_stats = await db_ohlcv_stats(self.data_store, symbol)
        status = await registry_status(self.redis, symbol, db_stats=db_stats)
        prog["db_timeframes"] = db_stats
        prog["db_total_rows"] = sum(v["rows"] for v in db_stats.values())
        prog["registry"] = status["timeframes"]
        task = self._tasks.get(symbol)
        prog["task_running"] = bool(task and not task.done())
        return prog

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        await shutdown_checkpoint(self.redis)
