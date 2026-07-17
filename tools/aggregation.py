"""Aggregation overview tools for homelab MCP server.

Cross-service tools that compose data from multiple service clients into
single overview responses. Each tool fans out to services in parallel via
asyncio.gather and returns partial results when individual services are
unreachable or not configured.

Per revised D-01: helpers access httpx clients directly from
ctx.lifespan_context rather than calling existing tool functions
(which are closure-scoped and not importable).
"""

import asyncio
import time

import httpx
from fastmcp import Context

from lib.certs import expiring_certs
from lib.crowdsec import is_community
from lib.crowdsec import local_bans as crowdsec_local_bans
from lib.gather import error_dict, safe_gather
from lib.hosts import canonical_prometheus_host, resolve_host
from lib.meta import build_meta
from lib.promql import HOST_QUERIES
from lib.redact import redact_exception

# Per-service summary helpers. Lifted out of register()'s closures to module
# level so they are importable and unit-testable (the overview payloads they
# build are deliberately condensed views, distinct from the full per-service
# tools, so they cannot be unified with those tools without changing the
# consumer-facing overview response shapes).


async def _hosts_summary(ctx: Context) -> dict:
    """Host health via Prometheus: CPU, RAM, disk per instance."""
    client = ctx.lifespan_context.get("prometheus")
    if not client:
        return {"error": "not_configured", "message": "Prometheus not configured"}

    try:
        queries = {
            k: HOST_QUERIES[k] for k in ("cpu_percent", "ram_percent", "disk_percent")
        }

        responses = await asyncio.gather(
            *[
                client.get("/api/v1/query", params={"query": q})
                for q in queries.values()
            ]
        )
        # Without this, a 4xx/5xx JSON error body parses to an empty result and
        # the overview reads 'no hosts monitored' rather than surfacing an error.
        for r in responses:
            r.raise_for_status()

        # Parse results into per-instance dict
        hosts = {}
        for metric_name, resp in zip(queries.keys(), responses, strict=False):
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                instance = item["metric"].get("instance", "unknown")
                value = round(float(item["value"][1]), 1)
                if instance not in hosts:
                    hosts[instance] = {}
                hosts[instance][metric_name] = value

        # Stamp the canonical host so the overview joins with per-host tools.
        for instance, values in hosts.items():
            values["host"] = canonical_prometheus_host(instance) or instance

        return hosts

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Prometheus did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _container_counts(ctx: Context) -> dict:
    """Container counts per Docker host via Portainer."""
    client = ctx.lifespan_context.get("portainer")
    if not client:
        return {"error": "not_configured", "message": "Portainer not configured"}

    try:
        resp = await client.get("/api/endpoints")
        resp.raise_for_status()
        endpoints = resp.json()

        # Fetch every up endpoint's container list in parallel rather than one
        # round trip at a time (a slow host no longer adds latency serially).
        up_endpoints = [ep for ep in endpoints if ep.get("Status") == 1]

        async def _fetch(ep):
            resp = await client.get(
                f"/api/endpoints/{ep['Id']}/docker/containers/json",
                params={"all": "true"},
            )
            resp.raise_for_status()
            return resp.json()

        container_lists = await asyncio.gather(*[_fetch(ep) for ep in up_endpoints])
        counts_by_id = {
            ep["Id"]: containers
            for ep, containers in zip(up_endpoints, container_lists, strict=False)
        }

        result = {}
        for ep in endpoints:
            name = ep.get("Name", f"endpoint-{ep.get('Id')}")
            host = resolve_host(name, "portainer") or name
            if ep.get("Status") != 1:
                result[name] = {
                    "host": host,
                    "status": "down",
                    "running": 0,
                    "stopped": 0,
                    "total": 0,
                }
                continue

            containers = counts_by_id.get(ep["Id"], [])
            running = sum(1 for c in containers if c.get("State") == "running")
            total = len(containers)
            result[name] = {
                "host": host,
                "status": "up",
                "running": running,
                "stopped": total - running,
                "total": total,
            }

        return result

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Portainer did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _storage_alerts(ctx: Context) -> dict:
    """Storage utilization from NAS, PBS, and Backblaze (fetched in parallel)."""
    nas, pbs, b2 = await asyncio.gather(
        _nas_storage(ctx),
        _pbs_storage(ctx),
        _backblaze_storage(ctx),
    )
    return {"nas": nas, "pbs": pbs, "backblaze": b2}


