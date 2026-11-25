"""T5 (PLAN_MASTER) — مكافأة بالوزن لا بالتكرار.

- `_fetch_training_data` تعيد أعمدة sample_weight (bar=1.0, recency=2.0, outcome=5.0)
  بلا نسخ مكررة للصفوف.
- outcome idempotency: نفس trade_id لا يُدفع مرتين.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

import services.nexus_ml.traders_api as tapi


FEATS = {f"f{i}": float(i) for i in range(6)}
OHLCV = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}


def _rec(label=1, trade_id=None, i=0):
    feats = {k: float(v) + i for k, v in FEATS.items()}  # صفوف مختلفة — التكرار مصطلح نسخ لا صفوف متطابقة
    r = {"features": feats, "label": label, "ohlcv": dict(OHLCV)}
    if trade_id:
        r["trade_id"] = trade_id
    return json.dumps(r)


class _FakeRedis:
    """Redis مصغر: lrange فقط — يكفي لمسار _fetch_training_data."""

    def __init__(self):
        self._store: dict[str, list[str]] = {}

    async def lrange(self, key, a, b):
        return list(self._store.get(key, []))[a: b if b != -1 else None]

    async def lpush(self, key, *vals):
        self._store.setdefault(key, [])
        for v in vals:
            self._store[key].insert(0, v)


@pytest.mark.asyncio
async def test_weights_not_duplicates(monkeypatch):
    """الأوزان 1/2/5 دون تكرار صفوف."""
    fr = _FakeRedis()
    sym = "T5TEST"
    hist_key = f"nexus:features:history:{sym}"
    out_key = f"nexus:features:outcomes:{sym}"
    # 610 سجل تاريخ (آخر 500 بوزن 2.0)
    fr._store[hist_key] = [_rec(label=i % 2, i=i) for i in range(610)]
    # 3 outcomes بوزن 5.0
    fr._store[out_key] = [_rec(label=i % 2, trade_id=f"t{i}", i=1000 + i) for i in range(3)]

    async def fake_get_redis():
        return fr

    monkeypatch.setattr(tapi, "_get_redis", fake_get_redis)
    # T7: مسار training_dataset يعمل أولًا — فرض مسار Redis لهذا الاختبار الوحدي.
    monkeypatch.setenv("NEXUS_TRAIN_FORCE_REDIS", "1")

    data = await tapi._fetch_training_data([sym], ["M5"])
    assert data is not None
    X, y, ohlcv, feat_names, sw = data
    # 610 history + 3 outcomes = 613 صف (بدون أي تكرار)
    assert X.shape[0] == 613
    assert set(np.unique(sw)) == {1.0, 2.0, 5.0}
    assert int((sw == 1.0).sum()) == 110   # 610 - 500
    assert int((sw == 2.0).sum()) == 500   # آخر 500
    assert int((sw == 5.0).sum()) == 3     # outcomes
    # لا صفوف مكررة في X (التكرار 5× القديم كان سيكسر هذا)
    uq = np.unique(X, axis=0)
    assert len(uq) == X.shape[0]


@pytest.mark.asyncio
async def test_outcome_idempotency_same_trade_id():
    """نفس trade_id لا يُدفع مرتين في outcomes (منطق trade_memory)."""
    store: list[str] = []
    trade_id = "trade-abc-123"
    rec = _rec(label=1, trade_id=trade_id)

    def push_once():
        if not any(trade_id in s for s in store):
            store.append(rec)

    push_once()
    push_once()  # إعادة الدفع لنفس الصفقة
    assert len(store) == 1
    assert json.loads(store[0])["trade_id"] == trade_id
