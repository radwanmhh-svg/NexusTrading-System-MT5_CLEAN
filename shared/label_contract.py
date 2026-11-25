"""T4 (PLAN_MASTER) — عقد Labels (F-01 تثبيت مؤتمت).

- HORIZON موثق هنا مركزياً: سجل التدريب الحي يستخدم آفاقاً بعدد شموع،
  والتدريب النظيف يستخدم future 1-bar label (ق19.7).
- أي تدريب يستخدم هذه العقدة يجب أن يستبعد آخر LABEL_HORIZON_BARS صفوفاً
  (لا يوجد مستقبل لتسميتها بعد).
- ``validate_labeled_record``: تحقق بصيغة ق19.7 —
  future_timestamp > timestamp و future_return = close/prev_close − 1 صحيح.
- ``label_distribution`` + ``assert_class_minimum``: رفض التدريب إذا أحد
  الاتجاهين أقل من الحد الأدنى.
"""
from __future__ import annotations

import os
from datetime import datetime

# HORIZON موثق: آفاق التسمية بالشموع لكل مسار (لا تُخلط أبداً).
LABEL_HORIZONS_BARS: dict[str, int] = {
    "live_history": 1,       # سجل التدريب الحي: close[t+1] مقابل close[t]
    "triple_barrier_5m": 5,  # عقد P6: آفاق 5/15/60 دقيقة
    "triple_barrier_15m": 15,
    "triple_barrier_60m": 60,
}

# الحد الأدنى لعينات كل اتجاه قبل قبول أي تدريب (قابل للرفع عبر env).
MIN_CLASS_SAMPLES = int(os.getenv("NEXUS_MIN_CLASS_SAMPLES", "500"))


def future_return(prev_close: float, next_close: float) -> float:
    """عائد المستقبل الصافي: close[t+1]/close[t] − 1 (ق19.7)."""
    if prev_close <= 0:
        raise ValueError("prev_close يجب أن يكون موجباً")
    return next_close / prev_close - 1.0


def future_label(prev_close: float, next_close: float) -> int:
    """label = 1 إذا close[t+1] > close[t] وإلا 0 (لا تسريب للماضي)."""
    return 1 if next_close > prev_close else 0


def future_timestamp_ok(timestamp: datetime | str, future_ts: datetime | str) -> bool:
    """ق19.7: future_timestamp > timestamp صرامة."""
    a = _dt(timestamp)
    b = _dt(future_ts)
    return b > a


def validate_labeled_record(rec: dict) -> dict:
    """تحقق كامل من سجل موسوم (ق19.7): يطلق ValueError عند أي خلل."""
    needed = ("close", "future_close", "label", "timestamp")
    missing = [k for k in needed if k not in rec or rec[k] in (None, "")]
    if missing:
        raise ValueError(f"سجل موسوم ناقص: {missing}")
    prev_c, next_c, label = float(rec["close"]), float(rec["future_close"]), int(rec["label"])
    if label not in (0, 1):
        raise ValueError(f"label خارج {0,1}: {label}")
    expected = future_label(prev_c, next_c)
    if label != expected:
        raise ValueError(
            f"label({label}) لا يطابق future_return: close={prev_c} future_close={next_c}"
        )
    fr = future_return(prev_c, next_c)
    if abs(fr - (next_c / prev_c - 1)) > 1e-12:
        raise ValueError("future_return غير متسق")
    ts, fts = rec.get("timestamp"), rec.get("future_timestamp")
    if ts and fts and not future_timestamp_ok(ts, fts):
        raise ValueError(f"future_timestamp({fts}) ≤ timestamp({ts})")
    return rec


def label_distribution(y) -> dict[str, float | int]:
    """توزيع الاتجاهات: {down, up, total, up_ratio, down_ratio}."""
    import numpy as np
    arr = np.asarray(y, dtype=np.int64)
    up = int((arr == 1).sum())
    down = int((arr == 0).sum())
    total = up + down
    return {
        "up": up,
        "down": down,
        "total": total,
        "up_ratio": round(up / total, 4) if total else 0.0,
        "down_ratio": round(down / total, 4) if total else 0.0,
    }


def assert_class_minimum(y, *, min_samples: int | None = None) -> dict[str, float | int]:
    """رفض التدريب إذا أحد الاتجاهين < الحد الأدنى (ValueError)."""
    dist = label_distribution(y)
    mn = min_samples if min_samples is not None else MIN_CLASS_SAMPLES
    if dist["total"] == 0:
        raise ValueError("لا توجد عينات موسومة — رفض التدريب")
    if dist["up"] < mn or dist["down"] < mn:
        raise ValueError(
            f"فئة أقل من الحد الأدنى ({mn}): up={dist['up']} down={dist['down']} — رفض التدريب"
        )
    return dist


def drop_last_horizon(df_or_len, horizon: int | None = None):
    """استبعاد آخر HORIZON صفوفاً (لا مستقبل لتسميتها)."""
    n = len(df_or_len)
    h = horizon if horizon is not None else LABEL_HORIZONS_BARS["live_history"]
    if n <= h:
        raise ValueError(f"عدد الصفوف ({n}) ≤ HORIZON ({h}) — لا يوجد ما يُدرّب")
    return n - h


def _dt(ts: datetime | str) -> datetime:
    d = ts
    if isinstance(d, str):
        d = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return d