async def _nas_storage(ctx: Context) -> dict:
    """Synology NAS volume utilization."""
    client = ctx.lifespan_context.get("synology")
    if not client:
        return {"error": "not_configured", "message": "Synology not configured"}

    try:
        resp = await client.get(
            "/webapi/entry.cgi",
            params={
                "api": "SYNO.Storage.CGI.Storage",
                "version": "1",
                "method": "load_info",
            },
        )
        data = resp.json()
        if not data.get("success"):
            return {"error": "api_error", "message": "Synology storage API failed"}

        volumes = []
        for vol in data.get("data", {}).get("volumes", []):
            total = int(vol.get("size", {}).get("total", 0))
            used = int(vol.get("size", {}).get("used", 0))
            used_pct = round(used / max(total, 1) * 100, 1)
            volumes.append(
                {
                    "id": vol.get("id"),
                    "used_percent": used_pct,
                    "total_bytes": total,
                    "used_bytes": used,
                }
            )
        return {"volumes": volumes}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Synology did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _pbs_storage(ctx: Context) -> dict:
    """PBS datastore utilization."""
    client = ctx.lifespan_context.get("pbs")
    if not client:
        return {"error": "not_configured", "message": "PBS not configured"}

    try:
        resp = await client.get("/api2/json/status/datastore-usage")
        data = resp.json()
        ds_list = data.get("data", data)
        if not isinstance(ds_list, list):
            ds_list = []

        datastores = []
        for ds in ds_list:
            total = ds.get("total", 0)
            used = ds.get("used", 0)
            used_pct = round(used / max(total, 1) * 100, 1)
            datastores.append(
                {
                    "store": ds.get("store", "unknown"),
                    "used_percent": used_pct,
                    "total_bytes": total,
                    "used_bytes": used,
                }
            )
        return {"datastores": datastores}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "PBS did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _backblaze_storage(ctx: Context) -> dict:
    """Backblaze B2 bucket count."""
    client = ctx.lifespan_context.get("backblaze")
    if not client:
        return {"error": "not_configured", "message": "Backblaze not configured"}

    try:
        await client.ensure_auth()
        # Public accessor for strategy-held state, not the private _strategy.
        account_id = client.strategy.account_id

        resp = await client.post(
            "/b2api/v3/b2_list_buckets",
            json={
                "accountId": account_id,
                "bucketTypes": ["allPublic", "allPrivate"],
            },
        )
        data = resp.json()
        buckets = data.get("buckets", [])
        return {"bucket_count": len(buckets)}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Backblaze did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _active_streams(ctx: Context) -> dict:
    """Active Plex media streams."""
    client = ctx.lifespan_context.get("plex")
    if not client:
        return {"error": "not_configured", "message": "Plex not configured"}

    try:
        resp = await client.get("/status/sessions")
        resp.raise_for_status()
        data = resp.json()
        mc = data.get("MediaContainer", {})
        metadata = mc.get("Metadata", [])

        streams = []
        for item in metadata:
            title = item.get("grandparentTitle") or item.get("title", "Unknown")
            user = item.get("User", {}).get("title", "Unknown")
            view_offset = item.get("viewOffset", 0)
            duration = item.get("duration", 1)
            progress = round(view_offset / max(duration, 1) * 100, 1)
            streams.append(
                {
                    "title": title,
                    "user": user,
                    "progress_percent": progress,
                }
            )

        return {"active_streams": len(streams), "streams": streams}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Plex did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _recent_errors(ctx: Context) -> dict:
    """Recent errors from Loki logs."""
    client = ctx.lifespan_context.get("loki")
    if not client:
        return {"error": "not_configured", "message": "Loki not configured"}

    try:
        now_ns = int(time.time() * 1_000_000_000)
        hour_ago_ns = now_ns - 3_600_000_000_000

        resp = await client.get(
            "/loki/api/v1/query_range",
            params={
                "query": '{job=~".+"} |~ "(?i)(error|fatal|panic)"',
                "start": str(hour_ago_ns),
                "end": str(now_ns),
                "limit": "5",
            },
        )
        data = resp.json()
        streams = data.get("data", {}).get("result", [])

        errors = []
        for stream in streams:
            job = stream.get("stream", {}).get("job", "unknown")
            for ts, msg in stream.get("values", []):
                errors.append(
                    {
                        "timestamp": ts,
                        "message": msg,
                        "job": job,
                    }
                )

        # Sort by timestamp desc, take last 5
        errors.sort(key=lambda e: e["timestamp"], reverse=True)
        errors = errors[:5]

        return {"error_count": len(errors), "recent": errors}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Loki did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _sonarr_summary(ctx: Context) -> dict:
    """Sonarr upcoming episodes and queue counts."""
    client = ctx.lifespan_context.get("sonarr")
    if not client:
        return {"error": "not_configured", "message": "Sonarr not configured"}

    try:
        now = time.strftime("%Y-%m-%d", time.gmtime())
        week_later = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 7 * 86400))
        cal_resp, queue_resp = await asyncio.gather(
            client.get("/api/v3/calendar", params={"start": now, "end": week_later}),
            client.get("/api/v3/queue"),
        )

        calendar = cal_resp.json()
        upcoming = [ep for ep in calendar if not ep.get("hasFile")]
        queue_data = queue_resp.json()
        queue_count = queue_data.get("totalRecords", 0)

        return {"upcoming_count": len(upcoming), "queue_count": queue_count}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Sonarr did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _radarr_summary(ctx: Context) -> dict:
    """Radarr upcoming movies and queue counts."""
    client = ctx.lifespan_context.get("radarr")
    if not client:
        return {"error": "not_configured", "message": "Radarr not configured"}

    try:
        now = time.strftime("%Y-%m-%d", time.gmtime())
        future = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 90 * 86400))
        cal_resp, queue_resp = await asyncio.gather(
            client.get("/api/v3/calendar", params={"start": now, "end": future}),
            client.get("/api/v3/queue"),
        )

        calendar = cal_resp.json()
        upcoming = [m for m in calendar if not m.get("hasFile")]
        queue_data = queue_resp.json()
        queue_count = queue_data.get("totalRecords", 0)

        return {"upcoming_count": len(upcoming), "queue_count": queue_count}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Radarr did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _transmission_summary(ctx: Context) -> dict:
    """Transmission torrent counts and aggregate speeds."""
    client = ctx.lifespan_context.get("transmission")
    if not client:
        return {"error": "not_configured", "message": "Transmission not configured"}

    try:
        resp = await client.post(
            "/transmission/rpc",
            json={
                "method": "torrent-get",
                "arguments": {"fields": ["status", "rateDownload", "rateUpload"]},
            },
        )
        data = resp.json()
        if data.get("result") != "success":
            return {"error": "rpc_error", "message": data.get("result", "unknown")}

        torrents = data.get("arguments", {}).get("torrents", [])
        downloading = sum(1 for t in torrents if t.get("status") == 4)
        total_dl_speed = sum(t.get("rateDownload", 0) for t in torrents)

        return {
            "torrent_count": len(torrents),
            "downloading_count": downloading,
            "total_download_speed_kbps": round(total_dl_speed / 1000, 1),
        }

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Transmission did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _overseerr_summary(ctx: Context) -> dict:
    """Overseerr request counts."""
    client = ctx.lifespan_context.get("overseerr")
    if not client:
        return {"error": "not_configured", "message": "Overseerr not configured"}

    try:
        # total_count from an unfiltered page; pending_count from a
        # filter=pending page's pageInfo so it is not undercounted by the
        # 20-item window (the old code only counted pending within page 1).
        total_resp, pending_resp = await asyncio.gather(
            client.get(
                "/api/v1/request",
                params={"take": "1", "skip": "0", "sort": "added"},
            ),
            client.get(
                "/api/v1/request",
                params={"take": "1", "skip": "0", "filter": "pending"},
            ),
        )
        total = total_resp.json().get("pageInfo", {}).get("results", 0)
        pending = pending_resp.json().get("pageInfo", {}).get("results", 0)

        return {"pending_count": pending, "total_count": total}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Overseerr did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _prowlarr_summary(ctx: Context) -> dict:
    """Prowlarr indexer counts and health warnings."""
    client = ctx.lifespan_context.get("prowlarr")
    if not client:
        return {"error": "not_configured", "message": "Prowlarr not configured"}
    try:
        indexer_resp, health_resp = await asyncio.gather(
            client.get("/api/v1/indexer"),
            client.get("/api/v1/health"),
        )
        indexer_resp.raise_for_status()
        health_resp.raise_for_status()
        indexers = indexer_resp.json()
        health = health_resp.json()
        enabled = sum(1 for i in indexers if i.get("enable"))
        warnings = [
            {"source": h.get("source", ""), "message": h.get("message", "")}
            for h in health
        ]
        return {
            "indexer_count": len(indexers),
            "enabled_count": enabled,
            "disabled_count": len(indexers) - enabled,
            "health_warnings": warnings,
            "health_warning_count": len(warnings),
        }
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Prowlarr did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _crowdsec_summary(ctx: Context) -> dict:
    """CrowdSec local ban count and community blocklist size."""
    client = ctx.lifespan_context.get("crowdsec")
    if not client:
        return {"error": "not_configured", "message": "CrowdSec not configured"}
    try:
        resp = await client.get("/v1/decisions")
        resp.raise_for_status()
        decisions = resp.json() or []
        capi_count = sum(1 for d in decisions if is_community(d))
        return {
            "local_ban_count": len(crowdsec_local_bans(decisions)),
            "community_blocklist_count": capi_count,
        }
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "CrowdSec did not respond in time"}
    except Exception as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _npm_summary(ctx: Context) -> dict:
    """NPM proxy host count, dead host count, and expiring certs."""
    client = ctx.lifespan_context.get("npm")
    if not client:
        return {"error": "not_configured", "message": "NPM not configured"}
    try:
        proxy_resp, dead_resp, cert_resp = await asyncio.gather(
            client.get("/api/nginx/proxy-hosts"),
            client.get("/api/nginx/dead-hosts"),
            client.get("/api/nginx/certificates"),
        )
        proxy_hosts = proxy_resp.json() if proxy_resp.status_code == 200 else []
        dead_hosts = dead_resp.json() if dead_resp.status_code == 200 else []
        certs = cert_resp.json() if cert_resp.status_code == 200 else []

        # Flag certs expiring within 30 days
        expiring = [
            {"domain": cert["domain"], "expires_on": cert["expires_on"]}
            for cert in expiring_certs(certs)
        ]

        result = {
            "proxy_host_count": len(proxy_hosts),
            "dead_host_count": len(dead_hosts),
            "cert_count": len(certs),
        }
        if expiring:
            result["expiring_certs"] = expiring
        return result
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "NPM did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _myspeed_summary(ctx: Context) -> dict:
    """MySpeed latest download/upload/ping."""
    client = ctx.lifespan_context.get("myspeed")
    if not client:
        return {"error": "not_configured", "message": "MySpeed not configured"}
    try:
        resp = await client.get("/api/speedtests", params={"limit": "1"})
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        data = resp.json()
        tests = data if isinstance(data, list) else [data] if data else []
        if not tests:
            return {"latest": None}
        latest = tests[0]
        return {
            "download_mbps": round(latest.get("download", 0), 1),
            "upload_mbps": round(latest.get("upload", 0), 1),
            "ping_ms": latest.get("ping", 0),
        }
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "MySpeed did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


