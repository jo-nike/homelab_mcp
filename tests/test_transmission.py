"""Tests for Transmission torrent tools."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import transmission
from tools.transmission import STATUS_MAP, TORRENT_FIELDS, TransmissionLoginStrategy

# --- Helpers ---


# --- Mock data ---

MOCK_RPC_RESPONSE = {
    "result": "success",
    "arguments": {
        "torrents": [
            {
                "id": 1,
                "name": "ubuntu-24.04.iso",
                "status": 4,
                "percentDone": 0.75,
                "rateDownload": 50000,
                "rateUpload": 10000,
                "eta": 300,
                "totalSize": 4000000000,
                "downloadedEver": 3000000000,
                "uploadedEver": 500000000,
                "uploadRatio": 0.1667,
                "peersConnected": 15,
                "addedDate": 1712100000,
            },
            {
                "id": 2,
                "name": "archlinux-2026.04.iso",
                "status": 6,
                "percentDone": 1.0,
                "rateDownload": 0,
                "rateUpload": 25000,
                "eta": -1,
                "totalSize": 800000000,
                "downloadedEver": 800000000,
                "uploadedEver": 1600000000,
                "uploadRatio": 2.0,
                "peersConnected": 3,
                "addedDate": 1712000000,
            },
        ]
    },
}


# --- Tests ---


@pytest.mark.asyncio
async def test_login_reuses_basic_auth_on_trigger_and_result():
    """login() sends the same Basic credential on the trigger request and in the result headers."""
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    captured = {}

    async def post(path, headers=None, json=None):
        captured["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(
            status_code=409,
            headers={"X-Transmission-Session-Id": "sess-1"},
            text="conflict",
            request=httpx.Request("POST", "http://test"),
        )

    mock_client = MagicMock()
    mock_client.post = post

    strategy = TransmissionLoginStrategy(username="user", password="pass")
    result = await strategy.login(mock_client)

    assert captured["auth"] == expected
    assert result.headers["Authorization"] == expected
    assert result.headers["X-Transmission-Session-Id"] == "sess-1"


@pytest.mark.asyncio
async def test_login_without_credentials_omits_authorization():
    """login() sends no Authorization header when no credentials are configured."""
    captured = {}

    async def post(path, headers=None, json=None):
        captured["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(
            status_code=409,
            headers={"X-Transmission-Session-Id": "sess-2"},
            text="conflict",
            request=httpx.Request("POST", "http://test"),
        )

    mock_client = MagicMock()
    mock_client.post = post

    strategy = TransmissionLoginStrategy()
    result = await strategy.login(mock_client)

    assert captured["auth"] is None
    assert "Authorization" not in result.headers


def test_transmission_is_auth_error_only_on_409():
    """A 409 (CSRF token needed) is the auth-retry trigger; other codes are not."""
    strategy = TransmissionLoginStrategy()
    req = httpx.Request("POST", "http://test")
    assert strategy.is_auth_error(httpx.Response(409, request=req)) is True
    assert strategy.is_auth_error(httpx.Response(200, request=req)) is False
    assert strategy.is_auth_error(httpx.Response(401, request=req)) is False


@pytest.mark.asyncio
async def test_transmission_timeout_reported_as_timeout():
    """A timed-out RPC must report error=timeout, not connection_error."""
    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_transmission_get_torrents():
    """Test get_transmission_torrents with mock RPC response."""
    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(MOCK_RPC_RESPONSE))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["total_count"] == 2
    assert result["showing"] == 2

    # First torrent: downloading (most recently added first)
    t1 = result["torrents"][0]
    assert t1["id"] == 1
    assert t1["name"] == "ubuntu-24.04.iso"
    assert t1["status"] == "downloading"
    assert t1["progress_percent"] == 75.0
    assert t1["download_speed_kb_per_sec"] == 50.0
    assert t1["upload_speed_kb_per_sec"] == 10.0
    assert t1["eta_seconds"] == 300
    assert t1["size_bytes"] == 4000000000
    assert t1["ratio"] == 0.17
    assert t1["peers"] == 15

    # Second torrent: seeding
    t2 = result["torrents"][1]
    assert t2["status"] == "seeding"
    assert t2["progress_percent"] == 100.0
    assert t2["eta_seconds"] is None  # eta=-1 maps to None

    # Aggregate speeds
    assert result["total_download_speed_kb_per_sec"] == 50.0
    assert result["total_upload_speed_kb_per_sec"] == 35.0


@pytest.mark.asyncio
async def test_transmission_status_map_all_values():
    """Test all 7 status code mappings (0-6)."""
    assert STATUS_MAP[0] == "stopped"
    assert STATUS_MAP[1] == "checking"
    assert STATUS_MAP[2] == "checking"
    assert STATUS_MAP[3] == "queued"
    assert STATUS_MAP[4] == "downloading"
    assert STATUS_MAP[5] == "queued"
    assert STATUS_MAP[6] == "seeding"
    # Unknown status should return "unknown" via .get default
    assert STATUS_MAP.get(99, "unknown") == "unknown"


@pytest.mark.asyncio
async def test_transmission_eta_handling():
    """Test eta: -1 -> None, -2 -> None, 300 -> 300."""
    torrents = [
        {
            "id": 1,
            "name": "a",
            "status": 4,
            "percentDone": 0.5,
            "rateDownload": 0,
            "rateUpload": 0,
            "eta": -1,
            "totalSize": 100,
            "downloadedEver": 50,
            "uploadedEver": 0,
            "uploadRatio": 0,
            "peersConnected": 0,
            "addedDate": 0,
        },
        {
            "id": 2,
            "name": "b",
            "status": 4,
            "percentDone": 0.5,
            "rateDownload": 0,
            "rateUpload": 0,
            "eta": -2,
            "totalSize": 100,
            "downloadedEver": 50,
            "uploadedEver": 0,
            "uploadRatio": 0,
            "peersConnected": 0,
            "addedDate": 0,
        },
        {
            "id": 3,
            "name": "c",
            "status": 4,
            "percentDone": 0.5,
            "rateDownload": 0,
            "rateUpload": 0,
            "eta": 300,
            "totalSize": 100,
            "downloadedEver": 50,
            "uploadedEver": 0,
            "uploadRatio": 0,
            "peersConnected": 0,
            "addedDate": 0,
        },
    ]
    rpc_response = {"result": "success", "arguments": {"torrents": torrents}}

    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(rpc_response))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["torrents"][0]["eta_seconds"] is None  # -1
    assert result["torrents"][1]["eta_seconds"] is None  # -2
    assert result["torrents"][2]["eta_seconds"] == 300  # positive


@pytest.mark.asyncio
async def test_transmission_speed_calculation():
    """Test speed conversion: rateDownload=50000 -> 50.0 kbps."""
    torrents = [
        {
            "id": 1,
            "name": "test",
            "status": 4,
            "percentDone": 0.5,
            "rateDownload": 50000,
            "rateUpload": 12500,
            "eta": 100,
            "totalSize": 100,
            "downloadedEver": 50,
            "uploadedEver": 10,
            "uploadRatio": 0.2,
            "peersConnected": 5,
            "addedDate": 0,
        },
    ]
    rpc_response = {"result": "success", "arguments": {"torrents": torrents}}

    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(rpc_response))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["torrents"][0]["download_speed_kb_per_sec"] == 50.0
    assert result["torrents"][0]["upload_speed_kb_per_sec"] == 12.5
    assert result["total_download_speed_kb_per_sec"] == 50.0
    assert result["total_upload_speed_kb_per_sec"] == 12.5


@pytest.mark.asyncio
async def test_transmission_empty_torrent_list():
    """Test with no torrents."""
    rpc_response = {"result": "success", "arguments": {"torrents": []}}

    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(rpc_response))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["total_count"] == 0
    assert result["showing"] == 0
    assert result["torrents"] == []
    assert result["total_download_speed_kb_per_sec"] == 0.0
    assert result["total_upload_speed_kb_per_sec"] == 0.0


@pytest.mark.asyncio
async def test_transmission_rpc_error():
    """Test RPC error response (result != 'success')."""
    rpc_response = {"result": "no method name"}

    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(rpc_response))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["error"] == "rpc_error"
    assert "no method name" in result["message"]


@pytest.mark.asyncio
async def test_transmission_http_error():
    """A 401 (bad Basic-auth creds) surfaces as http_error with status, not connection_error."""
    unauth = httpx.Response(
        status_code=401,
        text="<html>401 Unauthorized</html>",
        request=httpx.Request("POST", "http://test"),
    )
    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=unauth)
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    result = await fn(ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 401


@pytest.mark.asyncio
async def test_transmission_conditional_registration():
    """Test that tools are not registered when TRANSMISSION_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", None):
        transmission.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_transmission_registers_when_configured():
    """Test that tools are registered when TRANSMISSION_URL is set."""
    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)
    assert count_tools(app) == 1


@pytest.mark.asyncio
async def test_torrent_get_rpc_requests_expected_fields():
    """The torrent-get RPC body actually asks for the TORRENT_FIELDS list (with
    no duplicate addedDate), rather than a test that only echoes the constant."""
    mock_manager = AsyncMock()
    mock_manager.post = AsyncMock(return_value=make_response(MOCK_RPC_RESPONSE))
    ctx = make_mock_ctx(transmission=mock_manager)

    app = FastMCP("test")
    with patch.object(config, "TRANSMISSION_URL", "http://transmission:9091"):
        transmission.register(app)

    fn = get_tool_fn(app, "get_transmission_torrents")
    await fn(ctx)

    assert mock_manager.post.await_args is not None
    body = mock_manager.post.await_args.kwargs["json"]
    assert body["method"] == "torrent-get"
    fields = body["arguments"]["fields"]
    assert fields == TORRENT_FIELDS
    assert fields.count("addedDate") == 1  # no redundant duplicate
    for expected in ("id", "name", "percentDone", "rateDownload", "status"):
        assert expected in fields
