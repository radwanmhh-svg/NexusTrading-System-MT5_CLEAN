"""إعادة تدريب موديلات الـensemble من training.duckdb (مخطط feature_names.json الرسمي).

T17.5-fix (2026-11-23): موديلات الـensemble الإرثة (xgboost_direction/lgbm_regime/
rf_features/isolation_anomaly) كانت مدرّبة على متجه 24 ميزة من تاريخ Redis المختلط —
ففشلت عند التوقع مع المتجه الرسمي 43. هذا السكربت يدرّبها walk-forward من الـdataset
المستقل (بلا قفل) ويحفظ في models_store/<SYMBOL>/ — الـLSTM لا يُلمس (sequences=None).

الاستخدام (من جذر المشروع، الخدمات موقوفة):
    .venv\\Scripts\\python.exe scripts\\train_ensemble_from_dataset.py --symbol XAUUSD
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.nexus_ml.training import WalkForwardConfig, WalkForwardTrainer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "training.duckdb"))
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--out", default=None, help="افتراضي: models_store/<SYMBOL>")
    ap.add_argument("--train-months", type=int, default=6)
    ap.add_argument("--val-months", type=int, default=1)
    ap.add_argument("--step-months", type=int, default=3)
    args = ap.parse_args()

    out_dir = args.out or str(ROOT / "models_store" / args.symbol)
    canon_path = ROOT / "models_store" / args.symbol / "feature_names.json"
    canon = json.loads(canon_path.read_text(encoding="utf-8"))
    print(f"[train-ensemble] canon features: {len(canon)} (from {canon_path.name})")

    con = duckdb.connect(args.db, read_only=True)
    ds_id, = con.execute(
        "SELECT dataset_id FROM training_dataset GROUP BY dataset_id "
        "ORDER BY max(created_at) DESC LIMIT 1"
    ).fetchone()
    df = con.execute(
        f"SELECT * FROM training_dataset WHERE dataset_id='{ds_id}' ORDER BY timestamp"
    ).df()
    con.close()

    fcols = [f"f_{n}" for n in canon if f"f_{n}" in df.columns]
    if len(fcols) != len(canon):
        missing = [n for n in canon if f"f_{n}" not in df.columns]
        raise SystemExit(f"[train-ensemble] missing f_ columns: {missing}")
    X = df[fcols].to_numpy(dtype=np.float64)
    y = df["y"].to_numpy(dtype=np.int64)
    ts = df["timestamp"].to_numpy(dtype="datetime64[s]")
    mask = np.isfinite(X).all(axis=1)
    X, y, ts = X[mask], y[mask], ts[mask]
    feat_names = [c[2:] for c in fcols]
    print(f"[train-ensemble] dataset={ds_id} rows={len(X)} features={len(feat_names)}")
    print(f"[train-ensemble] span: {ts[0]} -> {ts[-1]} | UP={int(y.sum())} DOWN={int((y==0).sum())}")

    cfg = WalkForwardConfig(
        train_months=args.train_months,
        val_months=args.val_months,
        step_months=args.step_months,
        model_dir=out_dir,
    )
    trainer = WalkForwardTrainer(cfg)
    results = trainer.train_all_models(X, y, ts, feat_names)

    print(f"[train-ensemble] DONE — {len(results)} folds saved to {out_dir}")
    for r in results:
        print(f"  fold {r.fold}: {r.train_start[:10]}..{r.train_end[:10]} -> "
              f"val {r.val_start[:10]}..{r.val_end[:10]}")
        for name, m in r.metrics.items():
            nice = ", ".join(f"{k}={v:.4f}" for k, v in m.items())
            print(f"    {name}: {nice}")


if __name__ == "__main__":
    main()
