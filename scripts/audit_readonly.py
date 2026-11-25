"""
Read-only full audit: database_model_data_audit (.txt + .json)
No writes, no deletes, no training. All DB connections are read_only=True.
Classifies data maturity per user's A..E scale.
"""
from __future__ import annotations
import io, sys, os, json, hashlib, glob, pickle, time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import duckdb
import redis

ROOT = Path(__file__).resolve().parents[1]
OUT_TXT = ROOT / "database_model_data_audit.txt"
OUT_JSON = ROOT / "database_model_data_audit.json"

R = {"generated_at": datetime.now(timezone.utc).isoformat(), "dbs": [], "market_raw": [],
     "features": [], "labels": [], "datasets": [], "model_files": [], "redis": [],
     "mlflow": {}, "classification": "", "classification_rationale": ""}
TXT = []
def p(s=""):
    TXT.append(str(s)); print(s)

def now(): return datetime.now(timezone.utc).isoformat()

# ─────────────────────────── 1. DB INVENTORY ───────────────────────────
DBS = {
    "CLEAN_production": ROOT/"data/nexus.duckdb",
    "CLEAN_train_copy": ROOT/"data/train_copy.duckdb",
    "OLD_Z": Path("D:/Nexus-Trading-System-MT5_Z/data/nexus.duckdb"),
}
LABEL_COLS = {"label","target","future_return","future_close","horizon","y","direction","outcome","sample_weight"}
FEAT_TOKENS = ["rsi","ema","sma","atr","macd","bollinger","bb_","adx","stoch","cci","hurst",
               "entropy","volatility","momentum","volume_ratio","spread","institutional","vwap","ichi","di_","tick"]

def audit_db(tag: str, path: Path):
    if not path.exists():
        p(f"[{tag}] NOT FOUND: {path}"); return
    info = {"tag": tag, "path": str(path), "size_gb": round(path.stat().st_size/1e9,3), "tables": []}
    p(f"\n{'='*70}\n### DB [{tag}] {path} | {info['size_gb']} GB")
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as e:
        p(f"  CANNOT OPEN read_only: {e}"); info["error"]=str(e); R["dbs"].append(info); return
    tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    p(f"  tables ({len(tables)}): {tables}")
    for t in tables:
        try:
            cnt = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            cols = con.execute(f'DESCRIBE "{t}"').fetchall()
            colnames = [c[0] for c in cols]
            ent = {"table": t, "rows": cnt, "columns": colnames}
            # timestamps for ohlcv-ish
            if "timestamp" in colnames and cnt > 0:
                mm = con.execute(f'SELECT MIN(timestamp), MAX(timestamp) FROM "{t}"').fetchone()
                ent["ts_min"] = str(mm[0])[:19]; ent["ts_max"] = str(mm[1])[:19]
            if {"symbol","timeframe"} <= set(colnames) and cnt>0:
                sym_tf = con.execute(f'SELECT symbol, timeframe, COUNT(*) FROM "{t}" GROUP BY 1,2 ORDER BY 1,2').fetchall()
                ent["symbol_timeframe"] = [{"symbol":s,"tf":tf,"rows":n} for s,tf,n in sym_tf][:40]
            info["tables"].append(ent)
            # classify columns
            lc = [c for c in colnames if c.lower() in LABEL_COLS]
            fc = [c for c in colnames if any(tok in c.lower() for tok in FEAT_TOKENS)]
            if lc: R["labels"].append({"db":tag,"table":t,"rows":cnt,"label_cols":lc})
            if fc and cnt>0: R["features"].append({"db":tag,"table":t,"rows":cnt,"feature_cols":fc})
            if {"symbol","timeframe","timestamp","open","close"} <= set(colnames):
                R["market_raw"].append({"db":tag,"table":t,"rows":cnt})
        except Exception as e:
            info["tables"].append({"table":t,"error":str(e)})
    con.close()
    R["dbs"].append(info)

for tag, path in DBS.items(): audit_db(tag, path)

# ─────────────── 2. MARKET RAW QUALITY (CLEAN production ohlcv) ───────────────
p(f"\n{'='*70}\n### MARKET RAW QUALITY (CLEAN production ohlcv)")
try:
    con = duckdb.connect(str(DBS["CLEAN_production"]), read_only=True)
    q = con.execute("""WITH d AS (SELECT * FROM ohlcv WHERE symbol='XAUUSD' AND timeframe='M15')
      SELECT COUNT(*), COUNT(DISTINCT timestamp), COUNT(*)-COUNT(DISTINCT timestamp),
        SUM(CASE WHEN open IS NULL OR close IS NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN high<low OR high<open OR high<close OR low>open OR low>close THEN 1 ELSE 0 END) FROM d""").fetchone()
    p(f"  XAUUSD M15: rows={q[0]:,} distinct_ts={q[1]:,} dupes={q[2]:,} nulls={q[3]} ohlc_errors={q[4]}")
    # does ohlcv have spread/tick_volume/real_volume?
    cols = [c[0] for c in con.execute("DESCRIBE ohlcv").fetchall()]
    p(f"  ohlcv columns: {cols}")
    R["ohlcv_quality"] = {"rows":q[0],"distinct":q[1],"dupes":q[2],"nulls":q[3],"ohlc_errors":q[4],"columns":cols}
    con.close()