async def _proxmox_summary(ctx: Context) -> dict:
    """Proxmox node status."""
    client = ctx.lifespan_context.get("proxmox")
    if not client:
        return {"error": "not_configured", "message": "Proxmox not configured"}

    try:
        resp = await client.get("/api2/json/nodes")
        resp.raise_for_status()
        data = resp.json()
        nodes_list = data.get("data", [])

        nodes = {}
        for node in nodes_list:
            name = node.get("node", "unknown")
            mem = node.get("mem", 0)
            maxmem = node.get("maxmem", 1)
            nodes[name] = {
                "status": node.get("status", "unknown"),
                "cpu_percent": round(node.get("cpu", 0) * 100, 1),
                "ram_percent": round(mem / max(maxmem, 1) * 100, 1),
            }

        return {"nodes": nodes}

    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Proxmox did not respond in time"}
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}


def register(mcp):
    """Register aggregation tools. Always registers (no config guard)."""

    # --- Aggregate tools ---

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_homelab_overview(ctx: Context) -> dict:
        """Complete homelab overview: host health, container counts, storage utilization, active media streams, recent errors, and speed test results. Returns partial results if individual services are unreachable."""
        hosts, containers, storage, media, errors, myspeed = await safe_gather(
            _hosts_summary(ctx),
            _container_counts(ctx),
            _storage_alerts(ctx),
            _active_streams(ctx),
            _recent_errors(ctx),
            _myspeed_summary(ctx),
            on_error=error_dict,
        )
        return {
            "hosts": hosts,
            "containers": containers,
            "storage": storage,
            "media": media,
            "errors": errors,
            "speed_test": myspeed,
            "_meta": build_meta("aggregation"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_media_overview(ctx: Context) -> dict:
        """Media overview: Plex active streams, Sonarr/Radarr upcoming and queues, Transmission downloads, Overseerr requests, and Prowlarr indexer health."""
        plex, sonarr, radarr, transmission, overseerr, prowlarr = await safe_gather(
            _active_streams(ctx),
            _sonarr_summary(ctx),
            _radarr_summary(ctx),
            _transmission_summary(ctx),
            _overseerr_summary(ctx),
            _prowlarr_summary(ctx),
            on_error=error_dict,
        )
        return {
            "plex": plex,
            "sonarr": sonarr,
            "radarr": radarr,
            "transmission": transmission,
            "overseerr": overseerr,
            "prowlarr": prowlarr,
            "_meta": build_meta("aggregation"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_infra_overview(ctx: Context) -> dict:
        """Infrastructure overview: Proxmox node status, Docker container counts per host, storage utilization (NAS, PBS, Backblaze), CrowdSec security status, and NPM proxy status."""
        proxmox, docker, storage, crowdsec, npm = await safe_gather(
            _proxmox_summary(ctx),
            _container_counts(ctx),
            _storage_alerts(ctx),
            _crowdsec_summary(ctx),
            _npm_summary(ctx),
            on_error=error_dict,
        )
        return {
            "proxmox": proxmox,
            "docker": docker,
            "storage": storage,
            "crowdsec": crowdsec,
            "npm": npm,
            "_meta": build_meta("aggregation"),
        }
