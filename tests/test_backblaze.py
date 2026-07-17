"""Tests for Backblaze B2 tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import backblaze

# --- Helpers ---


def make_mock_manager(account_id="abc123"):
    """Create a mock SessionAuthManager with a strategy that has account_id."""
    manager = AsyncMock()
    manager.strategy = MagicMock()
    manager.strategy.account_id = account_id
    manager.ensure_auth = AsyncMock()  # No-op, but must be awaitable
    return manager


# --- Sample data ---

BUCKET_LIST_RESPONSE = {
    "buckets": [
        {
            "bucketName": "homelab-backups",
            "bucketId": "bucket_id_1",
            "bucketType": "allPrivate",
        },
        {
            "bucketName": "media-archive",
            "bucketId": "bucket_id_2",
            "bucketType": "allPrivate",
        },
        {
            "bucketName": "public-assets",
            "bucketId": "bucket_id_3",
            "bucketType": "allPublic",
        },
    ],
}

# Single-bucket list response, for tests that assert on file stats.
ONE_BUCKET_RESPONSE = {
    "buckets": [
        {
            "bucketName": "homelab-backups",
            "bucketId": "bucket_id_1",
            "bucketType": "allPrivate",
        },
    ],
}


def make_file_page(files, next_file_name=None):
    """Build a b2_list_file_names response page."""
    return {"files": files, "nextFileName": next_file_name}


def file_entry(name, size, upload_ms, action="upload"):
    """Build a single b2_list_file_names file entry."""
    return {
        "fileName": name,
        "contentLength": size,
        "uploadTimestamp": upload_ms,
        "action": action,
    }


@pytest.fixture(autouse=True)
def clear_file_stats_cache():
    """Reset the module-level file-stats cache and task registry between tests."""
    backblaze._file_stats_cache.clear()
    backblaze._refresh_tasks.clear()
    yield
    backblaze._file_stats_cache.clear()
    backblaze._refresh_tasks.clear()


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Backblaze tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "BACKBLAZE_KEY_ID", "fake-key-id"),
        patch.object(config, "BACKBLAZE_APP_KEY", "fake-app-key"),
    ):
        backblaze.register(app)
    return app


@pytest.fixture
def mock_manager():
    """Create a mock SessionAuthManager with account_id."""
    return make_mock_manager("test-account-123")


# --- Test: Conditional Registration ---


def test_register_skips_when_no_key_id():
    """When BACKBLAZE_KEY_ID is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "BACKBLAZE_KEY_ID", ""):
        backblaze.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_key_id_is_none():
    """When BACKBLAZE_KEY_ID is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "BACKBLAZE_KEY_ID", None):
        backblaze.register(app)
    assert count_tools(app) == 0


def test_register_adds_1_tool():
    """When BACKBLAZE_KEY_ID is set, register() adds 1 tool."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "BACKBLAZE_KEY_ID", "fake-key-id"),
        patch.object(config, "BACKBLAZE_APP_KEY", "fake-app-key"),
    ):
        backblaze.register(app)
    assert count_tools(app) == 1


# --- Test: BackblazeLoginStrategy stores account_id ---


def test_strategy_stores_account_id():
    """BackblazeLoginStrategy has account_id attribute initialized to None."""
    strategy = backblaze.BackblazeLoginStrategy("key", "secret")
    assert strategy.account_id is None


# --- Test: get_backblaze_usage ---


@pytest.mark.asyncio
async def test_get_backblaze_usage_full(mcp_app, mock_manager):
    """Returns bucket list with names, types, IDs, and count."""
    ctx = make_mock_ctx(backblaze=mock_manager)

    mock_manager.post = AsyncMock(return_value=make_response(BUCKET_LIST_RESPONSE))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["bucket_count"] == 3
    assert len(result["buckets"]) == 3

    # Check bucket details
    assert result["buckets"][0]["bucket_name"] == "homelab-backups"
    assert result["buckets"][0]["bucket_id"] == "bucket_id_1"
    assert result["buckets"][0]["bucket_type"] == "allPrivate"

    assert result["buckets"][2]["bucket_name"] == "public-assets"
    assert result["buckets"][2]["bucket_type"] == "allPublic"

    # Verify ensure_auth was called
    mock_manager.ensure_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_backblaze_usage_timeout_reported_as_timeout(mcp_app, mock_manager):
    """A timed-out B2 POST must report error=timeout, not connection_error."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    mock_manager.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_get_backblaze_usage_auth_failure_returns_error(mcp_app, mock_manager):
    """Regression (WP5): a login failure in ensure_auth must return an
    auth_error dict, not raise out of the tool."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    mock_manager.ensure_auth = AsyncMock(side_effect=httpx.HTTPError("401"))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "auth_error"


