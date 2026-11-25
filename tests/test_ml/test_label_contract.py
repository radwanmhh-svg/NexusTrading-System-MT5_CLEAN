"""T4 (PLAN_MASTER) — اختبارات عقد Labels بصيغة ق19.7.

future_timestamp > timestamp + future_return صحيح + استبعاد آخر HORIZON +
رفض التدريب عند فئة تحت الحد الأدنى.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from shared.label_contract import (
    LABEL_HORIZONS_BARS,
    assert_class_minimum,
    drop_last_horizon,
    future_label,
    future_return,
    future_timestamp_ok,
    label_distribution,
    validate_labeled_record,
)


def _rec(label=1, close=100.0, future_close=101.0):
    return {
        "close": close,
        "future_close": future_close,
        "label": label,
        "timestamp": "2026-11-19T10:00:00+00:00",
        "future_timestamp": "2026-11-19T10:05:00+00:00",
    }


# ── ق19.7: future_return والاتجاه ────────────────────────────────────────────

def test_future_return_formula():
    assert future_return(100.0, 101.0) == pytest.approx(0.01)
    assert future_return(200.0, 198.0) == pytest.approx(-0.01)


def test_future_return_nonpositive_prev_raises():
    with pytest.raises(ValueError):
        future_return(0.0, 101.0)


def test_future_label_direction():
    assert future_label(100.0, 101.0) == 1
    assert future_label(100.0, 99.0) == 0
    assert future_label(100.0, 100.0) == 0  # تساوي = 0 (لا صعود)


def test_validate_labeled_record_ok():
    validate_labeled_record(_rec(label=1, close=100.0, future_close=101.0))
    validate_labeled_record(_rec(label=0, close=100.0, future_close=99.0))


def test_validate_labeled_record_wrong_direction_raises():
    with pytest.raises(ValueError, match="لا يطابق"):
        validate_labeled_record(_rec(label=1, close=100.0, future_close=99.0))


def test_validate_labeled_record_missing_fields_raises():
    with pytest.raises(ValueError, match="ناقص"):
        validate_labeled_record({"close": 1.0, "label": 1})


def test_future_timestamp_strictly_greater():
    ts = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    assert future_timestamp_ok(ts, ts + timedelta(seconds=1)) is True
    assert future_timestamp_ok(ts, ts) is False            # تساوي = رفض
    assert future_timestamp_ok(ts, ts - timedelta(days=1)) is False


# ── استبعاد آخر HORIZON ──────────────────────────────────────────────────────

def test_drop_last_horizon_documented_default():
    assert LABEL_HORIZONS_BARS["live_history"] == 1
    assert drop_last_horizon(list(range(10))) == 9


def test_drop_last_horizon_custom():
    assert drop_last_horizon(list(range(10)), horizon=3) == 7


def test_drop_last_horizon_too_short_raises():
    with pytest.raises(ValueError):
        drop_last_horizon(list(range(2)), horizon=3)


# ── التوزيع + رفض التدريب عند فئة تحت الحد الأدنى ────────────────────────────

def test_label_distribution():
    dist = label_distribution(np.array([1, 1, 0, 1, 0]))
    assert dist["up"] == 3 and dist["down"] == 2 and dist["total"] == 5
    assert dist["up_ratio"] == pytest.approx(0.6)


def test_assert_class_minimum_rejects_skew():
    y = np.array([1] * 400 + [0] * 4)
    with pytest.raises(ValueError, match="الحد الأدنى"):
        assert_class_minimum(y, min_samples=500)


def test_assert_class_minimum_accepts_balanced():
    y = np.array([1] * 600 + [0] * 600)
    dist = assert_class_minimum(y, min_samples=500)
    assert dist["total"] == 1200


def test_assert_class_minimum_empty_raises():
    with pytest.raises(ValueError, match="لا توجد عينات"):
        assert_class_minimum(np.array([]))
