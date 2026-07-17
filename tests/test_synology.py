"""Tests for Synology NAS tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import synology
from tools.synology import SynologyLoginStrategy

# --- Helpers ---


def make_synology_response(data, success=True, error_code=None):
    """Create a Synology API JSON envelope."""
    body = {"success": success}
    if success:
        body["data"] = data
    else:
        body["error"] = {"code": error_code or 100}
    return body


# --- Sample data ---

SYSTEM_INFO = {
    "model": "DS920+",
    "serial": "ABCD1234",
    "version_string": "DSM 7.2-64570 Update 3",
    "uptime_seconds": 864000,
    "temperature": 42,
}

UTILIZATION = {
    "cpu": {"user_load": 12, "system_load": 5},
    "memory": {
        "total_real": 4194304,
        "avail_real": 2097152,
    },  # 4GB total, 2GB available (in KB)
}

STORAGE = {
    "disks": [
        {
            "id": "sata1",
            "name": "Drive 1",
            "model": "WDC WD40EFZX-68AWUN0",
            "size_total": "4000787030016",  # STRING! Pitfall 1
            "temp": 35,
            "status": "normal",
            "smart_status": "normal",
        },
        {
            "id": "sata2",
            "name": "Drive 2",
            "model": "WDC WD40EFZX-68AWUN0",
            "size_total": "4000787030016",
            "temp": 36,
            "status": "normal",
            "smart_status": "normal",
        },
    ],
    "volumes": [
        {
            "id": "volume_1",
            "status": "normal",
            "size": {"total": "7801405440000", "used": "5850000000000"},
            "fs_type": "btrfs",
        },
    ],
    "storagePools": [
        {
            "id": "pool_1",
            "status": "normal",
            "raidType": "raid_1",
            "disks": ["sata1", "sata2"],
        },
    ],
}


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Synology tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "SYNOLOGY_URL", "http://192.168.1.50:5000"),
        patch.object(config, "SYNOLOGY_USERNAME", "admin"),
        patch.object(config, "SYNOLOGY_PASSWORD", "fake-password"),
    ):
        synology.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock SessionAuthManager (duck-type: has .get method)."""
    client = AsyncMock()
    return client


# --- Test: SynologyLoginStrategy ---


@pytest.mark.asyncio
async def test_synology_login_posts_credentials():
    """login() POSTs credentials as form data, keeping passwd out of the URL."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=make_response({"success": True, "data": {"sid": "SID123"}})
    )

    strategy = SynologyLoginStrategy(username="admin", password="s3cret")
    result = await strategy.login(mock_client)

    assert result.params == {"_sid": "SID123"}
    args, kwargs = mock_client.post.call_args
    assert args[0] == "/webapi/auth.cgi"
    assert kwargs["data"]["passwd"] == "s3cret"
    assert kwargs["data"]["account"] == "admin"
    # No secret in query params.
    assert "params" not in kwargs
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_login_raises_on_http_error():
    """login() raises HTTPStatusError (not a cryptic JSONDecodeError) on a 502 HTML page."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=httpx.Response(
            status_code=502,
            text="<html>Bad Gateway</html>",
            request=httpx.Request("POST", "http://test/webapi/auth.cgi"),
        )
    )
    strategy = SynologyLoginStrategy(username="admin", password="pw")
    with pytest.raises(httpx.HTTPStatusError):
        await strategy.login(mock_client)


@pytest.mark.parametrize("code", [105, 106, 107, 119])
def test_is_auth_error_matches_session_codes(code):
    """is_auth_error() treats all DSM session-expiry codes as auth errors."""
    strategy = SynologyLoginStrategy(username="admin", password="pw")
    resp = make_response(make_synology_response(None, success=False, error_code=code))
    assert strategy.is_auth_error(resp) is True


def test_is_auth_error_ignores_non_session_codes():
    """is_auth_error() returns False for a non-session error code and for success."""
    strategy = SynologyLoginStrategy(username="admin", password="pw")
    non_session = make_response(
        make_synology_response(None, success=False, error_code=400)
    )
    assert strategy.is_auth_error(non_session) is False
    ok = make_response(make_synology_response({"sid": "x"}))
    assert strategy.is_auth_error(ok) is False


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When SYNOLOGY_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "SYNOLOGY_URL", ""):
        synology.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_is_none():
    """When SYNOLOGY_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "SYNOLOGY_URL", None):
        synology.register(app)
    assert count_tools(app) == 0


def test_register_adds_1_tool():
    """When SYNOLOGY_URL is set, register() adds 1 tool."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "SYNOLOGY_URL", "http://192.168.1.50:5000"),
        patch.object(config, "SYNOLOGY_USERNAME", "admin"),
        patch.object(config, "SYNOLOGY_PASSWORD", "fake-password"),
    ):
        synology.register(app)
    assert count_tools(app) == 1


# --- Test: get_nas_status ---


