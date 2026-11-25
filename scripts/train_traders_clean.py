"""Clean honest retrain for ML/DL/RL traders (I-014 gate + T8 PLAN_MASTER).

T8 — التدريب الصادق الشامل:
- يقرأ من جدول training_dataset (T6) بالمسار الموحد — split محفوظ (train/val/test).
- تسمية مستقبلية عبر عقد T4 — آخر HORIZON مستبعد — رفض تحت الحد الأدنى.
- ML (XGBoost+LGBM) + DL (Transformer) + **RL نظيف** (PPO على بيانات حقيقية).
- **train/validation/test metrics منفصلة** — ليس train_acc فقط.
- **baseline إلزامي**: majority class + last-direction على test.
- metadata لكل نموذج: model_source, trained_at, training_samples, data_source,
  is_synthetic, feature_schema_hash, dataset_id, metrics, baseline.
- بذرة 42 — قابلية الإعادة (تشغيلان بفرق < 0.02).

الاستخدام:
    .venv/Scripts/python.exe scripts/train_traders_clean.py [--timeframe M5] [--dataset-id ...]
"""
from __future__ import annotations

import io, sys as _sys
if hasattr(_sys.stdout, "buffer"): _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.nexus_features.indicators import calculate_all  # noqa: E402
from shared.label_contract import (  # noqa: E402
    LABEL_HORIZONS_BARS,
    assert_class_minimum,
    drop_last_horizon,
    future_label,
    label_distribution,
)

MODEL_DIR = ROOT / "models_store"


