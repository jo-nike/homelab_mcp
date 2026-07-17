"""CrowdSec tools for homelab MCP server."""

import httpx
from fastmcp import Context

import config
from lib.meta import build_meta


def _process_decision(d: dict) -> dict:
    return {
        "type": d.get("type", ""),
        "scope": d.get("scope", ""),
        "value": d.get("value", ""),
        "duration": d.get("duration", ""),
        "scenario": d.get("scenario", ""),
        "origin": d.get("origin", ""),
    }


def register(mcp):
    """Register CrowdSec tools. Skips if URL and API key are not configured."""
    if not (config.CROWDSEC_URL and config.CROWDSEC_API_KEY):
        return

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_crowdsec_overview(ctx: Context) -> dict:
        """Get CrowdSec security overview: local detections and community blocklist stats.

        Local decisions are threats detected on YOUR network (origin: crowdsec, cscli).
        Community (CAPI) decisions are the shared blocklist from the CrowdSec network.
        """
        client = ctx.lifespan_context["crowdsec"]
        try:
            resp = await client.get("/v1/decisions")
            resp.raise_for_status()
            raw_decisions = resp.json() or []
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "CrowdSec did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": str(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": str(e)}
        except ValueError:
            return {
                "error": "invalid_response",
                "message": "CrowdSec returned a non-JSON response",
            }

        # Classify by origin explicitly. Local detections are crowdsec/cscli;
        # CAPI and subscribed blocklists ('lists') are community (a popular
        # blocklist would otherwise dump thousands of third-party entries into
        # local_decisions); anything else goes to an 'other' bucket.
        local = []
        other = []
        capi_count = 0
        capi_scenarios = {}
        for d in raw_decisions:
            origin = d.get("origin", "")
            if origin in ("crowdsec", "cscli"):
                local.append(_process_decision(d))
            elif origin in ("CAPI", "lists"):
                capi_count += 1
                s = d.get("scenario", "unknown")
                capi_scenarios[s] = capi_scenarios.get(s, 0) + 1
            else:
                other.append(_process_decision(d))

        # Top community scenarios
        top_capi = sorted(capi_scenarios.items(), key=lambda x: -x[1])[:5]

        # Local scenario breakdown
        local_scenarios = {}
        for d in local:
            s = d["scenario"]
            local_scenarios[s] = local_scenarios.get(s, 0) + 1
        top_local = sorted(local_scenarios.items(), key=lambda x: -x[1])[:5]

        local_bans = sum(1 for d in local if d["type"] == "ban")

        summary = (
            f"{local_bans} local ban{'s' if local_bans != 1 else ''} "
            f"(your network), {capi_count} community blocklist entries"
        )

        return {
            "summary": summary,
            "local_decisions": local,
            "local_ban_count": local_bans,
            "local_top_scenarios": [{"scenario": s, "count": c} for s, c in top_local],
            "community_blocklist_count": capi_count,
            "community_top_scenarios": [
                {"scenario": s, "count": c} for s, c in top_capi
            ],
            "other_decisions": other,
            "other_count": len(other),
            "total_decisions": len(raw_decisions),
            "_meta": build_meta("crowdsec"),
        }
