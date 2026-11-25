"""T10 — دورة تقييم Shadow كاملة (ق21/20/4).

يُعيد تشغيل الـ test split (خارج العينة — من training_dataset T6) عبر النماذج
الحية (ML/DL) ويسجّل قراراتهم الافتراضية كـ shadow trades (آلية
shadow_trades الموجودة: pending → resolved)، ثم يقارن مع baseline
(majority + last-direction) لدورة تقييم كاملة.

هذا تقييم shadow بدون تنفيذ — لا أمر يصل لـ executor أبداً.
النتيجة: docs/SHADOW_CYCLE.json + سجل في Redis.
"""
from __future__ import annotations

import io, sys as _sys
if hasattr(_sys.stdout, "buffer"): _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_DIR = ROOT / "models_store"
DB_PATH = ROOT / "data" / "nexus.duckdb"


def load_test_split(dataset_id: str | None = None):
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        if dataset_id is None:
            dataset_id = con.execute(
                "SELECT dataset_id FROM training_dataset ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        df = con.execute(
            f"""SELECT * FROM training_dataset
                WHERE dataset_id='{dataset_id}' AND split='test'
                ORDER BY timestamp"""
        ).df()
    finally:
        con.close()
    fcols = [c for c in df.columns if c.startswith("f_")]
    feat_names = [c[2:] for c in fcols]
    return dataset_id, df, df[fcols].to_numpy(dtype=np.float64), df["y"].to_numpy(dtype=np.int64), feat_names


def main() -> None:
    ds_arg = sys.argv[1] if len(sys.argv) > 1 else None
    ds_id, df, X, y, feat_names = load_test_split(ds_arg)
    n = len(y)
    print(f"dataset={ds_id} test_rows={n}")

    # بذرة 42 — قابلية الإعادة
    np.random.seed(42)

    results: dict[str, dict] = {}

    # ── Baseline ──
    majority = int(np.bincount(y).argmax())
    base_majority = float(np.mean(y == majority))
    base_last_dir = float(np.mean(y[1:] == y[:-1]))
    results["baseline"] = {
        "majority_acc": round(base_majority, 4),
        "last_direction_acc": round(base_last_dir, 4),
    }
    print("baseline:", results["baseline"])

    # ── ML ──
    ml_acc = None
    try:
        from services.nexus_ml.traders.ml_trader import MLTrader
        ml = MLTrader()
        ml.load()
        proba = ml._dir_model.predict_proba(X)
        ml_pred = (proba[:, 1] > 0.5).astype(int)
        ml_acc = float(np.mean(ml_pred == y))
        # اتجاهات الظل: BUY عندما pred=1, SELL عندما pred=0
        ml_shadow = {
            "shadow_entries": n,
            "buys": int(ml_pred.sum()),
            "sells": int((1 - ml_pred).sum()),
        }
        results["ml"] = {"accuracy": round(ml_acc, 4), "shadow": ml_shadow,
                         "beats_majority": bool(ml_acc > base_majority)}
    except Exception as e:
        results["ml"] = {"error": str(e)}
    print("ml:", results["ml"])

    # ── DL ──
    try:
        from services.nexus_ml.traders.dl_trader import DLTrader
        import torch
        dl = DLTrader()
        dl.load()
        seq_len = dl.seq_len
        correct, total = 0, 0
        buys = sells = 0
        dl._model.eval()
        with torch.no_grad():
            B = 2048
            for s in range(seq_len - 1, len(X), B):
                idxs = list(range(s, min(s + B, len(X))))
                batch = np.stack([X[i - seq_len + 1: i + 1] for i in idxs])
                batch = (batch - dl._feature_means) / dl._feature_stds
                xb = torch.FloatTensor(batch).to(dl.device)
                logits, _, _ = dl._model(xb)
                preds = logits.argmax(dim=1).cpu().numpy()
                pred01 = (preds == 2).astype(int)
                buys += int(pred01.sum())
                sells += int((1 - pred01).sum())
                correct += int((pred01 == y[idxs]).sum())
                total += len(idxs)
        dl_acc = correct / total if total else float("nan")
        results["dl"] = {"accuracy": round(dl_acc, 4),
                         "shadow": {"shadow_entries": total, "buys": buys, "sells": sells},
                         "beats_majority": bool(dl_acc > base_majority)}
    except Exception as e:
        results["dl"] = {"error": str(e)}
    print("dl:", results["dl"])

    # ── الحكم (ق21/20): نموذج واحد فقط بعد اجتياز الدورة ──
    candidates = {k: results[k].get("accuracy") for k in ("ml", "dl") if "accuracy" in results.get(k, {})}
    passed = {k: (v is not None and v > results["baseline"]["majority_acc"]) for k, v in candidates.items()}
    results["verdict"] = {
        "dataset_id": ds_id,
        "test_rows": n,
        "out_of_sample": True,
        "candidates": {k: round(v, 4) for k, v in candidates.items()},
        "passed_majority_baseline": passed,
        "approved_models": [k for k, ok in passed.items() if ok],
        "note": "الأرقام من test split (خارج العينة) — لا تُعرض كمقياس اعتماد للتداول الحي؛ النافذة الحية لـshadow تُشغّل عند تشغيل الخدمات (وضع shadow في executor).",
    }
    print("verdict:", json.dumps(results["verdict"], ensure_ascii=False))

    # ── تسجيل دورة الظل في Redis (آلية shadow_trades الموجودة) ──
    try:
        import redis as _redis
        r = _redis.Redis(host="localhost", port=6379, decode_responses=True)
        r.ping()
        cycle_id = uuid.uuid4().hex[:12]
        record = {
            "id": cycle_id,
            "kind": "offline_shadow_cycle",
            "dataset_id": ds_id,
            "test_rows": n,
            "baseline_majority": results["baseline"]["majority_acc"],
            "ml_accuracy": results.get("ml", {}).get("accuracy"),
            "dl_accuracy": results.get("dl", {}).get("accuracy"),
            "approved_models": results["verdict"]["approved_models"],
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        r.rpush("nexus:shadow_trades:resolved", json.dumps(record))
        r.ltrim("nexus:shadow_trades:resolved", -2000, -1)
        results["redis_record_id"] = cycle_id
        print("redis shadow_trades:resolved ←", cycle_id)
    except Exception as e:
        results["redis_record_id"] = None
        print("redis unavailable:", e)

    out = ROOT / "docs" / "SHADOW_CYCLE.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("report:", out)


if __name__ == "__main__":
    main()
