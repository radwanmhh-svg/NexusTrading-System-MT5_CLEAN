"""T6 (PLAN_MASTER) — Dataset منظم دائم: جدول training_dataset في DuckDB.

- يقرأ OHLCV المنظف (فهرس uq_ohlcv_bar من T1 يضمن التفرد).
- يحسب الخصائص بمحرك الإنتاج (services.nexus_features.indicators.calculate_all).
- Labels مستقبلية عبر عقد T4 (shared.label_contract) — آخر HORIZON مستبعد.
- split زمني 70/15/15 **محفوظ في الجدول** (عمود split) — النوافذ لا تعبر الحدود.
- feature_schema_hash = sha256(feature_names.json) — أي تغيير ميزات = dataset جديد.
- dataset_id حتمي (deterministic) — إعادة البناء بنفس المدخلات = نفس الـid ونفس الأرقام.

الاستخدام:
    .venv/Scripts/python.exe scripts/build_training_dataset.py \
        [--symbol XAUUSD] [--timeframe M5] [--db data/nexus.duckdb]
"""
from __future__ import annotations

import argparse
import hashlib
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
from shared.label_contract import (  # noqa: E402
    LABEL_HORIZONS_BARS,
    assert_class_minimum,
    future_label,
    label_distribution,
)

SCHEMA_VERSION = 2
SPLIT_RATIOS = (0.70, 0.15, 0.15)  # train / val / test — زمني


def feature_schema_hash(symbol: str, feature_names: list[str]) -> str:
    payload = json.dumps({"symbol": symbol, "features": feature_names}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def dataset_id_for(symbol: str, timeframe: str, fhash: str, n_rows: int,
                   ts_min: str, ts_max: str, data_source: str,
                   label_config: dict | None = None) -> str:
    lc = label_config or {}
    payload = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol, "timeframe": timeframe,
        "feature_schema_hash": fhash,
        "n_rows": n_rows, "ts_min": ts_min, "ts_max": ts_max,
        "data_source": data_source,
        "split_ratios": SPLIT_RATIOS,
        "label_config": lc,
    }, sort_keys=True)
    return "ds_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def load_candles(db_path: str, symbol: str, timeframe: str) -> pd.DataFrame:
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
        raise SystemExit(f"لا شموع لـ {symbol}/{timeframe} في {db_path}")
    # توحيد UTC-naive للاتساق مع بقية النظام
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    return df


