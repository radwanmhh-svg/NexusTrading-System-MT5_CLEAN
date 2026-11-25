"""B-01 — Event-driven M5 backtest engine.

Per closed M5 bar:
  features (300-bar closed window, production modules)
    → signal (pluggable; default = the live fallback RuleBasedSignalGenerator)
    → `decide()` from services.nexus_decision (the SAME live function)
    → order sized by shared.risk_sizing (the same production sizer)
    → filled at the NEXT bar open with directive costs:
        spread 25 points (fixed, configurable) + 1-pt slippage + commission
  positions are managed bar-by-bar; intrabar SL and TP both touched → SL first
  (conservative, matches the P6 label policy).

Cost model (MT5 bid candles — candle prices are bid):
- BUY: enters at ask = open_next + spread + slippage; exits at bid.
- SELL: enters at bid = open_next - slippage; exits at ask = bid + spread.
Each side pays the full spread exactly once per round trip.
Commission defaults to 0 (the real MT5 deals for XAUUSDs carry 0), configurable.

Honest notes:
- `decide()` is long-only today (returns action BUY). The engine uses it for
  the full gate chain (economic / regime / conviction / caps) and sizes the
  actual order side with the production sizer. The long-only asymmetry is a
  documented audit finding, not engine logic duplication.
- portfolio caps inside decide() (daily loss / open positions) stay neutral
  in backtest (single position engine) — documented.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from shared.logger import get_logger
from shared.metrics import max_drawdown

logger = get_logger("backtest.engine")

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "config" / "broker_specs.json"

SPREAD_PIPS = float(os.getenv("NEXUS_BACKTEST_SPREAD_PIPS", "25"))    # directive: fixed 25 pts
SLIPPAGE_PIPS = float(os.getenv("NEXUS_BACKTEST_SLIPPAGE_PIPS", "1"))  # directive: 1-pt slippage
COMMISSION_PER_LOT = float(os.getenv("NEXUS_BACKTEST_COMMISSION_PER_LOT", "0.0"))
PIP_SIZE = 0.01            # D-04 unified pip definition (config/assets.yaml)
PIP_VALUE_PER_LOT = 1.0    # $ per pip per 1.0 lot (broker spec / MT5 tick value)

INITIAL_BALANCE = float(os.getenv("NEXUS_BACKTEST_INITIAL_BALANCE", "100000.0"))

SL_ATR_MULT = 1.0          # same constants as decide()
TP_ATR_MULT = 1.5
COOLDOWN_BARS = 1          # R-04 spirit: ≥1 bar between entries, same symbol


@dataclass
class BacktestTrade:
    side: str                  # BUY | SELL
    signal_time: pd.Timestamp  # closed bar that produced the signal
    entry_time: pd.Timestamp   # fill time (next bar open)
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    sl: float
    tp: float
    lot: float
    gross_pnl: float           # price movement before commission
    costs: float               # spread value + commission
    net_pnl: float
    exit_reason: str           # SL | TP | end_of_data
    signal_prob: float


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    initial_balance: float = INITIAL_BALANCE
    bars_evaluated: int = 0
    signals: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        trades = self.trades
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        curve = [self.initial_balance] + self.equity_curve
        return {
            "bars_evaluated": self.bars_evaluated,
            "signals": self.signals,
            "trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "net_pnl": round(sum(t.net_pnl for t in trades), 2),
            "total_costs": round(sum(t.costs for t in trades), 2),
            "max_drawdown": max_drawdown(curve),
            "initial_balance": self.initial_balance,
            "final_balance": round(curve[-1], 2) if curve else self.initial_balance,
            "rejected_by_gate": self.rejected,
        }


def load_spec(symbol: str = "XAUUSD") -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))[symbol]


class BacktestEngine:
    """Event-driven engine. `signal_fn(features) -> (direction, prob)`.

    Default signal_fn = RuleBasedSignalGenerator.predict — the live fallback
    used when models are unavailable (identical behaviour by construction).
    """

    def __init__(
        self,
        signal_fn: Callable[[dict], tuple[str, float]] | None = None,
        spread_pips: float = SPREAD_PIPS,
        slippage_pips: float = SLIPPAGE_PIPS,
        commission_per_lot: float = COMMISSION_PER_LOT,
        initial_balance: float = INITIAL_BALANCE,
        cooldown_bars: int = COOLDOWN_BARS,
        symbol: str = "XAUUSD",
        prob_min: float | None = None,
    ) -> None:
        self.signal_fn = signal_fn or self._make_default_signal_fn()
        self.spread = spread_pips * PIP_SIZE
        self.slippage = slippage_pips * PIP_SIZE
        self.commission_per_lot = commission_per_lot
        self.balance = initial_balance
        self.cooldown_bars = cooldown_bars
        self.symbol = symbol
        self.spec = load_spec(symbol)
        self.prob_min = prob_min  # None → decide()'s own env default

    @staticmethod
    def _make_default_signal_fn() -> Callable[[dict], tuple[str, float]]:
        from services.nexus_ml.rule_based_signal import RuleBasedSignalGenerator
        gen = RuleBasedSignalGenerator()

        def fn(features: dict) -> tuple[str, float]:
            sig = gen.predict(features)
            return sig.direction, float(sig.confidence)

        return fn

    # ── main loop ────────────────────────────────────────────────────────

    @staticmethod
    def _pin_dead_regime_stub() -> None:
        """Pin a dead-model stub so regime_from_features executes its OWN ADX
        fallback — the exact path live executes since B-03 (the trained LGBM
        regime model fails shape-check 43≠41 on every live call). Without the
        stub the engine would retry the dead model 85k times (50ms+ each).
        No decision logic is duplicated: the fallback lives inside the live
        module and runs unchanged."""
        import services.nexus_decision as _nd

        class _DeadRegimeModel:
            def predict(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise RuntimeError("regime LGBM dead in live (43≠41) — ADX fallback")

        if not isinstance(_nd._REGIME_BOOSTER, _DeadRegimeModel):
            _nd._REGIME_BOOSTER = _DeadRegimeModel()

    def run(
        self,
        df: pd.DataFrame,
        start_idx: int = 0,
        end_idx: int | None = None,
        feature_window: int = 300,
        on_bar: Callable[[int, dict], None] | None = None,
        lean_features: bool = False,
        resume_state: dict | None = None,
        close_at_end: bool = True,
    ) -> BacktestResult:
        """Signals fire on the just-closed bar i; fills happen at bar i+1 open.

        lean_features=False (default): full live feature vector (300-bar window
        exactly like the live publisher). lean_features=True is a verified
        non-equivalent reduced vector — kept only for quick diagnostics.
        """
        from services.nexus_backtest.features import compute_features_at
        from services.nexus_decision import decide
        self._pin_dead_regime_stub()

        n = len(df) if end_idx is None else min(end_idx, len(df))
        res = BacktestResult(initial_balance=self.balance)
        ts = df["timestamp"]
        pos: dict | None = None
        last_entry_bar = -(10**9)
        if resume_state:
            # chunked checkpointed runs: carry state across chunk boundaries
            self.balance = resume_state["balance"]
            last_entry_bar = resume_state["last_entry_bar"]
            res.equity_curve = list(resume_state["equity_curve"])
            res.trades = [
                BacktestTrade(**{**t,
                    "signal_time": pd.Timestamp(t["signal_time"]),
                    "entry_time": pd.Timestamp(t["entry_time"]),
                    "exit_time": pd.Timestamp(t["exit_time"]),
                })
                for t in resume_state["trades"]
            ]
            pos = resume_state.get("pos")
            if pos and isinstance(pos.get("entry_time"), str):
                pos = {**pos, "entry_time": pd.Timestamp(pos["entry_time"]),
                       "signal_time": pd.Timestamp(pos["signal_time"])}
            res.bars_evaluated = resume_state.get("bars_evaluated", 0)
            res.signals = resume_state.get("signals", 0)
            res.rejected = resume_state.get("rejected", {})
            res.initial_balance = resume_state.get("true_initial_balance", self.balance)

        for i in range(start_idx, n - 1):  # bar i+1 must exist to fill
            # 1) manage the open position against bar i's prices
            if pos is not None and self._manage_position(pos, i, df, ts, res):
                pos = None

            # 2) evaluate a signal on the just-closed bar i
            feats = compute_features_at(df, i, window=feature_window, lean=lean_features)
            res.bars_evaluated += 1
            if feats is None:
                # live publisher behaviour: insufficient bars → no features, no signal
                res.rejected["insufficient_features"] = res.rejected.get("insufficient_features", 0) + 1
                continue
            if on_bar:
                on_bar(i, feats)

            if pos is not None or i - last_entry_bar < self.cooldown_bars:
                continue

            direction, prob = self.signal_fn(feats)
            if direction not in ("BUY", "SELL"):
                res.rejected["signal_no_trade"] = res.rejected.get("signal_no_trade", 0) + 1
                continue
            res.signals += 1

            # 3) the SAME live decision function gates the signal
            entry_ref = float(df["open"].iloc[i + 1])
            atr = float(feats.get("atr_14") or feats.get("atr") or 0.0)
            kwargs: dict = {}
            if self.prob_min is not None:
                kwargs["conf_threshold"] = self.prob_min
            decision = decide(
                prob=prob,
                features=feats,
                economic_features=feats,
                symbol=self.symbol,
                balance=self.balance,
                entry=entry_ref,
                atr=atr,
                **kwargs,
            )
            if decision.action != "BUY":
                reason = decision.reason
                # aggregate: conviction rejections differ only by the prob value
                if reason.startswith("prob"):
                    reason = "conviction_below_threshold"
                else:
                    reason = reason.split("(")[0].strip()[:60]
                res.rejected[reason] = res.rejected.get(reason, 0) + 1
                continue

            # 4) size with the production sizer for the actual order side
            sizing = self._size(direction, entry_ref, atr)
            if sizing is None:
                res.rejected["sizing_rejected"] = res.rejected.get("sizing_rejected", 0) + 1
                continue

            pos = {
                "side": direction,
                "signal_bar": i,
                "signal_time": ts.iloc[i],
                "entry_bar": i + 1,
                "entry_time": ts.iloc[i + 1],
                "entry_price": self._fill_price(direction, entry_ref, entering=True),
                "sl": sizing["sl"],
                "tp": sizing["tp"],
                "lot": sizing["lot"],
                "prob": prob,
            }
            last_entry_bar = i

        if close_at_end and pos is not None:
            # manage the final bar intrabar first (honest): an SL hit inside the
            # last bar must exit as SL, not as a mark-to-market close.
            if not self._manage_position(pos, n - 1, df, ts, res):
                self._close_position(pos, n - 1, float(df["close"].iloc[n - 1]),
                                     ts.iloc[n - 1], "end_of_data", res)
            pos = None
        res.last_entry_bar = last_entry_bar
        res._open_pos = pos
        return res

    def state(self, res: BacktestResult, pos: dict | None, last_entry_bar: int) -> dict:
        """Serializable run state for chunked checkpointed runs."""
        return {
            "true_initial_balance": res.initial_balance,
            "balance": self.balance,
            "last_entry_bar": last_entry_bar,
            "equity_curve": res.equity_curve,
            "trades": [
                {**t.__dict__,
                 "signal_time": t.signal_time.isoformat(),
                 "entry_time": t.entry_time.isoformat(),
                 "exit_time": t.exit_time.isoformat()}
                for t in res.trades
            ],
            "bars_evaluated": res.bars_evaluated,
            "signals": res.signals,
            "rejected": res.rejected,
            "pos": (
                {**pos, "entry_time": pos["entry_time"].isoformat(),
                 "signal_time": pos["signal_time"].isoformat()}
                if pos else None
            ),
        }

    # ── fills & costs ────────────────────────────────────────────────────

    def _fill_price(self, direction: str, open_ref: float, entering: bool) -> float:
        if direction == "BUY":
            return open_ref + self.spread + self.slippage
        return open_ref - self.slippage  # SELL enters at bid; spread paid at exit

    def _size(self, direction: str, entry: float, atr: float) -> dict | None:
        from shared.risk_sizing import calc_lot, validate_stop_levels
        from services.nexus_decision import RISK_PCT

        if atr <= 0 or entry <= 0:
            return None
        sl = entry - SL_ATR_MULT * atr if direction == "BUY" else entry + SL_ATR_MULT * atr
        tp = entry + TP_ATR_MULT * atr if direction == "BUY" else entry - TP_ATR_MULT * atr
        try:
            sizing = calc_lot(self.spec, balance=self.balance, risk_pct=RISK_PCT, entry=entry, sl=sl)
        except ValueError:
            return None
        problems = validate_stop_levels(self.spec, side=direction, entry=entry, sl=sl, tp=tp)
        if problems:
            return None
        return {"lot": sizing.lot, "sl": sl, "tp": tp}

    def _manage_position(
        self, pos: dict, bar: int, df: pd.DataFrame, ts: pd.Series, res: BacktestResult
    ) -> bool:
        """Intrabar SL/TP on bar `bar` (bid prices). SL-first when both touched."""
        o = float(df["open"].iloc[bar])
        hi = float(df["high"].iloc[bar])
        lo = float(df["low"].iloc[bar])
        side = pos["side"]
        if side == "BUY":
            hit_sl = lo <= pos["sl"]
            hit_tp = hi >= pos["tp"]
            if hit_sl:
                self._close_position(pos, bar, min(o, pos["sl"]), ts.iloc[bar], "SL", res)
                return True
            if hit_tp:
                self._close_position(pos, bar, max(o, pos["tp"]), ts.iloc[bar], "TP", res)
                return True
        else:
            hit_sl = hi >= pos["sl"]
            hit_tp = lo <= pos["tp"]
            if hit_sl:
                # SELL exits at ask = bid trigger + spread
                self._close_position(pos, bar, max(o, pos["sl"]) + self.spread,
                                     ts.iloc[bar], "SL", res)
                return True
            if hit_tp:
                self._close_position(pos, bar, min(o, pos["tp"]) + self.spread,
                                     ts.iloc[bar], "TP", res)
                return True
        return False

    def _close_position(
        self,
        pos: dict,
        bar: int,
        exit_ref: float,
        exit_time: pd.Timestamp,
        reason: str,
        res: BacktestResult,
    ) -> None:
        """exit_ref is the bid-side price for longs; already-ask price for shorts."""
        side = pos["side"]
        lot = pos["lot"]
        if side == "BUY":
            gross = (exit_ref - pos["entry_price"]) / PIP_SIZE * PIP_VALUE_PER_LOT * lot
        else:
            gross = (pos["entry_price"] - exit_ref) / PIP_SIZE * PIP_VALUE_PER_LOT * lot
        commission = self.commission_per_lot * lot
        spread_value = self.spread / PIP_SIZE * PIP_VALUE_PER_LOT * lot
        net = gross - commission
        self.balance += net
        res.trades.append(BacktestTrade(
            side=side,
            signal_time=pos["signal_time"],
            entry_time=pos["entry_time"],
            entry_price=round(pos["entry_price"], 5),
            exit_time=exit_time,
            exit_price=round(exit_ref, 5),
            sl=round(pos["sl"], 5),
            tp=round(pos["tp"], 5),
            lot=lot,
            gross_pnl=round(gross, 2),
            costs=round(spread_value + commission, 2),
            net_pnl=round(net, 2),
            exit_reason=reason,
            signal_prob=pos["prob"],
        ))
        res.equity_curve.append(round(self.balance, 2))
