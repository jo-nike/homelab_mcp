"""Tests for Proxmox Backup Server tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import pbs

# --- Helpers ---


def make_dispatch(usage, groups_by_store, snapshots_by_key=None):
    """Build an async get() side-effect that routes by request path.

    get_pbs_status fans out from datastore-usage -> per-store groups ->
    per-group snapshots, so a URL-dispatching mock is more robust than an
    ordered side_effect list.

    - usage: full datastore-usage response ({"data": [...]}).
    - groups_by_store: {store_name: [group, ...]}.
    - snapshots_by_key: {(backup_type, backup_id): [snapshot, ...]}.
    """
    snapshots_by_key = snapshots_by_key or {}

    async def _dispatch(path, params=None):
        if path.endswith("/datastore-usage"):
            return make_response(usage)
        if path.endswith("/snapshots"):
            params = params or {}
            key = (params.get("backup-type"), params.get("backup-id"))
            return make_response({"data": snapshots_by_key.get(key, [])})
        if path.endswith("/groups"):
            store = path.split("/")[-2]
            return make_response({"data": groups_by_store.get(store, [])})
        return make_response({"data": []})

    return _dispatch


# --- Sample data ---

DATASTORE_USAGE = {
    "data": [
        {
            "store": "local-backups",
            "total": 1000000000000,
            "used": 400000000000,
            "avail": 600000000000,
        },
        {
            "store": "offsite-sync",
            "total": 2000000000000,
            "used": 1500000000000,
            "avail": 500000000000,
        },
    ]
}

# PBS serializes the group count as "backup-count" (kebab-case), not "count".
BACKUP_GROUPS_LOCAL = {
    "data": [
        {
            "backup-type": "vm",
            "backup-id": "100",
            "last-backup": 1712100000,
            "backup-count": 7,
            "comment": "ai-vm daily",
        },
        {
            "backup-type": "vm",
            "backup-id": "101",
            "last-backup": 1712090000,
            "backup-count": 5,
            "comment": "docker-host daily",
        },
        {
            "backup-type": "ct",
            "backup-id": "200",
            "last-backup": 1712080000,
            "backup-count": 3,
            "comment": None,
        },
    ]
}

BACKUP_GROUPS_OFFSITE = {
    "data": [
        {
            "backup-type": "host",
            "backup-id": "proxmox",
            "last-backup": 1712095000,
            "backup-count": 4,
            "comment": "pbs config backup",
        },
    ]
}

# Snapshots per group key (backup-type, backup-id). The tool reads the newest
# snapshot's verification state, backup-time, and size.
SNAPSHOTS = {
    ("vm", "100"): [
        {"backup-time": 1712099000, "size": 1000, "verification": {"state": "ok"}},
        {"backup-time": 1712100000, "size": 2048, "verification": {"state": "ok"}},
    ],
    ("vm", "101"): [
        {"backup-time": 1712090000, "size": 4096, "verification": {"state": "failed"}},
    ],
    # Newest snapshot has never been verified.
    ("ct", "200"): [
        {"backup-time": 1712080000, "size": 512},
    ],
    ("host", "proxmox"): [
        {"backup-time": 1712095000, "size": 8192, "verification": {"state": "ok"}},
    ],
}


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with PBS tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "PBS_URL", "https://192.168.1.114:8007"),
        patch.object(config, "PBS_TOKEN_ID", "fake@pbs!token"),
        patch.object(config, "PBS_TOKEN_SECRET", "fake-secret"),
    ):
        pbs.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When PBS_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "PBS_URL", ""):
        pbs.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_is_none():
    """When PBS_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "PBS_URL", None):
        pbs.register(app)
    assert count_tools(app) == 0


