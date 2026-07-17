"""Nginx Proxy Manager tools for homelab MCP server."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.certs import parse_expiry
from lib.http import service_request
from lib.meta import build_meta


class NpmLoginStrategy:
    """NPM JWT session auth via POST /api/tokens."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        resp = await client.post(
            "/api/tokens",
            json={
                "identity": self.email,
                "secret": self.password,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["token"]
        # NPM JWT default expiry is 1 day; use 23h to be safe
        expires_at = time.time() + 82800
        return LoginResult(
            headers={"Authorization": f"Bearer {token}"},
            expires_at=expires_at,
        )

    def is_auth_error(self, response: httpx.Response) -> bool:
        return response.status_code in (401, 403)


def register(mcp):
    """Register NPM tools. Skips if credentials are not configured."""
    if not (config.NPM_URL and config.NPM_EMAIL and config.NPM_PASSWORD):
        return

    async def _get(ctx: Context, path: str) -> Any:
        """Execute a GET request against NPM API via SessionAuthManager."""
        return await service_request(ctx, "npm", path, display_name="NPM")

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_npm_overview(ctx: Context) -> dict:
        """Get Nginx Proxy Manager overview: proxy hosts, SSL certificates, redirections, access lists, and dead hosts."""
        # gather(return_exceptions=True) never raises from the awaited coros, so
        # no outer try/except is needed. service_request maps httpx failures to
        # error dicts; the Exception scan below is the safety net for any other
        # exception it does not catch (e.g. a missing lifespan client).
        results = await asyncio.gather(
            _get(ctx, "/api/nginx/proxy-hosts"),
            _get(ctx, "/api/nginx/certificates"),
            _get(ctx, "/api/nginx/redirection-hosts"),
            _get(ctx, "/api/nginx/access-lists"),
            _get(ctx, "/api/nginx/dead-hosts"),
            return_exceptions=True,
        )

        # cast: gather(return_exceptions=True) types each result as
        # `Any | BaseException`; the guard loops below return on any exception,
        # so downstream these are plain JSON values (no runtime effect).
        raw_proxies, raw_certs, raw_redirects, raw_acls, raw_dead = cast(list, results)

        # Handle unexpected exceptions surfaced by gather
        for r in results:
            if isinstance(r, Exception):
                return {"error": "connection_error", "message": str(r)}

        # If any endpoint returned an error dict, propagate
        for r in results:
            if isinstance(r, dict) and "error" in r:
                return r

        # Process proxy hosts
        now = datetime.now(UTC)
        expiry_threshold = now + timedelta(days=30)

        proxy_hosts = []
        for h in raw_proxies:
            proxy_hosts.append(
                {
                    "domain": h["domain_names"][0] if h.get("domain_names") else "",
                    "all_domains": h.get("domain_names", []),
                    "target": f"{h.get('forward_scheme', 'http')}://{h.get('forward_host', '')}:{h.get('forward_port', '')}",
                    "ssl_forced": bool(h.get("ssl_forced")),
                    "enabled": bool(h.get("enabled")),
                    "online": h.get("meta", {}).get("nginx_online", False)
                    if h.get("meta")
                    else False,
                }
            )

        online_count = sum(1 for p in proxy_hosts if p["online"])

        # Process certificates
        certificates = []
        for c in raw_certs:
            expires_on = c.get("expires_on", "")
            exp_dt = parse_expiry(expires_on)
            cert_entry = {
                "name": c.get("nice_name", ""),
                "domains": c.get("domain_names", []),
                "expires_on": expires_on,
                "provider": c.get("provider", ""),
            }
            if expires_on and exp_dt is None:
                # A present-but-unparseable expiry must not be silently reported
                # as 'not expiring' (the unsafe default for a monitoring tool):
                # flag it as unknown so a real expiry can't hide.
                cert_entry["expiring_soon"] = None
                cert_entry["expiry_parse_error"] = True
            else:
                cert_entry["expiring_soon"] = (
                    exp_dt is not None and exp_dt <= expiry_threshold
                )
            certificates.append(cert_entry)

        expiring_soon_count = sum(1 for c in certificates if c["expiring_soon"])

        # Process redirection hosts
        redirection_hosts = []
        for r in raw_redirects:
            redirection_hosts.append(
                {
                    "domain": r["domain_names"][0] if r.get("domain_names") else "",
                    "forward_to": r.get("forward_domain_name", ""),
                    "http_code": r.get("forward_http_code", 0),
                    "enabled": bool(r.get("enabled")),
                }
            )

        # Process access lists
        access_lists = []
        for a in raw_acls:
            access_lists.append(
                {
                    "name": a.get("name", ""),
                    "rule_count": len(a.get("items", [])),
                }
            )

        # Process dead hosts
        dead_hosts = []
        for d in raw_dead:
            dead_hosts.append(
                {
                    "domain": d["domain_names"][0] if d.get("domain_names") else "",
                    "enabled": bool(d.get("enabled")),
                }
            )

        # Build summary
        parts = [
            f"{len(proxy_hosts)} proxy host{'s' if len(proxy_hosts) != 1 else ''} ({online_count} online)",
            f"{len(certificates)} SSL cert{'s' if len(certificates) != 1 else ''}",
        ]
        if expiring_soon_count:
            parts[-1] += f" ({expiring_soon_count} expiring soon)"
        parts.append(
            f"{len(redirection_hosts)} redirection{'s' if len(redirection_hosts) != 1 else ''}"
        )
        parts.append(
            f"{len(access_lists)} access list{'s' if len(access_lists) != 1 else ''}"
        )
        parts.append(
            f"{len(dead_hosts)} dead host{'s' if len(dead_hosts) != 1 else ''}"
        )

        return {
            "summary": ", ".join(parts),
            "proxy_hosts": proxy_hosts,
            "proxy_host_count": len(proxy_hosts),
            "certificates": certificates,
            "certificate_count": len(certificates),
            "expiring_soon_count": expiring_soon_count,
            "redirection_hosts": redirection_hosts,
            "redirection_count": len(redirection_hosts),
            "access_lists": access_lists,
            "access_list_count": len(access_lists),
            "dead_hosts": dead_hosts,
            "dead_host_count": len(dead_hosts),
            "_meta": build_meta("npm"),
        }
