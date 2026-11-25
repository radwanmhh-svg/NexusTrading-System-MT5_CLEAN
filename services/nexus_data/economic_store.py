"""Economic events store — P16.5 (T16.5).

يدير جدول economic_events في nexus.duckdb:
- المصدر: data/economic/nexus_economic_events.csv (موديل MQ5 — تقويم الوسيط MT5 نفسه).
- التحميل idempotent بـ event_id (ON CONFLICT DO UPDATE) — إعادة التحميل لا تكرر.
- التحديث دوري (الموديل MQ5 يحدّث الملف كل 5 دقائق).
- الاستخدام لاحقاً (T16.6): ميزات قبل/بعد الحدث مرتبطة بالشموع التاريخية.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb

_LONG_MIN = -9223372036854775808
_IMPORTANCE_MAP = {"HIGH": "high", "MODERATE": "medium", "LOW": "low"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS economic_events (
    event_id     VARCHAR NOT NULL,
    event_name   VARCHAR NOT NULL,
    country      VARCHAR,
    currency     VARCHAR,
    importance   VARCHAR,
    time_broker  TIMESTAMP,
    time_utc     TIMESTAMP NOT NULL,
    server_offset_sec INTEGER,
    actual       DOUBLE,
    forecast     DOUBLE,
    previous     DOUBLE,
    actual_raw   VARCHAR,
    forecast_raw VARCHAR,
    previous_raw VARCHAR,
    change_id    VARCHAR,
    fetched_at_utc TIMESTAMP,
    source       VARCHAR,
    PRIMARY KEY (event_id, time_utc)
)
"""

_DDL_READY = False


def _mt5_common_path() -> Path:
    """المسار الحقيقي الذي يكتبه EA على MT5 (FILE_COMMON — مشترك بين التيرمنالات)."""
    return (Path(os.environ.get("APPDATA", "")) /
            "MetaQuotes" / "Terminal" / "Common" / "Files" / "nexus_economic_events.csv")


def csv_path() -> Path:
    """أحدث مصدر: MT5 Common (إن وُجد وأحدث) وإلا نسخة المشروع.

    ونسخة المشروع تُحدَّث من الأحدث دائماً (تدقيق/إعادة بناء قابلة للإعادة).
    """
    custom = os.getenv("NEXUS_ECONOMIC_CSV")
    if custom:
        return Path(custom)
    project = Path(__file__).resolve().parents[2] / "data" / "economic" / "nexus_economic_events.csv"
    try:
        common = _mt5_common_path()
        if common.exists():
            if not project.exists() or common.stat().st_mtime > project.stat().st_mtime:
                project.parent.mkdir(parents=True, exist_ok=True)
                try:
                    data = common.read_bytes()
                    project.write_bytes(data)
                except OSError:
                    pass
                return common
    except Exception:
        pass
    return project


def _num(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if v == _LONG_MIN:
        return None
    return v


def _ts(raw: str | None):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def ensure_schema(conn) -> None:
    global _DDL_READY
    # FIX (2026-11-22): المفتاح القديم PRIMARY KEY(event_id) يعامل سلسلة
    # الأحداث (EIA أسبوعية = id واحد) كصفٍّ واحد — event_id يعرّف نوع الحدث
    # وليس حدوثه. الهوية الصحيحة (event_id, time_utc). هجرة تلقائية.
    if not _DDL_READY:
        try:
            pk = conn.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                "WHERE table_name='economic_events' AND constraint_type='PRIMARY KEY'"
            ).fetchall()
            if pk and list(pk[0][0] or []) != ["event_id", "time_utc"]:
                conn.execute("DROP TABLE IF EXISTS economic_events")
                print("[economic_store] migrated: dropped old PK(event_id) table — sync will repopulate from CSV")
        except Exception:
            pass
    conn.execute(_SCHEMA)
    _DDL_READY = True


