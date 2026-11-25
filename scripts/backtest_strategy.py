"""
P8/T8.1 — strategy-level backtest on out-of-sample fold predictions.

Pipeline (same data path as the live system):
    DuckDB H1 candles → calculate_all → triple-barrier labels (cost = real spread)

For each monthly walk-forward fold:
    train XGBoost, predict probabilities on the OUT-OF-SAMPLE month
    trade every signal with prob >= threshold:
        entry price = close[signal] + half_spread (paying from broker spec)
        SL/TP from ATR barriers (same as labels) — LOTS sized by risk from
        the REAL broker spec (nexus value per point)
        exit: TP first → +R, SL first → −R, ambiguous bar → −R (conservative),
              timeout → close at market, still paying cost
Metrics: Profit Factor, Expectancy, Max Drawdown, Sharpe (per-trade), trades.

Usage:
    .venv/Scripts/python.exe scripts/backtest_strategy.py [--symbol XAUUSD] [--horizon 60]
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from services.nexus_features.indicators import calculate_all  # noqa: E402
from services.nexus_ml.labels import triple_barrier_labels  # noqa: E402

DB_PATH = ROOT / "data" / "nexus.duckdb"
SPEC_PATH = ROOT / "config" / "broker_specs.json"


def load_candles(symbol: str, timeframe: str = "H1") -> pd.DataFrame:
    # FIX (2026-11-22): nexus-data يفتح nexus.duckdb بحق حصري على Windows —
    # لا قراءة ولا نسخة ممكنة أثناء التشغيل (PermissionError حتى على open للقراءة).
    # نجلب الشموع من API nexus-data (نفس مصدر النظام الحي) بترحيل 5000/طلب.
    data_url = os.getenv("NEXUS_DATA_URL", "http://127.0.0.1:9001").rstrip("/")
    out: list[dict] = []
    end: str | None = None
    while True:
        params = {"limit": "5000"}
        if end:
            params["end"] = end
        url = f"{data_url}/api/data/ohlcv/{symbol}/{timeframe}?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as r:
            page = json.loads(r.read().decode("utf-8"))
        if not page:
            break
        out.extend(page)
        end = page[-1]["timestamp"]
        if len(page) < 5000 or len(out) > 500_000:  # حارس + حارس الحجم
            break
    if not out:
        raise SystemExit(f"لا شموع {symbol}/{timeframe} من {data_url}")
    df = pd.DataFrame(out)[["timestamp", "open", "high", "low", "close", "volume"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = (df.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True))
    return df


def broker_spec(symbol: str) -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))[symbol]


def simulate(signals: pd.DataFrame, df: pd.DataFrame, spec: dict,
             *, horizon: int, tp_atr: float, sl_atr: float,
             risk_pct: float = 0.01, balance: float = 10_000.0,
             slippage_points: float = 2.0) -> pd.DataFrame:
    """Simulate one trade per signal row (index-aligned to df).

    signals DataFrame must contain columns: prob (model prob of label=1).
    Exits use subsequent bars' high/low; ambiguous bar (both touched) → SL.
    """
    point = float(spec["point"])
    tick_value = float(spec["trade_tick_value"])
    tick_size = float(spec["trade_tick_size"])
    point_value = tick_value * (point / tick_size) if tick_size > 0 else 0.0
    half_spread = float(spec["spread_points"]) * point / 2.0
    slip = slippage_points * point
    step = float(spec["volume_step"]) or 0.01
    vol_min = float(spec["volume_min"]) or 0.01
    vol_max = float(spec["volume_max"]) or 100.0

    opens = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    atr = df["atr"].to_numpy(float)

    df_index = df.index
    trades = []
    for _, sig in signals.iterrows():
        t = int(sig.name) if not isinstance(sig.name, (int, np.integer)) else int(sig.name)
        a = atr[t]
        if not np.isfinite(a) or a <= 0 or t + 1 >= len(df):
            continue
        entry = close[t] + half_spread + slip  # long entry pays half-spread + slip
        tp = entry + tp_atr * a
        sl = entry - sl_atr * a
        dist = entry - sl
        if dist <= 0 or point_value <= 0:
            continue
        risk_amount = balance * risk_pct
        raw_lot = risk_amount / (dist / point * point_value)
        lot = min(vol_max, max(vol_min, vol_min + round((raw_lot - vol_min) / step) * step))
        lot = round(lot, 8)

        exit_price = None
        outcome = None
        exit_t = min(t + horizon, len(df) - 1)
        for k in range(t + 1, exit_t + 1):
            sl_hit = low[k] <= sl
            tp_hit = high[k] >= tp
            if sl_hit and tp_hit:
                outcome, exit_price, exit_t = "SL", sl, k   # conservative
                break
            if sl_hit:
                outcome, exit_price, exit_t = "SL", sl, k
                break
            if tp_hit:
                outcome, exit_price, exit_t = "TP", tp, k
                break
        if outcome is None:
            exit_price = close[exit_t] + slip     # market close on exit bar, pay slip on exit
            outcome = "TIMEOUT"

        pnl = (exit_price - entry) / point * point_value * lot
        if outcome == "SL":
            pnl = -risk_amount                     # SL loss is bounded by sizing
        trades.append({
            "t": t, "prob": float(sig["prob"]), "side": "BUY",
            "lot": lot, "entry": entry, "exit": exit_price,
            "outcome": outcome, "pnl": round(pnl, 2),
            "pnl_r": round(pnl / risk_amount, 4),
            "bars_held": exit_t - t,
        })
    return pd.DataFrame(trades)


def backtest_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    pnl = trades["pnl"].to_numpy(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")
    equity = np.cumsum(pnl)
    dd = equity - np.maximum.accumulate(equity)
    sharpe = float(np.mean(pnl) / np.std(pnl) * np.sqrt(len(pnl))) if np.std(pnl) > 0 else 0.0
    return {
        "trades": int(len(trades)),
        "win_rate": round(float((pnl > 0).mean()), 4),
        "profit_factor": round(pf, 3) if np.isfinite(pf) else None,
        "expectancy": round(float(pnl.mean()), 2),
        "total_pnl": round(float(pnl.sum()), 2),
        "max_drawdown": round(float(dd.min()), 2),
        "sharpe_trades": round(sharpe, 3),
        "avg_bars_held": round(float(trades["bars_held"].mean()), 1),
        "sl_rate": round(float((trades["outcome"] == "SL").mean()), 4),
        "tp_rate": round(float((trades["outcome"] == "TP").mean()), 4),
        "timeout_rate": round(float((trades["outcome"] == "TIMEOUT").mean()), 4),
    }


def run(symbol: str, horizon: int, thresholds: list[float], gated: bool = False) -> dict:
    import xgboost as xgb

    spec = broker_spec(symbol)
    df = load_candles(symbol)
    feats = calculate_all(df.copy())
    point = float(spec["point"])
    cost = float(spec["spread_points"]) * point / 2.0
    feats["label"] = triple_barrier_labels(feats, horizon=horizon, cost=cost)

    drop = {"timestamp", "open", "high", "low", "close", "volume", "label"}
    feat_cols = [c for c in feats.columns if c not in drop and feats[c].dtype != object]
    feats = feats.dropna(subset=["label"])
    feats = feats[feats["label"] >= 0].reset_index(drop=True)
    X_all = np.nan_to_num(feats[feat_cols].to_numpy(dtype=np.float32))
    y_all = feats["label"].to_numpy(np.int8)
    ts = feats["timestamp"].reset_index(drop=True)

    months = ts.dt.to_period("M")
    uniq = sorted(months.unique())
    all_trades: dict[float, list[pd.DataFrame]] = {th: [] for th in thresholds}
    fold_stats = []
    i = 0
    tr_m, va_m = 6, 1
    while i + tr_m + va_m <= len(uniq):
        tr = (months.isin(uniq[i:i + tr_m])).to_numpy()
        va = (months.isin(uniq[i + tr_m:i + tr_m + va_m])).to_numpy()
        i += 1
        if len(np.unique(y_all[tr])) < 2 or va.sum() < 30:
            continue
        model = xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="logloss", tree_method="hist",
        )
        model.fit(X_all[tr], y_all[tr])
        prob = model.predict_proba(X_all[va])[:, 1]
        idx = feats.index[va]
        sig_df = pd.DataFrame({"prob": prob}, index=idx)
        if gated:
            # P9 gates: trade ONLY when the ADX-based regime is TRENDING
            regime_ok = feats["adx"] >= 20.0           # Series aligned to feats index
            for th in thresholds:
                sel = sig_df[sig_df["prob"] >= th]
                sel = sel[sel.index.map(lambda i: bool(regime_ok.loc[i]))]
                if len(sel):
                    all_trades[th].append(simulate(sel, feats, spec, horizon=horizon, tp_atr=1.5, sl_atr=1.0))
        else:
            for th in thresholds:
                sel = sig_df[sig_df["prob"] >= th]
                if len(sel):
                    all_trades[th].append(simulate(sel, feats, spec, horizon=horizon, tp_atr=1.5, sl_atr=1.0))
        fold_stats.append({"val_period": f"{ts[va].iloc[0]:%Y-%m}"})
    out: dict = {
        "symbol": symbol, "horizon_min": horizon,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cost_model": {"half_spread_points": spec["spread_points"] / 2,
                        "slippage_points": 2.0, "risk_pct": 0.01, "balance": 10000},
        "thresholds": {},
    }
    for th in thresholds:
        tr_list = all_trades[th]
        merged = pd.concat(tr_list) if tr_list else pd.DataFrame()
        out["thresholds"][str(th)] = backtest_metrics(merged)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--thresholds", default="0.5,0.55,0.6")
    ap.add_argument("--gated", action="store_true", help="P9 gates: TRENDING-only regime filter")
    args = ap.parse_args()

    res = run(args.symbol, args.horizon, [float(x) for x in args.thresholds.split(",")], gated=args.gated)
    suffix = "_gated" if args.gated else ""
    md = [
        f"# Strategy Backtest — {args.symbol} · h={args.horizon}min · {suffix or 'ungated'}",
        f"- generated: {res['generated_at']}",
        f"- cost model: {json.dumps(res['cost_model'])}",
        "",
        "| threshold | trades | win% | PF | expectancy | total$ | maxDD$ | sharpe | tp% | sl% | timeout% |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for th, m in res["thresholds"].items():
        if m.get("trades", 0) == 0:
            md.append(f"| {th} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        md.append(
            f"| {th} | {m['trades']} | {m['win_rate']} | {m['profit_factor']} "
            f"| {m['expectancy']} | {m['total_pnl']} | {m['max_drawdown']} "
            f"| {m['sharpe_trades']} | {m['tp_rate']} | {m['sl_rate']} | {m['timeout_rate']} |"
        )
    path = ROOT / "docs" / f"BACKTEST_{args.symbol}_h{args.horizon}{suffix}.md"
    path.write_text("\n".join(md), encoding="utf-8")
    (ROOT / "models_store" / args.symbol / f"baseline_h{args.horizon}" / f"backtest{suffix}.json") \
        .write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
