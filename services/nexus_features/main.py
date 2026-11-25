"""
Nexus Features — Feature Engineering Service (port 8002).

* FastAPI REST endpoints for on-demand feature retrieval / calculation.
* Background Redis-Stream consumer that reacts to new OHLCV bars,
  computes the full indicator set, and publishes FeatureEvents.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.constants import (
    CACHE_FEATURE_PREFIX,
    CACHE_OHLCV_PREFIX,
    HEARTBEAT_INTERVAL_SEC,
    CACHE_HEALTH_PREFIX,
    RedisStream,
    SERVICE_PORTS,
    STREAM_MAX_LEN,
    SUPPORTED_TIMEFRAMES,
    ServiceName,
)
from shared.events import FeatureEvent, OHLCVEvent
from shared.bar_contract import (
    BAR_DEDUP_KEY_TPL,
    BAR_DEDUP_TTL_SEC,
    validate_ohlcv_bar,
)
from shared.models import SystemHealth

from .indicators import calculate_all
from .key_levels import detect_order_blocks, detect_sr_levels, find_round_numbers
from .multi_timeframe import aggregate_features, calculate_mtf_trend_alignment, get_htf_context
from .session_tagger import get_current_session, tag_dataframe, tag_session
from .trend_analyzer import calculate_trend_score, detect_market_structure, detect_trend
from .volatility import (
    calculate_atr_normalized,
    calculate_bb_width,
    calculate_historical_vol,
    calculate_vol_percentile,
    detect_regime,
)
from .cross_asset import compute_cross_asset_features, cross_asset_refresh_loop, shift_monitor_loop
from .correlation import correlation_refresh_loop

# ── Configuration ────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SERVICE_NAME = ServiceName.FEATURES
PORT = SERVICE_PORTS[SERVICE_NAME]
CONSUMER_GROUP = "nexus-features-group"
CONSUMER_NAME = f"features-worker-{os.getpid()}"

from shared import setup_logger as _setup_logger
_setup_logger("nexus-features", level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("nexus.features")

# ── Globals ──────────────────────────────────────────────────────────────────

redis_pool: aioredis.Redis | None = None
_consumer_task: asyncio.Task | None = None
_heartbeat_task: asyncio.Task | None = None

# ── Institutional feature engine (singleton) ─────────────────────────────────
from .institutional_features import NexusInstitutionalFeatures as _InstEngine
_inst_engine = _InstEngine()
_INST_SYMBOLS = [s.strip() for s in os.getenv("NEXUS_SYMBOLS", "XAUUSD").split(",")]
_INST_INTERVAL_SEC = int(os.getenv("NEXUS_INST_INTERVAL_SEC", "60"))
_INST_REDIS_KEY = "nexus:features:institutional:{symbol}"
_INST_EQUITY_KEY = "nexus:risk:day_start_balance"
_INST_TRADES_KEY = "nexus:trade_memory:trades"

# ── Lifecycle ────────────────────────────────────────────────────────────────

_cross_asset_task: asyncio.Task | None = None
_correlation_task: asyncio.Task | None = None
_institutional_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ق19.6: فحص startup مبكر — إعداد NEXUS_DATA_URL مركزي أو فشل فوري.
    from shared.service_urls import data_service_url
    data_service_url(required=True)
    global redis_pool, _consumer_task, _heartbeat_task, _cross_asset_task, _correlation_task
    redis_pool = aioredis.from_url(REDIS_URL, decode_responses=True)
    await _ensure_consumer_group(redis_pool)
    _consumer_task = asyncio.create_task(_ohlcv_consumer(redis_pool))
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(redis_pool))
    # Cross-asset refresh loop: fetches DXY/US10Y every 5 minutes
    _cross_asset_task = asyncio.create_task(cross_asset_refresh_loop(redis_pool, interval_sec=300))
    # Shift monitor: detects intraday DXY/US10Y/regime changes every 5 minutes
    _shift_task = asyncio.create_task(shift_monitor_loop(redis_pool, check_interval_sec=300))
    # Correlation matrix: rolling 7d/30d for XAUUSD/BTCUSD/OIL/SPX, refresh every 5 min
    _corr_refresh = int(os.getenv("CORR_REFRESH_SEC", "300"))
    _correlation_task = asyncio.create_task(correlation_refresh_loop(redis_pool, refresh_sec=_corr_refresh))
    # Initial fetch at startup
    asyncio.create_task(compute_cross_asset_features("XAUUSD", redis_pool))
    # Institutional features loop: computes ergodic/fractal metrics every 60s
    _institutional_task = asyncio.create_task(_institutional_refresh_loop(redis_pool))
    logger.info("nexus-features started on port %s", PORT)
    yield
    _cross_asset_task.cancel()
    _shift_task.cancel()
    _correlation_task.cancel()
    _consumer_task.cancel()
    _heartbeat_task.cancel()
    _institutional_task.cancel()
    await redis_pool.aclose()
    logger.info("nexus-features shut down")


app = FastAPI(
    title="Nexus Features",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Request / Response schemas ───────────────────────────────────────────────

class CalculateRequest(BaseModel):
    symbol: str
    timeframe: str = "M5"
    ohlcv: list[dict[str, Any]] = Field(
        ...,
        description="List of OHLCV dicts with keys: open, high, low, close, volume, timestamp",
    )
    include_levels: bool = False
    include_trend: bool = False
    include_volatility: bool = False


class CalculateResponse(BaseModel):
    symbol: str
    timeframe: str
    bar_count: int
    features: dict[str, Any]
    session: dict[str, Any] | None = None
    trend: dict[str, Any] | None = None
    levels: list[dict[str, Any]] | None = None
    volatility: dict[str, Any] | None = None


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    ok = redis_pool is not None
    try:
        if ok:
            await redis_pool.ping()
    except Exception:
        ok = False
    return {
        "service": SERVICE_NAME,
        "status": "healthy" if ok else "degraded",
        "redis": "connected" if ok else "disconnected",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── GET /api/features/{symbol}/{timeframe} ───────────────────────────────────

@app.get("/api/features/{symbol}/{timeframe}")
async def get_features(symbol: str, timeframe: str):
    """Return the latest cached feature set for a symbol/timeframe pair."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(400, f"Unsupported timeframe: {timeframe}")

    cache_key = f"{CACHE_FEATURE_PREFIX}{symbol}:{timeframe}"
    raw = await redis_pool.get(cache_key)
    if raw is None:
        raise HTTPException(404, f"No cached features for {symbol}/{timeframe}")

    return json.loads(raw)


