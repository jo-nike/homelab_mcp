"""Compound convenience tools for agentic orchestration.

Multi-step tools that compose atomic operations with entity graph lookups
and health checks. Designed for local models that cannot chain tool calls.
"""

import asyncio
import time
from typing import Annotated

import httpx
from fastmcp import Context

import config
from lib.audit import audit_log
from lib.hosts import resolve_host
from lib.meta import build_meta
from lib.portainer import container_name, find_container_matches, is_critical


def register(mcp):
    """Register compound tools. safe_restart_container needs Portainer;
    create_task_from_alert needs only Vikunja, so each registers independently."""

    # --- Tool 2: create_task_from_alert (COMP-02) ---

    if config.VIKUNJA_URL and config.VIKUNJA_TOKEN:

        @mcp.tool(
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": False,
            }
        )
        async def create_task_from_alert(
            ctx: Context,
            entity: Annotated[
                str,
                "Entity name from the health verdict (e.g. 'docker-host', 'sonarr')",
            ],
            severity: Annotated[
                str, "Severity level: 'critical', 'warning', or 'info'"
            ],
            category: Annotated[
                str,
                "Category from verdict (e.g. 'resource', 'availability', 'security', 'certificate', 'cron')",
            ],
            message: Annotated[str, "The alert message describing the issue"],
            source: Annotated[
                str, "Source system (e.g. 'prometheus', 'healthchecks', 'crowdsec')"
            ] = "unknown",
            project_id: Annotated[
                int | None,
                "Vikunja project ID. Defaults to VIKUNJA_ALERT_PROJECT_ID env var or first available project.",
            ] = None,
            dry_run: Annotated[
                bool, "Preview the task that would be created without executing"
            ] = False,
        ) -> dict:
            """Create a Vikunja task from a health alert finding. Maps severity to priority and pre-fills context. Designed to work with verdicts from what_needs_attention."""

            # 1. Map severity to priority
            severity_map = {"critical": 4, "warning": 3, "info": 2}
            priority = severity_map.get(severity.lower(), 2)

            # 2. Build title
            title = f"[{severity.upper()}] {entity}: {message}"

            # 3. Build description
            description = (
                f"## Alert Details\n\n"
                f"- **Entity:** {entity}\n"
                f"- **Severity:** {severity}\n"
                f"- **Category:** {category}\n"
                f"- **Source:** {source}\n"
                f"- **Message:** {message}\n"
                f"- **Created:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n\n"
                f"Auto-created by homelab-mcp from health alert."
            )

            # 4. Determine project_id
            pid = project_id
            if pid is None and config.VIKUNJA_ALERT_PROJECT_ID:
                try:
                    pid = int(config.VIKUNJA_ALERT_PROJECT_ID)
                except (ValueError, TypeError):
                    pass

            vikunja_client: httpx.AsyncClient = ctx.lifespan_context["vikunja"]

            if pid is None:
                # Fallback: fetch project list and use first project
                try:
                    resp = await vikunja_client.get("/api/v1/projects")
                    resp.raise_for_status()
                    projects = resp.json()
                    if projects:
                        pid = projects[0].get("id")
                    else:
                        return {
                            "error": "no_projects",
                            "message": "No Vikunja projects found",
                        }
                except httpx.TimeoutException:
                    return {
                        "error": "timeout",
                        "message": "Vikunja did not respond in time",
                    }
                except httpx.HTTPError as e:
                    return {"error": "connection_error", "message": str(e)}

            # 5. Build task payload
            body = {"title": title, "description": description, "priority": priority}

            # 6. Dry run gate
            if dry_run:
                await audit_log(
                    ctx,
                    action="create_task_from_alert",
                    target=str(pid),
                    params={
                        "entity": entity,
                        "severity": severity,
                        "category": category,
                        "project_id": pid,
                    },
                    result="dry_run",
                    dry_run=True,
                )
                return {
                    "dry_run": True,
                    "action": "create_task_from_alert",
                    "project_id": pid,
                    "title": title,
                    "priority": priority,
                    "severity": severity,
                    "summary": f"Would create task: {title}",
                }

            # 7. Create task
            try:
                resp = await vikunja_client.put(
                    f"/api/v1/projects/{pid}/tasks", json=body
                )
                resp.raise_for_status()
                result = resp.json()
            except httpx.HTTPStatusError as e:
                await audit_log(
                    ctx,
                    action="create_task_from_alert",
                    target=str(pid),
                    params={"entity": entity, "severity": severity},
                    result="error",
                )
                return {
                    "error": "task_creation_failed",
                    "message": str(e),
                    "status_code": e.response.status_code,
                }
            except httpx.TimeoutException:
                await audit_log(
                    ctx,
                    action="create_task_from_alert",
                    target=str(pid),
                    params={"entity": entity, "severity": severity},
                    result="error",
                )
                return {
                    "error": "timeout",
                    "message": "Vikunja did not respond in time",
                }
            except httpx.HTTPError as e:
                await audit_log(
                    ctx,
                    action="create_task_from_alert",
                    target=str(pid),
                    params={"entity": entity, "severity": severity},
                    result="error",
                )
                return {"error": "connection_error", "message": str(e)}

            # 8. Audit success
            await audit_log(
                ctx,
                action="create_task_from_alert",
                target=str(result.get("id", "")),
                params={
                    "entity": entity,
                    "severity": severity,
                    "category": category,
                    "project_id": pid,
                },
                result="success",
            )

            # 9. Return result
            return {
                "action": "create_task_from_alert",
                "task_id": result.get("id"),
                "title": title,
                "project_id": pid,
                "priority": priority,
                "severity": severity,
                "result": "success",
                "summary": f"Created task '{title}' (ID: {result.get('id')})",
                "_meta": build_meta("vikunja"),
            }

    # safe_restart_container and its helpers need Portainer.
    if not (config.PORTAINER_URL and config.PORTAINER_API_KEY):
        return

    # --- Helper functions ---

    def _get_dependents(service_name: str) -> list[str]:
        """Get services that depend on the given service."""
        dependents = []
        for dep in config.TOPOLOGY.get("dependencies", []):
            if service_name in dep.get("to", []):
                dependents.append(dep.get("from"))
        return dependents

    def _find_service_for_container(container_name: str) -> str | None:
        """Find the topology service name matching a container name.

        Uses a prefix match, so a container like 'grafana-image-renderer'
        resolves to service 'grafana'; startswith already covers exact equality.
        """
        name_lower = container_name.lower()
        for stack in config.TOPOLOGY.get("vertical_stacks", []):
            for child in stack.get("children", []):
                for service in child.get("services", []):
                    if name_lower.startswith(service.lower()):
                        return service
        return None

    async def _find_container(ctx: Context, name: str) -> dict:
        """Find a container by name across all Portainer endpoints."""
        client: httpx.AsyncClient = ctx.lifespan_context["portainer"]
        matches = await find_container_matches(client, name)
        if isinstance(matches, dict):  # error dict from the endpoint listing
            return matches
        for m in matches:
            c = m["container"]
            return {
                "name": container_name(c),
                "host": resolve_host(m["ep_name"], "portainer") or m["ep_name"],
                "endpoint": m["ep_name"],
                "ep_id": m["ep_id"],
                "container_id": c.get("Id"),
                "status": c.get("State", "unknown"),
            }
        return {
            "error": "not_found",
            "message": f"Container '{name}' not found on any host",
        }

    async def _poll_container_health(
        ctx: Context,
        ep_id: int,
        container_id: str,
        timeout: int = 30,
        interval: int = 5,
    ) -> dict:
        """Poll container status until running/healthy or timeout."""
        client: httpx.AsyncClient = ctx.lifespan_context["portainer"]
        start = time.monotonic()
        checks = 0
        # Derive the check cap from the caller's timeout/interval instead of a
        # hardcoded 6, so timeout is actually honored. +1 covers the initial
        # poll before the first sleep.
        max_checks = max(1, timeout // max(interval, 1) + 1)
        last_status = "unknown"
        last_health = None

        while (time.monotonic() - start) < timeout and checks < max_checks:
            checks += 1
            try:
                resp = await client.get(
                    f"/api/endpoints/{ep_id}/docker/containers/{container_id}/json"
                )
                resp.raise_for_status()
                data = resp.json()
                state = data.get("State", {})
                status = state.get("Status", "unknown")
                last_status = status

                # If container has a healthcheck, also verify health
                health_status = state.get("Health", {}).get("Status")
                if health_status:
                    last_health = health_status
                    if status == "running" and health_status == "healthy":
                        return {
                            "healthy": True,
                            "status": status,
                            "health_status": health_status,
                            "checks": checks,
                            "elapsed_seconds": round(time.monotonic() - start, 1),
                        }
                else:
                    # No healthcheck -- running is sufficient
                    if status == "running":
                        return {
                            "healthy": True,
                            "status": status,
                            "checks": checks,
                            "elapsed_seconds": round(time.monotonic() - start, 1),
                        }
            except httpx.HTTPError:
                pass

            if (time.monotonic() - start) + interval < timeout:
                await asyncio.sleep(interval)

        elapsed = round(time.monotonic() - start, 1)
        # Carry the last observed state into the failure return rather than
        # discarding it as 'unknown'.
        result = {
            "healthy": False,
            "status": last_status,
            "checks": checks,
            "elapsed_seconds": elapsed,
        }
        if last_health is not None:
            result["health_status"] = last_health
        return result

    # --- Tool 1: safe_restart_container (COMP-01) ---

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        }
    )
    async def safe_restart_container(
        ctx: Context,
        name: Annotated[str, "Container name to restart (e.g. 'grafana', 'sonarr')"],
        dry_run: Annotated[bool, "Preview the restart plan without executing"] = False,
    ) -> dict:
        """Dependency-aware container restart. Checks entity graph for dependents, verifies not critical, restarts via Portainer, polls health, and reports result. Use dry_run=True to preview the full restart plan."""

        # 1. Find the container
        container = await _find_container(ctx, name)
        if "error" in container:
            return container

        # 2. Critical check
        if is_critical(name):
            return {
                "error": "blocked",
                "message": f"Container '{name}' is critical and cannot be restarted",
                "critical_containers": config.TOPOLOGY.get("critical_containers", []),
                "reason": "critical_container",
            }

        # 3. Find service name
        service = _find_service_for_container(name)

        # 4. Get dependents
        dependents = _get_dependents(service) if service else []

        # 5. Dependency warning
        dep_warning = (
            f"Warning: {', '.join(dependents)} depend on {service}"
            if dependents
            else None
        )

        # 6. Dry run gate
        if dry_run:
            await audit_log(
                ctx,
                action="safe_restart_container",
                target=name,
                params={
                    "host": container["host"],
                    "service": service,
                    "dependents": dependents,
                },
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": "safe_restart",
                "target": name,
                "host": container["host"],
                "service": service,
                "dependents": dependents,
                "dependency_warning": dep_warning,
                "would_restart": True,
                "summary": f"Would restart {name} on {container['host']}"
                + (f". {dep_warning}" if dep_warning else ""),
            }

        # 7. Execute restart. Audit the failure paths too (matching
        # docker.restart_container) -- a failed write attempt is exactly what the
        # audit trail should capture.
        audit_params = {
            "host": container["host"],
            "service": service,
            "dependents": dependents,
        }
        client: httpx.AsyncClient = ctx.lifespan_context["portainer"]
        try:
            restart_resp = await client.post(
                f"/api/endpoints/{container['ep_id']}/docker/containers/{container['container_id']}/restart"
            )
            if restart_resp.status_code != 204:
                await audit_log(
                    ctx,
                    action="safe_restart_container",
                    target=name,
                    params=audit_params,
                    result="failure",
                )
                return {
                    "error": "restart_failed",
                    "message": f"Restart returned status {restart_resp.status_code}",
                    "status_code": restart_resp.status_code,
                }
        except httpx.TimeoutException:
            await audit_log(
                ctx,
                action="safe_restart_container",
                target=name,
                params=audit_params,
                result="failure",
            )
            return {"error": "timeout", "message": "Portainer did not respond in time"}
        except httpx.HTTPError as e:
            await audit_log(
                ctx,
                action="safe_restart_container",
                target=name,
                params=audit_params,
                result="failure",
            )
            return {"error": "connection_error", "message": str(e)}

        # 8. Poll health
        health = await _poll_container_health(
            ctx, container["ep_id"], container["container_id"]
        )

        # 9. Audit log
        await audit_log(
            ctx,
            action="safe_restart_container",
            target=name,
            params={
                "host": container["host"],
                "service": service,
                "dependents": dependents,
                "healthy": health["healthy"],
            },
            result="success",
        )

        # 10. Return comprehensive report
        health_summary = (
            "OK"
            if health["healthy"]
            else f"NOT confirmed after {health['elapsed_seconds']}s"
        )
        return {
            "action": "safe_restart",
            "target": name,
            "host": container["host"],
            "service": service,
            "dependents": dependents,
            "dependency_warning": dep_warning,
            "restart_result": "success",
            "health_check": health,
            "summary": f"Restarted {name} on {container['host']}. Health: {health_summary}"
            + (f". Note: {dep_warning}" if dep_warning else ""),
            "_meta": build_meta("portainer"),
        }
