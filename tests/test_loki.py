"""Tests for Loki log query tools."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import config
from tests.conftest import make_response
from tools import loki

SAMPLE_LOKI_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {
                    "container": "grafana",
                    "host": "docker-host",
                    "job": "docker",
                },
                "values": [
                    [
                        "1712000000000000000",
                        'level=error msg="something failed"',
                    ],
                    [
                        "1711999000000000000",
                        'level=error msg="connection refused"',
                    ],
                ],
            }
        ],
    },
}

EMPTY_LOKI_RESPONSE = {
    "status": "success",
    "data": {"resultType": "streams", "result": []},
}


@pytest.fixture
def mock_loki_client():
    """Create a mock httpx.AsyncClient for Loki backed by a real httpx.Response
    (matching every other test file) so status-code semantics are real."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = make_response(SAMPLE_LOKI_RESPONSE)
    return client


@pytest.fixture
def mock_ctx(mock_loki_client):
    """Create a mock FastMCP Context with Loki client."""
    ctx = MagicMock()
    ctx.lifespan_context = {"loki": mock_loki_client}
    return ctx


@pytest.fixture
def registered_mcp(monkeypatch):
    """Register Loki tools onto a fresh FastMCP instance and return it."""
    from fastmcp import FastMCP

    monkeypatch.setattr(config, "LOKI_URL", "http://localhost:3100")
    mcp = FastMCP("test")
    loki.register(mcp)
    return mcp


@pytest.mark.asyncio
async def test_register_skips_when_no_url(monkeypatch):
    """When LOKI_URL is empty, register() adds no tools."""
    from fastmcp import FastMCP

    monkeypatch.setattr(config, "LOKI_URL", "")
    mcp = FastMCP("test")
    loki.register(mcp)
    tools = await mcp.list_tools()
    assert len(tools) == 0


@pytest.mark.asyncio
async def test_get_recent_errors_default(registered_mcp, mock_ctx):
    """get_recent_errors returns dict with entries list using default params."""
    tool = await registered_mcp.get_tool("get_recent_errors")
    result = await tool.fn(ctx=mock_ctx)

    assert "entries" in result
    assert "query" in result
    assert "time_range_minutes" in result
    assert result["time_range_minutes"] == 60
    assert len(result["entries"]) == 2

    # Each entry should have timestamp, line, labels
    entry = result["entries"][0]
    assert "timestamp" in entry
    assert "line" in entry
    assert "labels" in entry
    assert entry["labels"]["container"] == "grafana"


@pytest.mark.asyncio
async def test_get_recent_errors_with_host(registered_mcp, mock_ctx, mock_loki_client):
    """When host is provided, LogQL includes host filter."""
    tool = await registered_mcp.get_tool("get_recent_errors")
    await tool.fn(ctx=mock_ctx, host="plex-stack")

    # Verify the query parameter includes host filter
    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]
    assert 'host="plex-stack"' in params["query"]


@pytest.mark.asyncio
async def test_get_container_logs(registered_mcp, mock_ctx, mock_loki_client):
    """get_container_logs returns entries for a specific container."""
    tool = await registered_mcp.get_tool("get_container_logs")
    result = await tool.fn(ctx=mock_ctx, container="grafana")

    assert "container" in result
    assert result["container"] == "grafana"
    assert "entries" in result
    assert "time_range_minutes" in result

    # Verify the query uses container selector
    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]
    assert 'container="grafana"' in params["query"]


@pytest.mark.asyncio
async def test_get_recent_errors_escapes_host_quotes(
    registered_mcp, mock_ctx, mock_loki_client
):
    """A host value containing a double-quote must be escaped, not break out."""
    tool = await registered_mcp.get_tool("get_recent_errors")
    await tool.fn(ctx=mock_ctx, host='x" or job="')

    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]
    # The embedded quote is backslash-escaped, so the matcher stays intact.
    assert 'host="x\\" or job=\\""' in params["query"]


@pytest.mark.asyncio
async def test_get_container_logs_escapes_quotes(
    registered_mcp, mock_ctx, mock_loki_client
):
    """A container value containing a double-quote must be escaped."""
    tool = await registered_mcp.get_tool("get_container_logs")
    await tool.fn(ctx=mock_ctx, container='g" }|~"')

    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]
    assert 'container="g\\" }|~\\""' in params["query"]


@pytest.mark.asyncio
async def test_query_logs(registered_mcp, mock_ctx, mock_loki_client):
    """query_logs passes LogQL directly and returns raw response (D-03)."""
    tool = await registered_mcp.get_tool("query_logs")
    result = await tool.fn(ctx=mock_ctx, query='{job="docker"}')

    # Should return the raw Loki response (no transformation per D-03)
    assert result["status"] == "success"
    assert "data" in result
    assert result["data"]["resultType"] == "streams"

    # Verify query was passed through
    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]
    assert params["query"] == '{job="docker"}'


@pytest.mark.asyncio
async def test_get_helper_timeout(registered_mcp, mock_ctx, mock_loki_client):
    """Returns error dict on timeout."""
    mock_loki_client.get.side_effect = httpx.TimeoutException("timeout")
    tool = await registered_mcp.get_tool("get_recent_errors")
    result = await tool.fn(ctx=mock_ctx)

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_helper_http_error(registered_mcp, mock_ctx, mock_loki_client):
    """A 5xx from Loki surfaces {'error': 'http_error'} with the status code."""
    mock_loki_client.get.return_value = make_response(
        {"status": "error"}, status_code=500
    )
    tool = await registered_mcp.get_tool("get_recent_errors")
    result = await tool.fn(ctx=mock_ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_get_helper_connection_error(registered_mcp, mock_ctx, mock_loki_client):
    """A transport-level failure surfaces {'error': 'connection_error'}."""
    mock_loki_client.get.side_effect = httpx.ConnectError("refused")
    tool = await registered_mcp.get_tool("get_recent_errors")
    result = await tool.fn(ctx=mock_ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_loki_nanosecond_timestamps(registered_mcp, mock_ctx, mock_loki_client):
    """Start/end params use nanosecond Unix timestamps."""
    tool = await registered_mcp.get_tool("get_recent_errors")
    await tool.fn(ctx=mock_ctx)

    call_args = mock_loki_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1]

    # Nanosecond timestamps should be 19 digits
    start_ts = params["start"]
    end_ts = params["end"]
    assert len(start_ts) >= 19, f"Start timestamp {start_ts} not nanosecond precision"
    assert len(end_ts) >= 19, f"End timestamp {end_ts} not nanosecond precision"
