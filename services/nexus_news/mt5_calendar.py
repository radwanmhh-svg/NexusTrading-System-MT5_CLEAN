"""MT5 economic-calendar CSV loader — P16 (شاشة الأخبار والتقويم).

يقرأ الملف الذي يصدّره mql5/economic_calendar_export.mq5
(data/economic/nexus_economic_events.csv — مصدره تقويم الوسيط MT5 نفسه:
source=MT5-CALENDAR-ONEROYAL) ويحوّله إلى CalendarEvent النظامية.

- لا يعدّل أي حقل: يقرأ event_name/currency/importance/time_utc/actual/forecast/previous كما هي.
- LONG_MIN (-9223372036854775808) يعني "قيمة غير متاحة" → None (ليس صفراً).
- التوقيت المعتمد هو time_utc (الموديل يحفظ وقت الوسيط وUTC معاً + الفارق المقاس).
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.models import CalendarEvent

_LONG_MIN = -9223372036854775808
_IMPORTANCE_MAP = {"HIGH": "high", "MODERATE": "medium", "LOW": "low"}


def _fmt_value(raw: str) -> str | None:
    """تنسيق قيمة عادية/متوقعة/سابقة للعرض — بلا تعديل دلالي."""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return raw.strip() or None
    if v == _LONG_MIN:
        return None
    # إزالة الأصفار الزائدة: 4.405000 → 4.405 · 17.000000 → 17
    s = f"{v:g}"
    return s


def _parse_ts(raw: str) -> datetime | None:
    """time_utc بصيغة 2026.08.19 14:30:00 → datetime UTC."""
    if not raw:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def load_mt5_csv(path: str | Path) -> list[CalendarEvent]:
    """قراءة كامل ملف تقويم MT5 → قائمة CalendarEvent مرتبة زمنياً."""
    p = Path(path)
    if not p.exists():
        return []
    events: list[CalendarEvent] = []
    # EA يكتب أسماء أحداث بترميز Windows — نحمل بتسامح (utf-8 ثم cp1252 ثم latin-1)
    import io as _io
    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    try:
        with _io.StringIO(text, newline="") as f:
            for row in csv.DictReader(f):
                ts = _parse_ts(row.get("time_utc", ""))
                if ts is None:
                    continue
                events.append(CalendarEvent(
                    event_name=(row.get("event_name") or "").strip(),
                    currency=(row.get("currency") or "").strip().upper(),
                    impact=_IMPORTANCE_MAP.get(
                        (row.get("importance") or "").strip().upper(), "low"),
                    forecast=_fmt_value(row.get("forecast")),
                    previous=_fmt_value(row.get("previous")),
                    actual=_fmt_value(row.get("actual")),
                    timestamp=ts,
                    source=(row.get("source") or "mt5").strip(),
                ))
    except (OSError, csv.Error):
        return []
    events.sort(key=lambda e: e.timestamp)
    return events


def split_past_future(
    events: list[CalendarEvent], *, now: datetime | None = None,
) -> tuple[list[CalendarEvent], list[CalendarEvent]]:
    """تقسيم إلى (ماضية، قادمة) بحسب UTC الحالي."""
    now = now or datetime.now(timezone.utc)
    past = [e for e in events if e.timestamp <= now]
    future = [e for e in events if e.timestamp > now]
    return past, future


def economic_csv_path() -> Path:
    """مسار الملف الافتراضي — قابل للتجاوز بمتغير بيئة NEXUS_ECONOMIC_CSV."""
    custom = os.getenv("NEXUS_ECONOMIC_CSV")
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parents[2] / "data" / "economic" / "nexus_economic_events.csv"
