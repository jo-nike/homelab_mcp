"""Data freshness tools for homelab MCP server."""

import asyncio
import time

import httpx
from fastmcp import Context

from lib.meta import build_meta, staleness

# Map of lifespan client keys to their service names and health check paths.
# These are probed with a real GET (the client carries any needed auth header).
_SERVICE_CHECKS = {
    "prometheus": ("Prometheus", "/api/v1/status/config"),
    "loki": ("Loki", "/ready"),
    "proxmox": ("Proxmox", "/api2/json/version"),
    "portainer": ("Portainer", "/api/status"),
    "plex": ("Plex", "/identity"),
    "sonarr": ("Sonarr", "/api/v3/system/status"),
    "radarr": ("Radarr", "/api/v3/system/status"),
    "overseerr": ("Overseerr", "/api/v1/status"),
    "pbs": ("PBS", "/api2/json/version"),
    "prowlarr": ("Prowlarr", "/api/v1/health"),
    "gitea": ("Gitea", "/api/v1/version"),
    "scanopy": ("Scanopy", "/api/health"),
    "vikunja": ("Vikunja", "/api/v1/info"),
    "healthchecks": ("Healthchecks", "/api/v3/checks/"),
    "litellm": ("LiteLLM", "/health/readiness"),
    "llama_server": ("llama-server", "/health"),
    # CrowdSec is a plain AsyncClient with an API-key header (not session-auth),
    # so it can be probed like the others via its LAPI decisions endpoint.
    "crowdsec": ("CrowdSec", "/v1/decisions"),
}

# Services with no cheap, unauthenticated (or client-authed) health path -- their
# auth is a session login, dynamic base URL, or a query-string API key. Rather
# than probe them (and misreport a working service as an error, or a dead one as
# reachable), report them as configured-but-not-probed and exclude them from the
# reachable count. They stay visible so an unreachable one is not invisible.
_UNPROBED_SERVICES = {
    "transmission": "Transmission",
    "synology": "Synology NAS",
    "technitium": "Technitium DNS",
    "wireguard": "WireGuard",
    "npm": "NPM",
    "myspeed": "MySpeed",
    "backblaze": "Backblaze B2",
    "tautulli": "Tautulli",
}


def register(mcp):
    """Register freshness tools. Always registers."""

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def show_data_freshness(ctx: Context) -> dict:
        """Check connectivity and freshness status for all configured services. Returns which data sources are reachable and which are not."""
        services = {}

        async def _check_service(key, name, path):
            client = ctx.lifespan_context.get(key)
            if client is None:
                return key, {"name": name, "status": "not_configured"}
            start = time.monotonic()
            try:
                resp = await client.get(path)
                elapsed_ms = round((time.monotonic() - start) * 1000)
                return key, {
                    "name": name,
                    "status": "ok" if resp.status_code < 400 else "error",
                    "response_time_ms": elapsed_ms,
                    "http_status": resp.status_code,
                }
            except httpx.TimeoutException:
                return key, {"name": name, "status": "timeout"}
            except Exception as e:
                return key, {"name": name, "status": "error", "message": str(e)}

        def _check_unprobed(key, name):
            client = ctx.lifespan_context.get(key)
            if client is None:
                return key, {"name": name, "status": "not_configured"}
            # Configured but no safe health path -- do not claim reachability.
            return key, {
                "name": name,
                "status": "not_probed",
                "note": "configured; no health path probed",
            }

        # Probe the checkable services in parallel; classify the rest.
        checks = [
            _check_service(key, name, path)
            for key, (name, path) in _SERVICE_CHECKS.items()
        ]
        results = await asyncio.gather(*checks)
        for key, result in results:
            services[key] = result
        for key, name in _UNPROBED_SERVICES.items():
            k, result = _check_unprobed(key, name)
            services[k] = result

        # Count statuses. Only genuinely-probed 'ok' services count as reachable;
        # not_probed services are configured but unverified.
        ok_count = sum(1 for s in services.values() if s["status"] == "ok")
        not_probed_count = sum(
            1 for s in services.values() if s["status"] == "not_probed"
        )
        error_count = sum(
            1 for s in services.values() if s["status"] in ("error", "timeout")
        )
        not_configured = sum(
            1 for s in services.values() if s["status"] == "not_configured"
        )

        # Add knowledge refresh status
        knowledge_refresh = {}
        for key in ("registries", "docs"):
            st = staleness(key)
            if st:
                knowledge_refresh[key] = {
                    "last_refreshed": st["last_refreshed"],
                    "age_seconds": int(st["age_seconds"]),
                    "stale": st["stale"],
                }
            else:
                knowledge_refresh[key] = {"last_refreshed": None, "stale": True}

        return {
            "summary": (
                f"{ok_count} services reachable, {error_count} errors, "
                f"{not_probed_count} not probed, {not_configured} not configured"
            ),
            "ok_count": ok_count,
            "error_count": error_count,
            "not_probed_count": not_probed_count,
            "not_configured_count": not_configured,
            "services": services,
            "knowledge_refresh": knowledge_refresh,
            "_meta": build_meta("freshness_check"),
        }