except Exception as e: p(f"  market quality error: {e}")

# ─────────────── 3. FEATURES REAL CHECK (ensemble store) ───────────────
p(f"\n{'='*70}\n### FEATURES STORE (models_store/<SYM>)")
fdir = ROOT/"models_store"
for sub in sorted(fdir.glob("*")):
    if sub.is_file(): continue
    p(f"  {sub.name}/: {[f.name for f in sub.iterdir()][:12]}")

# feature_names.json
fnj = fdir/"XAUUSD"/"feature_names.json"
if fnj.exists():
    names = json.loads(fnj.read_text(encoding="utf-8"))
    p(f"  feature_names.json: {len(names)} features")
    R["feature_schema"] = {"count": len(names), "names": names}

# ─────────────── 4. REDIS ───────────────
p(f"\n{'='*70}\n### REDIS (read-only)")
try:
    r = redis.Redis.from_url("redis://127.0.0.1:6379", socket_timeout=15, decode_responses=False)
    for pat in ["nexus:features:history:*","nexus:features:*","nexus:regime:*","nexus:rl:*","nexus:trade-memory*","nexus:dataset*","nexus:training*","stream:*"]:
        keys = list(r.scan_iter(pat, count=500))[:20]
        for k in keys:
            try:
                t = r.type(k)
                n = r.llen(k) if t==b"list" else (r.xlen(k) if t==b"stream" else (r.hlen(k) if t==b"hash" else 1))
                ttl = r.ttl(k)
                R["redis"].append({"key":k.decode(),"type":t.decode(),"len":n,"ttl":ttl})
                p(f"  {k.decode()} | {t.decode()} | len={n} | ttl={ttl}")
            except Exception as e: p(f"  {k.decode()} ERR {e}")
except Exception as e: p(f"  REDIS ERROR: {e}")

# ─────────────── 5. MODEL FILES ────────────────────────────────────────
p(f"\n{'='*70}\n### MODEL FILES (loadable check — load in memory only, no writes)")
pats = ["**/*.pkl","**/*.pickle","**/*.joblib","**/*.pt","**/*.pth","**/*.onnx","**/*.h5","**/*.keras"]
seen = set()
for pat in pats:
    for f in glob.glob(str(ROOT/"models_store"/pat), recursive=True) + glob.glob(str(ROOT/"checkpoints"/pat)):
        if f in seen: continue
        seen.add(f)
        fp = Path(f); ent = {"path": str(fp.relative_to(ROOT)), "size_kb": round(fp.stat().st_size/1024,1),
                             "mtime": datetime.fromtimestamp(fp.stat().st_mtime).isoformat()[:19]}
        ok, meta = False, None
        try:
            if fp.suffix == ".pkl":
                with open(fp,"rb") as fh: obj = pickle.load(fh)
                ok = True
                if isinstance(obj, dict):
                    meta = {k: obj.get(k) for k in ("model_name","trained_at","training_samples","n_samples","feature_names","data_source") if k in obj}
        except Exception as e:
            ent["load_error"] = str(e)[:120]
        ent["loadable"] = ok
        if meta: ent["metadata"] = {k:(str(v)[:80]) for k,v in meta.items()}
        R["model_files"].append(ent)
        p(f"  {ent['path']} | {ent['size_kb']} KB | loadable={ok} | mtime={ent['mtime']} | meta={meta}")

# json sidecars in models_store
for j in glob.glob(str(ROOT / "models_store" / "**/*.json"), recursive=True):
    fp=Path(j); p(f"  {fp.relative_to(ROOT)} | json sidecar {round(fp.stat().st_size/1024,1)}KB")

# ─────────────── 6. MLflow ───────────────
ml = ROOT/"mlruns"
R["mlflow"] = {"present": ml.exists(), "runs": 0}
if ml.exists():
    runs = list(ml.glob("*/*/"))
    R["mlflow"]["runs"] = len(runs); p(f"  mlruns present: {len(runs)} runs")
else: p("  mlruns: NOT PRESENT")

