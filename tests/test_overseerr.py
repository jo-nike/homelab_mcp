"""Tests for Overseerr media request tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import overseerr

# --- Helpers ---


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Overseerr tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "OVERSEERR_URL", "http://fake:5055"),
        patch.object(config, "OVERSEERR_API_KEY", "fake-api-key"),
    ):
        overseerr.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Mock data ---


MOCK_REQUESTS_RESPONSE = {
    "pageInfo": {"pages": 3, "pageSize": 10, "results": 25},
    "results": [
        {
            "id": 1,
            "status": 1,  # pending
            "createdAt": "2026-04-01T10:00:00.000Z",
            "requestedBy": {"displayName": "Jon", "username": "jon"},
            "media": {
                "mediaType": "movie",
                "tmdbId": 693134,
                "status": 2,  # pending
            },
        },
        {
            "id": 2,
            "status": 2,  # approved
            "createdAt": "2026-03-30T15:00:00.000Z",
            "requestedBy": {"username": "alice"},
            "media": {
                "mediaType": "tv",
                "tmdbId": 94997,
                "status": 5,  # available
            },
        },
        {
            "id": 3,
            "status": 3,  # declined
            "createdAt": "2026-03-28T12:00:00.000Z",
            "requestedBy": {},
            "media": {
                "mediaType": "movie",
                "tmdbId": None,
                "status": 1,  # unknown
            },
        },
    ],
}

MOCK_MOVIE_DETAIL = {
    "id": 693134,
    "title": "Dune: Part Two",
    "releaseDate": "2024-03-01",
    "overview": "Follow the mythic journey of Paul Atreides...",
}

MOCK_TV_DETAIL = {
    "id": 94997,
    "name": "House of the Dragon",
    "firstAirDate": "2022-08-21",
    "overview": "The Targaryen civil war...",
}


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When OVERSEERR_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "OVERSEERR_URL", ""):
        overseerr.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_none():
    """When OVERSEERR_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "OVERSEERR_URL", None):
        overseerr.register(app)
    assert count_tools(app) == 0


