"""T1 (PLAN_MASTER) — اختبار فهرس uq_ohlcv_bar الفريد.

التوقع الحرفي: "مكررات = 0 + الفهرس موجود + اختبار pytest يفشل عند إدراج مكرر".
"""
from __future__ import annotations

import duckdb
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = ROOT / "data" / "nexus.duckdb"
TRAIN_DB = ROOT / "data" / "train_copy.duckdb"


def _dup_key_violation(con: duckdb.DuckDBPyConnection) -> bool:
    """يحاول إدراج أول صف مرة ثانية — يجب أن يُرفض بفشل قيد فريد."""
    cols = [c[0] for c in con.execute("DESCRIBE ohlcv").fetchall()]
    row = con.execute(f'SELECT {",".join(cols)} FROM ohlcv LIMIT 1').fetchone()
    ph = ",".join("?" * len(cols))
    with pytest.raises((duckdb.ConstraintException, duckdb.InvalidInputException)):
        con.execute(f'INSERT INTO ohlcv ({",".join(cols)}) VALUES ({ph})', row)
    return True


@pytest.mark.parametrize("db_path", [PROD_DB, TRAIN_DB])
def test_no_duplicates_in_production_databases(db_path: Path):
    """مكررات (symbol,timeframe,timestamp) = 0 في القاعدتين."""
    con = duckdb.connect(str(db_path), read_only=True)
    dupes = con.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT ROW_NUMBER() OVER (PARTITION BY symbol,timeframe,timestamp ORDER BY rowid) rn
          FROM ohlcv) WHERE rn > 1
        """
    ).fetchone()[0]
    assert dupes == 0, f"لا تزال هناك {dupes} مكرراً في {db_path}"


@pytest.mark.parametrize("db_path", [PROD_DB, TRAIN_DB])
def test_unique_index_exists(db_path: Path):
    """الفهرس الفريد uq_ohlcv_bar موجود فعلياً على ohlcv."""
    con = duckdb.connect(str(db_path), read_only=True)
    idxs = {r[0] for r in con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name='ohlcv'"
    ).fetchall()}
    assert "uq_ohlcv_bar" in idxs, f"الفهرس الفريد غير موجود: {idxs}"


@pytest.mark.parametrize("db_path", [PROD_DB, TRAIN_DB])
def test_duplicate_insert_rejected(db_path: Path):
    """إدراج صف بنفس (symbol,timeframe,timestamp) الموجود يجب أن يفشل."""
    con = duckdb.connect(str(db_path))
    try:
        assert _dup_key_violation(con)
        # لا صفوف زائدة بعد المحاولة
        assert con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == con.execute(
            "SELECT COUNT(*) FROM ohlcv"
        ).fetchone()[0]
    finally:
        con.close()