# ── POST /api/features/calculate ─────────────────────────────────────────────

@app.post("/api/features/calculate", response_model=CalculateResponse)
async def calculate_features(req: CalculateRequest):
    """Compute indicators on ad-hoc OHLCV data supplied in the request body."""
    df = pd.DataFrame(req.ohlcv)
    if df.empty or not {"open", "high", "low", "close"}.issubset(df.columns):
        raise HTTPException(400, "OHLCV data must contain open/high/low/close columns")

    df = calculate_all(df)
    df = tag_dataframe(df)

    last = df.iloc[-1]
    features = _extract_features(df)

    response: dict[str, Any] = {
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "bar_count": len(df),
        "features": features,
        "session": {
            "current": tag_session(datetime.now(timezone.utc)),
            "bar_session": str(last.get("session", "")),
        },
    }

    if req.include_trend:
        trend = detect_trend(df)
        score = calculate_trend_score(df)
        structure = detect_market_structure(df)
        response["trend"] = {
            "direction": trend.direction,
            "strength": trend.strength,
            "score": score,
            "structure": [
                {"label": s.label, "price": s.swing.price, "index": s.swing.index}
                for s in structure[-10:]
            ],
        }

    if req.include_levels:
        sr = detect_sr_levels(df)
        rounds = find_round_numbers(float(last["close"]))
        obs = detect_order_blocks(df)
        response["levels"] = {
            "support_resistance": [
                {"price": l.price, "strength": l.strength, "kind": l.kind, "touches": l.touches}
                for l in sr[:15]
            ],
            "round_numbers": [{"price": l.price, "kind": l.kind} for l in rounds],
            "order_blocks": [
                {"high": ob.high, "low": ob.low, "kind": ob.kind, "index": ob.index}
                for ob in obs[-10:]
            ],
        }

    if req.include_volatility:
        atr_n = calculate_atr_normalized(df)
        regime = detect_regime(df)
        bb_w = calculate_bb_width(df)
        hvol = calculate_historical_vol(df)
        vpct = calculate_vol_percentile(df)
        response["volatility"] = {
            "atr_normalized": _safe_float(atr_n.iloc[-1]),
            "regime": str(regime.iloc[-1]),
            "bb_width": _safe_float(bb_w.iloc[-1]),
            "hist_vol": _safe_float(hvol.iloc[-1]),
            "vol_percentile": _safe_float(vpct.iloc[-1]),
        }

    return response


