"""Tests for health assessment tools."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_response
from tools import health


def _vector(instance, value):
    return {
        "data": {
            "result": [{"metric": {"instance": instance}, "value": [0, str(value)]}]
        }
    }


@pytest.fixture
def health_env(canonical_hosts, monkeypatch):
    """Populate config.HOSTS/TOPOLOGY for health tools alongside the seed."""
    monkeypatch.setattr(
        config,
        "HOSTS",
        {
            "docker-host": {"ip": "192.168.1.79", "role": "primary Docker host"},
            "plex-stack": {"ip": "192.168.1.108", "role": "media"},
        },
    )
    monkeypatch.setattr(
        config,
        "SERVICES",
        {"plex": {"ip": "192.168.1.108", "port": 32400}},
    )
    monkeypatch.setattr(
        config,
        "TOPOLOGY",
        {
            "vertical_stacks": [
                {
                    "host": "proxmox",
                    "ip": "192.168.1.114",
                    "children": [
                        {
                            "type": "vm",
                            "name": "docker-host",
                            "ip": "192.168.1.79",
                            "services": ["prometheus", "grafana"],
                        },
                        {
                            "type": "vm",
                            "name": "plex-stack",
                            "ip": "192.168.1.108",
                            "services": ["plex", "sonarr"],
                        },
                    ],
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_what_needs_attention_reports_unreachable_source(health_env):
    """Regression (WP5): a down Prometheus must yield sources_failed and
    degraded confidence, never a false 'All clear'."""
    app = FastMCP("test")
    health.register(app)

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "what_needs_attention")
    result = await fn(ctx)

    assert "prometheus" in result["sources_failed"]
    assert result["_meta"]["confidence"] == "medium"
    assert "All clear" not in result["summary"]
    assert "could not check" in result["summary"]


@pytest.mark.asyncio
async def test_what_needs_attention_all_clear_when_healthy(health_env):
    """A reachable Prometheus with no problems is a genuine 'All clear'."""
    app = FastMCP("test")
    health.register(app)

    client = MagicMock()
    client.get = AsyncMock(return_value=make_response({"data": {"result": []}}))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "what_needs_attention")
    result = await fn(ctx)

    assert result["sources_failed"] == []
    assert result["_meta"]["confidence"] == "high"
    assert result["summary"] == "All clear -- no issues detected"


@pytest.mark.asyncio
async def test_explain_host_health_filters_by_instance_name(health_env):
    """Regression: queries must filter by hostname instance, not IP, and a
    high metric must surface as an issue (the IP filter matched nothing so the
    tool always reported healthy)."""
    app = FastMCP("test")
    health.register(app)

    captured_queries = []

    async def fake_get(path, params=None):
        query = (params or {}).get("query", "")
        captured_queries.append(query)
        if "node_cpu_seconds_total" in query:
            return make_response(_vector("docker-host", 97.0))
        return make_response({"data": {"result": []}})

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "explain_host_health")
    result = await fn(ctx, "docker-host")

    # Every query filters by the resolved instance name, never the raw IP.
    assert captured_queries
    assert all('instance=~"(docker-host)(:.*)?"' in q for q in captured_queries)
    assert all("192.168.1.79" not in q for q in captured_queries)
    # The 97% CPU reading is now visible and flagged critical.
    assert result["metrics"]["cpu_percent"] == 97.0
    assert result["status"] == "critical"
    assert any(i["severity"] == "critical" for i in result["issues"])


@pytest.mark.asyncio
async def test_explain_host_health_child_host_has_services(health_env):
    """Regression (item 21): a VM host that is a child of the proxmox stack
    still reports its own services and its parent host."""
    app = FastMCP("test")
    health.register(app)

    client = MagicMock()
    client.get = AsyncMock(return_value=make_response({"data": {"result": []}}))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "explain_host_health")
    result = await fn(ctx, "docker-host")

    assert result["service_count"] == 2
    assert "prometheus" in result["services"]
    assert result["parent_host"] == "proxmox"


@pytest.mark.asyncio
async def test_explain_host_health_unknown_host_returns_not_found(health_env):
    """Regression (WP5): a typo'd host returns not_found, not status healthy."""
    app = FastMCP("test")
    health.register(app)

    client = MagicMock()
    client.get = AsyncMock(return_value=make_response({"data": {"result": []}}))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "explain_host_health")
    result = await fn(ctx, "nonexistent-host")

    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_explain_service_health_unknown_service_returns_not_found(health_env):
    """Regression (WP5): a typo'd service returns not_found, not status healthy."""
    app = FastMCP("test")
    health.register(app)

    client = MagicMock()
    client.get = AsyncMock(return_value=make_response({"data": {"result": []}}))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "explain_service_health")
    result = await fn(ctx, "nonexistent-service")

    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_explain_service_health_reports_down(health_env):
    """Regression: target status resolves via the service's scrape instance,
    so a down node exporter is reported as down."""
    app = FastMCP("test")
    health.register(app)

    captured = []

    async def fake_get(path, params=None):
        captured.append((params or {}).get("query", ""))
        return make_response(_vector("plex-stack", 0))

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    fn = get_tool_fn(app, "explain_service_health")
    result = await fn(ctx, "plex")

    assert any('instance=~"(plex-stack)(:.*)?"' in q for q in captured)
    assert result["target_status"] == "down"
    assert result["status"] == "down"
    # Item 16: report the VM the service runs in, not just the hypervisor.
    assert result["runs_on"] == "plex-stack"
    assert result["vm_ip"] == "192.168.1.108"
    assert result["physical_host"] == "proxmox"
    assert "runs on plex-stack (proxmox)" in result["summary"]