@pytest.mark.asyncio
async def test_get_backblaze_usage_empty_buckets(mcp_app, mock_manager):
    """Returns empty bucket list when account has no buckets."""
    ctx = make_mock_ctx(backblaze=mock_manager)

    mock_manager.post = AsyncMock(return_value=make_response({"buckets": []}))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["bucket_count"] == 0
    assert result["buckets"] == []


@pytest.mark.asyncio
async def test_get_backblaze_usage_no_account_id(mcp_app):
    """Returns error when account_id is not available."""
    manager = make_mock_manager(account_id=None)
    ctx = make_mock_ctx(backblaze=manager)

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "auth_error"
    assert "account ID" in result["message"]


@pytest.mark.asyncio
async def test_get_backblaze_usage_api_error(mcp_app, mock_manager):
    """Returns error when B2 API call fails."""
    ctx = make_mock_ctx(backblaze=mock_manager)

    mock_manager.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Unauthorized",
            request=httpx.Request("POST", "https://test"),
            response=httpx.Response(
                status_code=401, request=httpx.Request("POST", "https://test")
            ),
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 401


# --- Test: per-bucket file stats ---


def routed_post(bucket_resp, file_pages):
    """side_effect routing b2_list_buckets vs b2_list_file_names.

    file_pages: response dicts returned in order for each list_file_names call.
    """
    pages = iter(file_pages)

    def _side(path, json=None):
        if "b2_list_buckets" in path:
            return make_response(bucket_resp)
        if "b2_list_file_names" in path:
            return make_response(next(pages))
        raise AssertionError(f"unexpected path: {path}")

    return _side


def count_file_name_calls(mock_post):
    """Count how many list_file_names requests the mock received."""
    return sum(1 for c in mock_post.call_args_list if "b2_list_file_names" in c.args[0])


@pytest.mark.asyncio
async def test_file_stats_single_page(mcp_app, mock_manager):
    """Aggregates size/count and picks the newest upload from one page."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    page = make_file_page(
        [
            file_entry("a.dat", 100, 1_000_000),
            file_entry("b.dat", 250, 3_000_000),  # newest
            file_entry("c.dat", 50, 2_000_000),
        ]
    )
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, [page]))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    b = result["buckets"][0]
    assert b["total_size_bytes"] == 400
    assert b["file_count"] == 3
    assert b["latest_upload_at"] == "1970-01-01T00:50:00Z"  # 3_000_000 ms
    assert "stats_truncated" not in b


@pytest.mark.asyncio
async def test_file_stats_skips_non_uploads(mcp_app, mock_manager):
    """hide markers and folder placeholders are excluded from size/count."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    page = make_file_page(
        [
            file_entry("real.dat", 100, 1_000_000),
            file_entry("hidden.dat", 0, 5_000_000, action="hide"),
            file_entry("folder/", 0, 6_000_000, action="folder"),
        ]
    )
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, [page]))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    b = result["buckets"][0]
    assert b["total_size_bytes"] == 100
    assert b["file_count"] == 1
    assert b["latest_upload_at"] == "1970-01-01T00:16:40Z"  # 1_000_000 ms upload


@pytest.mark.asyncio
async def test_file_stats_paginates(mcp_app, mock_manager):
    """Walks all pages following nextFileName until it is None."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    pages = [
        make_file_page([file_entry("a.dat", 100, 1_000_000)], next_file_name="b.dat"),
        make_file_page([file_entry("b.dat", 200, 4_000_000)], next_file_name="c.dat"),
        make_file_page([file_entry("c.dat", 300, 2_000_000)], next_file_name=None),
    ]
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, pages))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    b = result["buckets"][0]
    assert b["total_size_bytes"] == 600
    assert b["file_count"] == 3
    assert b["latest_upload_at"] == "1970-01-01T01:06:40Z"  # 4_000_000 ms
    assert count_file_name_calls(mock_manager.post) == 3


@pytest.mark.asyncio
async def test_file_stats_truncation_cap(mcp_app, mock_manager):
    """Hitting the page cap flags stats_truncated and stops walking."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    # Every page advertises another page, so the walk only stops at the cap.
    pages = [
        make_file_page([file_entry("a.dat", 100, 1_000_000)], next_file_name="more"),
        make_file_page([file_entry("b.dat", 100, 1_000_000)], next_file_name="more"),
    ]
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, pages))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    with patch.object(backblaze, "_MAX_PAGES", 2):
        result = await tool_fn(ctx=ctx)

    b = result["buckets"][0]
    assert b["stats_truncated"] is True
    assert b["file_count"] == 2
    assert count_file_name_calls(mock_manager.post) == 2


