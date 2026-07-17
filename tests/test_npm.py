"""Tests for Nginx Proxy Manager tools."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import npm
from tools.npm import NpmLoginStrategy

# --- NpmLoginStrategy ---


@pytest.mark.asyncio
async def test_npm_login_parses_jwt_into_bearer_header():
    """login() POSTs identity/secret and returns the JWT as a Bearer header."""
    captured = {}

    async def post(path, json=None):
        captured["path"] = path
        captured["json"] = json
        return make_response({"token": "jwt-abc", "expires": "..."})

    client = AsyncMock()
    client.post = post
    strategy = NpmLoginStrategy(email="admin@example.com", password="pw")

    result = await strategy.login(client)

    assert captured["path"] == "/api/tokens"
    assert captured["json"] == {"identity": "admin@example.com", "secret": "pw"}
    assert result.headers["Authorization"] == "Bearer jwt-abc"
    assert result.expires_at is not None


@pytest.mark.asyncio
async def test_npm_login_raises_on_http_error():
    """A non-2xx login response raises (surfaced as a failed auth)."""
    client = AsyncMock()
    client.post = AsyncMock(
        return_value=make_response({"error": "bad"}, status_code=401)
    )
    strategy = NpmLoginStrategy(email="admin@example.com", password="wrong")
    with pytest.raises(httpx.HTTPStatusError):
        await strategy.login(client)


def test_npm_is_auth_error_on_401_403():
    """401/403 trigger a re-login; other statuses do not."""
    strategy = NpmLoginStrategy(email="a", password="b")
    req = httpx.Request("GET", "http://test")
    assert strategy.is_auth_error(httpx.Response(401, request=req)) is True
    assert strategy.is_auth_error(httpx.Response(403, request=req)) is True
    assert strategy.is_auth_error(httpx.Response(200, request=req)) is False
    assert strategy.is_auth_error(httpx.Response(500, request=req)) is False


# --- Helpers ---


# --- Sample data ---

PROXY_HOSTS = [
    {
        "id": 1,
        "domain_names": ["app.example.com", "www.app.example.com"],
        "forward_host": "192.168.1.10",
        "forward_port": 8080,
        "forward_scheme": "http",
        "ssl_forced": 1,
        "certificate_id": 5,
        "enabled": 1,
        "meta": {"nginx_online": True},
    },
    {
        "id": 2,
        "domain_names": ["api.example.com"],
        "forward_host": "192.168.1.20",
        "forward_port": 3000,
        "forward_scheme": "https",
        "ssl_forced": 0,
        "certificate_id": 0,
        "enabled": 0,
        "meta": {"nginx_online": False},
    },
]

# One cert expiring soon (within 30 days), one not
CERTIFICATES = [
    {
        "id": 5,
        "nice_name": "Wildcard",
        "domain_names": ["*.example.com"],
        "expires_on": (datetime.now(UTC) + timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        "provider": "letsencrypt",
    },
    {
        "id": 6,
        "nice_name": "API cert",
        "domain_names": ["api.example.com"],
        "expires_on": (datetime.now(UTC) + timedelta(days=90)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        "provider": "letsencrypt",
    },
]

REDIRECTION_HOSTS = [
    {
        "id": 1,
        "domain_names": ["old.example.com"],
        "forward_domain_name": "new.example.com",
        "forward_http_code": 301,
        "enabled": 1,
    },
]

ACCESS_LISTS = [
    {
        "id": 1,
        "name": "Internal Only",
        "items": [{"allow": "192.168.1.0/24"}, {"deny": "all"}],
    },
]

DEAD_HOSTS = [
    {
        "id": 1,
        "domain_names": ["dead.example.com"],
        "enabled": 1,
    },
]


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with NPM tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "NPM_URL", "http://192.168.1.17:81"),
        patch.object(config, "NPM_EMAIL", "admin@example.com"),
        patch.object(config, "NPM_PASSWORD", "password"),
    ):
        npm.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock SessionAuthManager."""
    return AsyncMock()


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    app = FastMCP("test-skip")
    with (
        patch.object(config, "NPM_URL", ""),
        patch.object(config, "NPM_EMAIL", "a@b.com"),
        patch.object(config, "NPM_PASSWORD", "pw"),
    ):
        npm.register(app)
    assert count_tools(app) == 0


def test_register_adds_tool():
    app = FastMCP("test-add")
    with (
        patch.object(config, "NPM_URL", "http://192.168.1.17:81"),
        patch.object(config, "NPM_EMAIL", "a@b.com"),
        patch.object(config, "NPM_PASSWORD", "pw"),
    ):
        npm.register(app)
    assert count_tools(app) == 1


# --- Test: get_npm_overview ---


