"""Tests for Tautulli Plex analytics tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import tautulli

# --- Helpers ---


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Tautulli tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "TAUTULLI_URL", "http://fake:8181"),
        patch.object(config, "TAUTULLI_API_KEY", "fake-key"),
    ):
        tautulli.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Mock data ---


ACTIVITY_DATA = {
    "response": {
        "result": "success",
        "data": {
            "stream_count": "1",
            "total_bandwidth": "20000",
            "wan_bandwidth": "5000",
            "lan_bandwidth": "15000",
            "sessions": [
                {
                    "friendly_name": "Jon",
                    "full_title": "Inception",
                    "state": "playing",
                    "progress_percent": "40",
                },
            ],
        },
    }
}

HISTORY_DATA = {
    "response": {
        "result": "success",
        "data": {
            "data": [
                {
                    "full_title": "Dune",
                    "friendly_name": "Jon",
                    "date": 1712000000,
                    "duration": 7200,
                    "percent_complete": 100,
                },
            ]
        },
    }
}

STATS_DATA = {
    "response": {
        "result": "success",
        "data": [
            {
                "stat_id": "top_movies",
                "rows": [{"title": "Dune", "total_duration": 7200, "total_plays": 3}],
            },
            {
                "stat_id": "top_tv",
                "rows": [
                    {"title": "Severance", "total_duration": 3600, "total_plays": 5}
                ],
            },
            {
                "stat_id": "top_users",
                "rows": [
                    {"friendly_name": "Jon", "total_duration": 9000, "total_plays": 8}
                ],
            },
        ],
    }
}


# --- Tests ---


def test_register_adds_tool_when_config_set():
    """When Tautulli config is present, register() adds 1 tool."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "TAUTULLI_URL", "http://fake:8181"),
        patch.object(config, "TAUTULLI_API_KEY", "fake-key"),
    ):
        tautulli.register(app)
    assert count_tools(app) == 1


def test_register_skips_when_no_config():
    """When Tautulli config is missing, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "TAUTULLI_URL", ""):
        tautulli.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_slow_subqueries_use_tightened_timeout(mcp_app, mock_client):
    """get_history and get_home_stats are called with an 8s timeout override."""
    ctx = make_mock_ctx(tautulli=mock_client)

    def dispatch(path, **kwargs):
        cmd = kwargs["params"]["cmd"]
        if cmd == "get_activity":
            return make_response(ACTIVITY_DATA)
        if cmd == "get_history":
            return make_response(HISTORY_DATA)
        return make_response(STATS_DATA)

    mock_client.get = AsyncMock(side_effect=dispatch)

    tool_fn = get_tool_fn(mcp_app, "get_tautulli_activity")
    result = await tool_fn(ctx=ctx)

    # current_activity always succeeds and carries the bandwidth split.
    assert result["current_activity"]["lan_bandwidth_mbps"] == 15.0
    assert result["current_activity"]["wan_bandwidth_mbps"] == 5.0
    # Regression (item 26): progress reads the real 'progress_percent' field.
    assert result["current_activity"]["sessions"][0]["progress_percent"] == 40

    timeouts_by_cmd = {
        call.kwargs["params"]["cmd"]: call.kwargs.get("timeout")
        for call in mock_client.get.call_args_list
    }
    assert timeouts_by_cmd["get_activity"] is None
    assert timeouts_by_cmd["get_history"] == 8.0
    assert timeouts_by_cmd["get_home_stats"] == 8.0


@pytest.mark.asyncio
async def test_history_and_top_stats_parsed(mcp_app, mock_client):
    """recent_history fields and the top_movies/top_tv/top_users buckets are parsed
    (duration converted to minutes, Tautulli string numbers coerced to int)."""
    ctx = make_mock_ctx(tautulli=mock_client)

    def dispatch(path, **kwargs):
        cmd = kwargs["params"]["cmd"]
        if cmd == "get_activity":
            return make_response(ACTIVITY_DATA)
        if cmd == "get_history":
            return make_response(HISTORY_DATA)
        return make_response(STATS_DATA)

    mock_client.get = AsyncMock(side_effect=dispatch)

    tool_fn = get_tool_fn(mcp_app, "get_tautulli_activity")
    result = await tool_fn(ctx=ctx)

    hist = result["recent_history"][0]
    assert hist["title"] == "Dune"
    assert hist["user"] == "Jon"
    assert hist["duration_minutes"] == 120  # 7200s -> 120min
    assert hist["percent_complete"] == 100

    top = result["top_stats"]
    assert top["top_movies"][0]["title"] == "Dune"
    assert top["top_movies"][0]["total_plays"] == 3
    assert top["top_tv"][0]["title"] == "Severance"
    assert top["top_users"][0]["user"] == "Jon"
    assert top["top_users"][0]["total_plays"] == 8


@pytest.mark.asyncio
async def test_slow_subquery_timeout_does_not_sink_tool(mcp_app, mock_client):
    """A hanging history sub-query degrades to an error marker; activity still returns."""
    ctx = make_mock_ctx(tautulli=mock_client)

    def dispatch(path, **kwargs):
        cmd = kwargs["params"]["cmd"]
        if cmd == "get_activity":
            return make_response(ACTIVITY_DATA)
        raise httpx.TimeoutException("timed out")

    mock_client.get = AsyncMock(side_effect=dispatch)

    tool_fn = get_tool_fn(mcp_app, "get_tautulli_activity")
    result = await tool_fn(ctx=ctx)

    assert result["current_activity"]["stream_count"] == 1
    assert result["recent_history"]["error"] == "timeout"
    assert result["top_stats"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_api_level_error_result_surfaced(mcp_app, mock_client):
    """Regression (WP5): a 200 with response.result=='error' (e.g. invalid
    apikey) surfaces an api_error dict, not empty data at high confidence."""
    ctx = make_mock_ctx(tautulli=mock_client)
    mock_client.get = AsyncMock(
        return_value=make_response(
            {"response": {"result": "error", "message": "Invalid apikey", "data": {}}}
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_tautulli_activity")
    result = await tool_fn(ctx=ctx)

    # Every sub-command hit the same api_error.
    assert result["current_activity"]["error"] == "api_error"
    assert result["current_activity"]["message"] == "Invalid apikey"


@pytest.mark.asyncio
async def test_http_error_does_not_leak_api_key(mcp_app, mock_client):
    """A Tautulli 4xx must not put the apikey query param into the error message."""
    ctx = make_mock_ctx(tautulli=mock_client)

    request = httpx.Request(
        "GET", "http://fake:8181/api/v2?apikey=SUPER_SECRET_KEY&cmd=get_activity"
    )
    response = httpx.Response(401, request=request)
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Client error", request=request, response=response
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_tautulli_activity")
    result = await tool_fn(ctx=ctx)

    # The activity sub-result carries the error dict; the key must be scrubbed.
    activity = result["current_activity"]
    assert activity["error"] == "http_error"
    assert "SUPER_SECRET_KEY" not in activity["message"]