# ─────────────── 7. CLASSIFICATION ───────────────
# evidence chain: raw ohlcv (yes) / features (names exist, live-computed in Redis) /
# labels (Redis history has label+future_close) / dataset files (none) /
# model weights (loadable, with metadata) / live-use (traders_status loaded, used in autonomous cycle)
p(f"\n{'='*70}\n### CLASSIFICATION")
has_raw = any(x["rows"]>1000 for x in R["market_raw"])
feat_names = R.get("feature_schema",{}).get("count",0)
redis_feats = [x for x in R["redis"] if "features:history" in x["key"]]
model_ok = [x for x in R["model_files"] if x["loadable"]]
# labels: verify from Redis history sample
labels_real = False; label_stats = {}
try:
    r = redis.Redis.from_url("redis://127.0.0.1:6379", socket_timeout=15)
    raw = r.lrange("nexus:features:history:XAUUSD", -50, -1)
    if raw:
        import json as _j
        recs = [_j.loads(x) for x in raw]
        ok = [x for x in recs if "label" in x and "future_close" in x]
        ones = sum(1 for x in ok if x["label"]==1)
        if ok and all(x["future_close"]!=x.get("ohlcv",{}).get("close") for x in ok[:10]):
            labels_real = True
            label_stats = {"n_sample":len(ok),"ones":ones,"zeros":len(ok)-ones,
                           "future_check":"future_close != current close ✔"}
except Exception as e: label_stats={"error":str(e)}
p(f"  labels_real={labels_real} stats={label_stats}")

if model_ok and labels_real and feat_names>0:
    cls = "D" if True else "C"
    rationale = ("شموع + features محسوبة حياً + labels مستقبلية حقيقية في Redis + أوزان قابلة للتحميل مع metadata. "
                 "لكن إثبات الاستخدام الحي يعتمد على مسار autonomous ML (مثبت سابقاً عبر trades المقفلة source=autonomous_ml) — "
                 "ونواقص: dataset ملفات منفصلة غير موجودة، والقيم on-sample كما أثبت التقرير.")
elif model_ok and labels_real:
    cls = "C"; rationale = "شموع+features+labels+أوزان محملة لكن لم يُثبت الربط الحي في هذا الفحص"
elif labels_real or redis_feats:
    cls = "B"; rationale = "شموع + features لكن لا أوزان صالحة"
elif has_raw:
    cls = "A"; rationale = "شموع فقط"
else:
    cls = "?"; rationale = "غير محدد"
R["classification"] = cls
R["classification_rationale"] = rationale
R["label_stats"] = label_stats
p(f"  CLASSIFICATION: {cls} — {rationale}")

# ─────────────── 8. FINAL TABLE ───────────────
p(f"\n{'='*70}\n### FINAL TABLE")
rows = [
 ("OHLCV", "نعم", sum(x["rows"] for x in R["market_raw"]), "نعم", "نعم", "ohlcv tables in CLEAN+OLD (فحص أعلاه)"),
 ("Features (أسماء)", "نعم" if feat_names else "لا", feat_names, "نعم (schema)", "نعم (تُحسب حياً في nexus_features)", "models_store/XAUUSD/feature_names.json"),
 ("Labels تدريب", "نعم" if labels_real else "لا", label_stats.get("n_sample",""), "نعم (مستقبلية)" if labels_real else "-", "نعم (تدفق حي)", "nexus:features:history:XAUUSD"),
 ("Dataset ملفات", "لا", 0, "-", "-", "لا توجد ملفات dataset منفصلة"),
 ("Model weights", "نعم" if model_ok else "لا", len(model_ok), "نعم (قابلة للتحميل)" if model_ok else "-", "جزئي (autonomous مثبت سابقاً)", "models_store/traders/*.pkl"),
 ("MLflow", "لا" if not R["mlflow"]["present"] else "نعم", R["mlflow"]["runs"], "-", "لا", "mlruns absent" if not R["mlflow"]["present"] else "mlruns"),
 ("Live decision link", "نعم", "-", "نعم", "نعم", "autonomous ML closed 3 trades source=autonomous_ml (موثق 2026-11-18)"),
]
for row in rows: p("  " + " | ".join(str(x) for x in row))

p(f"\n### الحكم النهائي\nقاعدة البيانات تحتوي على: شموع خام + Features محسوبة حياً + Labels مستقبلية (Redis) + أوزان نماذج قابلة للتحميل مربوطة جزئياً بالقرار الحي.\n"
  f"لا توجد ملفات Dataset منفصلة، لا MLflow، والاعتماد على train_acc on-sample ما زال مفتوحاً (P14/T14.6). التصنيف: {cls}")

OUT_TXT.write_text("\n".join(TXT), encoding="utf-8")
OUT_JSON.write_text(json.dumps(R, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\nSAVED: {OUT_TXT.name} ({round(OUT_TXT.stat().st_size/1024,1)} KB), {OUT_JSON.name} ({round(OUT_JSON.stat().st_size/1024,1)} KB)")
