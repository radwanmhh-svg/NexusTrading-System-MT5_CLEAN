"""Tests for triple-barrier labels (P6/T6.1)."""
import numpy as np
import pandas as pd
import pytest

from services.nexus_ml.labels import class_balance, multi_horizon_labels, triple_barrier_labels


def _df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "atr"])


def test_tp_hit_first():
    # entry 100 (+cost 0), ATR 1 → TP 101.5, SL 99.0; bar1 reaches TP
    df = _df([
        [100, 100.2, 99.8, 100, 1.0],
        [100, 101.6, 100.0, 101.4, 1.0],
        [101, 102, 101, 102, 1.0],
    ])
    labels = triple_barrier_labels(df, horizon=2)
    assert labels.iloc[0] == 1


def test_sl_hit_first():
    df = _df([
        [100, 100.2, 99.8, 100, 1.0],
        [100, 100.1, 98.9, 99.0, 1.0],   # low 98.9 <= SL 99
        [99, 99, 98, 98, 1.0],
    ])
    labels = triple_barrier_labels(df, horizon=2)
    assert labels.iloc[0] == 0


def test_timeout_direction():
    # price drifts up but never touches TP within horizon → timeout by close
    df = _df([
        [100, 100.1, 99.9, 100, 1.0],
        [100.05, 100.3, 100.0, 100.25, 1.0],
        [100.25, 100.5, 100.2, 100.45, 1.0],
    ])
    labels = triple_barrier_labels(df, horizon=2)
    assert labels.iloc[0] == 1   # close 100.45 > entry 100


def test_cost_blocks_small_move():
    # cost 0.30 → timeout move +0.20 is below cost → 0
    df = _df([
        [100, 100.1, 99.9, 100, 1.0],
        [100.05, 100.3, 100.0, 100.2, 1.0],
        [100.2, 100.4, 100.1, 100.2, 1.0],
    ])
    labels = triple_barrier_labels(df, horizon=2, cost=0.30)
    assert labels.iloc[0] == 0


def test_ambiguous_both_in_one_bar():
    df = _df([
        [100, 100.2, 99.8, 100, 1.0],
        [100, 101.6, 98.9, 100.5, 1.0],  # touches TP 101.5 and SL 99 in one bar
    ])
    cons = triple_barrier_labels(df, horizon=1, ambiguous="sl")
    assert cons.iloc[0] == 0
    opt = triple_barrier_labels(df, horizon=1, ambiguous="tp")
    assert opt.iloc[0] == 1


def test_horizon_boundaries_and_nan():
    df = _df([[100, 100.1, 99.9, 100, np.nan]])
    labels = triple_barrier_labels(df, horizon=3)
    assert labels.iloc[0] == -1   # no ATR → not computable

    # rows whose horizon window is incomplete stay -1
    df2 = _df([[100, 100.1, 99.9, 100, 1.0]] * 5)
    labels2 = triple_barrier_labels(df2, horizon=4)
    assert (labels2.iloc[-4:] == -1).all()
    assert labels2.iloc[0] != -1


def test_multi_horizon_separate_columns():
    df = _df([
        [100, 100.2, 99.8, 100, 1.0],
        [100, 101.6, 100.0, 101.4, 1.0],
        [101, 102, 101, 102, 1.0],
        [102, 102.5, 101.8, 102.4, 1.0],
        [102.4, 102.6, 102.0, 102.5, 1.0],
        [102.5, 102.7, 102.2, 102.6, 1.0],
    ])
    out = multi_horizon_labels(df, horizons=[1, 3])
    assert list(out.columns) == ["label_h1", "label_h3"]
    # they can differ: h1 hit TP at bar1; h3 still TP → equal here, but no mixing
    assert set(out["label_h1"].unique()) <= {-1, 0, 1}
    assert set(out["label_h3"].unique()) <= {-1, 0, 1}


def test_class_balance_has_both_classes():
    rng = np.random.default_rng(7)
    n = 300
    price = 100 + np.cumsum(rng.normal(0, 0.05, n))
    df = pd.DataFrame({
        "open": price, "high": price + 0.05, "low": price - 0.05, "close": price,
        "atr": np.full(n, 0.1),
    })
    labels = triple_barrier_labels(df, horizon=10, tp_atr=1.5, sl_atr=1.0)
    bal = class_balance(labels)
    assert bal["n_valid"] > 200
    assert 0.05 < bal["up"] < 0.95 and 0.05 < bal["down"] < 0.95


def test_missing_columns_raise():
    with pytest.raises(ValueError):
        triple_barrier_labels(pd.DataFrame({"close": [1]}), horizon=5)
    with pytest.raises(ValueError):
        triple_barrier_labels(_df([[100, 100, 99, 100, 1]]), horizon=0)