def build_dataset(symbol: str, timeframe: str, db_path: str,
                  feature_names: list[str], horizon: int,
                  label_mode: str = "future1",
                  tp_atr: float = 1.5, sl_atr: float = 1.0, cost: float = 0.0,
                  with_spread: bool = False,
                  with_economic: bool = False) -> pd.DataFrame:
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
    # FIX (2026-11-22): شموع مستوردة بلا open (NULL) كانت تفسد كل ما يعتمد
    # عليها (tick_imbalance، o_open للباكتيست) — نملأها من close كاحتياط.
    _open_num = pd.to_numeric(df["open"], errors="coerce")
    _bad_open = int(_open_num.isna().sum())
    if _bad_open:
        print(f"تحذير: {_bad_open} شمعة بلا open — تُملأ من close")
        df["open"] = _open_num.fillna(pd.to_numeric(df["close"], errors="coerce"))
    df = df.reset_index(drop=True)

    feats = calculate_all(df.copy())

    # FIX (2026-11-22): محاذاة المخطط الحي (40 ميزة من /api/features XAUUSD M5).
    # - قابلة للحساب تاريخياً بنفس دوال الإنتاج: atr_14, atr_normalized, tick_imbalance.
    # - بلا تاريخ متاح (سياق cross-asset DXY/US10Y + trend composite لكل شمعة):
    #   تُملأ 0.0 حتى تبقى أعمدة المخطط موحّدة مع التشغيل الحي — تعامَل كإشارة صامتة.
    from services.nexus_features.volatility import calculate_atr_normalized
    feats["atr_14"] = feats["atr"] if "atr" in feats.columns else 0.0
    feats["atr_normalized"] = calculate_atr_normalized(feats)
    for _w in (20, 5):
        _buy_vol = (feats["close"] >= feats["open"]).astype(float) * feats["volume"]
        _vol_sum = feats["volume"].rolling(_w).sum()
        feats[f"tick_imbalance_{_w}"] = (
            _buy_vol.rolling(_w).sum() / _vol_sum.where(_vol_sum > 0, other=np.nan)
        ).fillna(0.5)
    for _c in (
        "trend_strength", "trend_score",
        "dxy_level", "dxy_change_5d", "dxy_rsi_14", "dxy_trend_score",
        "us10y_level", "us10y_change_5d",
        "gold_dxy_divergence", "corr_gold_dxy_20", "cross_asset_signal_pts",
    ):
        if _c not in feats.columns:
            feats[_c] = 0.0
    # الميزات المضافة لاحقاً (سبريد/تقويم) تُحسب أسفل — لا تُدرج في هذا الفحص
    later_added = {"spread_real", "eco_min_to_next_high", "eco_surprise_last24h", "eco_events_24h_back"}
    missing = [c for c in feature_names if c not in feats.columns and c not in later_added]
    if missing:
        raise SystemExit(f"محرك الإنتاج ينقصه أعمدة: {missing}")

    if label_mode == "triple_barrier":
        # T8+: أفق صفقة كامل (TP/SL تكيّفي بـATR + تكلفة داخل الهدف) — عقد P6
        from services.nexus_ml.labels import triple_barrier_labels
        feats["label"] = triple_barrier_labels(
            feats, horizon=horizon, tp_atr=tp_atr, sl_atr=sl_atr, cost=cost,
            ambiguous="sl",
        )
    else:
        # عقد T4: 1 إذا close[t+h] > close[t] — آخر h صفوف بلا مستقبل
        close = feats["close"].to_numpy()
        label = np.full(len(feats), -1, dtype=np.int8)
        label[:-horizon] = [future_label(close[i], close[i + horizon])
                            for i in range(len(close) - horizon)]
        feats["label"] = label

    data = feats.dropna(subset=["label"] + [c for c in feature_names if c not in later_added]).reset_index(drop=True)
    data = data[data["label"] >= 0].reset_index(drop=True)

    # T-edge: ميزة السبريد الحقيقي (من تِكات الوسيط عبر spread_by_minute)
    if with_spread:
        try:
            scon = duckdb.connect(db_path, read_only=True)
            spr = scon.execute(
                "SELECT minute, avg_spread FROM spread_by_minute WHERE symbol=?",
                [symbol],
            ).df()
            scon.close()
            if spr.empty:
                raise SystemExit("--with-spread: جدول spread_by_minute فارغ (شغّل scripts/build_spread_feature.py)")
            spr = spr.rename(columns={"avg_spread": "spread_real"})
            data_ts = pd.to_datetime(data["timestamp"])
            data = data.merge(
                spr.assign(minute=pd.to_datetime(spr["minute"])),
                left_on=data_ts.dt.floor("min"), right_on="minute", how="left",
            ).drop(columns=["minute", "key_0"], errors="ignore")
            # السبريد متاح فقط من 2026-01-28 — نبني الـdataset على نطاق السبريد لعدالة التدريب
            data = data.dropna(subset=["spread_real"]).reset_index(drop=True)
            if "spread_real" not in feature_names:
                feature_names = list(feature_names) + ["spread_real"]
        except duckdb.CatalogException:
            raise SystemExit("--with-spread: جدول spread_by_minute غير موجود")

    # T16.6: ميزات التقويم الاقتصادي (جدول economic_events — P16.5)
    if with_economic:
        eco = economic_features(db_path, data["timestamp"])
        data = pd.concat([data.reset_index(drop=True), eco.reset_index(drop=True)], axis=1)
        eco_names = ["eco_min_to_next_high", "eco_surprise_last24h", "eco_events_24h_back"]
        feature_names = list(feature_names) + [n for n in eco_names if n not in feature_names]

    return data


