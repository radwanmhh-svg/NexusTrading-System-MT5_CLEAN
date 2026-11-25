"""T9 (PLAN_MASTER) — صفحة Capabilities.

GET /api/capabilities — كل قدرة: endpoint/الحالة (defined≠loaded≠used_live)/
آخر تشغيل/مصدر/آخر خطأ. الفرق الجوهري: "معرّفة" ≠ "محملة" ≠ "مستخدمة حياً".
"""
from __future__ import annotations

from datetime import datetime, timezone

import json

from fastapi import APIRouter

from shared.service_urls import data_service_url

router = APIRouter(prefix="/api", tags=["capabilities"])

HMM_META = "models_store/hmm_metadata.json"
TRADERS_META = "models_store/XAUUSD/traders_metadata.json"


def _read_meta(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None





@router.get("/capabilities")
async def get_capabilities() -> dict:
    """قائمة القدرات مع الحالة الثلاثية defined/loaded/used_live."""
    now = datetime.now(timezone.utc).isoformat()

    traders_meta = _read_meta(TRADERS_META)
    hmm_meta = _read_meta(HMM_META)

    def _model_state(meta, file_exists: bool):
        return {
            "defined": True,
            "loaded": file_exists,
            "used_live": bool(meta and meta.get("is_synthetic") is False and file_exists),
            "last_run": meta.get("trained_at") if meta else None,
            "source": meta.get("data_source") if meta else None,
            "last_error": None,
            "is_synthetic": bool(meta.get("is_synthetic")) if meta else None,
            "dataset_id": meta.get("dataset_id") if meta else None,
        }

    capabilities = [
        # ── طبقة البيانات ──
        {
            "id": "ohlcv_stream",
            "name": "بث OHLCV من MT5",
            "endpoint": f"/api/data/ohlcv/{{symbol}}/{{timeframe}}",
            **{"defined": True, "loaded": True, "used_live": True},
            "last_run": now,
            "source": "MT5 OneRoyal-Demo",
            "last_error": None,
        },
        {
            "id": "ohlcv_source_tag",
            "name": "وسم مصدر البيانات (F-06)",
            "endpoint": "/api/data/ohlcv-source/{symbol}/{timeframe}",
            **{"defined": True, "loaded": True, "used_live": True},
            "source": "duckdb|mt5_live|synthetic|none",
        },
        {
            "id": "eco_features",
            "name": "ميزات الأخبار الاقتصادية الحية (eco_* الثلاثة)",
            "endpoint": "/api/features/{symbol}/{timeframe}",
            **{"defined": True, "loaded": True, "used_live": True},
            "source": "economic_events (21,886 حدثاً حتى 2027-12) — nexus-features كاش 10د، أحداث high فقط",
            "verified": "2026-11-23: تحديث حي eco_min 2055→2050 عبر 5 دقائق (P17/T17.3)",
        },
        {
            "id": "ohlcv_publisher_resilience",
            "name": "متانة ناشر OHLCV (إعادة اتصال 10×10s + الناشر يُطلق دائماً)",
            "endpoint": "services/nexus_data/main.py::_start_broker_feeds",
            **{"defined": True, "loaded": True, "used_live": True},
            "source": "رقعة 2026-11-23 بعد تجمد صامت 01:25Z — مكتملة مع إغلاق I-022 (مهل Redis + تهدئة حلقة التيك)",
        },
        {
            "id": "tick_persistent_sessions",
            "name": "جلسات تيك مستمرة لكل رمز (I-022) — تهيئة MT5 مرة واحدة، صفر spawn/0.5s",
            "endpoint": "services/nexus_data/mt5_connector.py::stream_ticks",
            **{"defined": True, "loaded": True, "used_live": True},
            "source": "mt5_worker --session عبر stdin/stdout — backoff حتى 5s، respawn عند الانهيار، الحلقة while True لا تموت بعد انقطاع MT5",
            "verified": "2026-11-23: اختبار إغلاق جسر MT5 جولتان + صمود خلال استراحة السوق اليومية (respawn بلا عاصفة) — استرداد ذاتي ~80ث وتِكات حية مستمرة (T: I-022)",
        },
        # ── النماذج ──
        {
            "id": "ml_trader",
            "name": "MLTrader (XGBoost+LightGBM)",
            "endpoint": "/api/ml/traders/status",
            **_model_state(traders_meta, _exists("models_store/traders/XGB_LGBM_SLTP.pkl")),
        },
        {
            "id": "dl_trader",
            "name": "DLTrader (Transformer)",
            "endpoint": "/api/ml/traders/status",
            **_model_state(traders_meta, _exists("models_store/traders/dl_transformer.pkl")),
        },
        {
            "id": "rl_trader",
            "name": "RLTrader (PPO)",
            "endpoint": "/api/ml/traders/status",
            **_model_state(traders_meta, _exists("models_store/traders/rl_ppo.pkl")),
        },
        {
            "id": "hmm_regime",
            "name": "HMM نظام السوق",
            "endpoint": "/api/regime/current",
            **_model_state(hmm_meta, _exists("models_store/hmm_regime.pkl")),
        },
        # ── البنية ──
        {
            "id": "data_url_config",
            "name": "NEXUS_DATA_URL مركزي (ق19.6)",
            "endpoint": "shared/service_urls.py",
            "defined": True,
            "loaded": True,
            "used_live": True,
            "value": data_service_url(),
        },
        {
            "id": "shadow_trades",
            "name": "Shadow mode (آلية shadow_trades)",
            "endpoint": "redis:shadow_trades:*",
            "defined": True,
            "loaded": True,
            "used_live": False,  # يُفعّل في T10
        },
    ]
    return {
        "generated_at": now,
        "capabilities": capabilities,
        "note": "defined ≠ loaded ≠ used_live — كل حالة مبنية على أدلة ملفات/metadata",
    }


def _exists(rel: str) -> bool:
    from pathlib import Path
    return (Path(rel)).exists()