def load_csv_rows(path: Path) -> list[tuple]:
    """قراءة CSV → tuples جاهزة للإدراج (event_id أولاً)."""
    if not path.exists():
        return []
    out: list[tuple] = []
    # EA يكتب أسماء الأحداث بترميز Windows للرموز الخاصة — نحمل بتسامح
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    import io as _io
    with _io.StringIO(text, newline="") as f:
        for row in csv.DictReader(f):
            ts_utc = _ts(row.get("time_utc"))
            if ts_utc is None or not row.get("event_id"):
                continue
            out.append((
                row["event_id"].strip(),
                (row.get("event_name") or "").strip(),
                (row.get("country") or "").strip(),
                (row.get("currency") or "").strip().upper(),
                _IMPORTANCE_MAP.get((row.get("importance") or "").strip().upper(), "low"),
                _ts(row.get("time_broker")),
                ts_utc,
                _num(row.get("server_offset_sec")),
                _num(row.get("actual")),
                _num(row.get("forecast")),
                _num(row.get("previous")),
                row.get("actual_raw") or "",
                row.get("forecast_raw") or "",
                row.get("previous_raw") or "",
                (row.get("change_id") or "").strip(),
                _ts(row.get("fetched_at_utc")),
                (row.get("source") or "mt5").strip(),
            ))
    out.sort(key=lambda r: r[6])
    return out


def upsert_rows(conn, rows: list[tuple]) -> dict:
    """إدراج/تحديث جماعي عبر جدول مرحلي — ON CONFLICT على event_id.

    FIX (2026-11-22): الـCSV تراكمي (نفس event_id يتكرر عشرات المرات) —
    نُزيل التكرار داخل الدفعة (الأحدث fetched_at) قبل الإدراج، ونُرجع
    إحصاءات تشخيصية (new_ids) لأن الـtable كان ثابتًا بلا شرح.
    """
    if not rows:
        return {"upserted": 0, "stage_rows": 0, "new_ids": 0}
    ensure_schema(conn)
    stage = "stage_economic_events"
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage} AS SELECT * FROM economic_events LIMIT 0")
    conn.executemany(
        f"INSERT INTO {stage} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE {stage}_dedup AS
        SELECT * FROM {stage}
        QUALIFY row_number() OVER (
            PARTITION BY event_id, time_utc
            ORDER BY fetched_at_utc DESC NULLS LAST, change_id DESC
        ) = 1
    """)
    new_ids = conn.execute(f"""
        SELECT COUNT(*) FROM {stage}_dedup s
        WHERE NOT EXISTS (
            SELECT 1 FROM economic_events e WHERE e.event_id = s.event_id
        )
    """).fetchone()[0]
    conn.execute(f"""
        INSERT INTO economic_events SELECT * FROM {stage}_dedup
        ON CONFLICT (event_id, time_utc) DO UPDATE SET
            actual = excluded.actual,
            forecast = excluded.forecast,
            previous = excluded.previous,
            change_id = excluded.change_id,
            fetched_at_utc = excluded.fetched_at_utc
    """)
    conn.execute(f"DROP TABLE IF EXISTS {stage}")
    conn.execute(f"DROP TABLE IF EXISTS {stage}_dedup")
    return {"upserted": len(rows), "stage_rows": len(rows), "new_ids": int(new_ids)}


def sync_from_csv(conn, *, path: Path | None = None) -> dict:
    """مزامنة كاملة من ملف MT5 → economic_events. تُستدعى دورياً من nexus-data."""
    path = path or csv_path()
    rows = load_csv_rows(path)
    stats = upsert_rows(conn, rows)
    total = conn.execute("SELECT COUNT(*) FROM economic_events").fetchone()[0]
    last = conn.execute("SELECT MAX(time_utc) FROM economic_events").fetchone()[0]
    return {"csv_rows": len(rows), **stats, "table_rows": int(total),
            "last_event_utc": str(last) if last else None,
            "synced_at": datetime.now(timezone.utc).isoformat()}