# ── Redis Stream Consumer ────────────────────────────────────────────────────

async def _ensure_consumer_group(r: aioredis.Redis) -> None:
    try:
        await r.xgroup_create(
            RedisStream.OHLCV_STREAM,
            CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _ohlcv_consumer(r: aioredis.Redis) -> None:
    """Listen for new OHLCV bars, compute features, cache & publish."""
    logger.info("OHLCV stream consumer started")
    while True:
        try:
            entries = await r.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={RedisStream.OHLCV_STREAM: ">"},
                count=10,
                block=200,  # 200ms for scalping responsiveness (was 2000)
            )
            if not entries:
                continue
            for _stream, messages in entries:
                for msg_id, data in messages:
                    await _process_ohlcv_message(r, data)
                    await r.xack(RedisStream.OHLCV_STREAM, CONSUMER_GROUP, msg_id)
        except asyncio.CancelledError:
            logger.info("OHLCV consumer cancelled")
            return
        except Exception:
            logger.exception("Error in OHLCV consumer loop")
            await asyncio.sleep(1)


async def _fetch_ohlcv_from_api(symbol: str, timeframe: str, count: int = 300) -> list[dict] | None:
    """Fallback: fetch OHLCV directly from nexus-data REST API."""
    import httpx
    from shared.service_urls import data_service_url
    data_url = data_service_url()
    url = f"{data_url}/api/data/ohlcv/{symbol}/{timeframe}?limit={count}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) >= 50:
                    return data
    except Exception:
        logger.warning("features.api_fallback_failed for %s/%s", symbol, timeframe)
    return None


# ── T16.6-live: ميزات التقويم الاقتصادي الحية — مطابقة لبنى build_training_dataset ──
# التاريخي يقرأ economic_events حيث importance='high' فقط (فلتر 'moderate' لا يطابق
# قيم المتجر 'medium') لذا الحي يستخدم high فقط للتطابق. يدعم: دقائق لأقرب حدث
# عالي مستقبلي، مفاجأة آخر حدث عالي المنجز، وعدّ أحداث الـ24 ساعة الماضية.

_ECO_CACHE_KEY = "nexus:eco:events_cache"
_ECO_CACHE_TTL = 600  # 10 دقائق


