"""Tests for Technitium DNS tools."""

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import technitium
from tools.technitium import TechnitiumLoginStrategy

# --- Helpers ---


# --- TechnitiumLoginStrategy Tests ---


@pytest.mark.asyncio
async def test_technitium_login_success():
    """login() POSTs user/pass in the form body (not the query string)."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=make_response(
            {
                "status": "ok",
                "token": "abc123",
            }
        )
    )

    strategy = TechnitiumLoginStrategy(password="testpass", username="admin")
    before = time.time()
    result = await strategy.login(mock_client)
    after = time.time()

    # Credentials must be POSTed as form data, keeping them out of the URL.
    mock_client.post.assert_called_once_with(
        "/api/user/login",
        data={"user": "admin", "pass": "testpass"},
    )
    mock_client.get.assert_not_called()

    # Verify LoginResult
    assert result.params == {"token": "abc123"}
    assert result.headers == {}
    assert result.base_url is None
    # expires_at should be roughly time.time() + 1500 (25 min)
    assert result.expires_at is not None
    assert result.expires_at >= before + 1500
    assert result.expires_at <= after + 1500


@pytest.mark.asyncio
async def test_technitium_login_failure():
    """login() raises RuntimeError when API returns status != 'ok'."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=make_response(
            {
                "status": "error",
                "errorMessage": "Invalid username or password.",
            }
        )
    )

    strategy = TechnitiumLoginStrategy(password="wrongpass")
    with pytest.raises(RuntimeError):
        await strategy.login(mock_client)


@pytest.mark.asyncio
async def test_technitium_is_auth_error_true():
    """is_auth_error() returns True for 200 with status=error and 'Invalid token' message."""
    strategy = TechnitiumLoginStrategy(password="testpass")
    resp = make_response(
        {
            "status": "error",
            "errorMessage": "Invalid token or session expired.",
        }
    )
    assert strategy.is_auth_error(resp) is True


@pytest.mark.asyncio
async def test_technitium_is_auth_error_invalid_token_status():
    """is_auth_error() returns True for the documented status='invalid-token' shape."""
    strategy = TechnitiumLoginStrategy(password="testpass")
    resp = make_response(
        {
            "status": "invalid-token",
            "errorMessage": "Invalid token or session expired.",
        }
    )
    assert strategy.is_auth_error(resp) is True


@pytest.mark.asyncio
async def test_technitium_is_auth_error_false():
    """is_auth_error() returns False for 200 with status=ok."""
    strategy = TechnitiumLoginStrategy(password="testpass")
    resp = make_response({"status": "ok", "response": {"zones": []}})
    assert strategy.is_auth_error(resp) is False


@pytest.mark.asyncio
async def test_technitium_is_auth_error_parse_error():
    """is_auth_error() returns False when JSON parsing fails."""
    strategy = TechnitiumLoginStrategy(password="testpass")
    resp = httpx.Response(
        status_code=500,
        text="Internal Server Error",
        request=httpx.Request("GET", "http://test"),
    )
    assert strategy.is_auth_error(resp) is False


@pytest.mark.asyncio
async def test_technitium_conditional_registration():
    """Tools are not registered when TECHNITIUM_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "TECHNITIUM_URL", None, create=True):
        technitium.register(app)
    assert count_tools(app) == 0


# --- Tool Function Helper ---


# --- Mock API Responses ---

# Real dnsClient/resolve shape (live-captured 2026-07-17): the DNS message
# lives under response.result with PascalCase fields and human-suffixed TTLs.
MOCK_RESOLVE_RESPONSE = {
    "status": "ok",
    "response": {
        "result": {
            "RCODE": "NoError",
            "ANCOUNT": 1,
            "Answer": [
                {
                    "Name": "google.com",
                    "Type": "A",
                    "Class": "IN",
                    "TTL": "300 (5m)",
                    "RDATA": {"IPAddress": "142.250.80.46"},
                    "DnssecStatus": "Disabled",
                }
            ],
        },
        "rawResponses": [],
    },
}

MOCK_ZONES_RESPONSE = {
    "status": "ok",
    "response": {
        "zones": [
            {
                "name": "example.com",
                "type": "Primary",
                "disabled": False,
                "internal": False,
            }
        ]
    },
}

MOCK_RECORDS_RESPONSE = {
    "status": "ok",
    "response": {
        "records": [
            {
                "name": "example.com",
                "type": "A",
                "ttl": 3600,
                "rData": {"ipAddress": "1.2.3.4"},
                "disabled": False,
            },
            {
                "name": "mail.example.com",
                "type": "MX",
                "ttl": 3600,
                "rData": {"exchange": "mx1.example.com", "preference": 10},
                "disabled": False,
            },
        ]
    },
}


# --- Technitium Tool Tests ---


@pytest.mark.asyncio
async def test_dns_lookup_success():
    """get_dns_lookup returns resolved records for domain and type."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(MOCK_RESOLVE_RESPONSE))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="google.com", record_type="A")

    assert result["domain"] == "google.com"
    assert result["type"] == "A"
    assert result["record_count"] == 1
    assert result["records"][0]["value"] == "142.250.80.46"
    assert result["records"][0]["ttl"] == 300


