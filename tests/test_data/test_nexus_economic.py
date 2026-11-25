"""Tests for nexus-economic reader: parsing, point-in-time leakage safety, features."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.nexus_economic import (
    EconomicEvent,
    build_features,
    events_available_at,
    load_events,
    next_upcoming,
)

SAMPLE_CSV = '''event_id,event_name,country,currency,importance,time_broker,time_utc,server_offset_sec,actual_raw,actual,forecast_raw,forecast,previous_raw,previous,revised_previous_raw,revised_previous,change_id,fetched_at_utc,source
1001,CPI m/m,United States,USD,HIGH,2026.11.12 15:30:00,2026-11-12 12:30:00,10800,3000000,3.0,2900000,2.9,3100000,3.1,0,,
1002,FOMC Rate Decision,United States,USD,HIGH,2026.11.15 21:00:00,2026-11-15 18:00:00,10800,3500000,3.5,3500000,3.5,3500000,3.5,0,,
1003,Jobless Claims,United States,USD,MODERATE,2026.11.13 15:30:00,2026-11-13 12:30:00,10800,-9223372036854775808,,-9223372036854775808,,310000,0.31,0,,
1004,Retail Sales,United States,USD,LOW,2026.11.16 14:00:00,2026-11-16 11:00:00,10800,,,2900000,2.9,0,,
'''


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "nexus_economic_events.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    return p


def test_load_parses_and_sorts(csv_file: Path):
    events = load_events(csv_file)
    assert len(events) == 4
    times = [e.time_utc for e in events]
    assert times == sorted(times)
    cpi = events[0]
    assert cpi.event_name == "CPI m/m" and cpi.importance == "HIGH"
    assert cpi.actual == 3.0 and cpi.forecast == 2.9 and cpi.previous == 3.1


def test_unavailable_value_is_none_not_zero(csv_file: Path):
    events = load_events(csv_file)
    claims = [e for e in events if e.event_name == "Jobless Claims"][0]
    assert claims.actual is None          # LONG_MIN encoded → empty → None
    assert claims.forecast is None
    assert claims.previous == pytest.approx(0.31)


def test_point_in_time_no_future_leak(csv_file: Path):
    events = load_events(csv_file)
    # decision at 2026-11-13 13:00 UTC: FOMC (Nov 15) & Retail (Nov 16) must be invisible
    decision = datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc)
    visible = events_available_at(events, decision)
    names = [e.event_name for e in visible]
    assert "FOMC Rate Decision" not in names
    assert "Retail Sales" not in names
    assert "CPI m/m" in names


def test_point_in_time_respects_fetched_at(csv_file: Path):
    events = load_events(csv_file)
    # same instant but pretend fetch happened later → nothing available yet
    for e in events:
        e.fetched_at_utc = datetime(2026, 11, 20, tzinfo=timezone.utc)
    visible = events_available_at(events, datetime(2026, 11, 13, 13, 0, tzinfo=timezone.utc))
    assert visible == []


def test_next_upcoming_risk_window(csv_file: Path):
    events = load_events(csv_file)
    now = datetime(2026, 11, 15, 17, 30, tzinfo=timezone.utc)
    up = next_upcoming(events, now, within_minutes=60)
    assert [e.event_name for e in up] == ["FOMC Rate Decision"]


def test_build_features_before_fomc(csv_file: Path):
    events = load_events(csv_file)
    now = datetime(2026, 11, 15, 17, 30, tzinfo=timezone.utc)  # FOMC in 30 min
    f = build_features(events, now=now)
    assert f["event_in_window"] == 1
    assert f["minutes_to_next_event"] == pytest.approx(30.0)
    assert f["next_event_importance"] == 3


def test_build_features_after_cpi(csv_file: Path):
    events = load_events(csv_file)
    now = datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)  # CPI 30 min earlier
    f = build_features(events, now=now)
    assert f["recent_high_impact_count"] == 1
    # surprise = actual 3.0 − forecast 2.9
    assert f["recent_surprise_sum"] == pytest.approx(0.1)
