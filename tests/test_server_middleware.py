"""Tests for LanBypassMiddleware: trusted-IP / proxy-header / Origin matrix."""

import os

# Import server under stdio transport so module load doesn't try to build the
# Authentik auth chain (which requires AUTHENTIK_CLIENT_ID). We only need the
# middleware class and the per-process bypass token from it. Transport now lives
# in config (all env access is centralized there), and config may already be
# imported with a non-stdio value from a .env, so force the constant directly
# before server's module-level create_server() runs.
os.environ["MCP_TRANSPORT"] = "stdio"

import config  # noqa: E402

config.MCP_TRANSPORT = "stdio"

import pytest  # noqa: E402

import server  # noqa: E402


def test_httpx_loggers_silenced_below_warning():
    """httpx/httpcore INFO logs the full request URL (secrets in query strings);
    importing server must raise their level to WARNING."""
    import logging

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def make_scope(client_ip, headers=None):
    return {
        "type": "http",
        "client": (client_ip, 12345),
        "headers": headers or [],
    }


async def _capture_headers(middleware, scope):
    """Run the middleware and return the headers the inner app finally saw."""
    captured = {}

    async def inner_app(scope, receive, send):
        captured["headers"] = scope.get("headers", [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    middleware.app = inner_app
    await middleware(scope, receive, send)
    return captured["headers"]


def _has_bypass_token(headers):
    expected = f"Bearer {server.LAN_BYPASS_TOKEN}".encode()
    return any(
        name.lower() == b"authorization" and value == expected
        for name, value in headers
    )


@pytest.fixture
def middleware():
    return server.LanBypassMiddleware(None, ["192.168.1.0/24", "127.0.0.0/8"])


@pytest.mark.asyncio
async def test_trusted_ip_no_headers_gets_bypass(middleware):
    headers = await _capture_headers(middleware, make_scope("192.168.1.42"))
    assert _has_bypass_token(headers)


@pytest.mark.asyncio
async def test_untrusted_ip_no_bypass(middleware):
    headers = await _capture_headers(middleware, make_scope("8.8.8.8"))
    assert not _has_bypass_token(headers)


@pytest.mark.asyncio
async def test_origin_header_refuses_bypass(middleware):
    """A LAN browser cross-origin request carries Origin and must be refused."""
    scope = make_scope("192.168.1.42", [(b"origin", b"https://evil.example")])
    headers = await _capture_headers(middleware, scope)
    assert not _has_bypass_token(headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy_header",
    [b"x-forwarded-for", b"x-real-ip", b"via", b"forwarded"],
)
async def test_proxy_header_refuses_bypass(middleware, proxy_header):
    scope = make_scope("192.168.1.42", [(proxy_header, b"1.2.3.4")])
    headers = await _capture_headers(middleware, scope)
    assert not _has_bypass_token(headers)


@pytest.mark.asyncio
async def test_bypass_replaces_client_supplied_authorization(middleware):
    """A trusted direct request's own Authorization header is stripped/replaced."""
    scope = make_scope("127.0.0.1", [(b"authorization", b"Bearer attacker")])
    headers = await _capture_headers(middleware, scope)
    assert _has_bypass_token(headers)
    assert not any(value == b"Bearer attacker" for _, value in headers)
