"""Change feed tool for homelab MCP server.

Aggregates recent events from all monitored sources into a unified
timeline: Prometheus alerts/flaps, Healthchecks status flips, CrowdSec
bans, NPM expiring certificates, and PBS backup failures.
"""

import asyncio
import datetime
import time
from typing import Annotated

from fastmcp import Context

from lib.certs import expiring_certs
from lib.crowdsec import is_community
from lib.gather import safe_gather
from lib.healthchecks import check_uuid, unwrap_checks, unwrap_flips
from lib.hosts import canonical_prometheus_host
from lib.meta import build_meta


def _parse_iso(ts) -> datetime.datetime | None:
    """Parse a mixed-shape timestamp to an aware UTC datetime, or None.

    Handles 'Z' suffixes and naive strings (assumed UTC) so heterogeneous event
    timestamps (isoformat +00:00, upstream 'Z', sub-second) can be compared.
    """
    if not ts:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def register(mcp):
    """Register change feed tools. Always registers (no config guard -- graceful degradation)."""

    # --- Source fetchers ---

    async def _prometheus_events(ctx: Context, hours: int) -> tuple[list[dict], bool]:
        """Prometheus: firing alerts, target flaps, targets currently down.

        Returns (events, failed). `failed` is True when any sub-query raised,
        so the caller can report the source as unreachable instead of a quiet
        day.
        """
        client = ctx.lifespan_context.get("prometheus")
        if not client:
            return [], False

        events = []
        failed = False
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()

        try:
            # 1. Currently firing alerts
            resp = await client.get(
                "/api/v1/query", params={"query": 'ALERTS{alertstate="firing"}'}
            )
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                metric = item.get("metric", {})
                events.append(
                    {
                        "timestamp": now_iso,
                        "source": "prometheus",
                        "type": "alert_firing",
                        "entity": metric.get("alertname", "unknown"),
                        "detail": f"Alert firing: {metric.get('alertname', '?')} - {metric.get('severity', 'unknown')} severity",
                        "severity": "critical"
                        if metric.get("severity") == "critical"
                        else "warning",
                    }
                )
        except Exception:
            failed = True

        try:
            # 2. Targets that flapped in the time window
            resp = await client.get(
                "/api/v1/query", params={"query": f"changes(up[{hours}h]) > 0"}
            )
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                metric = item.get("metric", {})
                instance = metric.get("instance", "unknown")
                flap_count = 0
                try:
                    flap_count = int(float(item["value"][1]))
                except (IndexError, ValueError, TypeError):
                    pass
                events.append(
                    {
                        "timestamp": now_iso,
                        "source": "prometheus",
                        "type": "target_flap",
                        "entity": canonical_prometheus_host(instance) or instance,
                        "detail": f"Target {instance} flapped {flap_count} time(s) in last {hours}h",
                        "severity": "warning",
                    }
                )
        except Exception:
            failed = True

        try:
            # 3. Targets currently down
            resp = await client.get("/api/v1/query", params={"query": "up == 0"})
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                metric = item.get("metric", {})
                instance = metric.get("instance", "unknown")
                job = metric.get("job", "unknown")
                events.append(
                    {
                        "timestamp": now_iso,
                        "source": "prometheus",
                        "type": "target_down",
                        "entity": instance,
                        "detail": f"Target {instance} (job: {job}) is currently down",
                        "severity": "critical",
                    }
                )
        except Exception:
            failed = True

        return events, failed

    async def _healthchecks_events(ctx: Context, hours: int) -> tuple[list[dict], bool]:
        """Healthchecks: checks that are down or in grace period, with recent flips."""
        client = ctx.lifespan_context.get("healthchecks")
        if not client:
            return [], False

        events: list[dict] = []
        failed = False
        try:
            resp = await client.get("/api/v3/checks/")
            resp.raise_for_status()
            checks = unwrap_checks(resp.json())

            problem_checks = []
            for check in checks:
                status = check.get("status", "up")
                if status in ("down", "grace"):
                    name = check.get("name", "unknown")
                    events.append(
                        {
                            "timestamp": check.get("last_ping")
                            or datetime.datetime.now(datetime.UTC).isoformat(),
                            "source": "healthchecks",
                            "type": "check_down" if status == "down" else "check_grace",
                            "entity": name,
                            "detail": f"Check '{name}' is {status}",
                            "severity": "warning" if status == "grace" else "critical",
                        }
                    )

                    uuid = check_uuid(check)
                    if uuid:
                        problem_checks.append((name, uuid))

            # Fetch flips for problem checks
            if problem_checks:
                seconds = hours * 3600

                async def fetch_flips(name, uuid):
                    try:
                        fr = await client.get(
                            f"/api/v3/checks/{uuid}/flips/", params={"seconds": seconds}
                        )
                        fr.raise_for_status()
                        raw_flips = unwrap_flips(fr.json())
                        flip_events = []
                        for f in raw_flips[:10]:
                            flip_events.append(
                                {
                                    "timestamp": f.get("timestamp", ""),
                                    "source": "healthchecks",
                                    "type": "check_flip",
                                    "entity": name,
                                    "detail": f"Check '{name}' flipped to {'up' if f.get('up') else 'down'}",
                                    "severity": "info",
                                }
                            )
                        return flip_events
                    except Exception:
                        return []

                flip_results = await asyncio.gather(
                    *[fetch_flips(name, uuid) for name, uuid in problem_checks],
                    return_exceptions=True,
                )
                for result in flip_results:
                    if isinstance(result, list):
                        events.extend(result)

        except Exception:
            failed = True

        return events, failed

    async def _crowdsec_events(ctx: Context, hours: int) -> tuple[list[dict], bool]:
        """CrowdSec: recent local bans (non-CAPI)."""
        client = ctx.lifespan_context.get("crowdsec")
        if not client:
            return [], False

        events = []
        failed = False
        try:
            resp = await client.get("/v1/decisions")
            resp.raise_for_status()
            decisions = resp.json() or []

            now = datetime.datetime.now(datetime.UTC)
            cutoff = now - datetime.timedelta(hours=hours)
            for d in decisions:
                if is_community(d):
                    continue  # Skip community blocklist entries
                created = d.get("created_at")
                ts = _parse_iso(created)
                # A ban older than the window is not a change in it; skip it.
                # Keep bans whose timestamp is unparsable, flagged distinctly.
                if ts is not None and ts < cutoff:
                    continue
                events.append(
                    {
                        "timestamp": created or now.isoformat(),
                        "source": "crowdsec",
                        "type": "ban" if ts is not None else "ban_active",
                        "entity": d.get("value", "unknown"),
                        "detail": f"Banned {d.get('scope', 'ip')}:{d.get('value', '?')} - {d.get('scenario', 'unknown')}",
                        "severity": "warning",
                    }
                )
        except Exception:
            failed = True

        return events, failed

    async def _npm_cert_events(ctx: Context, hours: int) -> tuple[list[dict], bool]:
        """NPM: SSL certificates expiring within 30 days."""
        client = ctx.lifespan_context.get("npm")
        if not client:
            return [], False

        events = []
        failed = False
        try:
            resp = await client.get("/api/nginx/certificates")
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            certs = resp.json()
            now = datetime.datetime.now(datetime.UTC)
            for cert in expiring_certs(certs, now=now):
                domain = cert["domain"]
                days_left = cert["days_left"]
                events.append(
                    {
                        "timestamp": now.isoformat(),
                        "source": "npm",
                        "type": "cert_expiring",
                        "entity": domain,
                        "detail": f"SSL cert for {domain} expires in {days_left} days ({cert['expires_on']})",
                        "severity": cert["severity"],
                    }
                )
        except Exception:
            failed = True

        return events, failed

    async def _pbs_events(ctx: Context, hours: int) -> tuple[list[dict], bool]:
        """PBS: failed or stale backups."""
        client = ctx.lifespan_context.get("pbs")
        if not client:
            return [], False

        events = []
        failed = False
        try:
            resp = await client.get("/api2/json/status/datastore-usage")
            resp.raise_for_status()
            data = resp.json()
            ds_list = data.get("data", data)
            if not isinstance(ds_list, list):
                return [], False

            now = time.time()
            stale_threshold = now - (hours * 3600)

            # Fetch every datastore's backup groups in parallel.
            async def _fetch_groups(store_name: str):
                try:
                    groups_resp = await client.get(
                        f"/api2/json/admin/datastore/{store_name}/groups"
                    )
                    groups_resp.raise_for_status()
                    groups_data = groups_resp.json()
                    groups = groups_data.get("data", groups_data)
                    return store_name, groups if isinstance(groups, list) else None
                except Exception:
                    return store_name, None

            store_names = [ds.get("store", "unknown") for ds in ds_list]
            group_results = await asyncio.gather(
                *[_fetch_groups(s) for s in store_names]
            )

            for store_name, groups in group_results:
                if groups is None:
                    failed = True
                    continue
                for group in groups:
                    last_backup = group.get("last-backup")
                    if last_backup and last_backup < stale_threshold:
                        backup_id = group.get("backup-id", "unknown")
                        events.append(
                            {
                                "timestamp": datetime.datetime.fromtimestamp(
                                    last_backup, tz=datetime.UTC
                                ).isoformat(),
                                "source": "pbs",
                                # The check measures backup age (last run older
                                # than the window), i.e. staleness -- not a failed
                                # job. 'backup_failed' contradicted the detail text.
                                "type": "backup_stale",
                                "entity": f"{store_name}/{backup_id}",
                                "detail": f"Backup '{backup_id}' in {store_name} last ran {int((now - last_backup) / 3600)}h ago",
                                "severity": "warning",
                            }
                        )
        except Exception:
            failed = True

        return events, failed

    # --- Main tool ---

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def what_changed_last_24h(
        ctx: Context,
        hours: Annotated[int, "Time window in hours to look back. Default 24."] = 24,
    ) -> dict:
        """Get a unified timeline of recent events across the homelab: alerts, target flaps, failed checks, security bans, expiring certs, and backup issues."""
        # A non-positive window makes changes(up[{hours}h]) invalid PromQL and
        # marks every PBS backup stale, so reject it up front.
        if hours <= 0:
            return {
                "error": "invalid_parameter",
                "message": "hours must be a positive integer",
            }

        source_names = ["prometheus", "healthchecks", "crowdsec", "npm", "pbs"]
        # A source coroutine that raises unexpectedly (rather than returning its
        # (events, failed) tuple) is treated as a failed source, not a quiet one.
        gathered = await safe_gather(
            _prometheus_events(ctx, hours),
            _healthchecks_events(ctx, hours),
            _crowdsec_events(ctx, hours),
            _npm_cert_events(ctx, hours),
            _pbs_events(ctx, hours),
            on_error=lambda exc: ([], True),
        )

        # Merge all events and record which sources could not be checked, so a
        # genuinely quiet day ('0 events') is distinguishable from 'monitoring
        # is down' (0 events + sources_failed + degraded confidence).
        all_events: list[dict] = []
        sources_failed: list[str] = []
        for name, (events, failed) in zip(source_names, gathered, strict=False):
            all_events.extend(events)
            if failed:
                sources_failed.append(name)

        # Sort by parsed timestamp descending. Event timestamps arrive in mixed
        # shapes (isoformat +00:00, upstream 'Z', sub-second, or empty), and a
        # plain string sort misorders differing suffixes and sinks empties to
        # the end regardless of real time. Parse to aware UTC with a sentinel.
        _epoch = datetime.datetime.min.replace(tzinfo=datetime.UTC)
        all_events.sort(
            key=lambda e: _parse_iso(e.get("timestamp")) or _epoch, reverse=True
        )

        # Build breakdowns
        by_source = {}
        by_severity = {}
        by_type = {}
        for event in all_events:
            src = event.get("source", "unknown")
            sev = event.get("severity", "info")
            typ = event.get("type", "unknown")
            by_source[src] = by_source.get(src, 0) + 1
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_type[typ] = by_type.get(typ, 0) + 1

        # Build summary string
        parts = []
        for sev in ("critical", "warning", "info"):
            count = by_severity.get(sev, 0)
            if count:
                parts.append(f"{count} {sev}")
        severity_summary = ", ".join(parts) if parts else "no events"

        source_parts = []
        for src, count in sorted(by_source.items()):
            source_parts.append(f"{count} from {src}")
        source_summary = ", ".join(source_parts)

        summary = f"{len(all_events)} events in last {hours}h: {severity_summary}"
        if source_summary:
            summary += f" ({source_summary})"
        if sources_failed:
            summary += f" [could not check: {', '.join(sources_failed)}]"

        return {
            "summary": summary,
            "hours": hours,
            "event_count": len(all_events),
            "events": all_events,
            "by_source": by_source,
            "by_severity": by_severity,
            "by_type": by_type,
            "sources_failed": sources_failed,
            "_meta": build_meta(
                "changefeed",
                data_window=f"{hours}h",
                confidence="medium" if sources_failed else "high",
            ),
        }
