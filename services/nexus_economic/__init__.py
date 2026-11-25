"""
nexus-economic — Python reader for the MQL5 calendar export.

Consumes the CSV produced by mql5/economic_calendar_export.mq5 and provides
leakage-safe access to economic events:

- parse the export schema (raw + normalized ×1e6 values, empty string = N/A)
- point-in-time API: ``events_available_at(ts)`` returns ONLY events whose
  release time has passed AND whose actual value was already fetched at the
  system's decision time (uses fetched_at_utc, never the latest revision)
- feature builders for ML: minutes_to_event, surprise, etc.

Leakage rules (master prompt §5, mandatory):
- never read actual before the event time
- never use revised_previous as if it were known historically; forecasts and
  previous values are only valid if they pre-date the decision time
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

COLUMNS = [
    "event_id", "event_name", "country", "currency", "importance",
    "time_broker", "time_utc", "server_offset_sec",
    "actual_raw", "actual", "forecast_raw", "forecast",
    "previous_raw", "previous", "revised_previous_raw", "revised_previous",
    "change_id", "fetched_at_utc", "source",
]

IMPORTANCE_ORDER = {"NONE": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3}

DEFAULT_CSV = Path(os.getenv(
    "NEXUS_ECONOMIC_CSV",
    Path(__file__).resolve().parents[2] / "data" / "economic" / "nexus_economic_events.csv",
))


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_num(value: str) -> float | None:
    """Empty string means value was NOT available (LONG_MIN in MQL5) — never zero."""
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class EconomicEvent:
    event_id: str
    event_name: str
    currency: str
    importance: str
    time_utc: datetime
    time_broker: datetime | None
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    revised_previous: float | None = None
    fetched_at_utc: datetime | None = None
    source: str = "MT5-CALENDAR"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def importance_rank(self) -> int:
        return IMPORTANCE_ORDER.get(self.importance.upper(), 0)

    def surprise_vs_forecast(self) -> float | None:
        """actual − forecast; None when either side unavailable (never 0)."""
        if self.actual is None or self.forecast is None:
            return None
        return self.actual - self.forecast

    def surprise_vs_previous(self) -> float | None:
        if self.actual is None or self.previous is None:
            return None
        return self.actual - self.previous


def load_events(csv_path: Path | str | None = None) -> list[EconomicEvent]:
    """Parse the MQL5 export CSV into EconomicEvent objects."""
    path = Path(csv_path) if csv_path else DEFAULT_CSV
    if not path.exists():
        return []
    events: list[EconomicEvent] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row.get("event_id"):
                continue
            events.append(EconomicEvent(
                event_id=str(row.get("event_id", "")),
                event_name=row.get("event_name", ""),
                currency=row.get("currency", ""),
                importance=(row.get("importance") or "NONE").upper(),
                time_utc=_parse_dt(row.get("time_utc") or "") or datetime.max.replace(tzinfo=timezone.utc),
                time_broker=_parse_dt(row.get("time_broker") or ""),
                actual=_parse_num(row.get("actual")),
                forecast=_parse_num(row.get("forecast")),
                previous=_parse_num(row.get("previous")),
                revised_previous=_parse_num(row.get("revised_previous")),
                fetched_at_utc=_parse_dt(row.get("fetched_at_utc") or ""),
                source=row.get("source", ""),
            ))
    events.sort(key=lambda e: e.time_utc)
    return events


def events_available_at(events: Iterable[EconomicEvent], decision_ts: datetime) -> list[EconomicEvent]:
    """Point-in-time filter: what the system could legitimately know.

    - release time must have passed (actual not visible before it)
    - the row must have been fetched before decision_ts (fetched_at_utc)
    - rows without an actual value are knowledge of *existence + forecast only*
    """
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.replace(tzinfo=timezone.utc)
    out: list[EconomicEvent] = []
    for e in events:
        if e.time_utc > decision_ts:
            continue
        if e.fetched_at_utc and e.fetched_at_utc > decision_ts:
            continue
        out.append(e)
    return out


def next_upcoming(events: Iterable[EconomicEvent], now: datetime, within_minutes: int = 120) -> list[EconomicEvent]:
    """Future events inside the risk window (news-sentinel usage)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out = []
    for e in events:
        dt = (e.time_utc - now).total_seconds() / 60.0
        if 0 <= dt <= within_minutes:
            out.append(e)
    return out


def build_features(
    events: Iterable[EconomicEvent],
    *,
    now: datetime,
    lookback_minutes: int = 240,
    ahead_minutes: int = 120,
) -> dict[str, Any]:
    """Leakage-safe feature vector for the decision pipeline at `now`."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    feats: dict[str, Any] = {
        "minutes_to_next_event": None,
        "next_event_importance": 0,
        "event_in_window": 0,
    }
    upcoming = sorted(next_upcoming(events, now, ahead_minutes), key=lambda e: e.time_utc)
    if upcoming:
        nxt = upcoming[0]
        feats["minutes_to_next_event"] = (nxt.time_utc - now).total_seconds() / 60.0
        feats["next_event_importance"] = nxt.importance_rank
        feats["event_in_window"] = 1 if nxt.importance_rank >= 2 else 0

    recent = [
        e for e in events_available_at(events, now)
        if e.actual is not None and 0 <= (now - e.time_utc).total_seconds() / 60.0 <= lookback_minutes
    ]
    feats["recent_high_impact_count"] = sum(1 for e in recent if e.importance_rank >= 3)
    surprises = [e.surprise_vs_forecast() for e in recent if e.surprise_vs_forecast() is not None]
    feats["recent_surprise_sum"] = sum(surprises) if surprises else 0.0
    return feats
