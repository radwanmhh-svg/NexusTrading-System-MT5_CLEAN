"""T7 (PLAN_MASTER) — Walk-forward موحد.

- الفولز زمنياً حقيقية: كل val بعد كل train صرامة (لا تعبر ولا تختلط).
- بذرة 42 قابلة للإعادة.
- المسار الموحد: يمكن تحميل التدريب من جدول training_dataset (T6).
"""
from __future__ import annotations

import duckdb
import numpy as np
import pytest

from services.nexus_ml.training import WalkForwardConfig, WalkForwardTrainer


def test_folds_chronological_and_disjoint():
    # 12 شهراً: 6 تدريب / 1 تحقق / خطوة 1
    n = 400
    ts = np.arange(np.datetime64("2025-01", "M"), np.datetime64("2025-01", "M") + n, dtype="datetime64[M]")
    ts = np.repeat(ts[:12], n // 12).astype("datetime64[s]")
    trainer = WalkForwardTrainer(WalkForwardConfig(train_months=6, val_months=1, step_months=1))
    folds = trainer._generate_folds(ts)
    assert len(folds) > 0
    for train_mask, val_mask in folds:
        t_train = ts[train_mask]
        t_val = ts[val_mask]
        # كل تحقق بعد كل تدريب صرامة (اختبار دائماً بالمستقبل)
        assert t_val.min() > t_train.max()
        # كلاهما غير فارغ وغير متقاطع
        assert len(set(train_mask.nonzero()[0]) & set(val_mask.nonzero()[0])) == 0


def test_walk_forward_reproducible_seed42():
    rng = np.random.default_rng(7)
    n, f = 600, 8
    ts = (np.datetime64("2025-01-01") + np.arange(n) * np.timedelta64(6, "h")).astype("datetime64[s]")
    X = rng.standard_normal((n, f))
    y = (rng.random(n) > 0.5).astype(int)

    def run():
        np.random.seed(42)
        trainer = WalkForwardTrainer(WalkForwardConfig(train_months=2, val_months=1, step_months=2))
        folds = trainer._generate_folds(ts)
        out = []
        for train_mask, val_mask in folds:
            if train_mask.sum() < 10 or val_mask.sum() < 5:
                continue
            out.append((train_mask.sum(), val_mask.sum()))
        return out

    assert run() == run()  # نفس البذرة → نفس الفولز والأعداد


def test_dataset_loader_shape_and_split_blocks(tmp_path):
    from scripts.build_training_dataset import (
        build_dataset, dataset_id_for, feature_schema_hash, store_dataset,
    )
    sys_path_done = True
    from services.nexus_ml.traders_api import _load_dataset_training_data
    import services.nexus_ml.traders_api as tapi
    import os

    # بناء قاعدة صغيرة
    rng = np.random.default_rng(42)
    n = 400
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    price = 2000 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame({"timestamp": ts, "open": price - 0.1, "high": price + 0.5,
                       "low": price - 0.5, "close": price, "volume": 100.0})
    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    con.execute("""CREATE TABLE ohlcv (symbol VARCHAR, timeframe VARCHAR, open DOUBLE,
        high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, timestamp TIMESTAMPTZ)""")
    con.executemany("INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)",
                    [("XAUUSD", "M5", float(r.open), float(r.high), float(r.low),
                      float(r.close), float(r.volume), r.timestamp) for r in df.itertuples()])
    con.execute("CHECKPOINT"); con.close()

    features = ["rsi", "macd", "atr", "adx"]  # مثال مبسط لكن يجب أن يغطيها محرك الإنتاج
    # استخدم أسماء موجودة فعلاً في محرك الإنتاج
    features = ["ema_9", "rsi", "macd", "atr", "adx", "vwap"]

    data = build_dataset("XAUUSD", "M5", str(tmp_path / "d.duckdb"), features, horizon=1)
    fhash = feature_schema_hash("XAUUSD", features)
    ds_id = dataset_id_for("XAUUSD", "M5", fhash, len(data),
                           str(data["timestamp"].iloc[0]), str(data["timestamp"].iloc[-1]), "test")
    store_dataset(str(tmp_path / "d.duckdb"), ds_id, fhash, "test", data, features)

    # المحمل الموحد يقرأ منها
    monkey_env = os.environ.copy()
    os.environ["NEXUS_DATASET_DB"] = str(tmp_path / "d.duckdb")
    try:
        result = _load_dataset_training_data("XAUUSD", "M5", ds_id)
        assert result is not None
        X, y, ohlcv, feat_names, sw, tsr = result
        assert X.shape[0] == len(data) == y.shape[0] == sw.shape[0]
        assert ohlcv.shape == (len(data), 5)
        assert feat_names == features
        assert (sw == 1.0).all()
        # timestamps حقيقية مرتبة
        assert tsr.dtype.kind == "M"
        assert (np.diff(tsr.astype("int64")) > 0).all()
    finally:
        os.environ.clear()
        os.environ.update(monkey_env)


import pandas as pd  # noqa: E402  (أسفل لتفادي الترتيب)
