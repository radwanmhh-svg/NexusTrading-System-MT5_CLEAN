"""T3 (PLAN_MASTER) — عقد البيانات: bar_id + تحقق صارم + حارس سباق.

- ``make_bar_id``: هوية موحدة لكل شمعة: ``symbol:timeframe:timestamp_iso``
- ``validate_ohlcv_bar``: تحقق صارم قبل الإدخال (ق19.3):
  مفاتيح مطلوبة + isfinite + high≥max(open,close) + low≤min(open,close)
  — أي شمعة غير صالحة → ValueError.
- ``race_guard_accept``: تجاهل حدث OHLCV إذا timestamp ≤ آخر bar معالج
  (لكل symbol:timeframe) (ق19.4 حرفياً).
- dedup النشر: ``SET NX EX 30d`` على ``nexus:features:bar:{bar_id}``
  — يستخدمه nexus-features قبل معالجة أي حدث (replay نفس الحدث → عينة واحدة).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

BAR_DEDUP_KEY_TPL = "nexus:features:bar:{bar_id}"
BAR_DEDUP_TTL_SEC = 30 * 24 * 3600  # 30d

REQUIRED_BAR_FIELDS = ("open", "high", "low", "close", "timestamp")


def make_bar_id(symbol: str, timeframe: str, timestamp: datetime | str) -> str:
    """هوية شمعة فريدة: SYMBOL:TF:timestamp_iso."""
    ts = timestamp
    if isinstance(ts, str):
        # تسامح مع صيغ ISO المختلفة (Z في النهاية)
        ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return f"{symbol}:{timeframe}:{ts.isoformat()}"


def validate_ohlcv_bar(bar: dict, *, context: str = "") -> dict:
    """تحقق صارم من شمعة قبل أي إدخال (يطلق ValueError عند أي خلل).

    bar يجب أن يحتوي على الأقل: open/high/low/close/timestamp (volume اختياري).
    """
    if not isinstance(bar, dict):
        raise ValueError(f"bar ليس قاموساً{_ctx(context)}")
    missing = [k for k in REQUIRED_BAR_FIELDS if k not in bar or bar[k] in (None, "")]
    if missing:
        raise ValueError(f"مفاتيح مفقودة {missing}{_ctx(context)}")
    try:
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
    except (TypeError, ValueError) as e:
        raise ValueError(f"أسعار غير رقمية{_ctx(context)}") from e
    vals = {"open": o, "high": h, "low": l, "close": c}
    for name, v in vals.items():
        if not math.isfinite(v):
            raise ValueError(f"قيمة غير منتهية (isfinite) في {name}{_ctx(context)}")
    if o <= 0 or c <= 0:
        raise ValueError(f"أسعار غير موجبة open={o} close={c}{_ctx(context)}")
    if h < max(o, c):
        raise ValueError(f"high({h}) < max(open,close)({max(o, c)}){_ctx(context)}")
    if l > min(o, c):
        raise ValueError(f"low({l}) > min(open,close)({min(o, c)}){_ctx(context)}")
    if h < l:
        raise ValueError(f"high({h}) < low({l}){_ctx(context)}")
    if "volume" in bar and bar["volume"] not in (None, ""):
        try:
            v = float(bar["volume"])
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"volume غير صالح {bar['volume']}{_ctx(context)}")
        except (TypeError, ValueError) as e:
            raise ValueError(f"volume غير رقمي{_ctx(context)}") from e
    return bar


def race_guard_accept(bar_ts: datetime | str, last_seen_ts: datetime | str | None) -> bool:
    """حارس سباق: اقبل الشمعة فقط إذا كانت أحدث من آخر شمعة معالجة لـ(symbol:tf)."""
    if last_seen_ts is None:
        return True
    a = _to_dt(bar_ts)
    b = _to_dt(last_seen_ts)
    return a > b


def _to_dt(ts: datetime | str) -> datetime:
    dt = ts
    if isinstance(dt, str):
        dt = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ctx(context: str) -> str:
    return f" ({context})" if context else ""
