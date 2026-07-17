"""Tests for Prowlarr indexer status tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import prowlarr

# --- Helpers ---


# --- Mock data ---

MOCK_INDEXERS = [
    {
        "id": 1,
        "name": "NZBgeek",
        "enable": True,
        "protocol": "usenet",
        "added": "2025-01-15T10:00:00Z",
        "fields": [],
    },
    {
        "id": 2,
        "name": "1337x",
        "enable": True,
        "protocol": "torrent",
        "added": "2025-02-20T12:00:00Z",
        "fields": [],
    },
    {
        "id": 3,
        "name": "OldIndexer",
        "enable": False,
        "protocol": "usenet",
        "added": "2024-06-01T08:00:00Z",
        "fields": [],
    },
]

MOCK_HEALTH = [
    {
        "source": "IndexerStatusCheck",
        "type": "warning",
        "message": "Indexer OldIndexer is unavailable due to failures",
        "wikiUrl": "https://wiki.servarr.com/prowlarr/system#indexers-unavailable",
    },
]

# OldIndexer (id=3) is backing off until the far future -> "error".
MOCK_INDEXER_STATUS = [
    {
        "id": 10,
        "indexerId": 3,
        "disabledTill": "2099-01-01T00:00:00Z",
        "mostRecentFailure": "2026-07-15T22:00:00Z",
        "initialFailure": "2026-07-15T21:00:00Z",
    },
]


# --- Tests ---


@pytest.mark.asyncio
async def test_indexerstatus_failure_degrades_confidence():
    """Regression (WP5): if the indexerstatus endpoint errors, indexers default
    to 'healthy' but confidence drops to medium and a flag is set, so 'all
    healthy' is distinguishable from 'health data unavailable'."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_INDEXERS),
            make_response(MOCK_HEALTH),
            httpx.ConnectError("indexerstatus down"),
        ]
    )
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    assert result["_meta"]["confidence"] == "medium"
    assert result["indexer_status_unavailable"] is True


@pytest.mark.asyncio
async def test_get_prowlarr_status():
    """Test happy path: indexer list with health status."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_INDEXERS),
            make_response(MOCK_HEALTH),
            make_response(MOCK_INDEXER_STATUS),
        ]
    )
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    # Summary string
    assert "3 indexers" in result["summary"]
    assert "2 enabled" in result["summary"]
    assert "1 disabled" in result["summary"]
    assert "1 health warning" in result["summary"]

    # Indexer list
    assert result["indexer_count"] == 3
    assert result["enabled_count"] == 2
    assert len(result["indexers"]) == 3
    assert result["indexers"][0]["name"] == "NZBgeek"
    assert result["indexers"][0]["enabled"] is True
    assert result["indexers"][0]["protocol"] == "usenet"
    assert result["indexers"][1]["name"] == "1337x"
    assert result["indexers"][1]["protocol"] == "torrent"
    assert result["indexers"][2]["enabled"] is False

    # Per-indexer status: only OldIndexer (id=3) is in indexerstatus, backing off.
    assert result["indexers"][0]["status"] == "healthy"
    assert result["indexers"][1]["status"] == "healthy"
    assert result["indexers"][2]["status"] == "error"

    # Health issues
    assert result["health_issue_count"] == 1
    assert len(result["health_issues"]) == 1
    assert result["health_issues"][0]["source"] == "IndexerStatusCheck"
    assert result["health_issues"][0]["type"] == "warning"
    assert "OldIndexer" in result["health_issues"][0]["message"]


@pytest.mark.asyncio
async def test_get_prowlarr_status_no_health_issues():
    """Test with no health warnings."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_INDEXERS),
            make_response([]),
            make_response([]),
        ]
    )
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    assert result["health_issue_count"] == 0
    assert result["health_issues"] == []
    assert "0 health warnings" in result["summary"]
    # No indexerstatus entries -> every indexer is healthy.
    assert all(i["status"] == "healthy" for i in result["indexers"])


@pytest.mark.asyncio
async def test_get_prowlarr_status_warning_and_error():
    """Test warning (failing, not disabled) vs error (backing off) states."""
    status = [
        # id=1 recorded failures but not currently disabled -> warning
        {"id": 20, "indexerId": 1, "disabledTill": None},
        # id=2 disabled in the past (no longer backing off) -> warning
        {"id": 21, "indexerId": 2, "disabledTill": "2000-01-01T00:00:00Z"},
        # id=3 currently backing off -> error
        {"id": 22, "indexerId": 3, "disabledTill": "2099-01-01T00:00:00Z"},
    ]
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_INDEXERS),
            make_response([]),
            make_response(status),
        ]
    )
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    by_name = {i["name"]: i["status"] for i in result["indexers"]}
    assert by_name["NZBgeek"] == "warning"
    assert by_name["1337x"] == "warning"
    assert by_name["OldIndexer"] == "error"


@pytest.mark.asyncio
async def test_get_prowlarr_status_indexerstatus_error_non_fatal():
    """A failure on the indexerstatus endpoint degrades to healthy, not a whole-tool error."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_INDEXERS),
            make_response([]),
            make_response({"message": "boom"}, status_code=500),
        ]
    )
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    assert "error" not in result
    assert result["indexer_count"] == 3
    assert all(i["status"] == "healthy" for i in result["indexers"])


@pytest.mark.asyncio
async def test_get_prowlarr_status_timeout():
    """Test timeout returns error dict."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(prowlarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)

    fn = get_tool_fn(app, "get_prowlarr_status")
    result = await fn(ctx)

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_prowlarr_conditional_registration():
    """Test that tools are not registered when PROWLARR_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "PROWLARR_URL", None):
        prowlarr.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_prowlarr_register_guard_matches_lifespan():
    """Regression (item 11): register() must require the same creds as the
    lifespan (URL + API key), so a URL-only config does not register a tool
    with no client behind it (which would KeyError at call time)."""
    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", None),
    ):
        prowlarr.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_prowlarr_registers_when_configured():
    """Test that tools are registered when PROWLARR_URL is set."""
    app = FastMCP("test")
    with (
        patch.object(config, "PROWLARR_URL", "http://prowlarr:9696"),
        patch.object(config, "PROWLARR_API_KEY", "test-key"),
    ):
        prowlarr.register(app)
    assert count_tools(app) == 1