@pytest.mark.asyncio
async def test_get_npm_overview_proxy_hosts(mcp_app, mock_client):
    """Returns proxy_hosts list with domain, target, ssl_forced, enabled fields."""
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(PROXY_HOSTS),
            make_response(CERTIFICATES),
            make_response(REDIRECTION_HOSTS),
            make_response(ACCESS_LISTS),
            make_response(DEAD_HOSTS),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    assert result["proxy_host_count"] == 2
    host1 = result["proxy_hosts"][0]
    assert host1["domain"] == "app.example.com"
    assert host1["all_domains"] == ["app.example.com", "www.app.example.com"]
    assert host1["target"] == "http://192.168.1.10:8080"
    assert host1["ssl_forced"] is True
    assert host1["enabled"] is True
    assert host1["online"] is True

    host2 = result["proxy_hosts"][1]
    assert host2["enabled"] is False
    assert host2["online"] is False


@pytest.mark.asyncio
async def test_get_npm_overview_certificates(mcp_app, mock_client):
    """Returns certificates list with name, domains, expires_on, provider, and expiring_soon flag."""
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(PROXY_HOSTS),
            make_response(CERTIFICATES),
            make_response(REDIRECTION_HOSTS),
            make_response(ACCESS_LISTS),
            make_response(DEAD_HOSTS),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    assert result["certificate_count"] == 2
    cert1 = result["certificates"][0]
    assert cert1["name"] == "Wildcard"
    assert cert1["domains"] == ["*.example.com"]
    assert cert1["provider"] == "letsencrypt"
    assert cert1["expiring_soon"] is True

    cert2 = result["certificates"][1]
    assert cert2["expiring_soon"] is False

    assert result["expiring_soon_count"] == 1


@pytest.mark.asyncio
async def test_get_npm_overview_unparseable_expiry_flagged(mcp_app, mock_client):
    """Regression (WP5): a present-but-unparseable expiry is flagged unknown
    (expiring_soon=None + parse_error), never silently reported as fine."""
    ctx = make_mock_ctx(npm=mock_client)
    bad_cert = [
        {
            "id": 9,
            "nice_name": "Broken",
            "domain_names": ["broken.example.com"],
            "expires_on": "not-a-real-date",
            "provider": "letsencrypt",
        }
    ]
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(PROXY_HOSTS),
            make_response(bad_cert),
            make_response(REDIRECTION_HOSTS),
            make_response(ACCESS_LISTS),
            make_response(DEAD_HOSTS),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    cert = result["certificates"][0]
    assert cert["expiring_soon"] is None
    assert cert["expiry_parse_error"] is True


@pytest.mark.asyncio
async def test_get_npm_overview_other_sections(mcp_app, mock_client):
    """Returns redirection_hosts, access_lists, and dead_hosts sections."""
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(PROXY_HOSTS),
            make_response(CERTIFICATES),
            make_response(REDIRECTION_HOSTS),
            make_response(ACCESS_LISTS),
            make_response(DEAD_HOSTS),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    # Redirections
    assert result["redirection_count"] == 1
    redir = result["redirection_hosts"][0]
    assert redir["domain"] == "old.example.com"
    assert redir["forward_to"] == "new.example.com"
    assert redir["http_code"] == 301
    assert redir["enabled"] is True

    # Access lists
    assert result["access_list_count"] == 1
    acl = result["access_lists"][0]
    assert acl["name"] == "Internal Only"
    assert acl["rule_count"] == 2

    # Dead hosts
    assert result["dead_host_count"] == 1
    dead = result["dead_hosts"][0]
    assert dead["domain"] == "dead.example.com"


@pytest.mark.asyncio
async def test_get_npm_overview_summary(mcp_app, mock_client):
    """Summary includes counts for each section."""
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(PROXY_HOSTS),
            make_response(CERTIFICATES),
            make_response(REDIRECTION_HOSTS),
            make_response(ACCESS_LISTS),
            make_response(DEAD_HOSTS),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    summary = result["summary"]
    assert "2 proxy hosts" in summary
    assert "1 online" in summary
    assert "2 SSL certs" in summary
    assert "1 expiring soon" in summary
    assert "1 redirection" in summary
    assert "1 access list" in summary
    assert "1 dead host" in summary


@pytest.mark.asyncio
async def test_get_npm_overview_connection_error(mcp_app, mock_client):
    """Returns error dict on connection failure."""
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "connection_error"
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_get_npm_overview_http_error(mcp_app, mock_client):
    """A non-2xx NPM response is surfaced as http_error, not passed through as data.

    Regression for the shared lib.http adoption: NPM's old _get skipped
    raise_for_status, so an error body flowed in as if it were real data.
    """
    ctx = make_mock_ctx(npm=mock_client)
    mock_client.get = AsyncMock(return_value=make_response({"error": "nope"}, 502))

    tool_fn = get_tool_fn(mcp_app, "get_npm_overview")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 502