def economic_features(db_path: str, timestamps: pd.Series) -> pd.DataFrame:
    """T16.6: ميزات تقويم MT5 لكل شمعة — السياق الحدثي (وليس اتجاهاً خام).

    - eco_min_to_next_high: دقائق حتى أقرب حدث عالي التأثير (حتى 72 ساعة؛ 999999 إن لا شيء)
    - eco_surprise_last24h: (actual − forecast) لآخر حدث عالي خلال 24 ساعة (0 إن لا شيء)
    - eco_events_24h_back: عدد أحداث (متوسط+عالي) في الـ24 ساعة السابقة
    """
    con = duckdb.connect(db_path, read_only=True)
    try:
        ev = con.execute(
            "SELECT time_utc, lower(importance) AS importance, actual, forecast FROM economic_events "
            "WHERE lower(importance) IN ('high','moderate') ORDER BY time_utc"
        ).df()
    finally:
        con.close()
    ts = pd.to_datetime(timestamps)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_localize(None)
    ev["time_utc"] = pd.to_datetime(ev["time_utc"])
    if getattr(ev["time_utc"].dt, "tz", None) is not None:
        ev["time_utc"] = ev["time_utc"].dt.tz_localize(None)

    out = pd.DataFrame(index=ts.index)
    if ev.empty:
        out["eco_min_to_next_high"] = 999999.0
        out["eco_surprise_last24h"] = 0.0
        out["eco_events_24h_back"] = 0.0
        return out

    high = ev[ev["importance"] == "high"].reset_index(drop=True)
    ev = ev.reset_index(drop=True)

    hi_ts = high["time_utc"].to_numpy()
    ts_np = ts.to_numpy()
    n = len(ts_np)
    mins = np.full(n, 999999.0)
    idx_next = np.searchsorted(hi_ts, ts_np, side="left")
    for i in range(n):
        j = idx_next[i]
        if j < len(hi_ts):
            mins[i] = (hi_ts[j] - ts_np[i]) / np.timedelta64(1, "m")
    out["eco_min_to_next_high"] = mins

    surprise = np.zeros(n)
    cnt = np.zeros(n)
    hi_actual = high["actual"].to_numpy(dtype=float)
    hi_forecast = high["forecast"].to_numpy(dtype=float)
    ev_ts = ev["time_utc"].to_numpy()
    j = 0
    k = 0
    last_surprise = 0.0
    ev_count = 0
    for i in range(n):
        t = ts_np[i]
        while j < len(hi_ts) and hi_ts[j] <= t:
            a, f = hi_actual[j], hi_forecast[j]
            if np.isfinite(a) and np.isfinite(f):
                last_surprise = a - f
            j += 1
        surprise[i] = last_surprise
        while k < len(ev_ts) and ev_ts[k] <= t:
            ev_count += 1
            k += 1
        cutoff = t - np.timedelta64(24, "h")
        cnt[i] = ev_count - int(np.searchsorted(ev_ts[:k], cutoff, side="left"))
    out["eco_surprise_last24h"] = surprise
    out["eco_events_24h_back"] = cnt
    return out


