"""Health assessment tools for homelab MCP server.

Server-side reasoning tools that fan out to multiple sources, apply
thresholds, and return prioritized verdicts. Designed so even small
local models can provide useful health answers without composing queries.
"""

import asyncio
from typing import Annotated

from fastmcp import Context

import config
from lib.certs import expiring_certs
from lib.crowdsec import local_bans as crowdsec_local_bans
from lib.gather import safe_gather
from lib.healthchecks import unwrap_checks
from lib.hosts import canonical_prometheus_host, prometheus_instances
from lib.meta import build_meta
from lib.promql import HOST_QUERIES, inject_matcher


def _instance_matcher(host_name: str) -> str | None:
    """Build a PromQL `instance=~"..."` matcher for a host's scrape instances.

    Prometheus `instance` labels are canonical hostnames (e.g. 'docker-host',
    'ai-vm-gpu'), never IPs, so filtering by IP matches nothing. Resolves the
    host to its prometheus alias(es) via lib.hosts and matches them with an
    optional ':port' suffix. Returns None when the host has no known instance.
    """
    instances = prometheus_instances(host_name)
    if not instances:
        return None
    alternation = "|".join(instances)
    return f'instance=~"({alternation})(:.*)?"'


# Threshold definitions for resource metrics
THRESHOLDS = {
    "cpu_percent": {"warning": 80, "critical": 95},
    "ram_percent": {"warning": 85, "critical": 95},
    "disk_percent": {"warning": 80, "critical": 90},
    "gpu_vram_percent": {"warning": 85, "critical": 95},
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def register(mcp):
    """Register health assessment tools. Always registers (no config guard)."""

    def _check_threshold(metric_name: str, value: float, entity: str) -> dict | None:
        """Check a metric value against thresholds, return verdict or None."""
        thresholds = THRESHOLDS.get(metric_name)
        if not thresholds:
            return None

        if value >= thresholds["critical"]:
            severity = "critical"
        elif value >= thresholds["warning"]:
            severity = "warning"
        else:
            return None

        label = metric_name.replace("_percent", "").replace("_", " ").upper()
        return {
            "entity": entity,
            "severity": severity,
            "category": "resource",
            "message": f"{label} at {round(value, 1)}%",
            "source": "prometheus",
        }

    # --- Verdict fetchers ---

    async def _resource_verdicts(ctx: Context) -> tuple[list[dict], bool]:
        """Check host CPU, RAM, disk, GPU against thresholds.

        Returns (verdicts, failed); `failed` is True when a query raised so the
        caller can report 'could not check' rather than a false 'all clear'.
        """
        client = ctx.lifespan_context.get("prometheus")
        if not client:
            return [], False

        verdicts = []
        failed = False
        queries = {
            k: HOST_QUERIES[k] for k in ("cpu_percent", "ram_percent", "disk_percent")
        }

        try:
            responses = await asyncio.gather(
                *[
                    client.get("/api/v1/query", params={"query": q})
                    for q in queries.values()
                ]
            )

            for metric_name, resp in zip(queries.keys(), responses, strict=False):
                data = resp.json().get("data", {}).get("result", [])
                for item in data:
                    instance = item.get("metric", {}).get("instance", "unknown")
                    entity = canonical_prometheus_host(instance) or instance
                    try:
                        value = float(item["value"][1])
                    except (IndexError, ValueError, TypeError):
                        continue
                    verdict = _check_threshold(metric_name, value, entity)
                    if verdict:
                        verdicts.append(verdict)
        except Exception:
            failed = True

        # GPU VRAM check
        try:
            resp = await client.get(
                "/api/v1/query",
                params={
                    "query": "nvidia_smi_memory_used_bytes / nvidia_smi_memory_total_bytes * 100"
                },
            )
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                instance = item.get("metric", {}).get("instance", "unknown")
                entity = canonical_prometheus_host(instance) or instance
                try:
                    value = float(item["value"][1])
                except (IndexError, ValueError, TypeError):
                    continue
                verdict = _check_threshold("gpu_vram_percent", value, entity)
                if verdict:
                    verdicts.append(verdict)
        except Exception:
            failed = True

        return verdicts, failed

    async def _target_down_verdicts(ctx: Context) -> tuple[list[dict], bool]:
        """Check for Prometheus targets that are down."""
        client = ctx.lifespan_context.get("prometheus")
        if not client:
            return [], False

        verdicts = []
        failed = False
        try:
            resp = await client.get("/api/v1/query", params={"query": "up == 0"})
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                instance = item.get("metric", {}).get("instance", "unknown")
                job = item.get("metric", {}).get("job", "unknown")
                entity = canonical_prometheus_host(instance) or instance
                verdicts.append(
                    {
                        "entity": entity,
                        "severity": "critical",
                        "category": "availability",
                        "message": f"Target {instance} (job: {job}) is down",
                        "source": "prometheus",
                    }
                )
        except Exception:
            failed = True

        return verdicts, failed

    async def _healthchecks_verdicts(ctx: Context) -> tuple[list[dict], bool]:
        """Check for Healthchecks monitors that are down or in grace."""
        client = ctx.lifespan_context.get("healthchecks")
        if not client:
            return [], False

        verdicts = []
        failed = False
        try:
            resp = await client.get("/api/v3/checks/")
            resp.raise_for_status()
            checks = unwrap_checks(resp.json())

            for check in checks:
                status = check.get("status", "up")
                if status == "down":
                    verdicts.append(
                        {
                            "entity": check.get("name", "unknown"),
                            "severity": "critical",
                            "category": "cron",
                            "message": f"Cron check '{check.get('name', '?')}' is down",
                            "source": "healthchecks",
                        }
                    )
                elif status == "grace":
                    verdicts.append(
                        {
                            "entity": check.get("name", "unknown"),
                            "severity": "warning",
                            "category": "cron",
                            "message": f"Cron check '{check.get('name', '?')}' is in grace period",
                            "source": "healthchecks",
                        }
                    )
        except Exception:
            failed = True

        return verdicts, failed

    async def _crowdsec_verdicts(ctx: Context) -> tuple[list[dict], bool]:
        """Check for recent local CrowdSec bans."""
        client = ctx.lifespan_context.get("crowdsec")
        if not client:
            return [], False

        verdicts = []
        failed = False
        try:
            resp = await client.get("/v1/decisions")
            resp.raise_for_status()
            decisions = resp.json() or []
            local_bans = crowdsec_local_bans(decisions)
            if local_bans:
                verdicts.append(
                    {
                        "entity": "crowdsec",
                        "severity": "warning",
                        "category": "security",
                        "message": f"{len(local_bans)} active local ban(s) -- check CrowdSec for details",
                        "source": "crowdsec",
                    }
                )
        except Exception:
            failed = True

        return verdicts, failed

    async def _cert_verdicts(ctx: Context) -> tuple[list[dict], bool]:
        """Check for expiring SSL certificates via NPM."""
        client = ctx.lifespan_context.get("npm")
        if not client:
            return [], False

        verdicts = []
        failed = False
        try:
            resp = await client.get("/api/nginx/certificates")
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            certs = resp.json()
            for cert in expiring_certs(certs):
                verdicts.append(
                    {
                        "entity": cert["domain"],
                        "severity": cert["severity"],
                        "category": "certificate",
                        "message": f"SSL cert for {cert['domain']} expires in {cert['days_left']} days",
                        "source": "npm",
                    }
                )
        except Exception:
            failed = True

        return verdicts, failed

    # --- Tools ---

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def what_needs_attention(ctx: Context) -> dict:
        """Get a prioritized list of things that need attention right now. Checks host resources, Prometheus target health, cron (Healthchecks) status, security events (CrowdSec), and SSL certificate expiry. Returns verdicts sorted by severity. (Does not check backups -- use get_pbs_status.)"""
        source_names = [
            "prometheus",
            "prometheus_targets",
            "healthchecks",
            "crowdsec",
            "npm",
        ]
        # An unexpectedly raised fetcher is treated as a failed source so a down
        # backend can never masquerade as 'all clear'.
        gathered = await safe_gather(
            _resource_verdicts(ctx),
            _target_down_verdicts(ctx),
            _healthchecks_verdicts(ctx),
            _crowdsec_verdicts(ctx),
            _cert_verdicts(ctx),
            on_error=lambda exc: ([], True),
        )

        all_verdicts: list[dict] = []
        sources_failed: list[str] = []
        for name, (verdicts, failed) in zip(source_names, gathered, strict=False):
            all_verdicts.extend(verdicts)
            if failed:
                sources_failed.append(name)

        # Sort by severity: critical first, then warning, then info
        all_verdicts.sort(
            key=lambda v: SEVERITY_ORDER.get(v.get("severity", "info"), 99)
        )

        # Build summary
        by_severity = {}
        by_category = {}
        entities = set()
        for v in all_verdicts:
            sev = v.get("severity", "info")
            cat = v.get("category", "unknown")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_category[cat] = by_category.get(cat, 0) + 1
            entities.add(v.get("entity", ""))

        parts = []
        for sev in ("critical", "warning", "info"):
            count = by_severity.get(sev, 0)
            if count:
                parts.append(f"{count} {sev}")
        severity_summary = ", ".join(parts) if parts else "all clear"

        if all_verdicts:
            summary = f"{severity_summary} across {len(entities)} entities"
        elif sources_failed:
            # No verdicts but some backends were unreachable: this is NOT 'all
            # clear', it is 'could not fully check'.
            summary = (
                f"No issues found, but could not check: {', '.join(sources_failed)}"
            )
        else:
            summary = "All clear -- no issues detected"

        if all_verdicts and sources_failed:
            summary += f" (could not check: {', '.join(sources_failed)})"

        return {
            "summary": summary,
            "verdict_count": len(all_verdicts),
            "verdicts": all_verdicts,
            "by_severity": by_severity,
            "by_category": by_category,
            "sources_failed": sources_failed,
            "_meta": build_meta(
                "health_check",
                confidence="medium" if sources_failed else "high",
            ),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def explain_host_health(
        ctx: Context,
        host_name: Annotated[
            str, "Host name (e.g., 'docker-host', 'beast', 'proxmox')"
        ],
    ) -> dict:
        """Get detailed health data for a specific host: resource usage, services running on it, and any issues detected."""
        # Look up host in config.HOSTS for IP/metadata
        host_info = config.HOSTS.get(host_name)
        if not host_info:
            # Try case-insensitive match
            for name, info in config.HOSTS.items():
                if name.lower() == host_name.lower():
                    host_info = info
                    host_name = name
                    break

        # An unknown host must not report 'healthy' -- a typo'd name would
        # otherwise return status 'healthy' with empty metrics.
        if host_info is None:
            known = list(config.HOSTS.keys())[:8]
            return {
                "error": "not_found",
                "message": f"Host '{host_name}' not found. Known hosts include: {', '.join(known)}",
            }

        host_ip = host_info.get("ip", "") if host_info else ""

        # Look up host in topology for children/services. A host can be either a
        # top-level stack host (proxmox, beast) or a VM/LXC child of one
        # (docker-host, plex-stack are children of the proxmox stack). Match both
        # so child hosts still report their own services and their parent.
        children = []
        services_on_host = []
        parent_host = ""
        topo = config.TOPOLOGY
        for stack in topo.get("vertical_stacks", []):
            if stack.get("host") == host_name or (
                host_ip and stack.get("ip") == host_ip
            ):
                for child in stack.get("children", []):
                    children.append(
                        {
                            "name": child.get("name", ""),
                            "type": child.get("type", ""),
                            "ip": child.get("ip", ""),
                            "services": child.get("services", []),
                        }
                    )
                    services_on_host.extend(child.get("services", []))
                break
            matched_child = next(
                (
                    c
                    for c in stack.get("children", [])
                    if c.get("name") == host_name
                    or (host_ip and c.get("ip") == host_ip)
                ),
                None,
            )
            if matched_child:
                services_on_host.extend(matched_child.get("services", []))
                parent_host = stack.get("host", "")
                break

        # Query Prometheus for host metrics. Filter by the host's canonical
        # scrape instance name (hostnames like 'docker-host'), never its IP --
        # Prometheus instance labels are hostnames, so an IP filter matches
        # nothing and the tool would always report 'healthy'.
        metrics = {}
        client = ctx.lifespan_context.get("prometheus")
        matcher = _instance_matcher(host_name)
        if client and matcher:
            queries = {
                key: inject_matcher(query, matcher)
                for key, query in HOST_QUERIES.items()
            }

            try:
                responses = await asyncio.gather(
                    *[
                        client.get("/api/v1/query", params={"query": q})
                        for q in queries.values()
                    ]
                )
                for metric_name, resp in zip(queries.keys(), responses, strict=False):
                    data = resp.json().get("data", {}).get("result", [])
                    if data:
                        try:
                            metrics[metric_name] = round(float(data[0]["value"][1]), 1)
                        except (IndexError, ValueError, TypeError):
                            pass
            except Exception:
                pass

        # Check for issues
        issues = []
        for metric_name, value in metrics.items():
            verdict = _check_threshold(metric_name, value, host_name)
            if verdict:
                issues.append(verdict)

        # Build summary
        status = "healthy"
        if any(i["severity"] == "critical" for i in issues):
            status = "critical"
        elif issues:
            status = "warning"

        parts = [f"{host_name}: {status}"]
        if metrics.get("cpu_percent") is not None:
            parts.append(f"CPU {metrics['cpu_percent']}%")
        if metrics.get("ram_percent") is not None:
            parts.append(f"RAM {metrics['ram_percent']}%")
        if metrics.get("disk_percent") is not None:
            parts.append(f"Disk {metrics['disk_percent']}%")
        summary = ", ".join(parts)

        return {
            "summary": summary,
            "host_name": host_name,
            "status": status,
            "ip": host_ip,
            "role": host_info.get("role", "") if host_info else "",
            "metrics": metrics,
            "children": children,
            "parent_host": parent_host,
            "service_count": len(services_on_host),
            "services": services_on_host,
            "issues": issues,
            "_meta": build_meta("health_check"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def explain_service_health(
        ctx: Context,
        service_name: Annotated[
            str, "Service name (e.g., 'plex', 'prometheus', 'sonarr')"
        ],
    ) -> dict:
        """Get detailed health data for a specific service: current status, where it runs, what depends on it, and any issues detected."""
        # Look up service in config.SERVICES
        svc_info = config.SERVICES.get(service_name)
        if not svc_info:
            for name, info in config.SERVICES.items():
                if name.lower() == service_name.lower():
                    svc_info = info
                    service_name = name
                    break

        # An unknown service must not report 'healthy' with empty data.
        if svc_info is None:
            known = list(config.SERVICES.keys())[:8]
            return {
                "error": "not_found",
                "message": f"Service '{service_name}' not found. Known services include: {', '.join(known)}",
            }

        svc_ip = svc_info.get("ip", "") if svc_info else ""
        svc_port = svc_info.get("port", "") if svc_info else ""

        # Look up topology for host and dependencies
        topo = config.TOPOLOGY
        host_name = ""
        host_ip = ""

        # Find which host this service is on. `runs_on` is the VM/LXC child the
        # service lives in (its Prometheus scrape target); `host_name` is the
        # physical machine that child sits on.
        runs_on = ""
        vm_ip = ""
        for stack in topo.get("vertical_stacks", []):
            for child in stack.get("children", []):
                if service_name in child.get("services", []):
                    host_name = stack.get("host", "")
                    host_ip = stack.get("ip", "")
                    runs_on = child.get("name", "")
                    vm_ip = child.get("ip", "")
                    break
            if host_name:
                break

        # Find dependencies (what this service depends on) and dependents (what depends on this)
        depends_on = []
        depended_by = []
        for dep in topo.get("dependencies", []):
            if dep.get("from") == service_name:
                depends_on.extend(dep.get("to", []))
            if service_name in dep.get("to", []):
                depended_by.append(dep.get("from", ""))

        # Find ingress domains
        ingress_domains = []
        for entry in topo.get("ingress", []):
            if entry.get("target") == service_name:
                ingress_domains.append(entry.get("domain", ""))

        # Check Prometheus target status. Match by the resolved scrape instance
        # name of the VM/LXC the service runs on (falling back to the service
        # name itself), never by IP -- instance labels are hostnames.
        target_status = "unknown"
        client = ctx.lifespan_context.get("prometheus")
        matcher = _instance_matcher(runs_on) or _instance_matcher(service_name)
        if client and matcher:
            try:
                resp = await client.get(
                    "/api/v1/query", params={"query": f"up{{{matcher}}}"}
                )
                data = resp.json().get("data", {}).get("result", [])
                values = []
                for item in data:
                    try:
                        values.append(float(item["value"][1]))
                    except (IndexError, ValueError, TypeError):
                        continue
                if values:
                    target_status = "down" if any(v == 0 for v in values) else "up"
            except Exception:
                pass

        # Build issues
        issues = []
        if target_status == "down":
            issues.append(
                {
                    "entity": service_name,
                    "severity": "critical",
                    "category": "availability",
                    "message": f"Prometheus target for {service_name} is down",
                    "source": "prometheus",
                }
            )

        # `issues` is only populated when target_status == "down" (which sets
        # status below), so there is no non-availability source that could
        # produce a "warning" verdict here -- the tool answers healthy or down.
        status = "down" if target_status == "down" else "healthy"

        summary_parts = [f"{service_name}: {status}"]
        if runs_on:
            # The VM/LXC the service actually lives in is the more useful answer
            # (where to find the container); the physical host is secondary.
            summary_parts.append(f"runs on {runs_on} ({host_name})")
        elif host_name:
            summary_parts.append(f"runs on {host_name}")
        if depends_on:
            summary_parts.append(f"depends on {', '.join(depends_on)}")
        if depended_by:
            summary_parts.append(f"used by {', '.join(depended_by)}")
        summary = ", ".join(summary_parts)

        return {
            "summary": summary,
            "service_name": service_name,
            "status": status,
            "target_status": target_status,
            "host": host_name,
            "host_ip": host_ip,
            # The VM/LXC the service runs in (matching graph.py's shape); the
            # physical machine is reported separately as physical_host.
            "runs_on": runs_on,
            "vm_ip": vm_ip,
            "physical_host": host_name,
            "service_ip": svc_ip,
            "service_port": svc_port,
            "depends_on": depends_on,
            "depended_by": depended_by,
            "ingress_domains": ingress_domains,
            "issues": issues,
            "_meta": build_meta("health_check"),
        }