def test_register_adds_1_tool():
    """When PBS_URL is set, register() adds 1 tool."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "PBS_URL", "https://192.168.1.114:8007"),
        patch.object(config, "PBS_TOKEN_ID", "fake@pbs!token"),
        patch.object(config, "PBS_TOKEN_SECRET", "fake-secret"),
    ):
        pbs.register(app)
    assert count_tools(app) == 1


# --- Test: get_pbs_status ---


@pytest.mark.asyncio
async def test_get_pbs_status_full(mcp_app, mock_client):
    """Returns datastore usage with recent backups sorted by last_backup desc."""
    ctx = make_mock_ctx(pbs=mock_client)

    mock_client.get = AsyncMock(
        side_effect=make_dispatch(
            DATASTORE_USAGE,
            {
                "local-backups": BACKUP_GROUPS_LOCAL["data"],
                "offsite-sync": BACKUP_GROUPS_OFFSITE["data"],
            },
            SNAPSHOTS,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    assert "datastores" in result
    assert len(result["datastores"]) == 2

    # First datastore
    ds1 = result["datastores"][0]
    assert ds1["name"] == "local-backups"
    assert ds1["total_bytes"] == 1000000000000
    assert ds1["used_bytes"] == 400000000000
    assert ds1["used_percent"] == 40.0
    assert ds1["total_backup_groups"] == 3

    # Backups sorted by last-backup desc
    assert len(ds1["recent_backups"]) == 3
    assert ds1["recent_backups"][0]["backup_id"] == "100"  # Most recent
    assert ds1["recent_backups"][0]["last_backup"] == 1712100000
    assert ds1["recent_backups"][1]["backup_id"] == "101"
    assert ds1["recent_backups"][2]["backup_id"] == "200"  # Oldest

    # backup_count now reads the real "backup-count" field (was always 0)
    assert ds1["recent_backups"][0]["backup_count"] == 7

    # Enrichment from the newest snapshot of each group
    b100, b101, b200 = ds1["recent_backups"]
    assert b100["verify_state"] == "ok"
    assert b100["last_verified"] == 1712100000
    assert b100["last_backup_size_bytes"] == 2048  # newest snapshot's size
    assert b101["verify_state"] == "failed"
    assert b101["last_verified"] == 1712090000
    assert b101["last_backup_size_bytes"] == 4096
    # Newest snapshot never verified -> "none", no last_verified, size still present
    assert b200["verify_state"] == "none"
    assert b200["last_verified"] is None
    assert b200["last_backup_size_bytes"] == 512

    # Second datastore
    ds2 = result["datastores"][1]
    assert ds2["name"] == "offsite-sync"
    assert ds2["used_percent"] == 75.0
    assert ds2["total_backup_groups"] == 1


@pytest.mark.asyncio
async def test_used_percent_calculation(mcp_app, mock_client):
    """Verify used_percent is correctly computed."""
    ctx = make_mock_ctx(pbs=mock_client)

    custom_data = {
        "data": [
            {
                "store": "test",
                "total": 500000000000,
                "used": 125000000000,
                "avail": 375000000000,
            },
        ]
    }

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(custom_data),
            make_response({"data": []}),  # No backup groups
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    assert result["datastores"][0]["used_percent"] == 25.0


@pytest.mark.asyncio
async def test_backup_limit_parameter(mcp_app, mock_client):
    """Verify only N most recent backups returned when backup_limit is specified."""
    ctx = make_mock_ctx(pbs=mock_client)

    # Only one datastore
    single_ds = {
        "data": [
            {
                "store": "local-backups",
                "total": 1000000000000,
                "used": 400000000000,
                "avail": 600000000000,
            },
        ]
    }

    mock_client.get = AsyncMock(
        side_effect=make_dispatch(
            single_ds,
            {"local-backups": BACKUP_GROUPS_LOCAL["data"]},  # Has 3 groups
            SNAPSHOTS,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx, backup_limit=2)

    # Should only get top 2 (by last-backup desc)
    assert len(result["datastores"][0]["recent_backups"]) == 2
    assert result["datastores"][0]["recent_backups"][0]["backup_id"] == "100"
    assert result["datastores"][0]["recent_backups"][1]["backup_id"] == "101"
    # Total groups should still reflect all groups
    assert result["datastores"][0]["total_backup_groups"] == 3


@pytest.mark.asyncio
async def test_graceful_groups_fetch_failure(mcp_app, mock_client):
    """When backup group fetch fails, include error but don't fail whole tool."""
    ctx = make_mock_ctx(pbs=mock_client)

    single_ds = {
        "data": [
            {
                "store": "local-backups",
                "total": 1000000000000,
                "used": 400000000000,
                "avail": 600000000000,
            },
        ]
    }

    # Datastore-usage succeeds, but groups fetch returns 403
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(single_ds),
            httpx.Response(
                status_code=403,
                json={"message": "forbidden"},
                request=httpx.Request(
                    "GET", "https://test/api2/json/admin/datastore/local-backups/groups"
                ),
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    ds = result["datastores"][0]
    assert ds["name"] == "local-backups"
    assert ds["used_percent"] == 40.0
    # Groups should have error but datastore entry still present
    assert ds["recent_backups"] == []
    assert "groups_error" in ds


@pytest.mark.asyncio
async def test_snapshot_fetch_failure_degrades_gracefully(mcp_app, mock_client):
    """When a group's snapshot fetch fails, enrichment defaults to none/None."""
    ctx = make_mock_ctx(pbs=mock_client)

    single_ds = {
        "data": [
            {
                "store": "local-backups",
                "total": 1000000000000,
                "used": 400000000000,
                "avail": 600000000000,
            },
        ]
    }
    one_group = {
        "data": [
            {
                "backup-type": "vm",
                "backup-id": "100",
                "last-backup": 1712100000,
                "backup-count": 7,
            },
        ]
    }

    async def _dispatch(path, params=None):
        if path.endswith("/datastore-usage"):
            return make_response(single_ds)
        if path.endswith("/snapshots"):
            return httpx.Response(
                status_code=500,
                json={"message": "boom"},
                request=httpx.Request("GET", "https://test/snapshots"),
            )
        if path.endswith("/groups"):
            return make_response(one_group)
        return make_response({"data": []})

    mock_client.get = AsyncMock(side_effect=_dispatch)

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    backup = result["datastores"][0]["recent_backups"][0]
    assert backup["backup_count"] == 7  # group data still intact
    assert backup["verify_state"] == "none"
    assert backup["last_verified"] is None
    assert backup["last_backup_size_bytes"] is None


@pytest.mark.asyncio
async def test_empty_snapshots_yield_none(mcp_app, mock_client):
    """A group with no snapshots reports verify_state 'none' and null size."""
    ctx = make_mock_ctx(pbs=mock_client)

    single_ds = {
        "data": [
            {
                "store": "local-backups",
                "total": 1000000000000,
                "used": 400000000000,
                "avail": 600000000000,
            },
        ]
    }
    one_group = {
        "data": [
            {
                "backup-type": "ct",
                "backup-id": "999",
                "last-backup": 1712080000,
                "backup-count": 2,
            },
        ]
    }

    mock_client.get = AsyncMock(
        side_effect=make_dispatch(single_ds, {"local-backups": one_group["data"]}, {}),
    )

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    backup = result["datastores"][0]["recent_backups"][0]
    assert backup["verify_state"] == "none"
    assert backup["last_verified"] is None
    assert backup["last_backup_size_bytes"] is None


@pytest.mark.asyncio
async def test_datastore_usage_error(mcp_app, mock_client):
    """When datastore-usage endpoint fails, returns error dict."""
    ctx = make_mock_ctx(pbs=mock_client)

    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_pbs_status")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"