def store_dataset(db_path: str, dataset_id: str, fhash: str, data_source: str,
                  data: pd.DataFrame, feature_names: list[str]) -> dict:
    n = len(data)
    n_train = int(n * SPLIT_RATIOS[0])
    n_val = int(n * SPLIT_RATIOS[1])
    splits = (["train"] * n_train + ["val"] * n_val + ["test"] * (n - n_train - n_val))

    con = duckdb.connect(db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS training_dataset (
               dataset_id VARCHAR,
               schema_version INTEGER,
               feature_schema_hash VARCHAR,
               data_source VARCHAR,
               created_at TIMESTAMP,
               split VARCHAR,
               timestamp TIMESTAMP,
               y INTEGER,
               sample_weight DOUBLE,
               horizon_bars INTEGER,
               o_open DOUBLE,
               o_high DOUBLE,
               o_low DOUBLE,
               o_close DOUBLE,
               o_volume DOUBLE
           )"""
    )
    # أعمدة الميزات تُضاف مرة واحدة (أول dataset يحدد البنية)
    cols = [c[0] for c in con.execute("DESCRIBE training_dataset").fetchall()]
    for fname in feature_names:
        col = f"f_{fname}"
        if col not in cols:
            con.execute(f'ALTER TABLE training_dataset ADD COLUMN "{col}" DOUBLE')
    for ocol in ("o_open", "o_high", "o_low", "o_close", "o_volume"):
        if ocol not in cols:
            con.execute(f"ALTER TABLE training_dataset ADD COLUMN {ocol} DOUBLE")
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # T6: بناء متجهي + إدراج جماعي عبر جدول مرحلي (executemany بطيء جداً هنا).
    ts_vals = pd.to_datetime(data["timestamp"]).dt.tz_localize(None)
    y_vals = data["label"].astype("int64")
    sw_vals = (data["sample_weight"].astype(float)
               if "sample_weight" in data.columns else pd.Series([1.0] * n))

    stage = "stage_training_dataset"
    col_list = ["dataset_id", "schema_version", "feature_schema_hash", "data_source",
                "created_at", "split", "timestamp", "y", "sample_weight", "horizon_bars"]
    col_list += [f"f_{f}" for f in feature_names]
    # FIX (2026-11-22): أعمدة o_* كانت تُحسب ولا تُدرج في الإدراج الصريح
    # فخزنت NULL — ومُرشّح NaN في traders_api أسقط كل الصفوف (rows=0).
    col_list += ["o_open", "o_high", "o_low", "o_close", "o_volume"]

    con.execute(f"CREATE OR REPLACE TEMP TABLE {stage} AS SELECT * FROM training_dataset LIMIT 0")
    stage_df = pd.DataFrame({
        "dataset_id": dataset_id,
        "schema_version": SCHEMA_VERSION,
        "feature_schema_hash": fhash,
        "data_source": data_source,
        "created_at": created_at,
        "split": splits,
        "timestamp": ts_vals,
        "y": y_vals.values,
        "sample_weight": sw_vals.values,
        "horizon_bars": int(LABEL_HORIZONS_BARS["live_history"]),
    })
    for fname in feature_names:
        stage_df[f"f_{fname}"] = data[fname].to_numpy(dtype="float64")
    # T7: OHLCV الخام بجانب العينة (مسار traders يحتاجها لـ SL/TP)
    stage_df["o_open"] = data["open"].to_numpy(dtype="float64")
    stage_df["o_high"] = data["high"].to_numpy(dtype="float64")
    stage_df["o_low"] = data["low"].to_numpy(dtype="float64")
    stage_df["o_close"] = data["close"].to_numpy(dtype="float64")
    stage_df["o_volume"] = data["volume"].to_numpy(dtype="float64")
    con.register("stage_df_view", stage_df)
    # FIX (2026-11-22): الإدراج بأعمدة صريحة — جدول training_dataset قد يحمل
    # أعمدة من مخططات أقدم (spread_real/eco_*) غير موجودة في هذا الـdataset؛
    # الإدراج بـSELECT * كان يفشل بـBinder Error (59 vs 55 عمود).
    col_sql = ", ".join(f'"{c}"' for c in col_list)
    con.execute(f"INSERT INTO {stage} ({col_sql}) SELECT {col_sql} FROM stage_df_view")
    con.execute(f"INSERT INTO training_dataset SELECT * FROM {stage}")
    con.execute(f"DROP VIEW IF EXISTS stage_df_view")
    con.execute(f"DROP TABLE IF EXISTS {stage}")
    con.execute("CHECKPOINT")
    con.close()
    return {"stored_rows": n, "train": splits.count("train"),
            "val": splits.count("val"), "test": splits.count("test")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--db", default=os.getenv("NEXUS_DATASET_DB", str(ROOT / "data" / "nexus.duckdb")))
    ap.add_argument("--data-source", default="broker_files+live_duckdb")
    ap.add_argument("--rebuild", action="store_true", help="حذف وصف dataset_id من جديد")
    ap.add_argument("--label-mode", default="future1", choices=["future1", "triple_barrier"])
    ap.add_argument("--horizon", type=int, default=None,
                    help="أفق التسمية بالشموع (افتراضي: عقد T4 live_history)")
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--sl-atr", type=float, default=1.0)
    ap.add_argument("--cost", type=float, default=0.30, help="تكلفة ذهاب-إياب بوحدات السعر")
    ap.add_argument("--with-spread", action="store_true", help="إضافة ميزة السبريد الحقيقي (نطاق 2026-01-28+)")
    ap.add_argument("--with-economic", action="store_true", help="T16.6: ميزات التقويم الاقتصادي من economic_events")
    args = ap.parse_args()
    horizon = args.horizon if args.horizon is not None else LABEL_HORIZONS_BARS["live_history"]
    label_config = {"mode": args.label_mode, "horizon": horizon,
                    "tp_atr": args.tp_atr, "sl_atr": args.sl_atr, "cost": args.cost,
                    "with_spread": args.with_spread,
                    "with_economic": args.with_economic}

    names_path = ROOT / "models_store" / args.symbol / "feature_names.json"
    feature_names = json.loads(names_path.read_text(encoding="utf-8"))
    # إصلاح: الميزات المضافة (سبريد/تقويم) يجب أن تدخل البصمة والقائمة قبل التخزين —
    # كانت تُضاف محلياً داخل build_dataset فقط فلم تُخزَّن قط ولا تغيّر dataset_id
    if args.with_spread and "spread_real" not in feature_names:
        feature_names.append("spread_real")
    if args.with_economic:
        for n in ("eco_min_to_next_high", "eco_surprise_last24h", "eco_events_24h_back"):
            if n not in feature_names:
                feature_names.append(n)
    fhash = feature_schema_hash(args.symbol, feature_names)

    data = build_dataset(args.symbol, args.timeframe, args.db, feature_names,
                         horizon, args.label_mode, args.tp_atr, args.sl_atr, args.cost,
                         with_spread=args.with_spread, with_economic=args.with_economic)

    dist = label_distribution(data["label"].to_numpy())
    assert_class_minimum(data["label"].to_numpy())

    ts_min = str(data["timestamp"].iloc[0])
    ts_max = str(data["timestamp"].iloc[-1])
    ds_id = dataset_id_for(args.symbol, args.timeframe, fhash, len(data), ts_min, ts_max,
                           args.data_source, label_config)

    # لا استبدال صامت لنفس dataset_id (إعادة بناء = نفس الأرقام حسباً)
    con = duckdb.connect(args.db, read_only=True)
    try:
        tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        existing = con.execute(
            "SELECT COUNT(*) FROM training_dataset WHERE dataset_id=?", [ds_id]
        ).fetchone()[0] if "training_dataset" in tables else 0
    finally:
        con.close()

    if existing == len(data) and not args.rebuild:
        print(f"dataset موجود مسبقاً — إعادة البناء حتمية: {ds_id}")
        summary = {"dataset_id": ds_id, "already_exists": True, "rows": int(existing), "distribution": dist,
                   "feature_schema_hash": fhash, "ts_min": ts_min, "ts_max": ts_max}
    else:
        if existing:
            # استبدال نظيف لنفس dataset_id (إدراج جزئي من تشغيل قُتل، أو --rebuild)
            wcon = duckdb.connect(args.db)
            wcon.execute("DELETE FROM training_dataset WHERE dataset_id=?", [ds_id])
            wcon.execute("CHECKPOINT")
            wcon.close()
        stored = store_dataset(args.db, ds_id, fhash, args.data_source, data, feature_names)
        print(f"dataset {ds_id} مخزّن: {stored}")
        summary = {"dataset_id": ds_id, "stored": stored, "distribution": dist,
                   "feature_schema_hash": fhash, "ts_min": ts_min, "ts_max": ts_max,
                   "schema_version": SCHEMA_VERSION}

    out = ROOT / "docs" / "DATASET_T6.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
