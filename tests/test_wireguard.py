"""Tests for WireGuard (wg-easy) VPN tools."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import wireguard
from tools.wireguard import WgEasyLoginStrategy

# --- Helpers ---


# --- WgEasyLoginStrategy Tests ---


@pytest.mark.asyncio
async def test_wireguard_login_relies_on_client_cookie_jar():
    """login() returns an empty result and leaves the session cookie in the client jar.

    Regression: a frozen Cookie header suppresses httpx's jar cookies, so a
    rotated session cookie would be ignored until a 401 forced re-login.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={},
            headers={"set-cookie": "connect.sid=abc123; Path=/; HttpOnly"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        strategy = WgEasyLoginStrategy(password="testpass")
        result = await strategy.login(client)

        # No frozen Cookie header — the client jar carries the session cookie.
        assert "Cookie" not in result.headers
        assert result.expires_at is None
        assert client.cookies.get("connect.sid") == "abc123"


@pytest.mark.asyncio
async def test_wireguard_login_failure():
    """login() raises RuntimeError on non-200 response."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=httpx.Response(
            status_code=401,
            json={"error": "Incorrect password"},
            request=httpx.Request("POST", "http://test/api/session"),
        )
    )

    strategy = WgEasyLoginStrategy(password="wrongpass")
    with pytest.raises(RuntimeError):
        await strategy.login(mock_client)


@pytest.mark.asyncio
async def test_wireguard_is_auth_error_true():
    """is_auth_error() returns True for 401 status."""
    strategy = WgEasyLoginStrategy(password="testpass")
    resp = httpx.Response(
        status_code=401,
        json={"error": "Unauthorized"},
        request=httpx.Request("GET", "http://test"),
    )
    assert strategy.is_auth_error(resp) is True


@pytest.mark.asyncio
async def test_wireguard_is_auth_error_false():
    """is_auth_error() returns False for 200 status."""
    strategy = WgEasyLoginStrategy(password="testpass")
    resp = httpx.Response(
        status_code=200,
        json={"ok": True},
        request=httpx.Request("GET", "http://test"),
    )
    assert strategy.is_auth_error(resp) is False


@pytest.mark.asyncio
async def test_wireguard_conditional_registration():
    """Tools are not registered when WIREGUARD_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "WIREGUARD_URL", None, create=True):
        wireguard.register(app)
    assert count_tools(app) == 0


# --- Tool Function Helper ---


# --- Mock Data ---

# Recent handshake: 60 seconds ago -> connected=True
_RECENT_HANDSHAKE = (datetime.now(UTC) - timedelta(seconds=60)).strftime(
    "%Y-%m-%dT%H:%M:%S.000Z"
)
# Old handshake: 600 seconds ago -> connected=False
_OLD_HANDSHAKE = (datetime.now(UTC) - timedelta(seconds=600)).strftime(
    "%Y-%m-%dT%H:%M:%S.000Z"
)

MOCK_PEERS_RESPONSE = [
    {
        "id": "peer-1-id",
        "name": "Phone",
        "address": "10.8.0.2/24",
        "enabled": True,
        "latestHandshakeAt": _RECENT_HANDSHAKE,
        "transferRx": 1048576,
        "transferTx": 524288,
    },
    {
        "id": "peer-2-id",
        "name": "Laptop",
        "address": "10.8.0.3/24",
        "enabled": True,
        "latestHandshakeAt": _OLD_HANDSHAKE,
        "transferRx": 2097152,
        "transferTx": 1048576,
    },
]


# --- WireGuard Tool Tests ---