@pytest.mark.asyncio
async def test_get_nas_status_full(mcp_app, mock_client):
    """Returns complete NAS status with system, utilization, disks, volumes, pools."""
    ctx = make_mock_ctx(synology=mock_client)

    # Mock three parallel API calls
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(make_synology_response(SYSTEM_INFO)),
            make_response(make_synology_response(UTILIZATION)),
            make_response(make_synology_response(STORAGE)),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    # System info
    assert result["system"]["model"] == "DS920+"
    assert result["system"]["dsm_version"] == "DSM 7.2-64570 Update 3"
    assert result["system"]["temperature_c"] == 42
    assert result["system"]["uptime_seconds"] == 864000

    # Utilization
    assert result["utilization"]["cpu_load_percent"] == 17  # 12 + 5
    assert result["utilization"]["ram_total_mb"] == 4096.0  # 4194304 KB / 1024
    assert result["utilization"]["ram_used_mb"] == 2048.0  # (4194304 - 2097152) / 1024
    assert result["utilization"]["ram_used_percent"] == 50.0

    # Disks
    assert len(result["disks"]) == 2
    assert result["disks"][0]["id"] == "sata1"
    assert (
        result["disks"][0]["size_bytes"] == 4000787030016
    )  # Correctly parsed from string
    assert result["disks"][0]["temperature_c"] == 35

    # Volumes
    assert len(result["volumes"]) == 1
    assert result["volumes"][0]["id"] == "volume_1"
    assert result["volumes"][0]["size_total_bytes"] == 7801405440000
    assert result["volumes"][0]["size_used_bytes"] == 5850000000000
    assert result["volumes"][0]["fs_type"] == "btrfs"

    # Storage pools
    assert len(result["storage_pools"]) == 1
    assert result["storage_pools"][0]["raid_type"] == "raid_1"
    assert result["storage_pools"][0]["disk_count"] == 2


@pytest.mark.asyncio
async def test_get_nas_status_timeout_reported_as_timeout(mcp_app, mock_client):
    """A timed-out API call must report error=timeout, not connection_error."""
    ctx = make_mock_ctx(synology=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    assert result["system"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_get_nas_status_storage_error_surfaced(mcp_app, mock_client):
    """Regression (WP5): a storage-API error is surfaced as storage_error, not
    silently replaced with empty disks/volumes (a NAS with zero disks)."""
    ctx = make_mock_ctx(synology=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(make_synology_response({})),  # info ok
            make_response(make_synology_response({})),  # utilization ok
            make_response(make_synology_response(None, success=False, error_code=500)),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    assert "storage_error" in result
    assert result["disks"] == []


@pytest.mark.asyncio
async def test_volume_size_parsing_from_strings(mcp_app, mock_client):
    """Verify int() conversion from string sizes (Pitfall 1)."""
    ctx = make_mock_ctx(synology=mock_client)

    storage_with_string_sizes = {
        "disks": [{"id": "d1", "size_total": "999999999999"}],
        "volumes": [
            {
                "id": "v1",
                "size": {"total": "1000000000000", "used": "750000000000"},
            },
        ],
        "storagePools": [],
    }

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(make_synology_response({})),
            make_response(make_synology_response({})),
            make_response(make_synology_response(storage_with_string_sizes)),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    # All sizes must be ints, not strings
    assert result["disks"][0]["size_bytes"] == 999999999999
    assert isinstance(result["disks"][0]["size_bytes"], int)

    assert result["volumes"][0]["size_total_bytes"] == 1000000000000
    assert isinstance(result["volumes"][0]["size_total_bytes"], int)
    assert result["volumes"][0]["size_used_bytes"] == 750000000000
    assert isinstance(result["volumes"][0]["size_used_bytes"], int)


@pytest.mark.asyncio
async def test_volume_used_percent_calculation(mcp_app, mock_client):
    """Verify used_percent is correctly computed."""
    ctx = make_mock_ctx(synology=mock_client)

    storage_data = {
        "disks": [],
        "volumes": [
            {
                "id": "v1",
                "size": {"total": "1000000000000", "used": "250000000000"},
            },
        ],
        "storagePools": [],
    }

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(make_synology_response({})),
            make_response(make_synology_response({})),
            make_response(make_synology_response(storage_data)),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    assert result["volumes"][0]["used_percent"] == 25.0


@pytest.mark.asyncio
async def test_api_error_response(mcp_app, mock_client):
    """When a Synology API returns success=false, the section contains an error dict."""
    ctx = make_mock_ctx(synology=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(make_synology_response(None, success=False, error_code=119)),
            make_response(make_synology_response(UTILIZATION)),
            make_response(make_synology_response(STORAGE)),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    # System should contain error
    assert result["system"]["error"] == "api_error"
    assert "119" in result["system"]["message"]

    # Other sections should still work
    assert result["utilization"]["cpu_load_percent"] == 17
    assert len(result["disks"]) == 2


@pytest.mark.asyncio
async def test_connection_error(mcp_app, mock_client):
    """When the NAS is unreachable, returns error dicts for all sections."""
    ctx = make_mock_ctx(synology=mock_client)

    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    tool_fn = get_tool_fn(mcp_app, "get_nas_status")
    result = await tool_fn(ctx=ctx)

    # All sections should have connection errors
    assert result["system"]["error"] == "connection_error"
    assert result["disks"] == []
    assert result["volumes"] == []
