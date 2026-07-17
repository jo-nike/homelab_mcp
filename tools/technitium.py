"""Technitium DNS tools for homelab MCP server."""

import time
from typing import Annotated

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta
from lib.redact import redact_exception

# --- Login Strategy ---

TOKEN_DURATION = 25 * 60  # 25 minutes (conservative for 30 min inactivity expiry)


class TechnitiumLoginStrategy:
    """Login strategy for Technitium DNS session token acquisition.

    Technitium's /api/user/login endpoint returns a session token. It accepts
    both GET and POST; we POST so the password is in the form body rather than
    the query string (query strings are logged by the target, by proxies, and
    by httpx's request logger). The returned token is passed as a query param
    on subsequent (non-secret) requests. Token expires after 30 minutes of
    inactivity; we refresh at 25 minutes.
    """

    def __init__(self, password: str, username: str = "admin"):
        self._username = username
        self._password = password

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        resp = await client.post(
            "/api/user/login",
            data={"user": self._username, "pass": self._password},
        )
        data = resp.json()

        if data.get("status") != "ok":
            raise RuntimeError(
                f"Technitium login failed: {data.get('errorMessage', 'unknown error')}"
            )

        token = data["token"]
        return LoginResult(
            params={"token": token},
            expires_at=time.time() + TOKEN_DURATION,
        )

    def is_auth_error(self, response: httpx.Response) -> bool:
        """Check if response indicates an auth error.

        Technitium documents three status values (ok, error, invalid-token).
        Expired/invalid sessions come back as status "invalid-token", while
        some auth failures arrive as status "error" with an "Invalid token..."
        message. Both mean re-login, not HTTP 401.
        """
        try:
            data = response.json()
            status = data.get("status")
            if status == "invalid-token":
                return True
            if status == "error":
                msg = data.get("errorMessage", "").lower()
                return "invalid token" in msg
        except Exception:
            pass
        return False


def _extract_rdata_value(rdata: dict) -> str:
    """Extract the most useful display value from a Technitium rData/RDATA dict.

    Key casing differs by endpoint (live-verified 2026-07-17): zone records use
    lowerCamel ({"ipAddress": ...}), dnsClient/resolve uses PascalCase
    ({"IPAddress": ...}, CNAME {"Domain": ...}, MX {"Exchange": ...},
    TXT {"Text": ...}, NS {"NameServer": ...}), so match case-insensitively.
    """
    lowered = {k.lower(): v for k, v in rdata.items()}
    for key in (
        "ipaddress",
        "cname",
        "domain",
        "exchange",
        "text",
        "ptrdname",
        "nameserver",
        "primarynameserver",
    ):
        if key in lowered:
            return str(lowered[key])
    return str(rdata) if rdata else ""


def _parse_ttl(ttl) -> int:
    """Parse a Technitium TTL, which resolve returns as e.g. '3600 (1h)'."""
    if isinstance(ttl, int):
        return ttl
    try:
        return int(str(ttl).split()[0])
    except (ValueError, IndexError):
        return 0


def register(mcp):
    """Register Technitium DNS tools. Skips if credentials are not configured."""
    if not (config.TECHNITIUM_URL and config.TECHNITIUM_PASSWORD):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> dict:
        """Execute a GET request against Technitium API via SessionAuthManager."""
        client = ctx.lifespan_context["technitium"]  # SessionAuthManager
        try:
            resp = await client.get(path, params=params or {})
            data = resp.json()
            if data.get("status") in ("error", "invalid-token"):
                return {
                    "error": "api_error",
                    "message": data.get("errorMessage", "Unknown error"),
                }
            return data
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Technitium did not respond in time"}
        except Exception as e:
            return {"error": "connection_error", "message": redact_exception(e)}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_dns_lookup(
        ctx: Context,
        domain: Annotated[str, "Domain name to resolve (e.g. google.com)"],
        record_type: Annotated[
            str, "DNS record type: A, AAAA, CNAME, MX, TXT, NS, SOA, etc."
        ] = "A",
    ) -> dict:
        """Perform a DNS lookup via Technitium DNS server. Returns resolved records for the given domain and type."""
        query_type = record_type or "A"
        # Endpoint is /api/dnsClient/resolve (camelCase); /api/dns/client/resolve 404s.
        # The API requires 'server': "this-server" resolves via Technitium itself.
        data = await _get(
            ctx,
            "/api/dnsClient/resolve",
            {"server": "this-server", "domain": domain, "type": query_type},
        )

        if "error" in data:
            return data

        # Live shape (verified 2026-07-17): response.result is a DNS message
        # with PascalCase fields — Answer[].Name/Type/TTL/RDATA.
        answer = data.get("response", {}).get("result", {}).get("Answer", [])
        records = [
            {
                "name": r.get("Name", ""),
                "type": r.get("Type", ""),
                "value": _extract_rdata_value(r.get("RDATA", {})),
                "ttl": _parse_ttl(r.get("TTL", 0)),
            }
            for r in answer
        ]

        return {
            "domain": domain,
            "type": query_type,
            "records": records,
            "record_count": len(records),
            "_meta": build_meta("technitium"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_dns_zones(ctx: Context) -> dict:
        """List all DNS zones managed by Technitium DNS server."""
        data = await _get(
            ctx, "/api/zones/list", {"pageNumber": 1, "zonesPerPage": 100}
        )

        if "error" in data:
            return data

        raw_zones = data.get("response", {}).get("zones", [])
        zones = [
            {
                "name": z.get("name", ""),
                "type": z.get("type", ""),
                "disabled": z.get("disabled", False),
                "internal": z.get("internal", False),
            }
            for z in raw_zones
        ]

        return {
            "zones": zones,
            "zone_count": len(zones),
            "_meta": build_meta("technitium"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_dns_zone_records(
        ctx: Context,
        zone: Annotated[str, "Zone name to list records for (e.g. example.com)"],
    ) -> dict:
        """List all DNS records for a specific zone in Technitium DNS server."""
        data = await _get(
            ctx, "/api/zones/records/get", {"domain": zone, "listZone": "true"}
        )

        if "error" in data:
            return data

        raw_records = data.get("response", {}).get("records", [])
        records = [
            {
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "ttl": r.get("ttl", 0),
                "value": _extract_rdata_value(r.get("rData", {})),
                "disabled": r.get("disabled", False),
            }
            for r in raw_records
        ]

        return {
            "zone": zone,
            "records": records,
            "record_count": len(records),
            "_meta": build_meta("technitium"),
        }
