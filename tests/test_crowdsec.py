"""Tests for CrowdSec security tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import crowdsec

# --- Helpers ---


# --- Sample data ---

DECISIONS = [
    # Local detections (origin: crowdsec)
    {
        "id": 201,
        "origin": "crowdsec",
        "type": "ban",
        "scope": "Ip",
        "value": "10.0.0.5",
        "duration": "3h59m",
        "scenario": "crowdsecurity/ssh-bf",
    },
    {
        "id": 202,
        "origin": "crowdsec",
        "type": "ban",
        "scope": "Ip",
        "value": "10.0.0.6",
        "duration": "2h15m",
        "scenario": "crowdsecurity/http-probing",
    },
    # Community blocklist (origin: CAPI)
    {
        "id": 301,
        "origin": "CAPI",
        "type": "ban",
        "scope": "Ip",
        "value": "1.2.3.4",
        "duration": "36m",
        "scenario": "crowdsecurity/ssh-bf",
    },
    {
        "id": 302,
        "origin": "CAPI",
        "type": "ban",
        "scope": "Ip",
        "value": "5.6.7.8",
        "duration": "36m",
        "scenario": "crowdsecurity/http-bruteforce",
    },
    {
        "id": 303,
        "origin": "CAPI",
        "type": "ban",
        "scope": "Ip",
        "value": "9.10.11.12",
        "duration": "36m",
        "scenario": "crowdsecurity/ssh-bf",
    },
]


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with CrowdSec tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "CROWDSEC_URL", "http://192.168.1.79:8180"),
        patch.object(config, "CROWDSEC_API_KEY", "test-bouncer-key"),
    ):
        crowdsec.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx client."""
    return AsyncMock()


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    app = FastMCP("test-skip")
    with (
        patch.object(config, "CROWDSEC_URL", ""),
        patch.object(config, "CROWDSEC_API_KEY", "key"),
    ):
        crowdsec.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_no_key():
    app = FastMCP("test-skip")
    with (
        patch.object(config, "CROWDSEC_URL", "http://crowdsec:8180"),
        patch.object(config, "CROWDSEC_API_KEY", ""),
    ):
        crowdsec.register(app)
    assert count_tools(app) == 0


def test_register_adds_tool():
    app = FastMCP("test-add")
    with (
        patch.object(config, "CROWDSEC_URL", "http://192.168.1.79:8180"),
        patch.object(config, "CROWDSEC_API_KEY", "test-key"),
    ):
        crowdsec.register(app)
    assert count_tools(app) == 1


# --- Test: get_crowdsec_overview ---


@pytest.mark.asyncio
async def test_splits_local_from_community(mcp_app, mock_client):
    """Local detections are separated from CAPI community blocklist."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(DECISIONS))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    assert result["local_ban_count"] == 2
    assert result["community_blocklist_count"] == 3
    assert result["total_decisions"] == 5
    assert len(result["local_decisions"]) == 2

    # Local decisions should not include CAPI
    for d in result["local_decisions"]:
        assert d["origin"] != "CAPI"


@pytest.mark.asyncio
async def test_lists_origin_counted_as_community_not_local(mcp_app, mock_client):
    """Regression (item 15): subscribed-blocklist decisions (origin 'lists') are
    community, not local; an unknown origin goes to the 'other' bucket."""
    decisions = [
        {"origin": "crowdsec", "type": "ban", "scenario": "ssh-bf", "value": "1.1.1.1"},
        {"origin": "lists", "type": "ban", "scenario": "firehol", "value": "2.2.2.2"},
        {"origin": "cscli", "type": "ban", "scenario": "manual", "value": "3.3.3.3"},
        {"origin": "weird", "type": "ban", "scenario": "?", "value": "4.4.4.4"},
    ]
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(decisions))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    # crowdsec + cscli are local; 'lists' is community; 'weird' is other.
    assert result["local_ban_count"] == 2
    assert result["community_blocklist_count"] == 1
    assert result["other_count"] == 1
    local_origins = {d["origin"] for d in result["local_decisions"]}
    assert local_origins == {"crowdsec", "cscli"}


@pytest.mark.asyncio
async def test_local_top_scenarios(mcp_app, mock_client):
    """Top scenarios are computed separately for local detections."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(DECISIONS))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    local_scenarios = {s["scenario"]: s["count"] for s in result["local_top_scenarios"]}
    assert local_scenarios["crowdsecurity/ssh-bf"] == 1
    assert local_scenarios["crowdsecurity/http-probing"] == 1


@pytest.mark.asyncio
async def test_community_top_scenarios(mcp_app, mock_client):
    """Top scenarios are computed for community blocklist."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(DECISIONS))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    capi_scenarios = {
        s["scenario"]: s["count"] for s in result["community_top_scenarios"]
    }
    assert capi_scenarios["crowdsecurity/ssh-bf"] == 2
    assert capi_scenarios["crowdsecurity/http-bruteforce"] == 1


@pytest.mark.asyncio
async def test_summary_distinguishes_local_and_community(mcp_app, mock_client):
    """Summary mentions both local and community counts."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(DECISIONS))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    # Assert the count and both terms appear, without pinning exact wording.
    assert "2" in result["summary"]
    assert "local" in result["summary"]
    assert "community" in result["summary"]
    assert result["local_ban_count"] == 2


@pytest.mark.asyncio
async def test_null_decisions(mcp_app, mock_client):
    """Handles null response (no active bans)."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(None))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    assert result["local_decisions"] == []
    assert result["local_ban_count"] == 0
    assert result["community_blocklist_count"] == 0


@pytest.mark.asyncio
async def test_timeout(mcp_app, mock_client):
    """Returns error dict on timeout."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_non_json_body(mcp_app, mock_client):
    """A 200 with a non-JSON body returns invalid_response, not a raised error."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    html_resp = httpx.Response(
        status_code=200,
        content=b"<html>error</html>",
        request=httpx.Request("GET", "http://test"),
    )
    mock_client.get = AsyncMock(return_value=html_resp)

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "invalid_response"


@pytest.mark.asyncio
async def test_connection_error(mcp_app, mock_client):
    """Returns error dict on connection failure."""
    ctx = make_mock_ctx(crowdsec=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    tool_fn = get_tool_fn(mcp_app, "get_crowdsec_overview")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "connection_error"
    assert "Connection refused" in result["message"]
