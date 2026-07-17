"""Tests for MySpeed speed test tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import myspeed

# --- Helpers ---


# --- Mock data ---

# MySpeed API returns download/upload already in Mbps (e.g., 862.2 = 862.2 Mbps)

MOCK_SPEEDTESTS = [
    {
        "id": 5,
        "download": 250.0,
        "upload": 25.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T20:00:00Z",
    },
    {
        "id": 4,
        "download": 240.0,
        "upload": 24.0,
        "ping": 14,
        "jitter": 4,
        "created": "2026-04-03T19:00:00Z",
    },
    {
        "id": 3,
        "download": 248.0,
        "upload": 24.8,
        "ping": 11,
        "jitter": 2,
        "created": "2026-04-03T18:00:00Z",
    },
    {
        "id": 2,
        "download": 244.0,
        "upload": 24.4,
        "ping": 13,
        "jitter": 3,
        "created": "2026-04-03T17:00:00Z",
    },
    {
        "id": 1,
        "download": 236.0,
        "upload": 23.6,
        "ping": 15,
        "jitter": 5,
        "created": "2026-04-03T16:00:00Z",
    },
]

# Improving trend: latest is >10% higher than avg of previous 4
MOCK_SPEEDTESTS_IMPROVING = [
    {
        "id": 5,
        "download": 400.0,
        "upload": 30.0,
        "ping": 10,
        "jitter": 2,
        "created": "2026-04-03T20:00:00Z",
    },
    {
        "id": 4,
        "download": 250.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T19:00:00Z",
    },
    {
        "id": 3,
        "download": 250.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T18:00:00Z",
    },
    {
        "id": 2,
        "download": 250.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T17:00:00Z",
    },
    {
        "id": 1,
        "download": 250.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T16:00:00Z",
    },
]

# Degrading trend: latest is >10% lower than avg of previous 4
MOCK_SPEEDTESTS_DEGRADING = [
    {
        "id": 5,
        "download": 200.0,
        "upload": 30.0,
        "ping": 20,
        "jitter": 5,
        "created": "2026-04-03T20:00:00Z",
    },
    {
        "id": 4,
        "download": 300.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T19:00:00Z",
    },
    {
        "id": 3,
        "download": 300.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T18:00:00Z",
    },
    {
        "id": 2,
        "download": 300.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T17:00:00Z",
    },
    {
        "id": 1,
        "download": 300.0,
        "upload": 30.0,
        "ping": 12,
        "jitter": 3,
        "created": "2026-04-03T16:00:00Z",
    },
]


@pytest.fixture
def mcp_app():
    """FastMCP with MySpeed tools registered (mirrors test_docker's pattern)."""
    app = FastMCP("test")
    with patch.object(config, "MYSPEED_URL", "http://myspeed:5216"):
        myspeed.register(app)
    return app


# --- Tests ---


@pytest.mark.asyncio
async def test_get_myspeed_status(mcp_app):
    """Test happy path: latest speed test with history."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_SPEEDTESTS))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    # Latest result
    latest = result["latest"]
    assert latest["download_mbps"] == 250.0
    assert latest["upload_mbps"] == 25.0
    assert latest["ping_ms"] == 12
    assert latest["jitter_ms"] == 3
    assert latest["timestamp"] == "2026-04-03T20:00:00Z"

    # History
    assert result["history_count"] == 5
    assert len(result["history"]) == 5

    # Summary string
    assert "250.0" in result["summary"]
    assert "25.0" in result["summary"]
    assert "12" in result["summary"]

    # Trend (stable - latest 250 vs avg of prev ~242 = ~3% diff, within 10%)
    assert result["trend"] == "stable"


@pytest.mark.asyncio
async def test_get_myspeed_status_improving_trend(mcp_app):
    """Test trend detection: improving."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_SPEEDTESTS_IMPROVING))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    # Latest 400 Mbps vs avg of prev 4 (250 each = 250 avg) -> 60% higher
    assert result["trend"] == "improving"


@pytest.mark.asyncio
async def test_get_myspeed_status_degrading_trend(mcp_app):
    """Test trend detection: degrading."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_SPEEDTESTS_DEGRADING))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    # Latest 200 Mbps vs avg of prev 4 (300 each = 300 avg) -> 33% lower
    assert result["trend"] == "degrading"


@pytest.mark.asyncio
async def test_get_myspeed_status_empty_history(mcp_app):
    """Test empty speed test history."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response([]))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["latest"] is None
    assert result["history_count"] == 0
    assert result["history"] == []
    assert result["trend"] == "unknown"
    assert "No speed tests" in result["summary"]


@pytest.mark.asyncio
async def test_get_myspeed_status_timeout(mcp_app):
    """Test timeout returns error dict."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_myspeed_status_http_error(mcp_app):
    """A 5xx returns {'error': 'http_error'} with the status code."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response({"detail": "boom"}, status_code=500)
    )
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_get_myspeed_status_connection_error(mcp_app):
    """A transport-level failure returns {'error': 'connection_error'} (distinct
    from the timeout branch)."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_get_myspeed_status_non_json_body(mcp_app):
    """A 200 with a non-JSON body must return invalid_response, not raise."""
    html_resp = httpx.Response(
        status_code=200,
        content=b"<html>gateway error</html>",
        request=httpx.Request("GET", "http://test"),
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=html_resp)
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["error"] == "invalid_response"


@pytest.mark.asyncio
async def test_get_myspeed_status_works_with_plain_client(mcp_app):
    """Test that tool works with plain httpx.AsyncClient (no password)."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_SPEEDTESTS[:1]))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    # Should work fine - both plain client and SessionAuthManager have .get()
    assert result["latest"]["download_mbps"] == 250.0
    assert result["trend"] == "unknown"  # Only 1 test, not enough for trend


@pytest.mark.asyncio
async def test_get_myspeed_status_handles_failed_test_with_null_download(mcp_app):
    """Regression (item 7): a failed test carries null download/upload; the tool
    must coalesce to 0 instead of raising TypeError in round()."""
    tests = [
        {
            "id": 6,
            "download": None,
            "upload": None,
            "error": "no server",
            "created": "t6",
        },
        {"id": 5, "download": 250.0, "upload": 25.0, "ping": 12, "created": "t5"},
        {"id": 4, "download": 240.0, "upload": 24.0, "ping": 11, "created": "t4"},
        {"id": 3, "download": 245.0, "upload": 24.5, "ping": 12, "created": "t3"},
        {"id": 2, "download": 242.0, "upload": 24.2, "ping": 12, "created": "t2"},
    ]
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(tests))
    ctx = make_mock_ctx(myspeed=mock_client)

    fn = get_tool_fn(mcp_app, "get_myspeed_status")
    result = await fn(ctx)

    assert result["latest"]["download_mbps"] == 0
    assert result["latest"]["failed"] is True
    assert result["history_count"] == 5


@pytest.mark.asyncio
async def test_myspeed_conditional_registration():
    """Test that tools are not registered when MYSPEED_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "MYSPEED_URL", None):
        myspeed.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_myspeed_registers_when_configured(mcp_app):
    """Test that tools are registered when MYSPEED_URL is set."""
    assert count_tools(mcp_app) == 1
