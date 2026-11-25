# -*- coding: utf-8 -*-
"""تحقق سريع من قاعدة التدريب: count / o_open / o_close لكل dataset ذهبي."""
import duckdb, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = duckdb.connect("data/training.duckdb", read_only=True)
rows = c.execute("""
    SELECT dataset_id, COUNT(*) AS n, COUNT(o_open) AS n_open, COUNT(o_close) AS n_close,
           MIN(timestamp) AS ts_min, MAX(timestamp) AS ts_max
    FROM training_dataset
    WHERE dataset_id='ds_d6e2b85575af7f37a90bf22e'
    GROUP BY dataset_id
""").fetchall()
for r in rows:
    print("VERIFY:", r)
ok = rows and rows[0][1] == rows[0][2] == rows[0][3] == 99762
print("RESULT:", "PASS" if ok else "FAIL — أعمدة o_* ناقصة")
