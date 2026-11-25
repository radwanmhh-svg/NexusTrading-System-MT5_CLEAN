"""
P7/T7.2 — train the LSTM sequence model and persist it for the live ensemble.

Uses the SAME production feature engine and triple-barrier labels as the
baseline (scripts/train_baseline.py), so the saved model is directly
loadable by services.nexus_ml.ensemble (models_store/<SYM>/lstm_sequence.pt).

Sequences are built chronologically with seq_len from DEFAULT_HPARAMS (20);
LSTMSequenceModel.train() itself reserves the LAST 15% as chronological
validation, so there is no time mixing.

Usage:
    .venv/Scripts/python.exe scripts/train_lstm.py [--symbol XAUUSD] [--horizon 60]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.nexus_features.indicators import calculate_all  # noqa: E402
from services.nexus_ml.labels import triple_barrier_labels  # noqa: E402
from services.nexus_ml.models.lstm_sequence import LSTMSequenceModel, DEFAULT_HPARAMS  # noqa: E402

# reuse the baseline's DB + cost loaders (NEXUS_TRAIN_DB-aware)
import importlib.util
_spec = importlib.util.spec_from_file_location("train_baseline", ROOT / "scripts" / "train_baseline.py")
_tb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tb)

SEQ_LEN = DEFAULT_HPARAMS["seq_len"]


def build_dataset(symbol: str, horizon: int, extended: bool) -> tuple[np.ndarray, np.ndarray, list[str]]:
    df = _tb.load_candles(symbol, extended=extended)
    feats = calculate_all(df.copy())
    cost = _tb.load_cost(symbol) / 2.0
    feats["label"] = triple_barrier_labels(feats, horizon=horizon, cost=cost)

    # use the exact column order the live ensemble already knows
    names_path = ROOT / "models_store" / symbol / "feature_names.json"
    feat_cols = json.loads(names_path.read_text(encoding="utf-8"))
    missing = [c for c in feat_cols if c not in feats.columns]
    if missing:
        raise RuntimeError(f"feature engine missing columns: {missing}")

    data = feats.dropna(subset=["label"] + feat_cols)
    data = data[data["label"] >= 0].reset_index(drop=True)

    X = data[feat_cols].to_numpy(dtype=np.float32)
    y = data["label"].to_numpy(dtype=np.int8)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X, y, feat_cols


def make_sequences(X: np.ndarray, y: np.ndarray, seq_len: int) -> tuple[np.ndarray, np.ndarray]:
    """(N, seq_len, F) windows; target = label of the LAST bar in the window."""
    n = len(X) - seq_len + 1
    seqs = np.lib.stride_tricks.sliding_window_view(X, (seq_len, X.shape[1])).reshape(n, seq_len, X.shape[1])
    labels = y[seq_len - 1:]
    return seqs, labels


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--extended", action="store_true")
    args = ap.parse_args()

    X, y, feat_cols = build_dataset(args.symbol, args.horizon, args.extended)
    seqs, labels = make_sequences(X, y, SEQ_LEN)
    up_pct = float(np.mean(labels == 1))
    print(f"samples={len(seqs)} seq_len={SEQ_LEN} features={len(feat_cols)} up_share={up_pct:.3f}")
    if len(np.unique(labels)) < 2 or not (0.05 < up_pct < 0.95):
        raise SystemExit("single/degenerate label class — refusing to train")

    model = LSTMSequenceModel()
    metrics = model.train(seqs, labels.astype(np.int64))
    print("train metrics:", json.dumps({k: round(float(v), 4) for k, v in metrics.items()}))

    out = ROOT / "models_store" / args.symbol / "lstm_sequence.pt"
    model.save(out, mlflow_run=False)
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