def _parse_bar_ts(raw: str | None) -> datetime:
    """حانة الشمعة من حدث OHLCV — الاستيراد الآمن مع الوقت الحالي كاحتياط."""
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _load_eco_events(r: aioredis.Redis) -> list[dict]:
    """أحداث high من economic_events عبر nexus-data (ماضي 48س + مستقبل 14يوم) مع كاش Redis."""
    raw = await r.get(_ECO_CACHE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    import httpx
    from shared.service_urls import data_service_url
    url = (f"{data_service_url()}/api/data/economic/events"
           f"?hours_back=48&hours_ahead=336&limit=1000")
    events: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                for e in resp.json():
                    if str(e.get("importance", "")).lower() != "high":
                        continue
                    events.append({
                        "time_utc": str(e.get("time_utc", "")),
                        "actual": e.get("actual"),
                        "forecast": e.get("forecast"),
                    })
    except Exception:
        logger.warning("eco.events_fetch_failed", exc_info=True)
        return []  # بلا كاش عند الفشل — المحاولة القادمة بعد 5 دقائق عبر دورة الشمعة
    await r.set(_ECO_CACHE_KEY, json.dumps(events), ex=_ECO_CACHE_TTL)
    return events


def _parse_event_ts(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if "+" in s or "Z" in s else None
    except ValueError:
        return None


async def _compute_eco_features(r: aioredis.Redis, bar_ts: datetime) -> dict[str, float]:
    """الثلاثية الحية — نفس دلالات الحساب التاريخي (high فقط)."""
    out = {"eco_min_to_next_high": 999999.0, "eco_surprise_last24h": 0.0, "eco_events_24h_back": 0.0}
    events = await _load_eco_events(r)
    if not events:
        return out
    parsed: list[tuple[datetime, float | None, float | None]] = []
    for e in events:
        t = _parse_event_ts(e.get("time_utc", ""))
        if t is None:
            continue
        a = e.get("actual"); f = e.get("forecast")
        a = float(a) if isinstance(a, (int, float)) else None
        f = float(f) if isinstance(f, (int, float)) else None
        parsed.append((t, a, f))
    if not parsed:
        return out
    parsed.sort(key=lambda x: x[0])

    # 1) دقائق لأقرب حدث عالي مستقبلي
    future = [t for (t, _, _) in parsed if t >= bar_ts]
    if future:
        out["eco_min_to_next_high"] = (future[0] - bar_ts).total_seconds() / 60.0

    # 2) مفاجأة آخر حدث عالي منجز (actual وforecast رقميان) — يحمل للأمام بلا قطع 24س (مطابق للتاريخي)
    last_surprise = 0.0
    for (t, a, f) in parsed:
        if t <= bar_ts and a is not None and f is not None:
            last_surprise = a - f
    out["eco_surprise_last24h"] = last_surprise

    # 3) عدد أحداث الـ24 ساعة الماضية (bar_ts−24h, bar_ts]
    cutoff = bar_ts.timestamp() - 24 * 3600
    out["eco_events_24h_back"] = float(sum(1 for (t, _, _) in parsed if cutoff < t.timestamp() <= bar_ts.timestamp()))
    return out


async def _process_ohlcv_message(r: aioredis.Redis, data: dict[str, str]) -> None:
    symbol = data.get("symbol", "")
    timeframe = data.get("timeframe", "")
    if not symbol or not timeframe:
        return

    # T3 عقد البيانات: dedup بالـ bar_id (ق19.4 حرفياً) — replay نفس الحدث
    # (أو إعادة نشر نفس الشمعة) لا يُعالج مرة ثانية: SET NX EX 30d.
    bar_id = data.get("bar_id", "")
    bar_timestamp = data.get("bar_timestamp", "")
    if bar_id:
        dedup_key = BAR_DEDUP_KEY_TPL.format(bar_id=bar_id)
        is_new = await r.set(dedup_key, "1", nx=True, ex=BAR_DEDUP_TTL_SEC)
        if not is_new:
            logger.info("features.duplicate_bar_skipped %s", bar_id)
            return

    # T3: تحقق صارم للشمعة الأخيرة قبل أي حساب (ق19.3).
    # (الناشر يتحقق من حدث OHLCV؛ هنا نعيد التحقق من الشمعة المستخدمة فعلياً
    # من الـ cache/الـ API لأنها قد تختلف عن الشمعة المنشورة في الحدث.)
    ohlcv_key = f"{CACHE_OHLCV_PREFIX}{symbol}:{timeframe}"
    raw = await r.get(ohlcv_key)

    bars = None
    if raw is not None:
        bars = json.loads(raw)

    if not bars or len(bars) < 50:
        logger.info("features.cache_miss %s/%s — trying API fallback", symbol, timeframe)
        bars = await _fetch_ohlcv_from_api(symbol, timeframe)
        if bars:
            await r.set(ohlcv_key, json.dumps(bars, default=str), ex=3600)

    if not bars or len(bars) < 50:
        logger.warning("features.insufficient_data %s/%s bars=%d", symbol, timeframe, len(bars or []))
        return

    try:
        validate_ohlcv_bar(bars[-1], context=f"features_input {symbol}:{timeframe}")
    except ValueError as _ve:
        logger.warning("features.invalid_bar_rejected %s/%s: %s", symbol, timeframe, _ve)
        return

    df = pd.DataFrame(bars)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    if len(df) < 50:
        logger.warning("features.too_few_valid_bars %s/%s rows=%d", symbol, timeframe, len(df))
        return

    df = calculate_all(df)
    df = tag_dataframe(df)

    features = _extract_features(df)

    trend = detect_trend(df)
    features["trend_direction"] = trend.direction.value
    features["trend_strength"] = trend.strength
    features["trend_score"] = calculate_trend_score(df)

    atr_n = calculate_atr_normalized(df)
    regime = detect_regime(df)
    features["atr_normalized"] = _safe_float(atr_n.iloc[-1])
    features["vol_regime"] = str(regime.iloc[-1])

    session_info = get_current_session()
    features["session"] = session_info.name
    features["session_overlap"] = session_info.is_overlap

    features["close"] = _safe_float(df["close"].iloc[-1])
    features["atr_14"] = _safe_float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0

    # ── Tick imbalance (buy pressure ratio from recent bars) ──────────────
    tick_imb = _compute_tick_imbalance(df, window=20)
    features["tick_imbalance_20"] = tick_imb
    tick_imb5 = _compute_tick_imbalance(df, window=5)
    features["tick_imbalance_5"] = tick_imb5

    # ── Cross-asset context (DXY, US10Y) for commodity/index assets ───────
    from shared.asset_profile import get_profile as _get_profile
    profile = _get_profile(symbol)
    if profile.cross_asset_correlators:
        try:
            closes = df["close"].values[-30:] if len(df) >= 30 else df["close"].values
            ca_feats = await compute_cross_asset_features(symbol, r, gold_closes=closes)
            features.update(ca_feats)
        except Exception:
            logger.debug("cross_asset features failed for %s", symbol, exc_info=True)

    # ── T16.6-live: سياق التقويم الاقتصادي (مطابق لبنى التدريب) ──────────
    try:
        eco_feats = await _compute_eco_features(r, _parse_bar_ts(data.get("bar_timestamp")))
        features.update(eco_feats)
    except Exception:
        logger.debug("eco features failed for %s", symbol, exc_info=True)

    cache_key = f"{CACHE_FEATURE_PREFIX}{symbol}:{timeframe}"
    last = df.iloc[-1]
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "features": features,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # T3: bar_id/bar_timestamp يمرّان مع الخصائص ليصلان لسجل التدريب.
        "bar_id": bar_id,
        "bar_timestamp": bar_timestamp,
        # F-02: raw bar carried alongside features so training records get a
        # complete OHLCV row (SL/TP/ATR targets need open/high/low/volume).
        "ohlcv": {
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": float(last["close"]),
            "volume": float(last["volume"]),
        },
    }
    await r.set(cache_key, json.dumps(payload, default=str), ex=3600)

    event = FeatureEvent(
        source=SERVICE_NAME,
        symbol=symbol,
        timeframe=timeframe,
        features={k: v for k, v in features.items() if isinstance(v, (int, float, str))},
        ohlcv=payload["ohlcv"],
        bar_id=bar_id,
        bar_timestamp=bar_timestamp,
    )
    await r.xadd(
        RedisStream.FEATURE_STREAM,
        event.to_stream_dict(),
        maxlen=STREAM_MAX_LEN,
    )
    logger.info(
        "features.published %s/%s close=%.2f rsi=%.1f atr=%.4f",
        symbol, timeframe,
        features.get("close", 0), features.get("rsi", 0), features.get("atr_14", 0),
    )


# ── Heartbeat ────────────────────────────────────────────────────────────────

async def _heartbeat_loop(r: aioredis.Redis) -> None:
    while True:
        try:
            health = SystemHealth(
                service_name=SERVICE_NAME,
                status="healthy",
                last_heartbeat=datetime.now(timezone.utc),
            )
            await r.set(
                f"{CACHE_HEALTH_PREFIX}{SERVICE_NAME}",
                health.model_dump_json(),
                ex=HEARTBEAT_INTERVAL_SEC * 3,
            )
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Heartbeat error")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


# ── Institutional features loop ──────────────────────────────────────────────

async def _get_trade_stats(r: aioredis.Redis) -> dict:
    """Read live trading stats from Redis trade memory."""
    try:
        raw_type = await r.type(_INST_TRADES_KEY)
        if raw_type == "string":
            raw = await r.get(_INST_TRADES_KEY)
            trades = json.loads(raw) if raw else []
        elif raw_type == "list":
            items = await r.lrange(_INST_TRADES_KEY, -200, -1)
            trades = [json.loads(t) for t in items if t]
        else:
            trades = []
    except Exception:
        trades = []

    if not trades:
        return {
            "win_rate": 0.40, "avg_win_pips": 10.0,
            "avg_loss_pips": 8.0, "equity": 191.0,
        }

    wins = [t for t in trades if t.get("is_win")]
    losses = [t for t in trades if not t.get("is_win")]
    win_rate = len(wins) / len(trades) if trades else 0.40
    avg_win = float(np.mean([abs(t.get("pnl_pips", 10)) for t in wins])) if wins else 10.0
    avg_loss = float(np.mean([abs(t.get("pnl_pips", 8)) for t in losses])) if losses else 8.0
    # Equity from day_start_balance or last known
    equity_raw = await r.get(_INST_EQUITY_KEY)
    equity = float(equity_raw) if equity_raw else 191.0
    # Equity history: build from cumulative pnl
    pnl_series = [t.get("pnl_usd", 0) for t in sorted(trades, key=lambda x: x.get("closed_at", ""))]
    equity_history = [equity - sum(pnl_series[i:]) for i in range(len(pnl_series))]
    equity_history.append(equity)

    return {
        "win_rate": float(win_rate),
        "avg_win_pips": float(avg_win),
        "avg_loss_pips": float(avg_loss),
        "equity": equity,
        "equity_history": equity_history,
    }


async def _institutional_refresh_loop(r: aioredis.Redis) -> None:
    """Background loop: compute and publish institutional features every 60s."""
    await asyncio.sleep(15)  # let other services start first
    while True:
        try:
            for symbol in _INST_SYMBOLS:
                # Get recent close prices
                ohlcv_key = f"{CACHE_OHLCV_PREFIX}{symbol}:M5"
                raw = await r.get(ohlcv_key)
                if not raw:
                    ohlcv_key = f"{CACHE_OHLCV_PREFIX}{symbol}:H1"
                    raw = await r.get(ohlcv_key)
                if not raw:
                    continue

                bars = json.loads(raw)
                if not bars or len(bars) < 60:
                    continue

                closes = np.array([float(b.get("close", 0)) for b in bars], dtype=float)
                if len(closes) < 60 or closes[-1] == 0:
                    continue

                stats = await _get_trade_stats(r)
                equity_history = stats.get("equity_history", [stats["equity"]])

                # Session multiplier
                session_info = get_current_session()
                session_name = session_info.name if hasattr(session_info, "name") else str(session_info)
                _sess_mults = {
                    "LONDON": 1.0, "NEW_YORK": 1.0,
                    "OVERLAP": 1.25, "TOKYO": 0.75, "SYDNEY": 0.5,
                }
                sess_mult = _sess_mults.get(session_name.upper(), 1.0)

                # Vol regime from cached features
                feat_raw = await r.get(f"{CACHE_FEATURE_PREFIX}{symbol}:M5")
                vol_regime = "NORMAL"
                if feat_raw:
                    try:
                        feat_data = json.loads(feat_raw)
                        vol_regime = feat_data.get("features", {}).get("vol_regime", "NORMAL") or "NORMAL"
                    except Exception:
                        pass

                inst = _inst_engine.compute_all(
                    prices=closes,
                    equity_history=equity_history,
                    win_rate=stats["win_rate"],
                    avg_win_pips=stats["avg_win_pips"],
                    avg_loss_pips=stats["avg_loss_pips"],
                    account_equity=stats["equity"],
                    session_mult=sess_mult,
                    vol_regime=vol_regime,
                )

                redis_key = _INST_REDIS_KEY.format(symbol=symbol)
                await r.set(redis_key, json.dumps(inst), ex=300)
                await r.publish(
                    "nexus:ws:broadcast",
                    json.dumps({"type": "institutional_features", "symbol": symbol, "data": inst}),
                )
                logger.info(
                    "institutional.published %s score=%.0f rec=%s H=%.3f g=%s",
                    symbol,
                    inst["operability_score"],
                    inst["recommendation"],
                    inst["hurst"]["H"],
                    inst["temporal_growth"].get("temporal_growth_g"),
                )

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("institutional_refresh_loop error")

        await asyncio.sleep(_INST_INTERVAL_SEC)


# ── GET /api/features/institutional/{symbol} ─────────────────────────────────

@app.get("/api/features/institutional/{symbol}")
async def get_institutional_features(symbol: str):
    """Return pre-computed institutional features (Hurst, Kelly ergódico, etc.)."""
    key = _INST_REDIS_KEY.format(symbol=symbol.upper())
    raw = await redis_pool.get(key)
    if raw is None:
        raise HTTPException(404, f"No institutional features for {symbol} — loop may still be initializing")
    return {"status": "ok", "symbol": symbol.upper(), "data": json.loads(raw)}


# ── Utilities ────────────────────────────────────────────────────────────────

def _extract_features(df: pd.DataFrame) -> dict[str, Any]:
    """Pull the last row's indicator values into a flat dict."""
    last = df.iloc[-1]
    skip = {"open", "high", "low", "close", "volume", "timestamp", "symbol", "timeframe"}
    features: dict[str, Any] = {}
    for col in df.columns:
        if col in skip:
            continue
        val = last[col]
        if isinstance(val, (int, float, np.integer, np.floating)):
            features[col] = _safe_float(val)
        elif isinstance(val, str):
            features[col] = val
    return features


def _compute_tick_imbalance(df: pd.DataFrame, window: int = 20) -> float:
    """Estimate buy-side pressure from OHLCV bars.

    Proxy: for each bar, classify as 'buy' if close > open, 'sell' otherwise.
    Returns fraction of buy bars in the last `window` bars (0.0 – 1.0).
    0.5 = balanced, > 0.65 = strong buy pressure, < 0.35 = strong sell pressure.
    """
    if len(df) < 2:
        return 0.5
    tail = df.tail(window)
    if "volume" in tail.columns and tail["volume"].sum() > 0:
        buy_vol = (tail["volume"] * (tail["close"] >= tail["open"]).astype(float)).sum()
        total_vol = tail["volume"].sum()
        return round(float(buy_vol / max(total_vol, 1e-8)), 4)
    # Fallback: count bars
    buy_bars = (tail["close"] >= tail["open"]).sum()
    return round(float(buy_bars) / max(len(tail), 1), 4)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return round(f, 6) if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "services.nexus_features.main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
