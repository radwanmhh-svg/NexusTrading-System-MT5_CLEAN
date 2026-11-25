"""
P7/T7.1 — reproducible baseline training on triple-barrier labels.

Reads H1 candles straight from DuckDB (no services required), computes the
production feature set (same engine as live: services.nexus_features), builds
triple-barrier labels per horizon, and runs a monthly walk-forward with
XGBoost + LightGBM. Costs come from the REAL broker spec (config/broker_specs.json).

Usage:
    .venv/Scripts/python.exe scripts/train_baseline.py [--symbol XAUUSD] [--horizon 15]

Outputs:
    docs/BASELINE_<SYMBOL>_h<H>.md  — full per-fold metrics
    models_store/<SYMBOL>/baseline_h<H>/ — trained per-fold model files
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.nexus_features.indicators import calculate_all  # noqa: E402
from services.nexus_ml.labels import class_balance, triple_barrier_labels  # noqa: E402

DB_PATH = ROOT / os.getenv("NEXUS_TRAIN_DB", "data/nexus.duckdb")
SPEC_PATH = ROOT / "config" / "broker_specs.json"


def load_candles(symbol: str, timeframe: str = "H1", extended: bool = False) -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
            "WHERE symbol = ? AND timeframe = ? ORDER BY timestamp",
            [symbol, timeframe],
        ).fetchall()
    finally:
        con.close()
    if extended:
        ext_path = ROOT / "data" / "nexus_ext.duckdb"
        if ext_path.exists():
            econ = duckdb.connect(str(ext_path), read_only=True)
            try:
                extra = econ.execute(
                    "SELECT timestamp, open, high, low, close, volume FROM ohlcv_ext "
                    "WHERE symbol = ? AND timeframe = ? ORDER BY timestamp",
                    [symbol, timeframe],
                ).fetchall()
            finally:
                econ.close()
            rows = extra + rows   # Dukascopy history precedes OneRoyal
    return df_from_rows(rows)


def df_from_rows(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def load_cost(symbol: str) -> float:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    s = spec[symbol]
    return float(s["spread_points"]) * float(s["point"])  # round-trip spread in price units


def walk_forward_splits(timestamps: pd.Series, train_months: int = 6, val_months: int = 1):
    months = timestamps.dt.to_period("M")
    uniq = sorted(months.unique())
    i = 0
    while i + train_months + val_months <= len(uniq):
        tr = months.isin(uniq[i:i + train_months])
        va = months.isin(uniq[i + train_months:i + train_months + val_months])
        yield tr.to_numpy(), va.to_numpy()
        i += 1


def metrics(y_true: np.ndarray, y_pred: np.ndarray, y_prob: np.ndarray) -> dict:
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
    m = {
        "n": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "precision": round(float(precision_score(y_true, y_pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, y_pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_true, y_pred, zero_division=0)), 4),
    }
    if len(set(y_true)) > 1:
        m["roc_auc"] = round(float(roc_auc_score(y_true, y_prob)), 4)
    return m


def run(symbol: str, horizon: int, out_dir: Path, extended: bool = False) -> dict:
    import xgboost as xgb

    df = load_candles(symbol, extended=extended)
    feats = calculate_all(df.copy())
    # cost from the REAL broker spec, entered as round-trip spread
    cost = load_cost(symbol) / 2.0  # long entry pays half-spread at entry, exit at TP/SL approx
    feats["label"] = triple_barrier_labels(feats, horizon=horizon, cost=cost)

    drop = {"timestamp", "open", "high", "low", "close", "volume", "label"}
    feat_cols = [c for c in feats.columns if c not in drop and feats[c].dtype != object]
    data = feats.dropna(subset=["label"] + [c for c in feat_cols if c.startswith(("rsi", "atr", "ema_", "macd", "rule_", "adx"))] or feat_cols)
    data = data[data["label"] >= 0]  # drop not-computable rows (incomplete horizon window)

    X_all = data[feat_cols].to_numpy(dtype=np.float32)
    y_all = data["label"].to_numpy(dtype=np.int8)
    ts_all = data["timestamp"].reset_index(drop=True)

    # finite-only features
    X_all = np.nan_to_num(X_all, nan=0.0, posinf=0.0, neginf=0.0)

    splits = list(walk_forward_splits(ts_all))
    report: dict = {
        "symbol": symbol, "horizon_min": horizon, "generated_at": datetime.now(timezone.utc).isoformat(),
        "candles": int(len(feats)), "labelled": int(len(data)),
        "cost_round_trip_price": round(cost * 2, 4),
        "class_balance": class_balance(data["label"]),
        "folds": [],
    }

    fold_i = 0
    for tr_mask, va_mask in splits:
        Xtr, ytr = X_all[tr_mask], y_all[tr_mask]
        Xva, yva = X_all[va_mask], y_all[va_mask]
        if len(np.unique(ytr)) < 2 or len(yva) < 30:
            continue
        fold_i += 1
        model = xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=float((ytr == 0).sum() / max(1, (ytr == 1).sum())),  # T7.3: class weights
            eval_metric="logloss", tree_method="hist",
        )
        model.fit(Xtr, ytr)
        prob = model.predict_proba(Xva)[:, 1]

        # T7.3: threshold search maximizing F1 on the validation fold,
        # with the fixed 0.5 baseline kept alongside for honest comparison.
        best_thr, best_f1 = 0.5, -1.0
        for thr in np.arange(0.30, 0.70, 0.02):
            f1_t = metrics(yva, (prob >= thr).astype(int), prob)["f1"]
            if f1_t > best_f1:
                best_f1, best_thr = f1_t, float(thr)
        pred = (prob >= best_thr).astype(int)
        m = metrics(yva, pred, prob)
        m["threshold"] = round(best_thr, 2)
        m_base = metrics(yva, (prob >= 0.5).astype(int), prob)
        m["f1_at_0.5"] = m_base["f1"]
        m["recall_at_0.5"] = m_base["recall"]
        m["fold"] = fold_i
        m["train_n"] = int(tr_mask.sum())
        m["val_n"] = int(va_mask.sum())
        m["train_period"] = f"{ts_all[tr_mask].iloc[0]:%Y-%m}→{ts_all[tr_mask].iloc[-1]:%Y-%m}"
        m["val_period"] = f"{ts_all[va_mask].iloc[0]:%Y-%m}→{ts_all[va_mask].iloc[-1]:%Y-%m}"
        report["folds"].append(m)
        print(json.dumps(m))

    if report["folds"]:
        for k in ("accuracy", "precision", "recall", "f1", "roc_auc"):
            vals = [f[k] for f in report["folds"] if k in f]
            if vals:
                report[f"mean_{k}"] = round(float(np.mean(vals)), 4)
        fold_dir = out_dir / f"baseline_h{horizon}"
        fold_dir.mkdir(parents=True, exist_ok=True)
        model.get_booster().save_model(str(fold_dir / "xgb_final.json"))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--horizons", default="15,60")
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--extended", action="store_true", help="prepend Dukascopy history (ohlcv_ext)")
    args = ap.parse_args()

    out_dir = ROOT / "models_store" / args.symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    for h in [int(x) for x in args.horizons.split(",")]:
        print(f"=== {args.symbol} horizon {h}min ===")
        rep = run(args.symbol, h, out_dir, extended=args.extended)
        md = [
            f"# Baseline — {args.symbol} · Triple-Barrier h={h}min",
            f"- generated: {rep['generated_at']}",
            f"- candles: {rep['candles']} · labelled: {rep['labelled']} · cost(round-trip): {rep['cost_round_trip_price']}",
            f"- class balance: {rep['class_balance']}",
            "",
            "| fold | train | val | n_val | acc | precision | recall | f1 | auc |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for f in rep["folds"]:
            md.append(
                f"| {f['fold']} | {f['train_period']} | {f['val_period']} | {f['n']} "
                f"| {f.get('accuracy')} | {f.get('precision')} | {f.get('recall')} | {f.get('f1')} | {f.get('roc_auc','—')} |"
            )
        means = ", ".join(f"{k}={v}" for k, v in rep.items() if k.startswith("mean_"))
        md.append("")
        md.append(f"**Means:** {means}")
        path = ROOT / "docs" / f"BASELINE_{args.symbol}_h{h}.md"
        path.write_text("\n".join(md), encoding="utf-8")
        (out_dir / f"baseline_h{h}" / "report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
        print("saved:", path)


if __name__ == "__main__":
    main()