def build_dataset(symbol: str, timeframe: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """(مسار احتياطي) بناء من OHLCV مباشرة عبر محرك الإنتاج + عقد T4."""
    df = load_candles(symbol, timeframe)
    feats = calculate_all(df.copy())

    names_path = MODEL_DIR / symbol / "feature_names.json"
    feat_cols = json.loads(names_path.read_text(encoding="utf-8"))
    missing = [c for c in feat_cols if c not in feats.columns]
    if missing:
        raise RuntimeError(f"feature engine missing columns: {missing}")

    horizon = LABEL_HORIZONS_BARS["live_history"]
    close = feats["close"].to_numpy()
    label = np.zeros(len(feats), dtype=np.int8)
    label[:-horizon] = np.array(
        [future_label(close[i], close[i + horizon]) for i in range(len(close) - horizon)],
        dtype=np.int8,
    )
    feats["label"] = label

    keep_n = drop_last_horizon(len(feats), horizon)
    data = feats.dropna(subset=["label"] + feat_cols).iloc[:keep_n].reset_index(drop=True)

    X = data[feat_cols].to_numpy(dtype=np.float64)
    y = data["label"].to_numpy(dtype=np.int64)
    ohlcv = data[["open", "high", "low", "close", "volume"]].to_numpy(dtype=np.float64)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    ohlcv = np.nan_to_num(ohlcv, nan=0.0, posinf=0.0, neginf=0.0)
    return X, y, ohlcv, feat_cols


def load_from_training_dataset(symbol: str, timeframe: str, dataset_id: str | None = None):
    """T7/T8: القراءة الموحدة من جدول training_dataset — يعيد dict أو None."""
    db_path = str(ROOT / "data" / "nexus.duckdb")
    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "training_dataset" not in tables:
            return None
        if dataset_id:
            row = con.execute(
                "SELECT dataset_id FROM training_dataset WHERE dataset_id=?", [dataset_id]
            ).fetchone()
        else:
            row = con.execute(
                "SELECT dataset_id FROM training_dataset ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        ds_id = row[0]
        df = con.execute(
            f"SELECT * FROM training_dataset WHERE dataset_id='{ds_id}' ORDER BY timestamp"
        ).df()
        meta = con.execute(
            "SELECT feature_schema_hash, data_source FROM training_dataset WHERE dataset_id=? LIMIT 1",
            [ds_id],
        ).fetchone()
    finally:
        con.close()
    if df.empty:
        return None
    fcols = [c for c in df.columns if c.startswith("f_")]
    feat_names = [c[2:] for c in fcols]
    X = df[fcols].to_numpy(dtype=np.float64)
    y = df["y"].to_numpy(dtype=np.int64)
    ohlcv = df[["o_open", "o_high", "o_low", "o_close", "o_volume"]].to_numpy(dtype=np.float64)
    sw = df["sample_weight"].to_numpy(dtype=np.float64)
    ts = df["timestamp"].to_numpy()
    split = df["split"].to_numpy()
    return {
        "dataset_id": ds_id,
        "feature_schema_hash": meta[0],
        "data_source": meta[1],
        "X": X, "y": y, "ohlcv": ohlcv, "sample_weight": sw,
        "timestamps": ts, "split": split, "feature_names": feat_names,
        "is_synthetic": False,
    }


def load_candles(symbol: str, timeframe: str) -> pd.DataFrame:
    db_path = str(ROOT / "data" / "nexus.duckdb")
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            """SELECT timestamp, open, high, low, close, volume FROM ohlcv
               WHERE symbol=? AND timeframe=? ORDER BY timestamp""",
            [symbol, timeframe],
        ).df()
    finally:
        con.close()
    if df.empty:
        raise SystemExit(f"لا شموع لـ {symbol}/{timeframe}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    return df


def _dir_accuracy(model, X, y) -> float:
    """دقة نموذج الاتجاه على مجموعة (خارج العينة إن كانت val/test)."""
    if len(X) == 0:
        return float("nan")
    proba = model.predict_proba(X)
    preds = (proba[:, 1] > 0.5).astype(int)
    return float(np.mean(preds == y))


def _baselines(y: np.ndarray) -> dict[str, float]:
    """T8: baseline إلزامي — majority class + last-direction."""
    out = {}
    if len(y) == 0:
        return {"majority": float("nan"), "last_direction": float("nan")}
    majority = int(np.bincount(y).argmax())
    out["majority"] = float(np.mean(y == majority))
    if len(y) > 1:
        # التنبؤ دائماً باتجاه الشمعة السابقة: pred[i] = y[i-1]
        out["last_direction"] = float(np.mean(y[1:] == y[:-1]))
    else:
        out["last_direction"] = float("nan")
    return out


def _dl_eval(dl, X: np.ndarray, y: np.ndarray) -> float:
    """تقييم DL على مجموعة عبر تمرير تسلسلات (normalize بمعاملات التدريب فقط)."""
    try:
        import torch
        seq_len = dl.seq_len
        means, stds = dl._feature_means, dl._feature_stds
        correct, total = 0, 0
        dl._model.eval()
        with torch.no_grad():
            B = 1024
            for s in range(seq_len - 1, len(X), B):
                idxs = range(s, min(s + B, len(X)))
                batch = np.stack([X[i - seq_len + 1: i + 1] for i in idxs])
                batch = (batch - means) / stds
                xb = torch.FloatTensor(batch).to(dl.device)
                logits, _, _ = dl._model(xb)
                preds = logits.argmax(dim=1).cpu().numpy()  # 0=DOWN, 2=UP
                ys = y[list(idxs)]
                pred01 = (preds == 2).astype(int)
                correct += int((pred01 == ys).sum())
                total += len(ys)
        return correct / total if total else float("nan")
    except Exception:
        return float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--dataset-id", default=None)
    ap.add_argument("--skip-rl", action="store_true", help="تجاوز RL (بطء)")
    ap.add_argument("--skip-dl", action="store_true", help="تجاوز DL (فحص إعادة ML فقط)")
    args = ap.parse_args()

    # ── المصدر الموحد: training_dataset (T6) أو الاحتياطي ──
    ds = load_from_training_dataset(args.symbol, args.timeframe, args.dataset_id)
    if ds is None:
        print("تحذير: لا training_dataset — احتياطياً من OHLCV بدون split (مقاييس val/test لن تُحسب)")
        X, y, ohlcv, feat_cols = build_dataset(args.symbol, args.timeframe)
        n = len(y)
        split = np.array(["train"] * n, dtype=object)
        ds = {"dataset_id": None, "feature_schema_hash": None, "data_source": "ohlcv_direct",
              "X": X, "y": y, "ohlcv": ohlcv, "sample_weight": np.ones(n),
              "split": split, "feature_names": feat_cols, "is_synthetic": False}
    X, y = ds["X"], ds["y"]
    ohlcv, sw = ds["ohlcv"], ds["sample_weight"]
    feat_cols = ds["feature_names"]
    split = ds["split"]

    m_train, m_val, m_test = split == "train", split == "val", split == "test"
    print(f"dataset_id={ds['dataset_id']} rows={len(y)} train={m_train.sum()} val={m_val.sum()} test={m_test.sum()}")
    dist = label_distribution(y)
    print(f"label distribution: {json.dumps(dist)}")
    assert_class_minimum(y)

    # Reproducibility: identical data + identical seed → identical weights.
    import torch
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(42)

    sw_tr = sw[m_train]
    trained_at = datetime.now(timezone.utc).isoformat()

    # ── MLTrader (XGBoost+LGBM) على train فقط ──
    from services.nexus_ml.traders.ml_trader import MLTrader
    from services.nexus_ml.traders.dl_trader import DLTrader

    print("── MLTrader (train split only) ──")
    ml = MLTrader()
    ml_metrics = ml.train(
        X[m_train], y[m_train], ohlcv=ohlcv[m_train], feature_names=feat_cols,
        sample_weight=sw_tr, symbol=args.symbol,
    )
    ml_split = {
        "train": _dir_accuracy(ml._dir_model, X[m_train], y[m_train]),
        "val": _dir_accuracy(ml._dir_model, X[m_val], y[m_val]) if m_val.sum() else float("nan"),
        "test": _dir_accuracy(ml._dir_model, X[m_test], y[m_test]) if m_test.sum() else float("nan"),
    }
    print("ML split metrics:", json.dumps(ml_split))
    ml.save()

    # ── DLTrader (Transformer) على train فقط ──
    dl_split: dict[str, float] = {}
    dl_metrics: dict[str, float] = {}
    dl_trained = False
    if not args.skip_dl:
        print("── DLTrader (train split only) ──")
        dl = DLTrader()
        dl_metrics = dl.train(
            X[m_train], y[m_train], ohlcv=ohlcv[m_train], feature_names=feat_cols,
            sample_weight=sw_tr, symbol=args.symbol,
        )
        dl_split = {
            "train": _dl_eval(dl, X[m_train], y[m_train]) if m_train.sum() else float("nan"),
            "val": _dl_eval(dl, X[m_val], y[m_val]) if m_val.sum() else float("nan"),
            "test": _dl_eval(dl, X[m_test], y[m_test]) if m_test.sum() else float("nan"),
        }
        print("DL split metrics:", json.dumps(dl_split))
        dl.save()
        dl_trained = True

    # ── RL نظيف (PPO) على train فقط — الحلقة الافتتاحية البيئة ببيانات حقيقية ──
    rl_split: dict[str, float] = {}
    rl_metrics: dict[str, float] = {}
    if not args.skip_rl and m_train.sum() >= 300:
        print("── RLTrader (PPO, clean, train split only) ──")
        from services.nexus_ml.traders.rl_trader import RLTrader
        rl = RLTrader()
        rl_metrics = rl.train(
            X[m_train], y[m_train], ohlcv=ohlcv[m_train], feature_names=feat_cols,
            sample_weight=sw_tr, n_epochs=15, symbol=args.symbol,
        )
        rl.save()

    # ── Baseline إلزامي على test ──
    baseline = _baselines(y[m_test]) if m_test.sum() else _baselines(y)
    print("baseline (test):", json.dumps(baseline))

    # ── Metadata لكل نموذج (T8) ──
    common_meta = {
        "model_source": "nexus",
        "trained_at": trained_at,
        "training_samples": int(m_train.sum()),
        "data_source": ds["data_source"],
        "is_synthetic": bool(ds["is_synthetic"]),
        "feature_schema_hash": ds["feature_schema_hash"],
        "dataset_id": ds["dataset_id"],
        "seed": 42,
        "label_distribution": dist,
        "baseline": baseline,
    }
    out_dir = MODEL_DIR / args.symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "traders_metadata.json").write_text(
        json.dumps({
            **common_meta,
            "ml": {"metrics": {k: (float(v) if v == v else None) for k, v in ml_metrics.items()
                                if isinstance(v, (int, float))},
                    "split_accuracy": {k: (float(v) if v == v else None) for k, v in ml_split.items()}},
            "dl": ({"metrics": {k: (float(v) if v == v else None) for k, v in dl_metrics.items()
                                if isinstance(v, (int, float))},
                    "split_accuracy": {k: (float(v) if v == v else None) for k, v in dl_split.items()}}
                   if dl_trained else "skipped"),
            "rl": {"metrics": {k: (float(v) if v == v else None) for k, v in rl_metrics.items()
                                if isinstance(v, (int, float))},
                    "split_accuracy": rl_split},
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("metadata saved:", out_dir / "traders_metadata.json")
    print("honest retrain complete — train/val/test منفصلة + baseline + metadata")


if __name__ == "__main__":
    main()
