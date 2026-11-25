"""
Nexus News — FastAPI service (Service 4: News + NLP).

Provides endpoints for latest news, economic calendar, sentiment analysis,
and free-form text analysis.  Background tasks poll news sources every 30 s
and the economic calendar every 5 min, publishing NewsAlertEvent to Redis
when high-impact news is detected.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from shared.constants import (
    HEARTBEAT_INTERVAL_SEC,
    REDIS_KEY_PREFIX,
    RedisStream,
    STREAM_MAX_LEN,
)
from shared.events import NewsAlertEvent
from shared.models import CalendarEvent, NewsItem

from .calendar_parser import CalendarParser
from .mt5_calendar import economic_csv_path, load_mt5_csv, split_past_future
from .news_fetcher import NewsFetcher
from .social_sentiment import SentimentResult, SocialSentiment

# ── Logging ──────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        structlog.get_config().get("min_level", 20)  # INFO
    ),
)

from shared import setup_logger as _setup_logger
_setup_logger("nexus-news")
log = structlog.get_logger("nexus_news")

# ── Configuration ────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
GNEWS_KEY = os.getenv("GNEWS_KEY", "")
NEWSDATA_KEY = os.getenv("NEWSDATA_KEY", "")
USE_FINBERT = os.getenv("USE_FINBERT", "true").lower() == "true"
NEWS_POLL_SEC = int(os.getenv("NEWS_POLL_SEC", "30"))
CALENDAR_POLL_SEC = int(os.getenv("CALENDAR_POLL_SEC", "300"))

# Redis TTLs for the gateway bridge (see _publish_cache).  The news TTL must
# outlive the poll interval so a slow/failed poll never empties the dashboard.
NEWS_CACHE_TTL_SEC = max(NEWS_POLL_SEC * 3, 1800)
CALENDAR_CACHE_TTL_SEC = 86400

DEFAULT_SYMBOLS = [
    "XAUUSD",
]

# ── Shared state ─────────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None
_fetcher: NewsFetcher | None = None
_calendar: CalendarParser | None = None
_sentiment: SocialSentiment | None = None

_latest_news: list[NewsItem] = []
_latest_calendar: list[CalendarEvent] = []
_mt5_calendar_events: list[CalendarEvent] = []  # P16: تقويم MT5 من ملف الموديل
_bg_tasks: list[asyncio.Task[None]] = []


def _cached_news() -> list[NewsItem]:
    """News already collected by the periodic poll — handed to the Sentinel.

    The Sentinel must never trigger its own fetch: both consumers share one
    ``NewsFetcher`` and ``fetch_all()`` owns de-duplication and API-quota
    accounting.  This provider exposes only what the poll already fetched.
    """
    return list(_latest_news)

# ── Request / response schemas ───────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    text: str
    symbol: str | None = None


class AnalyzeResponse(BaseModel):
    text: str
    keyword_score: float
    finbert_score: float
    combined_score: float
    affected_currencies: list[str]


class SentimentResponse(BaseModel):
    symbol: str
    social_score: float
    reddit: dict[str, Any] = Field(default_factory=dict)
    twitter: dict[str, Any] = Field(default_factory=dict)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _fetcher, _calendar, _sentiment  # noqa: PLW0603

    log.info("nexus_news_starting", port=8005)

    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    _fetcher = NewsFetcher(
        newsapi_key=NEWSAPI_KEY,
        gnews_key=GNEWS_KEY,
        newsdata_key=NEWSDATA_KEY,
    )
    _calendar = CalendarParser()
    _sentiment = SocialSentiment(use_finbert=USE_FINBERT)

    # P16: تقويم MT5 — تحميل أولي ثم إعادة قراءة دورية (الموديل MQ5 يحدّث الملف)
    global _mt5_calendar_events  # noqa: PLW0603
    _mt5_calendar_events = load_mt5_csv(economic_csv_path())
    _bg_tasks.append(asyncio.create_task(_mt5_csv_poll_loop()))
    _bg_tasks.append(asyncio.create_task(_news_poll_loop()))
    _bg_tasks.append(asyncio.create_task(_calendar_poll_loop()))
    _bg_tasks.append(asyncio.create_task(_heartbeat_loop()))

    # News Sentinel (Fase 4)
    from .sentinel import sentinel_loop
    _sentinel_poll = int(os.getenv("SENTINEL_POLL_SEC", "60"))
    _bg_tasks.append(asyncio.create_task(
        sentinel_loop(
            _redis,
            news_provider=_cached_news,
            calendar=_calendar,
            poll_sec=_sentinel_poll,
        )
    ))

    log.info("nexus_news_started")
    yield

    log.info("nexus_news_shutting_down")
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)

    if _fetcher:
        await _fetcher.close()
    if _calendar:
        await _calendar.close()
    if _redis:
        await _redis.aclose()

    log.info("nexus_news_stopped")


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Nexus News",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    redis_ok = False
    if _redis:
        try:
            await _redis.ping()
            redis_ok = True
        except Exception:
            pass

    return {
        "service": "nexus-news",
        "status": "healthy" if redis_ok else "degraded",
        "redis": redis_ok,
        "news_cache_size": len(_latest_news),
        "calendar_cache_size": len(_latest_calendar),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── GET /api/news/latest ────────────────────────────────────────────────────

@app.get("/api/news/latest", response_model=list[NewsItem])
async def get_latest_news(
    limit: int = Query(default=50, ge=1, le=200),
    symbol: str | None = Query(default=None),
):
    items = _latest_news
    if symbol:
        sym_upper = symbol.upper()
        items = [
            n for n in items
            if sym_upper in n.affected_currencies
            or sym_upper[:3] in n.affected_currencies
            or sym_upper[3:] in n.affected_currencies
        ]
    return items[:limit]


# ── GET /api/news/calendar ──────────────────────────────────────────────────

@app.get("/api/news/calendar", response_model=list[CalendarEvent])
async def get_calendar(
    minutes: int = Query(default=1440, ge=1, le=10080),
    impact: str | None = Query(default=None),
    history_hours: int = Query(default=0, ge=0, le=24*90),
):
    """تقويم اقتصادي: قادم + (اختيارياً) أحداث ماضية من تقويم MT5.

    المصدر الأساس = ملف تقويم الوسيط MT5 (P16) مدموجاً مع المصادر الحية.
    history_hours=0 → القادمة فقط. history_hours=N → يشمل ماضي N ساعة.
    """
    if not _calendar:
        raise HTTPException(503, "Calendar parser not initialised")

    now = datetime.now(timezone.utc)
    merged: dict[tuple, CalendarEvent] = {}

    # 1) مصدر MT5 (ملف الموديل MQ5) — له الأولوية عند التداخل
    mt5_events = _mt5_calendar_events
    past, future = split_past_future(mt5_events, now=now)
    for e in future:
        if (e.timestamp - now) <= timedelta(minutes=minutes):
            merged[(e.event_name, e.currency, e.timestamp)] = e
    if history_hours > 0:
        for e in past:
            if (now - e.timestamp) <= timedelta(hours=history_hours):
                merged[(e.event_name, e.currency, e.timestamp)] = e

    # 2) المصادر الحية (ForexFactory/investing) — ما لا يغطيه MT5
    try:
        live = await _calendar.get_upcoming_events(minutes=minutes)
    except Exception:
        live = []
    for e in live:
        key = (e.event_name, e.currency, e.timestamp)
        merged.setdefault(key, e)

    events = sorted(merged.values(), key=lambda e: e.timestamp)
    if impact:
        events = [e for e in events if e.impact == impact.lower()]
    return events


# ── GET /api/news/sentiment/{symbol} ────────────────────────────────────────

@app.get("/api/news/sentiment/{symbol}", response_model=SentimentResponse)
async def get_sentiment(symbol: str):
    if not _sentiment:
        raise HTTPException(503, "Sentiment analyser not initialised")

    # SocialSentiment exposes aggregate_social_sentiment(); the per-platform
    # fetch_* helpers were removed, so derive the overall score from that and
    # return neutral per-platform placeholders (the dashboard reads social_score).
    try:
        overall = await _sentiment.aggregate_social_sentiment(symbol)
    except Exception:
        log.warning("sentiment.aggregate_failed", symbol=symbol)
        overall = 0.0

    neutral = {"score": 0.0, "volume": 0, "source": "unavailable"}
    return SentimentResponse(
        symbol=symbol.upper(),
        social_score=round(float(overall or 0.0), 4),
        reddit=neutral,
        twitter=neutral,
    )


# ── POST /api/news/analyze ──────────────────────────────────────────────────

@app.post("/api/news/analyze", response_model=AnalyzeResponse)
async def analyze_text(req: AnalyzeRequest):
    from .news_fetcher import _map_currencies
    from .social_sentiment import _finbert_score, _keyword_score

    kw = _keyword_score(req.text)
    fb = await _finbert_score(req.text)
    combined = (kw * 0.4 + fb * 0.6) if fb != 0.0 else kw
    currencies = _map_currencies(req.text)

    return AnalyzeResponse(
        text=req.text,
        keyword_score=round(kw, 4),
        finbert_score=round(fb, 4),
        combined_score=round(combined, 4),
        affected_currencies=currencies,
    )


# ── Sentinel endpoints (Fase 4) ───────────────────────────────────────────────

@app.get("/api/news/sentinel/alerts")
async def get_sentinel_alerts(limit: int = Query(default=20, ge=1, le=50)):
    """Structured impact alerts from the news sentinel."""
    if not _redis:
        raise HTTPException(503, "Redis not initialised")
    try:
        raw = await _redis.lrange("nexus:news:sentinel:alerts", 0, limit - 1)
        return {"alerts": [json.loads(x) for x in raw], "count": len(raw)}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/news/sentinel/blackout")
async def get_blackout_status():
    """Current blackout window status for signal quality gate."""
    if not _redis:
        raise HTTPException(503, "Redis not initialised")
    try:
        raw = await _redis.get("nexus:news:sentinel:blackout")
        if raw:
            return json.loads(raw)
        return {"active": False, "reason": "no scan yet", "events": [], "ends_at": None}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/news/sentinel/status")
async def get_sentinel_status():
    """Status of the news sentinel service."""
    if not _redis:
        raise HTTPException(503, "Redis not initialised")
    try:
        last_scan = await _redis.get("nexus:news:sentinel:last_scan")
        blackout_raw = await _redis.get("nexus:news:sentinel:blackout")
        alert_count = await _redis.llen("nexus:news:sentinel:alerts")
        blackout = json.loads(blackout_raw) if blackout_raw else {}
        return {
            "last_scan": last_scan,
            "total_alerts": alert_count,
            "blackout_active": blackout.get("active", False),
            "blackout_reason": blackout.get("reason", ""),
        }
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


# ── Background: news polling ────────────────────────────────────────────────

async def _publish_cache(key: str, payload: list[Any], ttl_sec: int) -> None:
    """Bridge the in-memory cache to Redis for the API gateway.

    ``services/nexus_api/routers/news.py`` serves ``/api/news`` and
    ``/api/news/calendar`` exclusively from ``RedisClient.cache_get(key)``
    (full key ``nexus:<key>``).  Without this write the gateway always
    answers ``[]`` and the dashboard falls back to its built-in demo data.
    """
    if not _redis or not payload:
        return
    try:
        await _redis.set(
            f"{REDIS_KEY_PREFIX}{key}",
            json.dumps(payload, default=str),
            ex=ttl_sec,
        )
    except Exception as exc:
        log.error("news_cache_publish_error", key=key, error=str(exc))


async def _news_poll_loop() -> None:
    global _latest_news  # noqa: PLW0603
    log.info("news_poll_started", interval_sec=NEWS_POLL_SEC)

    while True:
        try:
            if _fetcher:
                items = await _fetcher.fetch_all(DEFAULT_SYMBOLS)
                if items:
                    _latest_news = sorted(
                        items, key=lambda n: n.timestamp, reverse=True,
                    )
                    await _publish_high_impact_news(items)
                    await _publish_cache(
                        "news:latest",
                        [n.model_dump(mode="json") for n in _latest_news],
                        NEWS_CACHE_TTL_SEC,
                    )
                    log.info("news_poll_ok", count=len(items))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("news_poll_error", error=str(exc))

        await asyncio.sleep(NEWS_POLL_SEC)


async def _publish_high_impact_news(items: list[NewsItem]) -> None:
    """Publish NewsAlertEvent to Redis for items with high-impact indicators."""
    if not _redis:
        return

    high_impact_keywords = {
        "breaking", "urgent", "flash", "emergency", "rate decision",
        "surprise", "shock", "crisis", "crash", "surge",
    }

    for item in items:
        headline_lower = item.headline.lower()
        is_high = (
            item.impact == "high"
            or any(kw in headline_lower for kw in high_impact_keywords)
        )
        if not is_high:
            continue

        event = NewsAlertEvent(
            source="nexus-news",
            headline=item.headline,
            source_name=item.source,
            impact=item.impact,
            sentiment=item.sentiment,
            affected_currencies=item.affected_currencies,
            finbert_score=item.finbert_score,
        )

        try:
            await _redis.xadd(
                RedisStream.NEWS_ALERT,
                event.to_stream_dict(),
                maxlen=STREAM_MAX_LEN,
            )
            log.info(
                "news_alert_published",
                headline=item.headline[:80],
                impact=item.impact,
            )
        except Exception as exc:
            log.error("news_alert_publish_error", error=str(exc))


# ── Background: MT5 calendar CSV reload (P16) ───────────────────────────────

async def _mt5_csv_poll_loop() -> None:
    """إعادة قراءة ملف تقويم MT5 كل CALENDAR_POLL_SEC (الموديل MQ5 يحدّثه كل 5 دقائق)."""
    global _mt5_calendar_events  # noqa: PLW0603
    path = economic_csv_path()
    log.info("mt5_calendar_loader_started", path=str(path), initial=len(_mt5_calendar_events))
    while True:
        try:
            await asyncio.sleep(CALENDAR_POLL_SEC)
            events = load_mt5_csv(path)
            if events:
                _mt5_calendar_events = events
                log.info("mt5_calendar_reloaded", count=len(events),
                         last=str(events[-1].timestamp) if events else "-")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("mt5_calendar_reload_error", error=str(exc))


# ── Background: calendar polling ─────────────────────────────────────────────

async def _calendar_poll_loop() -> None:
    global _latest_calendar  # noqa: PLW0603
    log.info("calendar_poll_started", interval_sec=CALENDAR_POLL_SEC)

    while True:
        try:
            if _calendar:
                events = await _calendar.fetch_calendar()
                if events:
                    _latest_calendar = sorted(
                        events, key=lambda e: e.timestamp,
                    )
                    await _publish_cache(
                        "news:calendar",
                        [e.model_dump(mode="json") for e in _latest_calendar],
                        CALENDAR_CACHE_TTL_SEC,
                    )
                    log.info("calendar_poll_ok", count=len(events))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("calendar_poll_error", error=str(exc))

        await asyncio.sleep(CALENDAR_POLL_SEC)


# ── Background: heartbeat ───────────────────────────────────────────────────

async def _heartbeat_loop() -> None:
    while True:
        try:
            if _redis:
                await _redis.set(
                    "nexus:health:nexus-news",
                    datetime.now(timezone.utc).isoformat(),
                    ex=HEARTBEAT_INTERVAL_SEC * 3,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "services.nexus_news.main:app",
        host="0.0.0.0",
        port=8005,
        reload=True,
    )
