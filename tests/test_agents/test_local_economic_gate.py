"""P5.2 — local MT5-calendar gate inside the A2 NewsSentinel agent."""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from services.nexus_agents.agents.news_sentinel import NewsSentinelAgent
from services.nexus_economic import EconomicEvent


def _state():
    return {
        "symbol": "XAUUSD", "timeframe": "M15", "session": "LONDON",
        "ml_signal": {}, "features": {}, "htf_context": {},
        "regime_analysis": {}, "technical_analysis": "", "news_assessment": "",
        "intermarket_score": 0.0, "flow_score": 0.0,
        "bull_case": "", "bull_conviction": 0, "bear_case": "", "bear_conviction": 0,
        "verdict": "NO_TRADE", "verdict_conviction": 0, "verdict_reasoning": "",
        "risk_decision": "REJECT", "position_size": 0.0, "risk_reasoning": "",
        "open_positions": [], "order": None, "messages": [],
        "cycle_id": "t", "errors": [], "trade_memory": {},
    }


@pytest.mark.asyncio
async def test_high_impact_event_blocks(monkeypatch):
    """A HIGH-impact MT5-calendar event inside the blackout → recommendation AVOID."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    ev = [EconomicEvent(
        event_id="1", event_name="FOMC Rate Decision", currency="USD",
        importance="HIGH", time_utc=soon, time_broker=soon,
    )]
    monkeypatch.setattr("services.nexus_economic.load_events", lambda: ev)
    agent = NewsSentinelAgent(redis_client=None)
    agent._invoke_with_budget = AsyncMock(return_value=type("R", (), {"content": "x"})())
    state = await agent.invoke(_state())
    assert state["news_assessment"] != ""
    # fallback raw assessment (LLM bypassed) still carries the verdict
    recs = [m.get("recommendation") for m in state["messages"] if isinstance(m, dict)]
    assert "AVOID" in recs
    # the raw fallback counts the MT5-calendar event into the blackout tally
    contents = [m.get("content", "") for m in state["messages"] if isinstance(m, dict)]
    assert any("High-impact events: 1" in c for c in contents)


@pytest.mark.asyncio
async def test_no_events_trade_freely(monkeypatch):
    monkeypatch.setattr("services.nexus_economic.load_events", lambda: [])
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.fetch_news", AsyncMock(return_value=[]))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.get_calendar", AsyncMock(return_value=[]))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.get_sentiment", AsyncMock(return_value={"score": 0.0}))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.finbert_score", AsyncMock(return_value=0.0))
    agent = NewsSentinelAgent(redis_client=None)
    agent._invoke_with_budget = AsyncMock(return_value=type("R", (), {"content": "calm markets"})())
    state = await agent.invoke(_state())
    recs = [m.get("recommendation") for m in state["messages"] if isinstance(m, dict)]
    assert "TRADE_FREELY" in recs
    assert state["errors"] == [] or not any("economic gate" in e for e in state["errors"])


@pytest.mark.asyncio
async def test_gate_failure_is_not_fatal(monkeypatch):
    """If the local calendar file is missing/corrupt, A2 continues without crash."""
    def _boom():
        raise RuntimeError("csv unreadable")
    monkeypatch.setattr("services.nexus_economic.load_events", _boom)
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.fetch_news", AsyncMock(return_value=[]))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.get_calendar", AsyncMock(return_value=[]))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.get_sentiment", AsyncMock(return_value={"score": 0.0}))
    monkeypatch.setattr("services.nexus_agents.agents.news_sentinel.finbert_score", AsyncMock(return_value=0.0))
    agent = NewsSentinelAgent(redis_client=None)
    agent._invoke_with_budget = AsyncMock(return_value=type("R", (), {"content": "ok"})())
    state = await agent.invoke(_state())
    assert any("local economic gate failed" in e for e in state["errors"])