@pytest.mark.asyncio
async def test_wireguard_peers_success():
    """get_wireguard_peers returns peers with name, address, connected status, and traffic."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(MOCK_PEERS_RESPONSE))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["total_peers"] == 2
    assert result["connected_peers"] == 1  # Only Phone is recent enough

    p1 = result["peers"][0]
    assert p1["name"] == "Phone"
    assert p1["address"] == "10.8.0.2"  # CIDR stripped
    assert p1["enabled"] is True
    assert p1["connected"] is True
    assert p1["transfer_rx_bytes"] == 1048576
    assert p1["transfer_tx_bytes"] == 524288

    p2 = result["peers"][1]
    assert p2["name"] == "Laptop"
    assert p2["connected"] is False


@pytest.mark.asyncio
async def test_wireguard_peers_timeout_reported_as_timeout():
    """A timed-out request must report error=timeout, not connection_error."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_wireguard_peers_connected_recent_handshake():
    """Peer with handshake within 3 minutes is connected=True."""
    recent = (datetime.now(UTC) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    peers = [
        {
            "id": "1",
            "name": "P1",
            "address": "10.8.0.2/24",
            "enabled": True,
            "latestHandshakeAt": recent,
            "transferRx": 0,
            "transferTx": 0,
        }
    ]

    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(peers))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["peers"][0]["connected"] is True
    assert result["connected_peers"] == 1


@pytest.mark.asyncio
async def test_wireguard_peers_disconnected_old_handshake():
    """Peer with handshake older than 3 minutes is connected=False."""
    old = (datetime.now(UTC) - timedelta(seconds=300)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    peers = [
        {
            "id": "1",
            "name": "P1",
            "address": "10.8.0.2/24",
            "enabled": True,
            "latestHandshakeAt": old,
            "transferRx": 0,
            "transferTx": 0,
        }
    ]

    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(peers))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["peers"][0]["connected"] is False
    assert result["connected_peers"] == 0


@pytest.mark.asyncio
async def test_wireguard_peers_null_handshake():
    """Peer with null latestHandshakeAt is connected=False."""
    peers = [
        {
            "id": "1",
            "name": "P1",
            "address": "10.8.0.2/24",
            "enabled": True,
            "latestHandshakeAt": None,
            "transferRx": 0,
            "transferTx": 0,
        }
    ]

    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(peers))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["peers"][0]["connected"] is False


@pytest.mark.asyncio
async def test_wireguard_peers_sentinel_handshake():
    """Peer with wg-easy sentinel '0001-01-01T00:00:00.000Z' is connected=False."""
    peers = [
        {
            "id": "1",
            "name": "P1",
            "address": "10.8.0.2/24",
            "enabled": True,
            "latestHandshakeAt": "0001-01-01T00:00:00.000Z",
            "transferRx": 0,
            "transferTx": 0,
        }
    ]

    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(peers))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["peers"][0]["connected"] is False


@pytest.mark.asyncio
async def test_wireguard_peers_empty():
    """Empty peers list returns total_peers=0, connected_peers=0."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response([]))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["total_peers"] == 0
    assert result["connected_peers"] == 0
    assert result["peers"] == []


@pytest.mark.asyncio
async def test_wireguard_peers_connection_error():
    """Connection error returns error dict."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(side_effect=Exception("Connection refused"))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["error"] == "connection_error"
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_wireguard_peers_wg_api_error():
    """A wg-easy error envelope (dict with error/statusCode) surfaces as wg_api_error."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(
        return_value=make_response(
            {"error": True, "message": "Unauthorized", "statusCode": 401}
        )
    )
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["error"] == "wg_api_error"
    assert result["message"] == "Unauthorized"
    assert result["status"] == 401


@pytest.mark.asyncio
async def test_wireguard_peers_unexpected_response():
    """A non-list, non-error payload (version mismatch) surfaces as unexpected_response."""
    mock_manager = AsyncMock()
    # A dict without an 'error' key is neither the v14 list nor the error envelope.
    mock_manager.get = AsyncMock(return_value=make_response({"unexpected": "shape"}))
    ctx = make_mock_ctx(wireguard=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)

    fn = get_tool_fn(app, "get_wireguard_peers")
    result = await fn(ctx)

    assert result["error"] == "unexpected_response"


@pytest.mark.asyncio
async def test_wireguard_registers_1_tool():
    """Registers 1 tool when WIREGUARD_URL and WIREGUARD_PASSWORD are set."""
    app = FastMCP("test")
    with (
        patch.object(config, "WIREGUARD_URL", "http://wg:51821"),
        patch.object(config, "WIREGUARD_PASSWORD", "testpass"),
    ):
        wireguard.register(app)
    assert count_tools(app) == 1
