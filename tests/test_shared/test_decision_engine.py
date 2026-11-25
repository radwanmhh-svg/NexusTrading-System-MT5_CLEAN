"""P9 tests — decision engine gates + integration with risk sizing."""
import pytest

import services.nexus_decision as de
from services.nexus_decision import decide, regime_from_features

SPEC = {
    "point": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01,
    "trade_contract_size": 100.0, "volume_min": 0.01, "volume_max": 35.0,
    "volume_step": 0.01, "trade_stops_level": 0, "trade_freeze_level": 0,
    "spread_points": 7,
}


@pytest.fixture(autouse=True)
def _force_adx_fallback(monkeypatch):
    """Deterministic tests: disable the trained LGBM regime model."""
    monkeypatch.setattr(de, "_REGIME_BOOSTER", False)


@pytest.fixture(autouse=True)
def patched_spec(monkeypatch):
    import services.nexus_decision as nd
    monkeypatch.setattr(nd, "SPEC_PATH", nd.Path(__file__).with_name("_spec_tmp.json"))
    nd.Path(__file__).with_name("_spec_tmp.json").write_text(
        __import__("json").dumps({"XAUUSD": SPEC}))
    yield
    nd.Path(__file__).with_name("_spec_tmp.json").unlink(missing_ok=True)


FEATS = {"adx": 30.0, "ema_9": 100.2, "ema_21": 100.0, "rsi": 55.0}


def test_economic_gate_blocks_first():
    d = decide(prob=0.9, features=FEATS,
               economic_features={"event_in_window": 1, "next_event_importance": 3},
               entry=100.0, atr=1.0)
    assert d.action == "NO_TRADE"
    assert "economic" in d.reason


def test_regime_gate_blocks_ranging():
    d = decide(prob=0.9, features={"adx": 10.0},
               economic_features={"event_in_window": 0},
               entry=100.0, atr=1.0)
    assert d.action == "NO_TRADE" and "regime" in d.reason


def test_conviction_gate_blocks_low_prob():
    d = decide(prob=0.55, features=FEATS,
               economic_features={"event_in_window": 0},
               entry=100.0, atr=1.0)
    assert d.action == "NO_TRADE" and "threshold" in d.reason


def test_pass_produces_sized_buy():
    d = decide(prob=0.70, features=FEATS,
               economic_features={"event_in_window": 0},
               balance=10_000.0, entry=100.0, atr=1.0)
    assert d.action == "BUY"
    assert d.lot == pytest.approx(1.0)    # $100 risk / (100 pts × $1/pt/lot)
    assert d.sl < d.entry < d.tp
    assert d.gate_trace["sizing"]["risk"] == pytest.approx(100.0)


def test_regime_fallback_adx():
    assert regime_from_features({"adx": 25.0}) == "TRENDING"
    assert regime_from_features({"adx": 12.0}) == "RANGING"


def test_daily_loss_kill_switch(monkeypatch):
    monkeypatch.setattr(de, "_REGIME_BOOSTER", False)
    d = decide(prob=0.9, features={"adx": 30.0},
               economic_features={"event_in_window": 0, "daily_loss_pct": 20.0},
               entry=100.0, atr=1.0)
    assert d.action == "NO_TRADE" and "kill-switch" in d.reason


def test_open_positions_cap(monkeypatch):
    monkeypatch.setattr(de, "_REGIME_BOOSTER", False)
    d = decide(prob=0.9, features={"adx": 30.0},
               economic_features={"event_in_window": 0, "open_positions": 5},
               entry=100.0, atr=1.0)
    assert d.action == "NO_TRADE" and "open positions" in d.reason
