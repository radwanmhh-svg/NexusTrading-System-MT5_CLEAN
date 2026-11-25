"""
Triple-barrier labels — the training targets (P6/T6.1).

Replaces the naive "next candle up/down" target with a trade-outcome target:

For each completed candle t (entry at close[t] + cost for a LONG):
    TP barrier = entry + tp_atr × ATR[t]      (volatility-adaptive)
    SL barrier = entry − sl_atr × ATR[t]
    scan the next `horizon` completed candles:
        - TP touched first  → label 1  (good trade)
        - SL touched first  → label 0  (bad trade)
        - both in one bar   → ambiguous (conservative default: treat as SL)
        - timeout           → label by signed move net of cost, dead-zone = cost

Labels are computed per horizon (5/15/60 min) — never mixed. Cost model:
`cost` is the round-trip spread+commission in price units (from broker_specs).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier_labels(
    df: pd.DataFrame,
    *,
    horizon: int,
    tp_atr: float = 1.5,
    sl_atr: float = 1.0,
    cost: float = 0.0,
    atr_col: str = "atr",
    ambiguous: str = "sl",  # "sl" (conservative) | "tp" | "zero"
) -> pd.Series:
    """Return labels (1=TP first, 0=SL first/flat) aligned to df.index.

    `df` must contain open/high/low/close and ATR column, sorted by time,
    containing ONLY completed candles.
    """
    required = {"open", "high", "low", "close", atr_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"df missing columns: {missing}")
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if ambiguous not in ("sl", "tp", "zero"):
        raise ValueError("ambiguous must be 'sl' | 'tp' | 'zero'")

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    atr = df[atr_col].to_numpy(dtype=float)

    n = len(df)
    labels = np.full(n, -1, dtype=np.int8)  # -1 = not computable (no future window)

    for t in range(n - horizon):
        a = atr[t]
        if not np.isfinite(a) or a <= 0:
            continue  # leave as not-computable
        entry = close[t] + cost                      # LONG entry pays the spread
        tp = entry + tp_atr * a
        sl = entry - sl_atr * a

        tp_hit = False
        sl_hit = False
        for k in range(t + 1, t + horizon + 1):
            # A single bar can touch both barriers (gap/spike): resolve per policy
            if low[k] <= sl:
                sl_hit = True
            if high[k] >= tp:
                tp_hit = True
            if sl_hit and tp_hit:
                if ambiguous == "sl":
                    labels[t] = 0
                elif ambiguous == "tp":
                    labels[t] = 1
                else:
                    labels[t] = 0
                break
            if sl_hit:
                labels[t] = 0
                break
            if tp_hit:
                labels[t] = 1
                break
        else:
            # timeout: net-of-cost direction, dead zone → 0 (no edge)
            move = close[t + horizon] - entry
            labels[t] = 1 if move > cost else 0

    return pd.Series(labels, index=df.index, name=f"label_h{horizon}")


def multi_horizon_labels(
    df: pd.DataFrame,
    horizons: list[int],
    **kwargs,
) -> pd.DataFrame:
    """Labels for each horizon in separate columns (never mixed)."""
    out = {}
    for h in horizons:
        out[f"label_h{h}"] = triple_barrier_labels(df, horizon=h, **kwargs)
    return pd.DataFrame(out, index=df.index)


def class_balance(labels: pd.Series) -> dict[str, float]:
    """Share of computable labels — used to assert no single-class dataset."""
    valid = labels[labels >= 0]
    if len(valid) == 0:
        return {"n_valid": 0, "up": 0.0, "down": 0.0}
    return {
        "n_valid": int(len(valid)),
        "up": float((valid == 1).mean()),
        "down": float((valid == 0).mean()),
    }
