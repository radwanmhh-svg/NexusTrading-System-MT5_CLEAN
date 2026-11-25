"""Contract tests for nexus_api routers (audit F-07).

These tests pin the REAL contract used by the dashboard: every endpoint the
UI calls must return 200 with the expected schema — a 404 (dead route) is a
failure, not an acceptable outcome. This replaces the old tolerant
`in (200, 404)` assertions that let dead routes pass silently.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestPositionsRouter:
    @pytest.mark.asyncio
    async def test_list_positions_contract(self, api_client: AsyncClient):
        resp = await api_client.get("/api/positions")
        assert resp.status_code == 200
        body = resp.json()
        # Contract: count + positions list always present (values depend on
        # the live paper session — do not pin exact counts).
        assert "count" in body
        assert isinstance(body["positions"], list)

    @pytest.mark.asyncio
    async def test_get_position_not_found(self, api_client: AsyncClient):
        resp = await api_client.get("/api/positions/nonexistent")
        assert resp.status_code == 404


class TestTradesRouter:
    @pytest.mark.asyncio
    async def test_list_trades_contract(self, api_client: AsyncClient):
        resp = await api_client.get("/api/trades")
        assert resp.status_code == 200
        body = resp.json()
        # T9 (ق19.7 حرفياً): العقد الكامل {"trades","total","page","page_size"}
        assert {"trades", "total", "page", "page_size"} <= body.keys()


class TestPerformanceRouter:
    @pytest.mark.asyncio
    async def test_get_performance_contract(self, api_client: AsyncClient):
        resp = await api_client.get("/api/performance")
        assert resp.status_code == 200
        body = resp.json()
        # nexus_data.get_performance_metrics shape
        assert {"total_pnl", "total_trades", "win_rate"} <= body.keys()


class TestAgentsRouter:
    @pytest.mark.asyncio
    async def test_list_agents_status(self, api_client: AsyncClient):
        # The dashboard consumes /api/agents/status (not bare /api/agents).
        resp = await api_client.get("/api/agents/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "agents" in body


class TestRiskRouter:
    @pytest.mark.asyncio
    async def test_get_risk_status(self, api_client: AsyncClient):
        resp = await api_client.get("/api/risk/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "balance" in body or "circuit_breakers" in body


class TestSystemRouter:
    @pytest.mark.asyncio
    async def test_health(self, api_client: AsyncClient):
        resp = await api_client.get("/api/system/health")
        assert resp.status_code == 200


class TestConfigRouter:
    @pytest.mark.asyncio
    async def test_get_risk_settings(self, api_client: AsyncClient):
        # Dashboard reads risk knobs from /api/config/risk (audit F-05 fix).
        resp = await api_client.get("/api/config/risk")
        assert resp.status_code == 200
        body = resp.json()
        assert {"max_risk_per_trade_pct", "daily_trade_limit"} <= body.keys()


class TestRemovedRoutes:
    """Routes the dashboard once called but that never existed (audit F-05).

    After the UI fix these must remain dead — a new alias would hide the
    mistake again. The GatewayDiagnostic now calls the real routes.
    """

    @pytest.mark.asyncio
    async def test_settings_alias_is_dead(self, api_client: AsyncClient):
        resp = await api_client.get("/api/settings")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_trade_memory_metrics_alias_is_dead(self, api_client: AsyncClient):
        resp = await api_client.get("/api/trade-memory/metrics")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_auth_v2_is_not_implemented(self, api_client: AsyncClient):
        # Login UI is deferred by decision (2026-11-18) — no half-auth allowed.
        resp = await api_client.post("/api/v2/auth/login", json={})
        assert resp.status_code == 404


class TestKillSwitchContract:
    """T10: بوابة القبول — kill switch يعمل (ق21)."""

    @pytest.mark.asyncio
    async def test_kill_switch_endpoint(self, api_client: AsyncClient):
        resp = await api_client.post("/api/system/kill-switch", json={"reason": "t10_acceptance_test", "pin": "000000"})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "kill_switch_activated"

    @pytest.mark.asyncio
    async def test_shadow_mode_registered(self):
        # وضع SHADOW معرف رسمياً في الثوابت (لا تنفيذ — تسجيل فقط)
        from shared.constants import TradingMode
        assert TradingMode.SHADOW.value == "shadow"
