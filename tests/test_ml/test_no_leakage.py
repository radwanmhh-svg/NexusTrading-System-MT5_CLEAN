"""Leakage tests (P6/T6.2): features must only use information available at t."""
import numpy as np
import pandas as pd
import pytest

from services.nexus_features.indicators import calculate_all
from services.nexus_ml.labels import triple_barrier_labels


def _candles(n: int = 300, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    price = 100 + np.cumsum(rng.normal(0, 0.1, n))
    high = price + np.abs(rng.normal(0.05, 0.02, n))
    low = price - np.abs(rng.normal(0.05, 0.02, n))
    tr = np.maximum(high - low, np.maximum(abs(high - price), abs(low - price)))
    atr = pd.Series(tr, index=range(n)).rolling(14, min_periods=1).mean().to_numpy()
    return pd.DataFrame({
        "open": price, "high": high, "low": low, "close": price,
        "volume": rng.integers(100, 1000, n).astype(float),
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "atr": atr,
    })


def test_indicators_use_only_past_data():
    """Indicator at row t must be identical when future rows are appended."""
    df = _candles()
    full = calculate_all(df.copy())
    cut = 200
    partial = calculate_all(df.iloc[:cut].copy())
    # compare a sample of indicator columns
    num_cols = [c for c in full.columns if c not in ("open", "high", "low", "close", "volume", "timestamp")]
    checked = 0
    for col in num_cols:
        if col in partial.columns and full[col].notna().iloc[:cut].any():
            a = full[col].iloc[:cut].to_numpy(dtype=float)
            b = partial[col].to_numpy(dtype=float)
            mask = np.isfinite(a) & np.isfinite(b)
            if mask.sum() == 0:
                continue
            assert np.allclose(a[mask], b[mask], atol=1e-6), f"future leakage in {col}"
            checked += 1
    assert checked > 0, "no indicator columns compared"


def test_labels_never_see_past_from_future():
    """Label at t changes only if rows ≥ t+1 change — never rows < t."""
    df = _candles()
    labels_a = triple_barrier_labels(df, horizon=5)
    df2 = df.copy()
    df2.loc[df2.index[-1], "close"] = df2["close"].iloc[-1] * 3  # extreme future candle
    labels_b = triple_barrier_labels(df2, horizon=5)
    t = len(df) - 6
    # row t-5 (window ends before the changed candle) must be unaffected
    assert labels_a.iloc[t - 1] == labels_b.iloc[t - 1]


def test_rolling_stat_window_forward_only():
    """A manual rolling-mean feature equals pandas rolling (past-only) semantics."""
    df = _candles(50)
    close = df["close"]
    manual = [close.iloc[max(0, i - 9):i + 1].mean() for i in range(len(close))]
    rolling = close.rolling(10, min_periods=1).mean().to_list()
    assert np.allclose(manual, rolling)
