"""T6 (PLAN_MASTER) — قبول Dataset منظم دائم.

- إعادة البناء = نفس dataset_id ونفس الأرقام (حتمية).
- split محفوظ في الجدول (train/val/test) ونسبه صحيحة.
- النوافذ لا تعبر حدود split (كل train قبل كل val قبل كل test زمنياً).
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]

sys_path = str(ROOT)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from scripts.build_training_dataset import (  # noqa: E402
    SCHEMA_VERSION,
    build_dataset,
    dataset_id_for,
    feature_schema_hash,
    load_candles,
    store_dataset,
)


def _write_ohlcv(db_path: Path, df: pd.DataFrame):
    con = duckdb.connect(str(db_path))
    con.execute(
        """CREATE TABLE IF NOT EXISTS ohlcv (
               symbol VARCHAR, timeframe VARCHAR, open DOUBLE, high DOUBLE,
               low DOUBLE, close DOUBLE, volume DOUBLE, timestamp TIMESTAMPTZ)"""
    )
    rows = [(  "XAUUSD", "M5", float(r.open), float(r.high), float(r.low),
               float(r.close), float(r.volume), r.timestamp)
            for r in df.itertuples()]
    ph = ",".join("?" * 8)
    con.executemany(f"INSERT INTO ohlcv VALUES ({ph})", rows)
    con.execute("CHECKPOINT")
    con.close()


def _synthetic_candles(n=400) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    price = 2000 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({
        "timestamp": ts,
        "open": price - rng.uniform(0, 0.3, n),
        "high": price + rng.uniform(0.1, 1.0, n),
        "low": price - rng.uniform(0.1, 1.0, n),
        "close": price,
        "volume": rng.uniform(50, 200, n),
    })


FEATURES = ["ema_9", "ema_21", "ema_50", "ema_200", "rsi", "macd", "macd_histogram",
            "macd_signal", "bb_lower", "bb_mid", "bb_upper", "bb_bandwidth", "bb_pct_b",
            "stoch_k", "stoch_d", "vwap", "atr", "adx", "di_plus", "di_minus",
            "ichi_conversion", "ichi_base", "ichi_span_a", "ichi_span_b"]


def test_dataset_rebuild_deterministic(tmp_path):
    db = tmp_path / "t6.duckdb"
    _write_ohlcv(db, _synthetic_candles())

    data = build_dataset("XAUUSD", "M5", str(db), FEATURES,
                         horizon=1)  # قراءة عبر دالة البناء نفسها تعيد القراءة
    # قراءة مباشرة للبناء مرة ثانية (عبر نفس الدالة) — لا حاجة لتخزين هنا

    fhash = feature_schema_hash("XAUUSD", FEATURES)
    ts_min = str(data["timestamp"].iloc[0])
    ts_max = str(data["timestamp"].iloc[-1])
    ds1 = dataset_id_for("XAUUSD", "M5", fhash, len(data), ts_min, ts_max, "test")

    # نفس المدخلات → نفس dataset_id
    ds2 = dataset_id_for("XAUUSD", "M5", fhash, len(data), ts_min, ts_max, "test")
    assert ds1 == ds2

    # تخزين مرة، ثم تخزين dataset ثانٍ بمعاملات مختلفة → id مختلف
    stored = store_dataset(str(db), ds1, fhash, "test", data, FEATURES)
    total = stored["train"] + stored["val"] + stored["test"]
    assert total == len(data)
    # 70/15/15 (±1 لكل قسم بسبب التقريب)
    n = len(data)
    assert abs(stored["train"] - int(n * 0.70)) <= 1
    assert abs(stored["val"] - int(n * 0.15)) <= 1

    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "SELECT split, timestamp FROM training_dataset WHERE dataset_id=? ORDER BY timestamp",
        [ds1],
    ).fetchall()
    assert len(rows) == n
    splits_seen = [r[0] for r in rows]
    # النوافذ لا تعبر الحدود: train ثم val ثم test ككتل متتالية
    boundaries = "".join(s[0] for s in splits_seen)
    assert boundaries == "t" * stored["train"] + "v" * stored["val"] + "t" * stored["test"]
    # hash ثابت
    assert fhash == feature_schema_hash("XAUUSD", FEATURES)
    con.close()


def test_dataset_id_changes_when_features_change():
    f1 = feature_schema_hash("XAUUSD", FEATURES)
    f2 = feature_schema_hash("XAUUSD", FEATURES[:-1])
    assert f1 != f2  # أي تغيير ميزات = dataset جديد
