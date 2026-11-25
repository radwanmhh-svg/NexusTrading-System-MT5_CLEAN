"""
One-time export: copy the training_dataset table out of the hot DuckDB
(data/nexus.duckdb) into a dedicated training DB (data/training.duckdb).

Why (2026-11-22): nexus-data holds a persistent DuckDB writer lock on
nexus.duckdb (tick streaming) — on Windows no second process can open the
file, so nexus-ml's training could never read training_dataset while the
system was running. The training dataset is STATIC (only the build script
writes it, during downtime), so it belongs in its own file that nothing
else ever opens. nexus-ml's loader prefers data/training.duckdb when it
exists.

Run ONLY while nexus-data is stopped (it is the writer):
    .venv\\Scripts\\python.exe scripts\\export_training_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "nexus.duckdb"
DST = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "training.duckdb"


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"source DB missing: {SRC}")
    print(f"source:      {SRC}")
    print(f"destination: {DST}")

    con = duckdb.connect(str(DST))
    try:
        src = str(SRC).replace("'", "''")
        con.execute(f"ATTACH '{src}' AS src_db (READ_ONLY)")
        tables = {r[0] for r in con.execute("SHOW TABLES FROM src_db").fetchall()}
        if "training_dataset" not in tables:
            raise SystemExit("training_dataset table not found in source DB")

        con.execute(
            "CREATE TABLE IF NOT EXISTS training_dataset "
            "AS SELECT * FROM src_db.training_dataset LIMIT 0"
        )
        con.execute("DELETE FROM training_dataset")
        con.execute(
            "INSERT INTO training_dataset SELECT * FROM src_db.training_dataset"
        )
        n = con.execute("SELECT COUNT(*) FROM training_dataset").fetchone()[0]
        datasets = con.execute(
            "SELECT dataset_id, feature_schema_hash, COUNT(*) AS rows "
            "FROM training_dataset GROUP BY dataset_id, feature_schema_hash "
            "ORDER BY MAX(created_at) DESC"
        ).fetchall()
        con.execute("CHECKPOINT")
        print(f"exported rows: {n}")
        for ds_id, fhash, rows in datasets:
            print(f"  dataset {ds_id}  schema={fhash}  rows={rows}")
        print("OK")
    finally:
        try:
            con.execute("DETACH src_db")
        except Exception:
            pass
        con.close()


if __name__ == "__main__":
    main()