@pytest.mark.asyncio
async def test_file_stats_per_bucket_error(mcp_app, mock_manager):
    """A failed file walk yields stats_error with null stat fields, not a crash."""
    ctx = make_mock_ctx(backblaze=mock_manager)

    def _side(path, json=None):
        if "b2_list_buckets" in path:
            return make_response(ONE_BUCKET_RESPONSE)
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("POST", "https://test"),
            response=httpx.Response(
                status_code=500, request=httpx.Request("POST", "https://test")
            ),
        )

    mock_manager.post = AsyncMock(side_effect=_side)

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    result = await tool_fn(ctx=ctx)

    b = result["buckets"][0]
    assert "stats_error" in b
    assert b["total_size_bytes"] is None
    assert b["file_count"] is None
    assert b["latest_upload_at"] is None


# --- Test: TTL cache (fresh fetch, cached hit, expiry) ---


@pytest.mark.asyncio
async def test_cache_hit_skips_second_walk(mcp_app, mock_manager):
    """Within the TTL, a repeat call reuses cached stats (no new file walk)."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    pages = [make_file_page([file_entry("a.dat", 100, 1_000_000)])]
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, pages))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    with patch.object(backblaze, "_now") as mock_now:
        mock_now.return_value = 1000.0
        r1 = await tool_fn(ctx=ctx)
        mock_now.return_value = 1500.0  # still < 1000 + 900 TTL
        r2 = await tool_fn(ctx=ctx)

    assert r1["buckets"][0]["total_size_bytes"] == 100
    assert r2["buckets"][0]["total_size_bytes"] == 100
    # Only the first invocation walked the bucket.
    assert count_file_name_calls(mock_manager.post) == 1


@pytest.mark.asyncio
async def test_cache_expiry_serves_stale_and_refreshes_in_background(
    mcp_app, mock_manager
):
    """After the TTL elapses, stale stats are served while the walk re-runs."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    pages = [
        make_file_page([file_entry("a.dat", 100, 1_000_000)]),
        make_file_page(
            [file_entry("a.dat", 100, 1_000_000), file_entry("b.dat", 100, 2_000_000)]
        ),
    ]
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, pages))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    with patch.object(backblaze, "_now") as mock_now:
        mock_now.return_value = 1000.0
        r1 = await tool_fn(ctx=ctx)
        mock_now.return_value = 1000.0 + backblaze._FILE_STATS_TTL + 1  # expired
        r2 = await tool_fn(ctx=ctx)
        # The expired call answered from stale cache and kicked off a
        # background walk; let it finish, then the next call is fresh.
        await backblaze._refresh_tasks["bucket_id_1"]
        r3 = await tool_fn(ctx=ctx)

    assert r1["buckets"][0]["file_count"] == 1
    assert r2["buckets"][0]["file_count"] == 1  # stale, served immediately
    assert r3["buckets"][0]["file_count"] == 2  # background refresh landed
    assert count_file_name_calls(mock_manager.post) == 2


@pytest.mark.asyncio
async def test_cold_concurrent_calls_share_one_walk(mcp_app, mock_manager):
    """Concurrent first-ever calls await the same walk instead of stampeding."""
    ctx = make_mock_ctx(backblaze=mock_manager)
    # A single file page: a second walk would exhaust the iterator and fail.
    pages = [make_file_page([file_entry("a.dat", 100, 1_000_000)])]
    mock_manager.post = AsyncMock(side_effect=routed_post(ONE_BUCKET_RESPONSE, pages))

    tool_fn = get_tool_fn(mcp_app, "get_backblaze_usage")
    r1, r2 = await asyncio.gather(tool_fn(ctx=ctx), tool_fn(ctx=ctx))

    assert r1["buckets"][0]["file_count"] == 1
    assert r2["buckets"][0]["file_count"] == 1
    assert count_file_name_calls(mock_manager.post) == 1
