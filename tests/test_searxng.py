"""Tests for SearXNG web search and page fetch tools."""

import ipaddress
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import searxng

# A public IP so the SSRF guard lets happy-path fetches through without real DNS.
_PUBLIC_IP = [ipaddress.ip_address("93.184.216.34")]


def _allow_public():
    """Patch the resolver to a public IP (no network) for happy-path fetches."""
    return patch.object(searxng, "_resolve_ips", AsyncMock(return_value=_PUBLIC_IP))


# --- Helpers ---


# --- Registration ---


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
def test_register_creates_tools():
    app = FastMCP("test")
    searxng.register(app)
    # web_search, search_code, search_academic, search_news, fetch_page
    assert count_tools(app) == 5


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("search_code", {"engines": "github,stackoverflow"}),
        ("search_academic", {"engines": "arxiv,google scholar"}),
        ("search_news", {"categories": "news"}),
    ],
)
async def test_category_helpers_pass_expected_params(tool_name, expected):
    """Each helper delegates to /search with its engine/category selection."""
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, tool_name)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response({"results": []}))
    ctx = make_mock_ctx({"searxng": mock_client})

    await fn(ctx, query="q")

    assert mock_client.get.await_args is not None
    params = mock_client.get.await_args.kwargs.get("params") or {}
    for key, value in expected.items():
        assert params[key] == value
    # search_news does not constrain engines; the search_* ones do not set categories.
    if "engines" not in expected:
        assert "engines" not in params
    if "categories" not in expected:
        assert "categories" not in params


@patch.object(config, "SEARXNG_URL", "")
def test_register_skips_search_but_keeps_fetch_page_when_no_url():
    """fetch_page has no SearXNG dependency, so it registers even with no URL;
    the web-search tools are skipped."""
    app = FastMCP("test")
    searxng.register(app)
    names = {
        k[len("tool:") : k.index("@")]
        for k in app._local_provider._components
        if k.startswith("tool:")
    }
    assert names == {"fetch_page"}


# --- web_search ---


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_web_search_returns_structured_results():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "web_search")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response(
            {
                "results": [
                    {
                        "title": "Python Docs",
                        "url": "https://docs.python.org",
                        "content": "Official Python documentation",
                        "engines": ["google"],
                        "score": 1.0,
                        "category": "general",
                    },
                    {
                        "title": "PyPI",
                        "url": "https://pypi.org",
                        "content": "Python Package Index",
                        "engines": ["duckduckgo"],
                        "score": 0.8,
                        "category": "general",
                    },
                ],
                "number_of_results": 100,
            }
        )
    )

    ctx = make_mock_ctx({"searxng": mock_client})
    result = await fn(ctx, query="python")

    assert result["query"] == "python"
    assert result["result_count"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "Python Docs"
    assert result["results"][0]["url"] == "https://docs.python.org"
    assert result["results"][0]["snippet"] == "Official Python documentation"
    assert "_meta" in result


# --- fetch_page ---


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_sends_user_agent():
    """Verify fetch_page uses the web_fetch client which carries the User-Agent header."""
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response(
            "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>",
            content_type="text/html",
        )
    )

    ctx = make_mock_ctx({"web_fetch": mock_client})
    with _allow_public():
        result = await fn(ctx, url="https://example.com")

    mock_client.get.assert_called_once_with(
        "https://example.com", follow_redirects=False
    )
    assert "error" not in result
    assert result["url"] == "https://example.com"
    assert result["title"] == "Test"


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_returns_error_on_timeout():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    ctx = make_mock_ctx({"web_fetch": mock_client})
    with _allow_public():
        result = await fn(ctx, url="https://slow.example.com")

    assert result["error"] == "timeout"


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_rejects_binary_content():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            status_code=200,
            content=b"binary data",
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "http://test"),
        )
    )

    ctx = make_mock_ctx({"web_fetch": mock_client})
    with _allow_public():
        result = await fn(ctx, url="https://example.com/file.pdf")

    assert result["error"] == "binary_content"


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_rejects_non_http_scheme():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    ctx = make_mock_ctx({"web_fetch": mock_client})
    result = await fn(ctx, url="file:///etc/passwd")

    assert result["error"] == "blocked_target"
    mock_client.get.assert_not_called()


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://100.100.100.200/",
    ],
)
async def test_fetch_page_blocks_loopback_and_metadata(url):
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    ctx = make_mock_ctx({"web_fetch": mock_client})
    result = await fn(ctx, url=url)

    assert result["error"] == "blocked_target"
    mock_client.get.assert_not_called()


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@patch.object(config, "FETCH_ALLOW_PRIVATE", False)
@pytest.mark.asyncio
async def test_fetch_page_blocks_rfc1918_when_disallowed():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    ctx = make_mock_ctx({"web_fetch": mock_client})
    result = await fn(ctx, url="http://192.168.1.79:9090/")

    assert result["error"] == "blocked_target"
    mock_client.get.assert_not_called()


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@patch.object(config, "FETCH_ALLOW_PRIVATE", True)
@pytest.mark.asyncio
async def test_fetch_page_allows_rfc1918_when_permitted():
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response(
            "<html><head><title>LAN</title></head><body><p>internal</p></body></html>",
            content_type="text/html",
        )
    )
    ctx = make_mock_ctx({"web_fetch": mock_client})
    result = await fn(ctx, url="http://192.168.1.79:9090/")

    assert "error" not in result
    assert result["title"] == "LAN"


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_revalidates_redirect_target():
    """A redirect to an internal address must be blocked, not followed."""
    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")

    redirect = httpx.Response(
        status_code=302,
        headers={"location": "http://169.254.169.254/latest/meta-data/"},
        request=httpx.Request("GET", "http://93.184.216.34"),
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=redirect)

    ctx = make_mock_ctx({"web_fetch": mock_client})
    # IP literals: first hop (public) is fetched, the redirect to the metadata
    # IP (link-local) is re-validated and blocked before any request is made.
    result = await fn(ctx, url="http://93.184.216.34/")

    assert result["error"] == "blocked_target"
    # Exactly one request (the first hop); the metadata redirect is not fetched.
    assert mock_client.get.call_count == 1


@patch.object(config, "SEARXNG_URL", "http://searxng:8080")
@pytest.mark.asyncio
async def test_fetch_page_request_carries_user_agent_header():
    """Behavioral check: a web_fetch client built the way server.py builds it
    sends the homelab-mcp User-Agent on the actual outgoing request."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(
            200,
            text="<html><head><title>T</title></head><body><p>hi</p></body></html>",
            headers={"content-type": "text/html"},
        )

    # Same construction as server.py's web_fetch client.
    ua = "Mozilla/5.0 (compatible; homelab-mcp/1.0; +https://github.com/homelab-mcp)"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": ua},
    )

    app = FastMCP("test")
    searxng.register(app)
    fn = get_tool_fn(app, "fetch_page")
    ctx = make_mock_ctx({"web_fetch": client})
    async with client:
        with _allow_public():
            await fn(ctx, url="https://example.com")

    assert captured["ua"] is not None
    assert "homelab-mcp/1.0" in captured["ua"]
