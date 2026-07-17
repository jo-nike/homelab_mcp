"""SearXNG web search and page fetch tools."""

import asyncio
import ipaddress
import re
import socket
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta

# Cloud instance-metadata endpoints, blocked unconditionally (169.254.169.254 is
# also link-local, but listing them makes the intent explicit and covers the
# non-link-local ones like Alibaba's 100.100.100.200 and AWS's IPv6 IMDS).
_METADATA_IPS = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254")
)

_MAX_FETCH_REDIRECTS = 10


class _BlockedTarget(Exception):
    """Raised when a fetch_page target (or redirect hop) is disallowed."""


def _is_blocked_ip(ip) -> bool:
    """Decide whether an IP is an SSRF risk given the current fetch policy."""
    if ip in _METADATA_IPS:
        return True
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if ip.is_private:
        return not config.FETCH_ALLOW_PRIVATE
    return False


async def _resolve_ips(host: str) -> list:
    """Resolve host to a list of ipaddress objects. IP literals resolve to
    themselves; hostnames go through the event-loop resolver."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    loop = asyncio.get_event_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


async def _assert_fetch_allowed(url: str) -> None:
    """Reject non-http(s) schemes and targets that resolve to blocked IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise _BlockedTarget(
            f"scheme '{parsed.scheme or 'none'}' is not allowed (http/https only)"
        )
    host = parsed.hostname
    if not host:
        raise _BlockedTarget("URL has no host")
    try:
        ips = await _resolve_ips(host)
    except socket.gaierror as e:
        raise _BlockedTarget(f"could not resolve host '{host}'") from e
    for ip in ips:
        if _is_blocked_ip(ip):
            raise _BlockedTarget(
                f"target host '{host}' resolves to a blocked address ({ip})"
            )


