"""
Decision Engine (P9/T9.1) — separates PREDICTION from DECISION.

The model's probability never becomes a trade by itself. Every signal passes
three independent gates, each with a documented reason:

1. ECONOMIC gate  — MT5-calendar HIGH event inside blackout window → NO_TRADE
2. REGIME gate    — trade only when the market state allows the strategy
                    (LGBM regime model when loadable, ADX fallback otherwise)
3. CONVICTION gate— model probability >= threshold (calibrated, env-tunable)

On PASS, position sizing and SL/TP come from the REAL broker spec via
shared.risk_sizing (calc_lot + validate_stop_levels).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.risk_sizing import calc_lot, validate_stop_levels

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "config" / "broker_specs.json"

CONF_THRESHOLD = float(os.getenv("NEXUS_DECISION_MIN_PROB", "0.60"))
REGIME_MODE = os.getenv("NEXUS_DECISION_REGIME_MODE", "trending_only")  # trending_only | any
TRENDING_MIN_ADX = float(os.getenv("NEXUS_DECISION_TRENDING_ADX", "20"))
RISK_PCT = float(os.getenv("NEXUS_DECISION_RISK_PCT", "0.01"))

# Portfolio risk caps (I-007) — defaults aligned with config/risk_rules.yaml;
# runtime values may be tightened via env.
DAILY_LOSS_LIMIT_PCT = float(os.getenv("NEXUS_DECISION_DAILY_LOSS_LIMIT_PCT", "20.0"))
MAX_OPEN_POSITIONS = int(os.getenv("NEXUS_DECISION_MAX_OPEN_POSITIONS", "5"))

_REGIME_BOOSTER = None


@dataclass
class Decision:
    action: str                  # BUY | NO_TRADE
    reason: str
    gate_trace: dict[str, Any] = field(default_factory=dict)
    lot: float = 0.0
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0


def regime_from_features(features: dict[str, float]) -> str:
    """TRENDING / RANGING classification from the production indicator row.

    Uses the trained LGBM regime model when loadable; deterministic ADX
    fallback otherwise (documented, tested).
    """
    global _REGIME_BOOSTER
    try:
        if _REGIME_BOOSTER is None:
            import lightgbm as lgbm
            model_dir = ROOT / "models_store" / "XAUUSD"
            if (model_dir / "lgbm_regime.txt").exists():
                _REGIME_BOOSTER = lgbm.Booster(model_file=str(model_dir / "lgbm_regime.txt"))
        if _REGIME_BOOSTER is not None:
            import numpy as np
            names = json.loads((ROOT / "models_store" / "XAUUSD" / "feature_names.json").read_text())
            row = np.array([[float(features.get(n, 0.0) or 0.0) for n in names]], dtype=np.float32)
            cls = int(_REGIME_BOOSTER.predict(row)[0].argmax()) if _REGIME_BOOSTER.predict(row).ndim > 1 else int(_REGIME_BOOSTER.predict(row)[0])
            return "TRENDING" if cls == 0 else "RANGING"
    except Exception:
        pass
    # Fallback: ADX strength (documented in DECISIONS)
    adx = float(features.get("adx", 0.0) or 0.0)
    return "TRENDING" if adx >= TRENDING_MIN_ADX else "RANGING"


def decide(
    *,
    prob: float,
    features: dict[str, float],
    economic_features: dict[str, Any] | None = None,
    symbol: str = "XAUUSD",
    balance: float = 10_000.0,
    entry: float = 0.0,
    atr: float = 0.0,
    conf_threshold: float | None = None,
) -> Decision:
    """Full gate pipeline. Returns Decision (NO_TRADE carries the blocking reason)."""
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))[symbol]
    trace: dict[str, Any] = {}
    th = conf_threshold if conf_threshold is not None else CONF_THRESHOLD

    # Gate 1 — economic calendar (leakage-safe local reader)
    econ = economic_features or {}
    if econ.get("event_in_window", 0):
        trace["economic"] = {"pass": False, "detail": econ}
        return Decision("NO_TRADE", f"economic event within blackout window (importance={econ.get('next_event_importance')})", trace)
    trace["economic"] = {"pass": True}

    # Gate 2 — regime
    regime = regime_from_features(features)
    trace["regime"] = {"regime": regime}
    if REGIME_MODE == "trending_only" and regime != "TRENDING":
        return Decision("NO_TRADE", f"regime={regime} not tradable (mode={REGIME_MODE})", trace)

    # Gate 3 — conviction
    trace["conviction"] = {"prob": round(prob, 4), "threshold": th}
    if prob < th:
        return Decision("NO_TRADE", f"prob {prob:.3f} < threshold {th}", trace)

    # Gate 4 — portfolio/risk caps (I-007): daily loss + concurrent exposure
    daily_loss = float(econ.get("daily_loss_pct", 0.0) or 0.0)
    open_positions = int(econ.get("open_positions", 0) or 0)
    trace["risk_caps"] = {"daily_loss_pct": daily_loss, "open_positions": open_positions}
    if daily_loss >= DAILY_LOSS_LIMIT_PCT:
        return Decision("NO_TRADE", f"daily loss {daily_loss}% >= limit {DAILY_LOSS_LIMIT_PCT}% (kill-switch)", trace)
    if open_positions >= MAX_OPEN_POSITIONS:
        return Decision("NO_TRADE", f"open positions {open_positions} >= max {MAX_OPEN_POSITIONS}", trace)

    # Sizing from the REAL spec
    if entry <= 0 or atr <= 0:
        return Decision("NO_TRADE", "missing entry/atr for sizing", trace)
    sl = entry - 1.0 * atr
    tp = entry + 1.5 * atr
    try:
        sizing = calc_lot(spec, balance=balance, risk_pct=RISK_PCT, entry=entry, sl=sl)
    except ValueError as exc:
        return Decision("NO_TRADE", f"sizing rejected: {exc}", trace)
    problems = validate_stop_levels(spec, side="BUY", entry=entry, sl=sl, tp=tp)
    if problems:
        return Decision("NO_TRADE", "stops rejected: " + "; ".join(problems), trace)

    trace["sizing"] = {"lot": sizing.lot, "risk": sizing.risk_amount}
    return Decision("BUY", "all gates passed", trace, lot=sizing.lot, entry=entry, sl=sl, tp=tp)