@pytest.mark.asyncio
async def test_dns_lookup_timeout_reported_as_timeout():
    """A timed-out request must report error=timeout, not connection_error."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="google.com", record_type="A")

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_dns_lookup_default_type():
    """get_dns_lookup uses 'A' as default type when not specified."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(MOCK_RESOLVE_RESPONSE))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="google.com")

    assert result["type"] == "A"
    # Verify the exact API call: default type is A, passed as a params dict.
    # Path must be the camelCase /api/dnsClient/resolve — the slashed variant
    # 404s on real Technitium — and 'server' is required (live-verified 2026-07-17).
    mock_manager.get.assert_called_once_with(
        "/api/dnsClient/resolve",
        params={"server": "this-server", "domain": "google.com", "type": "A"},
    )


@pytest.mark.asyncio
async def test_dns_lookup_api_error():
    """get_dns_lookup returns error dict on Technitium API error."""
    error_response = {
        "status": "error",
        "errorMessage": "Server failure",
    }
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(error_response))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="fail.com", record_type="A")

    assert result["error"] == "api_error"
    assert "Server failure" in result["message"]


@pytest.mark.asyncio
async def test_dns_lookup_invalid_token_status():
    """get_dns_lookup returns an error dict (not silent empty) for status='invalid-token'."""
    invalid_token_response = {
        "status": "invalid-token",
        "errorMessage": "Invalid token or session expired.",
    }
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(invalid_token_response))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="google.com", record_type="A")

    assert result["error"] == "api_error"
    assert "records" not in result


@pytest.mark.asyncio
async def test_dns_lookup_connection_error():
    """get_dns_lookup returns error dict on connection failure."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(side_effect=Exception("Connection refused"))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_lookup")
    result = await fn(ctx, domain="google.com", record_type="A")

    assert result["error"] == "connection_error"
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_dns_zones_success():
    """get_dns_zones returns zone list with count."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(MOCK_ZONES_RESPONSE))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_zones")
    result = await fn(ctx)

    assert result["zone_count"] == 1
    assert result["zones"][0]["name"] == "example.com"
    assert result["zones"][0]["type"] == "Primary"
    assert result["zones"][0]["disabled"] is False
    assert result["zones"][0]["internal"] is False


@pytest.mark.asyncio
async def test_dns_zones_empty():
    """get_dns_zones returns zone_count 0 for empty zone list."""
    empty_response = {"status": "ok", "response": {"zones": []}}
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(empty_response))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_zones")
    result = await fn(ctx)

    assert result["zone_count"] == 0
    assert result["zones"] == []


@pytest.mark.asyncio
async def test_dns_zone_records_success():
    """get_dns_zone_records returns records with name, type, ttl, value, disabled fields."""
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(MOCK_RECORDS_RESPONSE))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_zone_records")
    result = await fn(ctx, zone="example.com")

    assert result["zone"] == "example.com"
    assert result["record_count"] == 2
    # A record
    r1 = result["records"][0]
    assert r1["name"] == "example.com"
    assert r1["type"] == "A"
    assert r1["ttl"] == 3600
    assert r1["value"] == "1.2.3.4"
    assert r1["disabled"] is False
    # MX record
    r2 = result["records"][1]
    assert r2["type"] == "MX"
    assert r2["value"] == "mx1.example.com"


@pytest.mark.asyncio
async def test_dns_zone_records_empty():
    """get_dns_zone_records handles zone with no records."""
    empty_response = {"status": "ok", "response": {"records": []}}
    mock_manager = AsyncMock()
    mock_manager.get = AsyncMock(return_value=make_response(empty_response))
    ctx = make_mock_ctx(technitium=mock_manager)

    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)

    fn = get_tool_fn(app, "get_dns_zone_records")
    result = await fn(ctx, zone="empty.com")

    assert result["zone"] == "empty.com"
    assert result["record_count"] == 0
    assert result["records"] == []


@pytest.mark.asyncio
async def test_technitium_registers_3_tools():
    """Registers 3 tools when TECHNITIUM_URL is set."""
    app = FastMCP("test")
    with (
        patch.object(config, "TECHNITIUM_URL", "http://dns:5380"),
        patch.object(config, "TECHNITIUM_PASSWORD", "fake-password"),
    ):
        technitium.register(app)
    assert count_tools(app) == 3