def test_register_adds_tools_when_url_set():
    """When OVERSEERR_URL is set, register() adds the read + write tools."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "OVERSEERR_URL", "http://fake:5055"),
        patch.object(config, "OVERSEERR_API_KEY", "fake-api-key"),
    ):
        overseerr.register(app)
    assert count_tools(app) == 3


# --- Test: get_overseerr_requests ---


@pytest.mark.asyncio
async def test_get_overseerr_requests_with_titles(mcp_app, mock_client):
    """Returns requests with resolved titles from parallel API calls."""
    ctx = make_mock_ctx(overseerr=mock_client)

    # Call sequence: /api/v1/request, the parallel pending-total call, then title
    # lookups for each.
    mock_client.get = AsyncMock(
        side_effect=[
            # Main request listing
            make_response(MOCK_REQUESTS_RESPONSE),
            # Global pending total (filter=pending)
            make_response({"pageInfo": {"results": 4}}),
            # Title lookup for movie 693134
            make_response(MOCK_MOVIE_DETAIL),
            # Title lookup for tv 94997
            make_response(MOCK_TV_DETAIL),
            # Title lookup for movie with tmdbId=None (skipped, returns Unknown Title)
            # _fetch_title returns "Unknown Title" without making a call when tmdb_id is None
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_overseerr_requests")
    result = await tool_fn(ctx=ctx, limit=10)

    assert result["total_count"] == 25
    assert result["pending_count"] == 1
    assert result["pending_total"] == 4
    assert len(result["requests"]) == 3

    # First request: pending movie with resolved title
    r1 = result["requests"][0]
    assert r1["id"] == 1  # request id — keys the approve/decline write path
    assert r1["title"] == "Dune: Part Two"
    assert r1["year"] == 2024
    assert r1["type"] == "movie"
    assert r1["status"] == "pending"
    assert r1["requested_by"] == "Jon"
    assert r1["requested_at"] == "2026-04-01T10:00:00.000Z"
    assert r1["media_status"] == "pending"

    # Second request: approved TV with resolved title
    r2 = result["requests"][1]
    assert r2["title"] == "House of the Dragon"
    assert r2["year"] == 2022
    assert r2["type"] == "tv"
    assert r2["status"] == "approved"
    assert r2["requested_by"] == "alice"  # falls back to username
    assert r2["media_status"] == "available"

    # Third request: declined with no tmdbId -> Unknown Title, no year
    r3 = result["requests"][2]
    assert r3["title"] == "Unknown Title"
    assert r3["year"] is None
    assert r3["status"] == "declined"
    assert r3["requested_by"] == "Unknown"  # empty requestedBy


@pytest.mark.asyncio
async def test_get_overseerr_requests_title_fetch_failure(mcp_app, mock_client):
    """When title resolution fails, returns 'Unknown Title' gracefully."""
    ctx = make_mock_ctx(overseerr=mock_client)

    # The title lookup raises HTTPStatusError; _get catches it and returns an
    # error dict, which _fetch_title detects and maps to "Unknown Title".
    error_response = httpx.Response(
        status_code=404, request=httpx.Request("GET", "http://test")
    )
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                {
                    "pageInfo": {"results": 1},
                    "results": [
                        {
                            "id": 1,
                            "status": 2,
                            "createdAt": "2026-04-01T10:00:00.000Z",
                            "requestedBy": {"displayName": "Jon"},
                            "media": {
                                "mediaType": "movie",
                                "tmdbId": 999999,
                                "status": 5,
                            },
                        }
                    ],
                }
            ),
            # Global pending total (filter=pending), fetched in parallel.
            make_response({"pageInfo": {"results": 1}}),
            httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "http://test"),
                response=error_response,
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_overseerr_requests")
    result = await tool_fn(ctx=ctx, limit=10)

    assert result["requests"][0]["title"] == "Unknown Title"
    assert result["requests"][0]["status"] == "approved"


@pytest.mark.asyncio
async def test_get_overseerr_requests_status_mapping(mcp_app, mock_client):
    """Verifies integer status codes are mapped to human-readable strings."""
    ctx = make_mock_ctx(overseerr=mock_client)

    requests_data = {
        "pageInfo": {"results": 5},
        "results": [
            {
                "id": i,
                "status": status,
                "createdAt": f"2026-04-0{i}T10:00:00Z",
                "requestedBy": {"displayName": f"User{i}"},
                "media": {"mediaType": "movie", "tmdbId": None, "status": 1},
            }
            for i, status in enumerate([1, 2, 3, 4, 5], start=1)
        ],
    }

    mock_client.get = AsyncMock(return_value=make_response(requests_data))

    tool_fn = get_tool_fn(mcp_app, "get_overseerr_requests")
    result = await tool_fn(ctx=ctx, limit=10)

    statuses = [r["status"] for r in result["requests"]]
    # MediaRequestStatus: 4=FAILED, 5=COMPLETED (not available/partial).
    assert statuses == ["pending", "approved", "declined", "failed", "completed"]


@pytest.mark.asyncio
async def test_get_overseerr_requests_empty(mcp_app, mock_client):
    """Returns empty list when no requests exist."""
    ctx = make_mock_ctx(overseerr=mock_client)

    mock_client.get = AsyncMock(
        return_value=make_response(
            {
                "pageInfo": {"results": 0},
                "results": [],
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_overseerr_requests")
    result = await tool_fn(ctx=ctx, limit=10)

    assert result["requests"] == []
    assert result["total_count"] == 0
    assert result["pending_count"] == 0


@pytest.mark.asyncio
async def test_get_overseerr_requests_error(mcp_app, mock_client):
    """Returns error dict when Overseerr is unreachable."""
    ctx = make_mock_ctx(overseerr=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_overseerr_requests")
    result = await tool_fn(ctx=ctx, limit=10)

    assert result["error"] == "timeout"


# --- Test: overseerr_approve_request ---


@pytest.mark.asyncio
async def test_approve_request_success(mcp_app, mock_client):
    """Approving a request POSTs to /approve and returns the new status."""
    ctx = make_mock_ctx(overseerr=mock_client)
    # Approve endpoint returns the updated request object (status 2 = approved)
    mock_client.post = AsyncMock(return_value=make_response({"id": 42, "status": 2}))

    tool_fn = get_tool_fn(mcp_app, "overseerr_approve_request")
    result = await tool_fn(ctx=ctx, request_id=42)

    mock_client.post.assert_awaited_once_with(
        "/api/v1/request/42/approve", params=None, json=None
    )
    assert result["action"] == "approve"
    assert result["request_id"] == 42
    assert result["status"] == "approved"
    assert result["result"] == "success"
    assert "_meta" in result


@pytest.mark.asyncio
async def test_approve_request_dry_run(mcp_app, mock_client):
    """dry_run=True previews without POSTing, GETting the request to confirm
    it exists and report its current status."""
    ctx = make_mock_ctx(overseerr=mock_client)
    mock_client.post = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response({"id": 42, "status": 1}))

    tool_fn = get_tool_fn(mcp_app, "overseerr_approve_request")
    result = await tool_fn(ctx=ctx, request_id=42, dry_run=True)

    mock_client.post.assert_not_awaited()
    assert result["dry_run"] is True
    assert result["action"] == "approve"
    assert result["request_id"] == 42
    assert result["exists"] is True
    assert result["current_status"] == "pending"


@pytest.mark.asyncio
async def test_approve_request_dry_run_missing_request_returns_error(
    mcp_app, mock_client
):
    """Regression (WP5): a dry-run for a nonexistent id surfaces the 404 error
    instead of fabricating 'Would approve'."""
    ctx = make_mock_ctx(overseerr=mock_client)
    mock_client.post = AsyncMock()
    err = httpx.Response(status_code=404, request=httpx.Request("GET", "http://t"))
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=err.request, response=err)
    )

    tool_fn = get_tool_fn(mcp_app, "overseerr_approve_request")
    result = await tool_fn(ctx=ctx, request_id=999999, dry_run=True)

    mock_client.post.assert_not_awaited()
    assert result["error"] == "http_error"
    assert result["status"] == 404


@pytest.mark.asyncio
async def test_approve_request_error(mcp_app, mock_client):
    """Returns error dict when the POST fails, and does not raise."""
    ctx = make_mock_ctx(overseerr=mock_client)
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "overseerr_approve_request")
    result = await tool_fn(ctx=ctx, request_id=42)

    assert result["error"] == "timeout"


# --- Test: overseerr_decline_request ---


@pytest.mark.asyncio
async def test_decline_request_success(mcp_app, mock_client):
    """Declining a request POSTs to /decline and returns the new status."""
    ctx = make_mock_ctx(overseerr=mock_client)
    # Decline endpoint returns the updated request object (status 3 = declined)
    mock_client.post = AsyncMock(return_value=make_response({"id": 7, "status": 3}))

    tool_fn = get_tool_fn(mcp_app, "overseerr_decline_request")
    result = await tool_fn(ctx=ctx, request_id=7)

    mock_client.post.assert_awaited_once_with(
        "/api/v1/request/7/decline", params=None, json=None
    )
    assert result["action"] == "decline"
    assert result["request_id"] == 7
    assert result["status"] == "declined"
    assert result["result"] == "success"
    assert "_meta" in result


@pytest.mark.asyncio
async def test_decline_request_dry_run(mcp_app, mock_client):
    """dry_run=True previews without POSTing."""
    ctx = make_mock_ctx(overseerr=mock_client)
    mock_client.post = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response({"id": 7, "status": 1}))

    tool_fn = get_tool_fn(mcp_app, "overseerr_decline_request")
    result = await tool_fn(ctx=ctx, request_id=7, dry_run=True)

    mock_client.post.assert_not_awaited()
    assert result["dry_run"] is True
    assert result["action"] == "decline"
    assert result["request_id"] == 7
    assert result["current_status"] == "pending"


@pytest.mark.asyncio
async def test_decline_request_http_error(mcp_app, mock_client):
    """Maps an HTTP error into the standard error dict without raising."""
    ctx = make_mock_ctx(overseerr=mock_client)
    error_response = httpx.Response(
        status_code=404, request=httpx.Request("POST", "http://test")
    )
    mock_client.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("POST", "http://test"),
            response=error_response,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "overseerr_decline_request")
    result = await tool_fn(ctx=ctx, request_id=999)

    assert result["error"] == "http_error"
    assert result["status"] == 404
