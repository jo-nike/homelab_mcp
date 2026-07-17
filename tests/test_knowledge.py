"""Tests for knowledge lookup tools: doc search, service/host/IP lookup."""

from unittest.mock import MagicMock

import pytest

import config
from tools import knowledge


@pytest.fixture(autouse=True)
def populate_config(
    sample_services_yaml,
    sample_hosts_yaml,
    sample_docs_dir,
    monkeypatch,
    restore_config_registries,
):
    """Populate config registries with sample data for all knowledge tests.

    Both sample fixtures write into the same tmp_path, so hosts.yaml already sits
    next to services.yaml -- no copy needed. restore_config_registries snapshots
    and restores the registries so sample data does not leak into later tests."""
    monkeypatch.setattr(config, "DATA_DIR", sample_services_yaml.parent)
    monkeypatch.setattr(config, "DOCS_DIR", sample_docs_dir)

    config.SERVICES.clear()
    config.HOSTS.clear()
    config.IP_INDEX.clear()
    config.DOCS_INDEX.clear()

    config.SERVICES.update(config.load_services())
    config.HOSTS.update(config.load_hosts())
    config.IP_INDEX.update(config.build_ip_index())
    config.DOCS_INDEX.update(config.load_docs_index())


@pytest.fixture
def registered_mcp():
    """Register knowledge tools onto a fresh FastMCP instance."""
    from fastmcp import FastMCP

    mcp = FastMCP("test")
    knowledge.register(mcp)
    return mcp


@pytest.fixture
def mock_ctx():
    """Create a mock Context (knowledge tools don't need httpx clients)."""
    ctx = MagicMock()
    ctx.lifespan_context = {}
    return ctx


@pytest.mark.asyncio
async def test_search_docs_finds_match(registered_mcp, mock_ctx):
    """search_docs('monitoring') returns results with matching paragraphs."""
    tool = await registered_mcp.get_tool("search_docs")
    result = await tool.fn(ctx=mock_ctx, query="monitoring")

    assert "results" in result
    assert len(result["results"]) > 0
    assert result["query"] == "monitoring"
    # Each result has file, section, snippet
    hit = result["results"][0]
    assert "file" in hit
    assert "section" in hit
    assert "snippet" in hit


@pytest.mark.asyncio
async def test_search_docs_case_insensitive(registered_mcp, mock_ctx):
    """search_docs('MONITORING') returns same results as 'monitoring'."""
    tool = await registered_mcp.get_tool("search_docs")
    result_upper = await tool.fn(ctx=mock_ctx, query="MONITORING")
    result_lower = await tool.fn(ctx=mock_ctx, query="monitoring")

    assert len(result_upper["results"]) == len(result_lower["results"])
    assert len(result_upper["results"]) > 0


@pytest.mark.asyncio
async def test_search_docs_no_match(registered_mcp, mock_ctx):
    """search_docs('xyznonexistent') returns empty results list."""
    tool = await registered_mcp.get_tool("search_docs")
    result = await tool.fn(ctx=mock_ctx, query="xyznonexistent")

    assert result["results"] == []
    assert result["total_matches"] == 0


@pytest.mark.asyncio
async def test_search_docs_no_docs_loaded_is_distinguishable(
    registered_mcp, mock_ctx, monkeypatch
):
    """Regression (WP5): an empty DOCS_INDEX (unsynced data/) returns an
    explicit message with lowered confidence, not a bare no-match."""
    monkeypatch.setattr(config, "DOCS_INDEX", {})
    tool = await registered_mcp.get_tool("search_docs")
    result = await tool.fn(ctx=mock_ctx, query="monitoring")

    assert result["results"] == []
    assert "refresh_docs" in result["message"]
    assert result["_meta"]["confidence"] == "medium"


