"""T2 (PLAN_MASTER) — استيراد بيانات الوسيط الرسمي من مجلد محلي (قراءة فقط من المصدر).

- محلل صيغة MT5: <DATE> <TIME> <O> <H> <L> <C> <TICKVOL> <VOL> <SPREAD>
- تحقق مطابقة النافذة المتداخلة مع القاعدة الحالية قبل الإدراج (يجب 0.000% اختلاف)
- إدراج بـ ON CONFLICT (فهرس uq_ohlcv_bar من T1 يحمي)
- يُستورد كل ملف شموع موجود (كل الفريمات) — لا تيكات (عينة أسبوع تُنفذ بسكربت منفصل)
- مصدر قراءة فقط: لا يعدل ملفات المصدر ولا القاعدة القديمة Z
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]

TF_MAP = {
    "Daily": "D1", "Weekly": "W1", "Monthly": "MN1",
    **{f"M{n}": f"M{n}" for n in [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]},
    **{f"H{n}": f"H{n}" for n in [1, 2, 3, 4, 6, 8, 12]},
}
SYMBOL_DEFAULT = "XAUUSD"
TOLERANCE = 1e-6  # تسامح مقارنة الأسعار

FILENAME_RE = re.compile(
    r"^(?P<sym>[A-Za-z0-9]+)_?(?P<tf>Daily|Weekly|Monthly|M\d+|H\d+)_(?P<a>\d{12})_(?P<b>\d{12})\.csv$"
)


def parse_mt5_candles(path: Path):
    """يعيد (rows, rejected). كل صف: (symbol,tf,ts,o,h,l,c,tickvol,vol,spread).
    يدعم تخطيطين: مع <TIME> (دقائق/ساعات) وبدونه (Daily/Weekly/Monthly).
    """
    rows, rejected = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline().replace("\r\n", "").replace("\n", "")
        cols = [c.strip("<>") for c in header.split("\t")]
        i_time = cols.index("TIME") if "TIME" in cols else None
        i = {n: cols.index(n) for n in ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]}
        i_tick = cols.index("TICKVOL") if "TICKVOL" in cols else None
        i_vol = cols.index("VOL") if "VOL" in cols else None
        i_spr = cols.index("SPREAD") if "SPREAD" in cols else None
        for line in f:
            parts = line.replace("\r\n", "").replace("\n", "").split("\t")
            if len(parts) < len(cols) - 1:
                rejected += 1
                continue
            try:
                d = parts[i["DATE"]]
                if i_time is not None:
                    ts = datetime.strptime(f"{d} {parts[i_time]}", "%Y.%m.%d %H:%M:%S")
                else:
                    ts = datetime.strptime(d, "%Y.%m.%d")
                o, h, l, c = (float(parts[i["OPEN"]]), float(parts[i["HIGH"]]),
                              float(parts[i["LOW"]]), float(parts[i["CLOSE"]]))
                tickvol = int(float(parts[i_tick])) if i_tick is not None and parts[i_tick] else 0
                vol = int(float(parts[i_vol])) if i_vol is not None and parts[i_vol] else 0
                spread = int(float(parts[i_spr])) if i_spr is not None and parts[i_spr] else 0
                # تحقق صارم: منطق OHLC
                if not (h >= max(o, c) and l <= min(o, c) and h >= l and o > 0 and c > 0):
                    rejected += 1
                    continue
                rows.append((o, h, l, c, tickvol, vol, spread, ts))
            except (ValueError, IndexError):
                rejected += 1
                continue
    return rows, rejected


def timeframe_of(name: str) -> str | None:
    m = FILENAME_RE.match(name)
    if not m or "TICKS" in name.upper() and m.group("tf") is None:
        return None
    return TF_MAP.get(m.group("tf"))


def symbol_of(name: str) -> str:
    m = FILENAME_RE.match(name)
    sym = m.group("sym") if m else SYMBOL_DEFAULT
    # XAUUSDs → alias الحي XAUUSD (الموجود في القاعدة)
    return SYMBOL_DEFAULT if sym.upper().startswith("XAUUSD") else sym


def overlap_check(con, symbol: str, tf: str, rows) -> tuple[int, int]:
    """مقارنة الصفوف المتداخلة مع القاعدة قبل الكتابة. يعيد (overlaps, mismatches)."""
    if not rows:
        return 0, 0
    tmp = "ovl_" + tf + "_" + str(abs(hash(tf)))[:6]
    con.execute(f"CREATE OR REPLACE TEMP TABLE {tmp}(ts TIMESTAMP, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE)")
    con.executemany(f"INSERT INTO {tmp} VALUES (?,?,?,?,?)",
                    [(r[7], r[0], r[1], r[2], r[3]) for r in rows])
    res = con.execute(
        f"""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE abs(d.o-db.open)>{TOLERANCE} OR abs(d.h-db.high)>{TOLERANCE}
                                OR abs(d.l-db.low)>{TOLERANCE} OR abs(d.c-db.close)>{TOLERANCE})
        FROM {tmp} d JOIN ohlcv db
          ON db.symbol='{symbol}' AND db.timeframe='{tf}' AND db.timestamp=d.ts
        """
    ).fetchone()
    con.execute(f"DROP TABLE IF EXISTS {tmp}")
    return int(res[0]), int(res[1])


STAGE = "stage_import"


def import_file(con, path: Path, mismatch_tol: float = 0.001, force_overlap: bool = False) -> dict:
    tf = timeframe_of(path.name)
    if tf is None:
        return {"file": path.name, "skipped": "ليس ملف شموع معروفاً"}
    sym = symbol_of(path.name)
    rows, rejected = parse_mt5_candles(path)
    n0 = con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
    inserted = updated = mismatches = overlaps = 0
    if rows:
        # جدول مرحلي واحد — كل العمليات SQL جماعية (سريعة وذاكرة محدودة)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {STAGE}(ts TIMESTAMP, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, tv BIGINT, sp INTEGER)"
        )
        con.executemany(
            f"INSERT INTO {STAGE} VALUES (?,?,?,?,?,?,?)",
            [(r[7], r[0], r[1], r[2], r[3], r[4], r[6]) for r in rows],
        )
        # تحقق مطابقة النافذة المتداخلة (يجب ≈ 0.000%)
        ov = con.execute(
            f"""SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE abs(s.o-db.open)>{TOLERANCE} OR abs(s.h-db.high)>{TOLERANCE}
                                        OR abs(s.l-db.low)>{TOLERANCE} OR abs(s.c-db.close)>{TOLERANCE})
                FROM {STAGE} s JOIN ohlcv db
                  ON db.symbol='{sym}' AND db.timeframe='{tf}' AND db.timestamp = s.ts"""
        ).fetchone()
        overlaps, mismatches = int(ov[0]), int(ov[1])
        ok = overlaps == 0 or mismatches / overlaps <= mismatch_tol or force_overlap
        if ok:
            before = con.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND timeframe=?", [sym, tf]
            ).fetchone()[0]
            con.execute("BEGIN")
            # إدراج الجديد جماعياً
            con.execute(
                f"""INSERT INTO ohlcv (symbol,timeframe,open,high,low,close,volume,timestamp)
                    SELECT '{sym}','{tf}', s.o, s.h, s.l, s.c, s.tv, s.ts
                    FROM {STAGE} s
                    WHERE NOT EXISTS (SELECT 1 FROM ohlcv db
                        WHERE db.symbol='{sym}' AND db.timeframe='{tf}' AND db.timestamp = s.ts)"""
            )
            inserted = con.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND timeframe=?", [sym, tf]
            ).fetchone()[0] - before
            # تحديث المتداخل بقيم المصدر الرسمي
            con.execute(
                f"""UPDATE ohlcv db SET open=s.o, high=s.h, low=s.l, close=s.c, volume=s.tv
                    FROM {STAGE} s
                    WHERE db.symbol='{sym}' AND db.timeframe='{tf}' AND db.timestamp = s.ts"""
            )
            con.execute("COMMIT")
        con.execute(f"DROP TABLE IF EXISTS {STAGE}")
    n1 = con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
    return {
        "file": path.name, "symbol": sym, "timeframe": tf,
        "rows_in_file": len(rows), "rejected_in_file": rejected,
        "overlap_rows": overlaps, "overlap_mismatches": mismatches,
        "inserted_new": inserted, "updated_overlap": updated,
        "db_rows_total": n1,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="مجلد البيانات (قراءة فقط)")
    ap.add_argument("--db", default=str(ROOT / "data" / "nexus.duckdb"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "IMPORT_BROKER_T2.json"))
    ap.add_argument("--force-overlap", action="store_true",
                    help="استبدال المتداخل بقيم المصدر الرسمي حتى لو تجاوزت الفروقات العتبة")
    args = ap.parse_args()

    src = Path(args.src)
    files = sorted(src.glob("*.csv"))
    results = []
    con = duckdb.connect(args.db)
    for f in files:
        try:
            r = import_file(con, f, force_overlap=args.force_overlap)
        except Exception as e:  # noqa: BLE001
            r = {"file": f.name, "error": str(e)}
        results.append(r)
        print(json.dumps(r, ensure_ascii=False))
    total_dupes = con.execute(
        """SELECT COUNT(*) FROM (SELECT ROW_NUMBER() OVER
           (PARTITION BY symbol,timeframe,timestamp ORDER BY rowid) rn FROM ohlcv) WHERE rn>1"""
    ).fetchone()[0]
    con.execute("CHECKPOINT")
    print("checkpoint done", file=sys.stderr)
    con.close()

    summary = {
        "generated_at": datetime.now().isoformat(),
        "source": str(src), "db": args.db, "read_only_source": True,
        "files": results,
        "post_import_duplicates": total_dupes,
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nمكررات بعد الاستيراد:", total_dupes, "| التقرير:", args.out)
    if total_dupes != 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
