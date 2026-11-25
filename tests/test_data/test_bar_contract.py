"""T3 (PLAN_MASTER) — اختبارات عقد البيانات.

قبول: replay نفس الحدث → عينة واحدة + bar غير صالح → ValueError + حارس سباق.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.bar_contract import (
    BAR_DEDUP_KEY_TPL,
    BAR_DEDUP_TTL_SEC,
    make_bar_id,
    race_guard_accept,
    validate_ohlcv_bar,
)


GOOD_BAR = {
    "open": 1938.57, "high": 1941.10, "low": 1937.38, "close": 1940.34,
    "volume": 100.0, "timestamp": "2026-11-19T10:00:00+00:00",
}


# ── bar_id ────────────────────────────────────────────────────────────────────

def test_make_bar_id_format():
    bid = make_bar_id("XAUUSD", "M5", datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc))
    assert bid == "XAUUSD:M5:2026-11-19T10:00:00+00:00"


def test_make_bar_id_accepts_iso_string():
    a = make_bar_id("XAUUSD", "M5", "2026-11-19T10:00:00+00:00")
    b = make_bar_id("XAUUSD", "M5", "2026-11-19T10:00:00Z")
    assert a == b  # Z و +00:00 متكافئان


# ── تحقق صارم (ق19.3) ────────────────────────────────────────────────────────

def test_valid_bar_passes():
    assert validate_ohlcv_bar(dict(GOOD_BAR)) == GOOD_BAR


def test_missing_key_raises():
    bad = {k: v for k, v in GOOD_BAR.items() if k != "close"}
    with pytest.raises(ValueError, match="مفاتيح مفقودة"):
        validate_ohlcv_bar(bad)


def test_nonfinite_raises():
    bad = dict(GOOD_BAR, close=float("nan"))
    with pytest.raises(ValueError, match="isfinite"):
        validate_ohlcv_bar(bad)
    bad2 = dict(GOOD_BAR, high=float("inf"))
    with pytest.raises(ValueError, match="isfinite"):
        validate_ohlcv_bar(bad2)


def test_high_below_max_open_close_raises():
    bad = dict(GOOD_BAR, high=1939.0)  # close=1940.34
    with pytest.raises(ValueError, match="high"):
        validate_ohlcv_bar(bad)


def test_low_above_min_open_close_raises():
    bad = dict(GOOD_BAR, low=1939.9)  # open=1938.57
    with pytest.raises(ValueError, match="low"):
        validate_ohlcv_bar(bad)


def test_high_below_low_raises():
    bad = dict(GOOD_BAR, high=1937.0, low=1938.0)
    with pytest.raises(ValueError, match="high"):
        validate_ohlcv_bar(bad)


def test_nonpositive_price_raises():
    with pytest.raises(ValueError):
        validate_ohlcv_bar(dict(GOOD_BAR, open=0.0))


def test_bad_volume_raises():
    with pytest.raises(ValueError, match="volume"):
        validate_ohlcv_bar(dict(GOOD_BAR, volume=-1))


# ── حارس السباق (ق19.4) ──────────────────────────────────────────────────────

def test_race_guard_first_bar_accepted():
    assert race_guard_accept("2026-11-19T10:05:00+00:00", None) is True


def test_race_guard_rejects_older_or_equal():
    last = "2026-11-19T10:05:00+00:00"
    assert race_guard_accept("2026-11-19T10:05:00+00:00", last) is False
    assert race_guard_accept("2026-11-19T10:00:00+00:00", last) is False


def test_race_guard_accepts_newer():
    last = "2026-11-19T10:05:00+00:00"
    newer = datetime.fromisoformat(last) + timedelta(minutes=5)
    assert race_guard_accept(newer, last) is True


# ── dedup stream: SET NX EX 30d ──────────────────────────────────────────────

def test_dedup_key_template():
    bid = make_bar_id("XAUUSD", "M5", "2026-11-19T10:00:00+00:00")
    key = BAR_DEDUP_KEY_TPL.format(bar_id=bid)
    assert key == "nexus:features:bar:XAUUSD:M5:2026-11-19T10:00:00+00:00"
    assert BAR_DEDUP_TTL_SEC == 30 * 24 * 3600


# ── الأحداث تحمل bar_id/bar_timestamp ────────────────────────────────────────

def test_events_carry_bar_id_roundtrip():
    from shared.events import OHLCVEvent, FeatureEvent, parse_event

    bid = make_bar_id("XAUUSD", "M5", "2026-11-19T10:00:00+00:00")
    ev = OHLCVEvent(
        source="nexus-data", symbol="XAUUSD", timeframe="M5",
        open=1, high=2, low=0.5, close=1.5, volume=3,
        bar_id=bid, bar_timestamp="2026-11-19T10:00:00+00:00",
    )
    flat = ev.to_stream_dict()
    assert "bar_id" in flat and flat["bar_id"] == bid
    back = parse_event(flat)
    assert isinstance(back, OHLCVEvent)
    assert back.bar_id == bid

    fe = FeatureEvent(source="x", symbol="XAUUSD", timeframe="M5", bar_id=bid)
    fe_flat = fe.to_stream_dict()
    fe_back = parse_event(fe_flat)
    assert fe_back.bar_id == bid
