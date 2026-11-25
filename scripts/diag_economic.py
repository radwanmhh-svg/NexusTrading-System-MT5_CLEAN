# -*- coding: utf-8 -*-
"""تشخيص مزامنة الأخبار: يقرأ CSV الـEA مباشرة ويحلل محتواه + يقارن مع الجدول عبر API."""
import csv, glob, json, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]

# 1) إيجاد ملف الـEA في مجلدات MT5 (عادي / Common / Portable)
patterns = [
    os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\*\MQL5\Files\nexus_economic_events.csv"),
    os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\Common\Files\nexus_economic_events.csv"),
    os.path.expandvars(r"%APPDATA%\MetaQuotes\Tester\*\MQL5\Files\nexus_economic_events.csv"),
    r"C:\Program Files\*\MQL5\Files\nexus_economic_events.csv",
    r"C:\Program Files (x86)\*\MQL5\Files\nexus_economic_events.csv",
    r"D:\*\MQL5\Files\nexus_economic_events.csv",
    r"D:\*\*\MQL5\Files\nexus_economic_events.csv",
]
files = []
for pat in patterns:
    files += glob.glob(pat)
if not files:
    # بحث موجه محدود العمق على محركات C و D عن مجلد MQL5\Files
    seen_dirs = set()
    for root_drive in ("C:/", "D:/"):
        base_depth = root_drive.count("/")
        for dirpath, dirnames, _ in os.walk(root_drive):
            depth = dirpath.count("/") - base_depth
            if depth > 6:
                dirnames[:] = []
                continue
            name = os.path.basename(dirpath).lower()
            if name == "files" and dirpath.lower().replace(chr(92), "/").endswith("mql5/files"):
                cand = os.path.join(dirpath, "nexus_economic_events.csv")
                if os.path.exists(cand):
                    files.append(cand)
            if name in ("windows", "program files", "program files (x86)", "$recycle.bin", "node_modules"):
                dirnames[:] = []
    print("fallback search found:", files)
if not files:
    print("ERR: لم أجد CSV. ابحث يدويًا: في MT5 → File → Open Data Folder → MQL5\\Files")
    sys.exit(1)
path = Path(max(files, key=lambda p: os.path.getmtime(p)))
print(f"CSV: {path} | size={path.stat().st_size/1e6:.2f}MB | mtime={datetime.fromtimestamp(path.stat().st_mtime)}")

# فحص ملف حالة الـEA (وجوده = لا استيراد عميق)
state = path.parent / "nexus_calendar_state.txt"
if state.exists():
    print(f"STATE FILE موجود: {state} | محتواه: {state.read_text(encoding='utf-8', errors='replace').strip()}")
    print("⚠ يجب حذفه ثم إعادة إرفاق الـEA لتفعيل الاستيراد العميق 730")
else:
    print("STATE FILE غير موجود ✓ — الاستيراد العميق سيعمل عند تهيئة الـEA")

rows = list(csv.DictReader(path.open(encoding="utf-8-sig", errors="replace")))
print(f"raw rows: {len(rows)}")
if rows:
    print("header:", list(rows[0].keys()))
ids = [r.get("event_id", "").strip() for r in rows if r.get("event_id", "").strip()]
print(f"rows with event_id: {len(ids)} | distinct ids: {len(set(ids))}")

def _ts(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

tss = [_ts(r.get("time_utc")) for r in rows]
tss = [t for t in tss if t]
now = datetime.now(timezone.utc)
if tss:
    print(f"time_utc range: {min(tss)} → {max(tss)}")
    fut = [t for t in tss if t > now]
    print(f"future events: {len(fut)}" + (f" (أولها {min(fut)})" if fut else ""))
imp = {}
for r in rows:
    k = (r.get("importance") or "").strip().upper()
    imp[k] = imp.get(k, 0) + 1
print("importance distribution:", imp)
cur = {}
for r in rows:
    k = (r.get("currency") or "").strip().upper() or "?"
    cur[k] = cur.get(k, 0) + 1
print("currencies:", cur)
fut_rows = [r for r in rows if _ts(r.get("time_utc")) and _ts(r.get("time_utc")) > now][:3]
for r in fut_rows:
    print("future sample:", {k: r.get(k) for k in ("event_id","event_name","currency","importance","time_utc","actual","forecast")})

# 2) الجدول عبر API للمقارنة
try:
    st = json.load(urllib.request.urlopen("http://127.0.0.1:9001/api/data/economic/stats", timeout=15))
    print("DB via API:", st)
except Exception as e:
    print("API ERR:", e)