async def _guarded_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET url, following redirects manually so every hop is re-validated
    against the SSRF policy before the request is made."""
    current = url
    for _ in range(_MAX_FETCH_REDIRECTS + 1):
        await _assert_fetch_allowed(current)
        resp = await client.get(current, follow_redirects=False)
        location = resp.headers.get("location")
        if resp.is_redirect and location:
            current = urljoin(current, location)
            continue
        resp.raise_for_status()
        return resp
    raise _BlockedTarget("too many redirects")


def register(mcp):
    """Register SearXNG tools. fetch_page needs only the web_fetch client (no
    SearXNG dependency) and always registers; the web-search tools skip when
    SEARXNG_URL is not configured."""

    @mcp.tool(annotations={"readOnlyHint": True})
    async def fetch_page(
        ctx: Context,
        url: Annotated[str, "URL to fetch and extract content from"],
    ) -> dict:
        """Fetch a web page and extract its content as clean markdown. Strips navigation, ads, and boilerplate.

        Only http/https URLs are fetched. Loopback, link-local, and cloud-metadata
        targets are always blocked; private/RFC1918 targets are blocked unless
        FETCH_ALLOW_PRIVATE is set (default allows LAN fetches). Redirects are
        re-validated at every hop.
        """
        client: httpx.AsyncClient = ctx.lifespan_context["web_fetch"]

        try:
            resp = await _guarded_get(client, url)
        except _BlockedTarget as e:
            return {"error": "blocked_target", "message": str(e)}
        except httpx.TimeoutException:
            return {
                "error": "timeout",
                "message": f"Page did not respond within 30 seconds: {url}",
            }
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": str(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": str(e)}

        # Check content type (D-16)
        content_type = (
            resp.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        text_types = {
            "text/html",
            "text/plain",
            "application/json",
            "application/xml",
            "text/xml",
        }
        if content_type and content_type not in text_types:
            return {
                "error": "binary_content",
                "message": f"Cannot extract text from {content_type}",
            }

        # Check size (D-13)
        if len(resp.content) > 10_485_760:
            return {
                "error": "too_large",
                "message": f"Response exceeds 10MB limit ({len(resp.content):,} bytes)",
            }

        # Extract with trafilatura (D-11, D-20). trafilatura.extract is
        # synchronous and CPU-bound; on a large page it would block the single
        # asyncio loop (stalling other tool calls and the refresh task), so run
        # it in a worker thread.
        html = resp.text
        extracted = await asyncio.to_thread(
            trafilatura.extract,
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            include_formatting=True,
            include_images=False,
            favor_recall=True,
            deduplicate=True,
        )

        if not extracted:
            # Fallback: return raw text for non-HTML content types (including XML
            # documents trafilatura can't parse, whose raw text is still useful).
            if content_type in (
                "text/plain",
                "application/json",
                "application/xml",
                "text/xml",
            ):
                extracted = resp.text
            else:
                return {
                    "error": "extraction_failed",
                    "message": f"Could not extract readable content from {url}",
                }

        # Truncate if needed (D-14)
        truncated = False
        if len(extracted) > 100_000:
            extracted = extracted[:100_000] + "\n\n[Content truncated due to length...]"
            truncated = True

        # Title extraction
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = match.group(1).strip() if match else ""

        return {
            "url": url,
            "title": title,
            "content": extracted,
            "content_length": len(extracted),
            "truncated": truncated,
            "summary": extracted[:200],
            "_meta": build_meta("web", confidence="medium"),
        }

    if not config.SEARXNG_URL:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against SearXNG."""
        return await service_request(
            ctx, "searxng", path, params=params, display_name="SearXNG"
        )

    async def _search(
        ctx: Context,
        query: str,
        engines: str | None = None,
        categories: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Core search function. Category helpers delegate here."""
        params = {"q": query, "format": "json"}
        if engines:
            params["engines"] = engines
        if categories:
            params["categories"] = categories

        data = await _get(ctx, "/search", params)

        if isinstance(data, dict) and "error" in data:
            return data

        results = data.get("results", [])[:limit]

        processed = []
        for r in results:
            processed.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                    "engines": r.get("engines", []),
                    "score": r.get("score", 0),
                    "category": r.get("category", ""),
                    "publishedDate": r.get("publishedDate"),
                }
            )

        return {
            "query": query,
            "results": processed,
            "result_count": len(processed),
            "total_results": data.get("number_of_results", 0),
            "_meta": build_meta("searxng"),
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def web_search(
        ctx: Context,
        query: Annotated[str, "Search query"],
        engines: Annotated[
            str | None,
            "Comma-separated engine names (e.g. 'google,duckduckgo'). Available: google, duckduckgo, bing, wikipedia, github, stackoverflow, arxiv, 'google scholar'. Default: all enabled.",
        ] = None,
        categories: Annotated[
            str | None,
            "Comma-separated categories (e.g. 'general,news'). Default: all.",
        ] = None,
        limit: Annotated[int, "Max results to return"] = 10,
    ) -> dict:
        """Search the web via SearXNG. Returns structured results with title, URL, snippet, and engine metadata."""
        return await _search(
            ctx, query, engines=engines, categories=categories, limit=limit
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def search_code(
        ctx: Context,
        query: Annotated[str, "Code search query"],
        limit: Annotated[int, "Max results to return"] = 10,
    ) -> dict:
        """Search code repositories and Q&A sites (GitHub + StackOverflow)."""
        return await _search(ctx, query, engines="github,stackoverflow", limit=limit)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def search_academic(
        ctx: Context,
        query: Annotated[str, "Academic search query"],
        limit: Annotated[int, "Max results to return"] = 10,
    ) -> dict:
        """Search academic papers and research (ArXiv + Google Scholar)."""
        return await _search(ctx, query, engines="arxiv,google scholar", limit=limit)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def search_news(
        ctx: Context,
        query: Annotated[str, "News search query"],
        limit: Annotated[int, "Max results to return"] = 10,
    ) -> dict:
        """Search recent news articles."""
        return await _search(ctx, query, categories="news", limit=limit)
