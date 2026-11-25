"""Unit tests for shared/risk_sizing — broker-spec-based position sizing."""
import pytest

from shared.risk_sizing import calc_lot, point_value_per_lot, validate_stop_levels

# Realistic OneRoyal-style XAUUSD spec
XAU_SPEC = {
    "point": 0.01,
    "trade_tick_size": 0.01,
    "trade_tick_value": 0.01,      # $0.01 per tick (0.01 price) at 0.01 lot? -> see below
    "trade_contract_size": 100.0,  # 100 oz per lot
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "trade_stops_level": 30,       # in points
    "trade_freeze_level": 10,
}


def test_point_value_from_tick_value():
    # tick_value=1.0 per 0.01 tick at 1 lot → $100 per point (0.01)
    spec = dict(XAU_SPEC, trade_tick_value=1.0)
    assert point_value_per_lot(spec) == pytest.approx(1.0 * (0.01 / 0.01))


def test_point_value_fallback_contract():
    spec = dict(XAU_SPEC)
    spec.pop("trade_tick_value")
    spec.pop("trade_tick_size")
    # 100 oz × $0.01 point = $1 per point per lot
    assert point_value_per_lot(spec) == pytest.approx(1.0)


def test_lot_basic():
    spec = dict(XAU_SPEC, trade_tick_value=1.0)
    # balance 10_000, risk 1% = $100, SL distance $5.00 = 500 points
    # value per point per lot = $1 → 100/500 = 0.2 lots
    r = calc_lot(spec, balance=10_000, risk_pct=0.01, entry=2000.0, sl=1995.0)
    assert r.lot == pytest.approx(0.2)
    assert r.risk_amount == pytest.approx(100.0)


def test_lot_rounds_down_to_step():
    spec = dict(XAU_SPEC, trade_tick_value=1.0)
    # $100 risk, SL 2000→1999.67 = 33 points → 100/33 = 3.0303 → step 0.01
    r = calc_lot(spec, balance=10_000, risk_pct=0.01, entry=2000.0, sl=1999.67)
    assert r.lot == pytest.approx(3.03, abs=0.011)


def test_lot_clamped_to_min():
    spec = dict(XAU_SPEC, trade_tick_value=1.0)
    r = calc_lot(spec, balance=100, risk_pct=0.005, entry=2000.0, sl=1990.0)
    assert r.lot == 0.01
    assert r.clamped == "min"


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        calc_lot(XAU_SPEC, balance=0, risk_pct=0.01, entry=1, sl=2)
    with pytest.raises(ValueError):
        calc_lot(XAU_SPEC, balance=1000, risk_pct=0.01, entry=2000, sl=2000)


def test_validate_stops_buy():
    ok = validate_stop_levels(XAU_SPEC, side="BUY", entry=2000.0, sl=1995.0, tp=2010.0)
    assert ok == []
    bad = validate_stop_levels(XAU_SPEC, side="BUY", entry=2000.0, sl=2001.0, tp=1999.5)
    assert len(bad) == 2


def test_validate_stops_min_distance():
    # stops_level=30 points × 0.01 = $0.30 minimum
    problems = validate_stop_levels(XAU_SPEC, side="SELL", entry=2000.0, sl=2000.1, tp=1999.9)
    assert any("SL distance" in p for p in problems)
    assert any("TP distance" in p for p in problems)


def test_validate_stops_sell_ok():
    ok = validate_stop_levels(XAU_SPEC, side="SELL", entry=2000.0, sl=2001.0, tp=1990.0)
    assert ok == []
