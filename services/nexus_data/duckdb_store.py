"""
DuckDB persistent storage for ticks and OHLCV bars.

All heavy I/O is offloaded to a thread-pool so the event loop stays free.
Includes automatic data-retention pruning.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from shared.models import Tick, OHLCV

log = structlog.get_logger("nexus_data.duckdb")

try:
    import duckdb  # type: ignore[import-untyped]
except ImportError:
    duckdb = None  # type: ignore[assignment]

_POOL = ThreadPoolExecutor(max_workers=2)

# Retention: keep ticks for 7 days, OHLCV for 365 days
TICK_RETENTION_DAYS = 7
OHLCV_RETENTION_DAYS = 365


class DataStore:
    """Async-friendly DuckDB wrapper for market-data persistence."""

    def __init__(self, db_path: str = "./data/nexus.duckdb") -> None:
        self._db_path = db_path
        self._conn: Any | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _recover_stale_wal(self, db_path: str) -> None:
        """Checkpoint or remove a large WAL left by an unclean shutdown (Windows)."""
        wal_path = Path(f"{db_path}.wal")
        if not wal_path.exists():
            return
        wal_mb = wal_path.stat().st_size / (1024 * 1024)
        if wal_mb < 5:
            return

        await log.awarning("duckdb.large_wal_detected", path=str(wal_path), size_mb=round(wal_mb, 1))

        def _checkpoint() -> bool:
            try:
                conn = duckdb.connect(db_path)
                try:
                    conn.execute("CHECKPOINT")
                finally:
                    conn.close()
                return True
            except Exception:
                return False

        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(_POOL, _checkpoint)
        if ok:
            await log.ainfo("duckdb.wal_checkpoint_ok", path=db_path)
            return

        if os.getenv("NEXUS_DUCKDB_WAL_RECOVERY", "1").lower() in ("1", "true", "yes"):
            try:
                wal_path.unlink(missing_ok=True)
                await log.awarning("duckdb.wal_removed_for_recovery", path=str(wal_path))
            except OSError as exc:
                await log.awarning("duckdb.wal_remove_failed", error=str(exc))

    async def init_tables(self, *, timeout_s: float = 60.0) -> None:
        if duckdb is None:
            raise RuntimeError("duckdb package is not installed")

        db_path = str(Path(self._db_path).resolve())
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path

        await self._recover_stale_wal(db_path)

        loop = asyncio.get_running_loop()
        # T15.6b: إعادة محاولة مريحة — إعادة الإقلاع خارج الترتيب (api قبل data)
        # كانت تقتل nexus-data نهائياً؛ الآن ينتظر تحرر القفل حتى 3 دقائق
        last_exc: Exception | None = None
        for attempt in range(1, 13):
            try:
                self._conn = await asyncio.wait_for(
                    loop.run_in_executor(_POOL, lambda: duckdb.connect(db_path)),
                    timeout=timeout_s,
                )
                await asyncio.wait_for(
                    loop.run_in_executor(_POOL, self._create_schemas),
                    timeout=timeout_s,
                )
                break
            except (TimeoutError, Exception) as exc:  # noqa: B014 — نلتقط كليهما عمداً
                last_exc = exc
                if attempt < 12:
                    await log.awarning(
                        "duckdb.connect_retry",
                        attempt=attempt,
                        error=str(exc)[:200],
                    )
                    await asyncio.sleep(5)
                else:
                    raise RuntimeError(
                        f"DuckDB open failed after {attempt} attempts ({db_path}): {exc}"
                    ) from exc
        if self._conn is None and last_exc is not None:
            raise RuntimeError(f"DuckDB open failed ({db_path})") from last_exc

        await log.ainfo("duckdb.tables_ready", path=db_path)

    def _create_schemas(self) -> None:
        assert self._conn is not None
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                symbol     VARCHAR   NOT NULL,
                bid        DOUBLE    NOT NULL,
                ask        DOUBLE    NOT NULL,
                spread     DOUBLE    NOT NULL DEFAULT 0,
                timestamp  TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                symbol     VARCHAR   NOT NULL,
                timeframe  VARCHAR   NOT NULL,
                open       DOUBLE    NOT NULL,
                high       DOUBLE    NOT NULL,
                low        DOUBLE    NOT NULL,
                close      DOUBLE    NOT NULL,
                volume     DOUBLE    NOT NULL DEFAULT 0,
                timestamp  TIMESTAMP WITH TIME ZONE NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          VARCHAR PRIMARY KEY,
                symbol      VARCHAR   NOT NULL,
                side        VARCHAR   NOT NULL,
                entry_price DOUBLE    NOT NULL,
                exit_price  DOUBLE    NOT NULL,
                pnl_pips    DOUBLE    NOT NULL DEFAULT 0,
                pnl_usd     DOUBLE    NOT NULL DEFAULT 0,
                rr_ratio    DOUBLE    NOT NULL DEFAULT 0,
                duration_s  DOUBLE    NOT NULL DEFAULT 0,
                session     VARCHAR,
                conviction  DOUBLE    NOT NULL DEFAULT 0,
                opened_at   TIMESTAMP NOT NULL,
                closed_at   TIMESTAMP NOT NULL
            )
        """)
        # Migration for DBs created before per-strategy attribution existed.
        self._conn.execute(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy VARCHAR DEFAULT ''"
        )
        # D-01/D-02: MT5 deal fields on the single source of truth. ADD COLUMN
        # only — no column is ever dropped, no row is rewritten (rule #1/#8).
        for _col in (
            "volume DOUBLE DEFAULT 0",
            "commission DOUBLE DEFAULT 0",
            "swap DOUBLE DEFAULT 0",
            "deal_ticket BIGINT",
            "position_id BIGINT",
            "magic BIGINT",
            "comment VARCHAR",
        ):
            self._conn.execute(f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {_col}")
        # D-02: magic travels with the deals journal too (reconcile is
        # INSERT OR REPLACE, so re-running it fills the new column).
        self._conn.execute(
            "ALTER TABLE execution_journal_deals ADD COLUMN IF NOT EXISTS magic BIGINT"
        )
        # Execution journal (P3): broker orders/deals reconciled for audit.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_journal_orders (
                order_ticket     BIGINT PRIMARY KEY,
                position_id      BIGINT,
                symbol           VARCHAR NOT NULL,
                side             VARCHAR NOT NULL,
                order_type       INTEGER,
                volume_requested DOUBLE,
                volume_filled    DOUBLE,
                price_requested  DOUBLE,
                state            INTEGER,
                retcode          INTEGER,
                comment          VARCHAR,
                time_setup       TIMESTAMP WITH TIME ZONE,
                time_done        TIMESTAMP WITH TIME ZONE
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_journal_deals (
                deal_ticket    BIGINT PRIMARY KEY,
                order_ticket   BIGINT,
                position_id    BIGINT,
                symbol         VARCHAR NOT NULL,
                side           VARCHAR NOT NULL,
                entry          VARCHAR NOT NULL,
                volume         DOUBLE,
                price          DOUBLE,
                commission     DOUBLE,
                swap           DOUBLE,
                profit         DOUBLE,
                comment        VARCHAR,
                time           TIMESTAMP WITH TIME ZONE
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_journal_slippage (
                deal_ticket    BIGINT PRIMARY KEY,
                position_id    BIGINT,
                symbol         VARCHAR NOT NULL,
                side           VARCHAR NOT NULL,
                intended_price DOUBLE,
                filled_price   DOUBLE,
                slippage       DOUBLE,
                slippage_points DOUBLE,
                time           TIMESTAMP WITH TIME ZONE
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts
            ON ticks (symbol, timestamp DESC)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf_ts
            ON ohlcv (symbol, timeframe, timestamp DESC)
        """)
        # FIX (audit F-04): unique constraint on the natural bar key — broker
        # redeliveries must not create duplicate training rows.
        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ohlcv_bar "
                "ON ohlcv (symbol, timeframe, timestamp)"
            )
        except Exception:
            # Pre-existing duplicates would block index creation; keep the
            # service running (dedup migration handled by backfill).
            pass
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_closed_at
            ON trades (closed_at DESC)
        """)
        # Flush WAL after unclean shutdown so the next open is fast on Windows.
        try:
            self._conn.execute("CHECKPOINT")
        except Exception:
            pass

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Insert ───────────────────────────────────────────────────────────

    async def insert_tick(self, tick: Tick) -> None:
        await self._run(
            "INSERT INTO ticks (symbol, bid, ask, spread, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (tick.symbol, tick.bid, tick.ask, tick.spread, tick.timestamp),
        )

    async def insert_ohlcv(self, bar: OHLCV) -> None:
        # FIX (audit F-04): idempotent insert — the unique index uq_ohlcv_bar
        # makes duplicate broker deliveries a no-op instead of a new row.
        await self._run(
            "INSERT INTO ohlcv "
            "(symbol, timeframe, open, high, low, close, volume, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET "
            "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
            "close = EXCLUDED.close, volume = EXCLUDED.volume",
            (
                bar.symbol, bar.timeframe, bar.open, bar.high,
                bar.low, bar.close, bar.volume, bar.timestamp,
            ),
        )

    # ── Execution journal (P3) ─────────────────────────────────────────────

    async def upsert_journal(self, table: str, rows: list[dict], key: str) -> int:
        """Upsert normalized journal rows (idempotent across repeated reconciles)."""
        if not rows:
            return 0
        await self.init_tables()
        cols = list(rows[0].keys())
        col_list = ", ".join(cols)
        ph = ", ".join("?" for _ in cols)
        n = 0
        for r in rows:
            await self._run(
                f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({ph})",
                tuple(r[c] for c in cols),
            )
            n += 1
        return n

    async def insert_batch(self, items: list[Tick | OHLCV]) -> None:
        if not items:
            return

        loop = asyncio.get_running_loop()

        def _bulk() -> None:
            assert self._conn is not None
            for item in items:
                if isinstance(item, Tick):
                    self._conn.execute(
                        "INSERT INTO ticks (symbol, bid, ask, spread, timestamp) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (item.symbol, item.bid, item.ask, item.spread, item.timestamp),
                    )
                elif isinstance(item, OHLCV):
                    # T15.6: صراع آمن مع فهرس uq_ohlcv_bar (T1) — إعادة استيراد شمعة
                    # موجودة تحدّثها بدل إسقاط كل الدفعة بخطأ مكتوم "duckdb insert failed"
                    self._conn.execute(
                        "INSERT INTO ohlcv "
                        "(symbol, timeframe, open, high, low, close, volume, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE SET "
                        "open = excluded.open, high = excluded.high, "
                        "low = excluded.low, close = excluded.close, volume = excluded.volume",
                        (
                            item.symbol, item.timeframe, item.open, item.high,
                            item.low, item.close, item.volume, item.timestamp,
                        ),
                    )

        await loop.run_in_executor(_POOL, _bulk)

    async def insert_trade(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl_pips: float,
        pnl_usd: float,
        rr_ratio: float,
        duration_s: float,
        session: str,
        conviction: float,
        opened_at: datetime,
        closed_at: datetime,
        strategy: str = "",
        *,
        volume: float = 0.0,
        commission: float = 0.0,
        swap: float = 0.0,
        deal_ticket: int | None = None,
        position_id: int | None = None,
        magic: int | None = None,
        comment: str = "",
    ) -> None:
        cols = [
            "id", "symbol", "side", "entry_price", "exit_price", "pnl_pips",
            "pnl_usd", "rr_ratio", "duration_s", "session", "conviction",
            "opened_at", "closed_at", "strategy",
            "volume", "commission", "swap", "deal_ticket", "position_id",
            "magic", "comment",
        ]
        vals = [
            trade_id, symbol, side, entry_price, exit_price,
            pnl_pips, pnl_usd, rr_ratio, duration_s,
            session, conviction, opened_at, closed_at, strategy,
            volume, commission, swap, deal_ticket, position_id, magic, comment,
        ]
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        await self._run(
            f"INSERT INTO trades ({col_list}) VALUES ({placeholders}) "
            "ON CONFLICT (id) DO NOTHING",
            tuple(vals),
        )

    # ── Query ────────────────────────────────────────────────────────────

    async def query_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[OHLCV]:
        conditions = ["symbol = ?", "timeframe = ?"]
        params: list[Any] = [symbol, timeframe]

        if start:
            conditions.append("timestamp >= ?")
            params.append(start)
        if end:
            conditions.append("timestamp <= ?")
            params.append(end)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT symbol, timeframe, open, high, low, close, volume, timestamp "
            f"FROM ohlcv WHERE {where} "
            f"ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)

        rows = await self._fetch(sql, tuple(params))
        return [
            OHLCV(
                symbol=r[0], timeframe=r[1],
                open=r[2], high=r[3], low=r[4], close=r[5],
                volume=r[6], timestamp=r[7],
            )
            for r in rows
        ]

    async def query_trades(
        self,
        symbol: str | None = None,
        session: str | None = None,
        since: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol.upper())
        if session:
            conditions.append("session = ?")
            params.append(session)
        if since:
            conditions.append("closed_at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_rows = await self._fetch(
            f"SELECT COUNT(*) FROM trades {where}", tuple(params)
        )
        total = count_rows[0][0] if count_rows else 0

        offset = (page - 1) * page_size
        rows = await self._fetch(
            f"SELECT id, symbol, side, entry_price, exit_price, pnl_pips, pnl_usd, "
            f"rr_ratio, duration_s, session, conviction, opened_at, closed_at, strategy, "
            f"volume, commission, swap, deal_ticket, position_id, magic, comment "
            f"FROM trades {where} ORDER BY closed_at DESC LIMIT ? OFFSET ?",
            tuple([*params, page_size, offset]),
        )
        cols = [
            "id", "symbol", "side", "entry_price", "exit_price", "pnl_pips",
            "pnl_usd", "rr_ratio", "duration_s", "session", "conviction",
            "opened_at", "closed_at", "strategy",
            "volume", "commission", "swap", "deal_ticket", "position_id",
            "magic", "comment",
        ]
        trades = []
        for row in rows:
            d = dict(zip(cols, row))
            for k in ("opened_at", "closed_at"):
                if d[k] is not None:
                    d[k] = d[k].isoformat() if hasattr(d[k], "isoformat") else str(d[k])
            trades.append(d)
        return {"trades": trades, "total": total, "page": page, "page_size": page_size}

    async def backfill_trades_from_journal(self) -> dict[str, Any]:
        """D-01: close the Journal↔History gap. Build closed trades from
        execution_journal_deals (MT5 deal history = truth) and INSERT any
        position missing from ``trades``. INSERT-only: existing rows are
        never modified or deleted (rule #1). Idempotent via trade id.
        """
        from .execution_journal import positions_to_trades

        deal_cols = [
            "deal_ticket", "position_id", "symbol", "side", "entry",
            "volume", "price", "commission", "swap", "profit", "comment", "time",
        ]
        rows = await self._fetch(
            "SELECT deal_ticket, position_id, symbol, side, entry, "
            "volume, price, commission, swap, profit, comment, time "
            "FROM execution_journal_deals WHERE position_id IS NOT NULL "
            "AND position_id > 0 ORDER BY time",
            (),
        )
        deals = [dict(zip(deal_cols, r)) for r in rows]
        built = positions_to_trades(deals)

        existing_rows = await self._fetch("SELECT id FROM trades", ())
        existing = {str(r[0]) for r in existing_rows}
        added = 0
        skipped = 0
        for t in built:
            if t["id"] in existing:
                skipped += 1
                continue
            await self.insert_trade(
                trade_id=t["id"],
                symbol=t["symbol"],
                side=t["side"],
                entry_price=t["entry_price"],
                exit_price=t["exit_price"],
                pnl_pips=t["pnl_pips"],
                pnl_usd=t["pnl_usd"],
                rr_ratio=0.0,
                duration_s=t["duration_s"],
                session=t["session"],
                conviction=0.0,
                opened_at=t["opened_at"],
                closed_at=t["closed_at"],
                strategy=t["strategy"],
                volume=t["volume"],
                commission=t["commission"],
                swap=t["swap"],
                deal_ticket=t["deal_ticket"],
                position_id=t["position_id"],
                magic=None,
                comment=t["comment"],
            )
            added += 1
        await log.ainfo(
            "duckdb.backfill_from_journal",
            built=len(built), added=added, skipped=skipped,
        )
        return {"built": len(built), "added": added, "skipped": skipped}

    async def backfill_trade_fields_from_journal(self) -> dict[str, Any]:
        """D-02: fill EMPTY MT5 fields (volume/commission/swap/deal_ticket/
        position_id/magic/comment) on existing trades rows from the execution
        journal. UPDATE writes touch ONLY empty fields — any populated value
        (P&L, prices, strategy…) is never rewritten (rule #1). Idempotent.
        Run /api/data/journal/{symbol}?days=N first so the journal is fresh
        (it now carries the magic column).
        """
        from .execution_journal import fill_missing_fields, positions_to_trades

        deal_cols = [
            "deal_ticket", "position_id", "symbol", "side", "entry",
            "volume", "price", "commission", "swap", "profit", "comment", "time", "magic",
        ]
        rows = await self._fetch(
            "SELECT deal_ticket, position_id, symbol, side, entry, "
            "volume, price, commission, swap, profit, comment, time, magic "
            "FROM execution_journal_deals WHERE position_id IS NOT NULL "
            "AND position_id > 0 ORDER BY time",
            (),
        )
        deals = [dict(zip(deal_cols, r)) for r in rows]
        built = {t["position_id"]: t for t in positions_to_trades(deals)}

        trade_cols = ["id", "volume", "commission", "swap", "deal_ticket",
                      "position_id", "magic", "comment"]
        existing_rows = await self._fetch(
            "SELECT id, volume, commission, swap, deal_ticket, "
            "position_id, magic, comment FROM trades",
            (),
        )
        updated = scanned = 0
        for r in existing_rows:
            e = dict(zip(trade_cols, r))
            scanned += 1
            pid = e["position_id"] or (
                int(str(e["id"])) if str(e["id"]).isdigit() else None
            )
            m = built.get(pid)
            fill = fill_missing_fields(e, m)
            if not fill:
                continue
            set_clause = ", ".join(f"{k} = ?" for k in fill)
            await self._run(
                f"UPDATE trades SET {set_clause} WHERE id = ?",
                (*fill.values(), e["id"]),
            )
            updated += 1
            await log.ainfo(
                "duckdb.d02_fields_filled",
                trade_id=e["id"], filled=sorted(fill.keys()),
            )
        return {
            "scanned": scanned,
            "updated": updated,
            "journal_positions": len(built),
        }

    async def get_reconcile_snapshot(self) -> dict[str, Any]:
        """D-03 inputs: Σ(pnl_usd) + per-position maps (stored vs MT5 journal)."""
        from .execution_journal import positions_to_trades

        sum_rows = await self._fetch(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades", (),
        )
        stored_sum = float(sum_rows[0][0] or 0) if sum_rows else 0.0

        trows = await self._fetch(
            "SELECT id, position_id, pnl_usd FROM trades", (),
        )
        stored_map: dict[int, float] = {}
        for tid, pid, pnl in trows:
            key = int(pid) if pid else (
                int(str(tid)) if str(tid).isdigit() else None
            )
            if key:
                stored_map[key] = float(pnl or 0)

        deal_cols = [
            "deal_ticket", "position_id", "symbol", "side", "entry",
            "volume", "price", "commission", "swap", "profit", "comment", "time", "magic",
        ]
        drows = await self._fetch(
            "SELECT deal_ticket, position_id, symbol, side, entry, "
            "volume, price, commission, swap, profit, comment, time, magic "
            "FROM execution_journal_deals WHERE position_id IS NOT NULL "
            "AND position_id > 0 ORDER BY time",
            (),
        )
        deals = [dict(zip(deal_cols, r)) for r in drows]
        mt5_map = {
            t["position_id"]: t["pnl_usd"] for t in positions_to_trades(deals)
        }
        return {
            "stored_sum": stored_sum,
            "stored_map": stored_map,
            "mt5_map": mt5_map,
        }

    async def query_latest_ticks(
        self, symbol: str, count: int = 50,
    ) -> list[Tick]:
        rows = await self._fetch(
            "SELECT symbol, bid, ask, spread, timestamp "
            "FROM ticks WHERE symbol = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (symbol, count),
        )
        return [
            Tick(symbol=r[0], bid=r[1], ask=r[2], spread=r[3], timestamp=r[4])
            for r in rows
        ]

    async def get_performance_metrics(self) -> dict[str, Any]:
        from shared.metrics import max_drawdown_pct, per_trade_equity_curve

        rows = await self._fetch(
            """
            SELECT
                COALESCE(SUM(pnl_usd), 0)                        AS total_pnl,
                COUNT(*)                                          AS total_trades,
                COALESCE(AVG(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0) AS avg_win,
                COALESCE(AVG(CASE WHEN pnl_usd <= 0 THEN pnl_usd END), 0) AS avg_loss,
                CASE WHEN COUNT(*) > 0
                     THEN ROUND(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2)
                     ELSE 0 END AS win_rate,
                CASE WHEN ABS(COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN pnl_usd END), 1)) > 0
                     THEN ROUND(COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd END), 0)
                          / ABS(COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN pnl_usd END), 1)), 2)
                     ELSE 0 END AS profit_factor
            FROM trades
            """
        )
        if not rows or not rows[0]:
            return {}
        row = rows[0]
        cols = ["total_pnl", "total_trades", "avg_win", "avg_loss", "win_rate", "profit_factor"]
        out = dict(zip(cols, row))
        # D-05: real per-trade drawdown (was permanently 0.0% — never computed).
        try:
            initial = float(os.getenv("NEXUS_INITIAL_BALANCE", "100000.0"))
            pnl_rows = await self._fetch(
                "SELECT pnl_usd FROM trades ORDER BY closed_at ASC", (),
            )
            pnls = [float(r[0] or 0) for r in pnl_rows]
            curve = per_trade_equity_curve(initial, pnls)
            out["max_drawdown"] = -max_drawdown_pct(curve) / 100.0   # negative fraction
            out["max_drawdown_pct"] = -max_drawdown_pct(curve)
        except Exception:
            await log.aexception("duckdb.max_drawdown_failed")
        return out

    async def get_pnl_curve(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._fetch(
            """
            WITH recent AS (
                SELECT closed_at, pnl_usd
                FROM trades
                ORDER BY closed_at DESC
                LIMIT ?
            )
            SELECT
                closed_at,
                pnl_usd,
                SUM(pnl_usd) OVER (
                    ORDER BY closed_at ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_pnl
            FROM recent
            ORDER BY closed_at ASC
            """,
            (limit,),
        )
        return [
            {
                "closed_at": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "pnl_usd": r[1],
                "cumulative_pnl": r[2],
            }
            for r in rows
        ]

    async def get_daily_pnl(self, since: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE closed_at >= ?" if since else ""
        params = (since,) if since else ()
        rows = await self._fetch(
            f"""
            SELECT
                CAST(closed_at AS DATE) AS date,
                COALESCE(SUM(pnl_usd), 0) AS pnl,
                COUNT(*) AS trades,
                COALESCE(ROUND(
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
                    * 100.0 / NULLIF(COUNT(*), 0), 2), 0) AS win_rate
            FROM trades {where}
            GROUP BY CAST(closed_at AS DATE)
            ORDER BY date DESC
            """,
            params,
        )
        return [
            {"date": str(r[0]), "pnl": r[1], "trades": r[2], "win_rate": r[3]}
            for r in rows
        ]

    async def get_grouped_performance(self, group_col: str) -> list[dict[str, Any]]:
        safe_col = group_col if group_col in ("session", "symbol") else "session"
        rows = await self._fetch(
            f"""
            SELECT
                {safe_col} AS grp,
                COALESCE(SUM(pnl_usd), 0) AS total_pnl,
                COUNT(*) AS trades,
                COALESCE(ROUND(
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
                    * 100.0 / NULLIF(COUNT(*), 0), 2), 0) AS win_rate,
                COALESCE(AVG(rr_ratio), 0) AS avg_rr
            FROM trades
            GROUP BY {safe_col}
            ORDER BY total_pnl DESC
            """,
        )
        return [
            {"group": r[0], "total_pnl": r[1], "trades": r[2],
             "win_rate": r[3], "avg_rr": r[4]}
            for r in rows
        ]

    # ── Retention ────────────────────────────────────────────────────────

    async def prune_old_data(self) -> None:
        tick_cutoff = datetime.now(timezone.utc) - timedelta(days=TICK_RETENTION_DAYS)
        ohlcv_cutoff = datetime.now(timezone.utc) - timedelta(days=OHLCV_RETENTION_DAYS)

        await self._run(
            "DELETE FROM ticks WHERE timestamp < ?", (tick_cutoff,),
        )
        await self._run(
            "DELETE FROM ohlcv WHERE timestamp < ?", (ohlcv_cutoff,),
        )
        await log.ainfo(
            "duckdb.pruned",
            tick_cutoff=str(tick_cutoff),
            ohlcv_cutoff=str(ohlcv_cutoff),
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _run(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        loop = asyncio.get_running_loop()

        def _exec() -> None:
            assert self._conn is not None
            self._conn.execute(sql, params)

        await loop.run_in_executor(_POOL, _exec)

    async def _fetch(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        loop = asyncio.get_running_loop()

        def _query() -> list[tuple[Any, ...]]:
            assert self._conn is not None
            return self._conn.execute(sql, params).fetchall()

        return await loop.run_in_executor(_POOL, _query)