@pytest.mark.asyncio
async def test_get_service_info_found(registered_mcp, mock_ctx):
    """get_service_info('prometheus') returns full service info."""
    tool = await registered_mcp.get_tool("get_service_info")
    result = await tool.fn(ctx=mock_ctx, name="prometheus")

    assert result["name"] == "prometheus"
    assert result["host"] == "docker-host"
    assert result["ip"] == "192.168.1.79"
    assert result["port"] == 9090
    assert result["stack"] == "dh_grafana_stack"
    assert "role" in result
    assert "auth" in result


@pytest.mark.asyncio
async def test_get_service_info_not_found(registered_mcp, mock_ctx):
    """get_service_info('nonexistent') returns error dict."""
    tool = await registered_mcp.get_tool("get_service_info")
    result = await tool.fn(ctx=mock_ctx, name="nonexistent")

    assert result["error"] == "not_found"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_service_info_cross_reference(registered_mcp, mock_ctx):
    """get_service_info('prometheus') includes co-located services (D-06)."""
    tool = await registered_mcp.get_tool("get_service_info")
    result = await tool.fn(ctx=mock_ctx, name="prometheus")

    # prometheus and grafana both sit on docker-host (192.168.1.79), so grafana
    # must appear in prometheus's co_located_services -- a positive cross-ref case.
    assert "co_located_services" in result
    co_located = result["co_located_services"]
    names = (
        co_located
        if not co_located or isinstance(co_located[0], str)
        else [s.get("name") for s in co_located]
    )
    assert "grafana" in names
    assert "prometheus" not in names  # a service is not co-located with itself


@pytest.mark.asyncio
async def test_get_host_info_by_name(registered_mcp, mock_ctx):
    """get_host_info('docker-host') returns host info with services."""
    tool = await registered_mcp.get_tool("get_host_info")
    result = await tool.fn(ctx=mock_ctx, name_or_ip="docker-host")

    assert result["name"] == "docker-host"
    assert result["ip"] == "192.168.1.79"
    assert "role" in result
    assert "os" in result
    assert "services" in result
    # prometheus is on docker-host
    service_names = [s["name"] for s in result["services"]]
    assert "prometheus" in service_names


@pytest.mark.asyncio
async def test_get_host_info_by_ip(registered_mcp, mock_ctx):
    """get_host_info('192.168.1.79') returns docker-host info."""
    tool = await registered_mcp.get_tool("get_host_info")
    result = await tool.fn(ctx=mock_ctx, name_or_ip="192.168.1.79")

    assert result["name"] == "docker-host"
    assert result["ip"] == "192.168.1.79"


@pytest.mark.asyncio
async def test_get_host_info_not_found(registered_mcp, mock_ctx):
    """get_host_info('nonexistent') returns error dict."""
    tool = await registered_mcp.get_tool("get_host_info")
    result = await tool.fn(ctx=mock_ctx, name_or_ip="nonexistent")

    assert result["error"] == "not_found"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_ip_info(registered_mcp, mock_ctx):
    """get_ip_info('192.168.1.79') returns host and services."""
    tool = await registered_mcp.get_tool("get_ip_info")
    result = await tool.fn(ctx=mock_ctx, ip="192.168.1.79")

    assert result["ip"] == "192.168.1.79"
    assert result["host"] is not None
    assert result["host"]["name"] == "docker-host"
    assert "services" in result
    service_names = [s["name"] for s in result["services"]]
    assert "prometheus" in service_names


@pytest.mark.asyncio
async def test_get_ip_info_not_found(registered_mcp, mock_ctx):
    """get_ip_info('10.0.0.1') returns error dict."""
    tool = await registered_mcp.get_tool("get_ip_info")
    result = await tool.fn(ctx=mock_ctx, ip="10.0.0.1")

    assert result["error"] == "not_found"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_ip_info_shorthand(registered_mcp, mock_ctx):
    """get_ip_info('.79') expands to '192.168.1.79'."""
    tool = await registered_mcp.get_tool("get_ip_info")
    result = await tool.fn(ctx=mock_ctx, ip=".79")

    assert result["ip"] == "192.168.1.79"
    assert result["host"]["name"] == "docker-host"
