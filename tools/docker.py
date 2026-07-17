"""Docker/Portainer container management tools for homelab infrastructure."""

from typing import Annotated, Any

import httpx
from fastmcp import Context

import config
from lib.audit import audit_log
from lib.hosts import resolve_host
from lib.http import service_request
from lib.meta import build_meta
from lib.portainer import find_container_matches, is_critical


def register(mcp):
    """Register Docker tools. Skips if Portainer credentials missing."""
    if not (config.PORTAINER_URL and config.PORTAINER_API_KEY):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Portainer API."""
        return await service_request(
            ctx, "portainer", path, params=params, display_name="Portainer"
        )

    def _format_ports(ports: list[dict]) -> list[str]:
        """Format Docker port mappings as human-readable strings.

        Input: [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}]
        Output: ["8080:80/tcp"]
        Empty or no mapping returns empty list. Dual-stack bindings (one entry
        per bind IP, e.g. 0.0.0.0 and ::) are de-duplicated, preserving order.
        """
        formatted = {}
        for p in ports:
            public = p.get("PublicPort")
            private = p.get("PrivatePort")
            proto = p.get("Type", "tcp")
            if public and private:
                formatted[f"{public}:{private}/{proto}"] = None
            elif private:
                formatted[f"{private}/{proto}"] = None
        return list(formatted)

    async def _post(ctx: Context, path: str, params: dict | None = None) -> dict:
        """Execute POST request against Portainer API."""
        client: httpx.AsyncClient = ctx.lifespan_context["portainer"]
        try:
            resp = await client.post(path, params=params)
            if resp.status_code == 204:
                return {"status": "success"}
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Portainer did not respond in time"}
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
                "message": "Portainer returned a non-JSON response",
            }

    # ---- Tool 1: list_containers (DOCK-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def list_containers(
        ctx: Context,
        host: Annotated[
            str | None,
            "Docker host name (e.g. 'beast', 'docker-host', 'ai-vm'). "
            "Omit for all hosts.",
        ] = None,
    ) -> dict:
        """List all Docker containers across all hosts with status, grouped by host."""
        # Step 1: Discover all Portainer endpoints (Docker hosts)
        endpoints = await _get(ctx, "/api/endpoints")
        if isinstance(endpoints, dict) and "error" in endpoints:
            return endpoints

        # Resolve the requested host to its canonical name so a canonical
        # ('docker-host', 'ai-vm') matches Portainer's own dialect
        # ('docker host', 'AI').
        host_canonical = resolve_host(host) if host else None

        result = {}
        for ep in endpoints:
            ep_name = ep.get("Name", "unknown")
            ep_id = ep.get("Id")

            if host and not (
                ep_name.lower() == host.lower()
                or (
                    host_canonical
                    and resolve_host(ep_name, "portainer") == host_canonical
                )
            ):
                continue

            # The endpoint name is Portainer's own ("docker host", "AI"); stamp
            # the canonical host additively so the map stays keyed by endpoint.
            # Present on every branch — a down endpoint is still a known host.
            ep_host = resolve_host(ep_name, "portainer")

            # Status: 1 = up, 2 = down
            if ep.get("Status") != 1:
                result[ep_name] = {"host": ep_host, "status": "down", "containers": []}
                continue

            # Fetch containers for this endpoint
            containers_data = await _get(
                ctx,
                f"/api/endpoints/{ep_id}/docker/containers/json",
                params={"all": "true"},
            )
            if isinstance(containers_data, dict) and "error" in containers_data:
                # Flatten to the standard {error, message} shape rather than
                # nesting the whole error dict under "error".
                result[ep_name] = {
                    "host": ep_host,
                    "status": "error",
                    "containers": [],
                    "error": containers_data.get("error", "connection_error"),
                    "message": containers_data.get("message", ""),
                }
                continue

            containers = []
            for c in containers_data:
                name = c.get("Names", ["/unknown"])[0].lstrip("/")
                containers.append(
                    {
                        "name": name,
                        "status": c.get("State", "unknown"),
                        "image": c.get("Image", "unknown"),
                        "ports": _format_ports(c.get("Ports", [])),
                    }
                )

            result[ep_name] = {
                "host": ep_host,
                "status": "up",
                "running": sum(1 for c in containers if c["status"] == "running"),
                "total": len(containers),
                "containers": containers,
            }
        result["_meta"] = build_meta("portainer")
        return result

    # ---- Tool 2: get_container_info (DOCK-02) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_container_info(
        ctx: Context,
        name: Annotated[str, "Container name (e.g. 'grafana', 'sonarr')"],
    ) -> Any:
        """Get detailed info for a Docker container by name, searching across all hosts."""
        client = ctx.lifespan_context["portainer"]
        found = await find_container_matches(client, name)
        if isinstance(found, dict):  # error dict from the endpoint listing
            return found

        matches = []
        for m in found:
            ep_name = m["ep_name"]
            ep_id = m["ep_id"]
            c = m["container"]
            c_name = m["container"].get("Names", ["/unknown"])[0].lstrip("/")
            container_id = c.get("Id")
            # Get full inspect data
            detail = await _get(
                ctx,
                f"/api/endpoints/{ep_id}/docker/containers/{container_id}/json",
            )
            if isinstance(detail, dict) and "error" not in detail:
                state = detail.get("State", {})
                cfg = detail.get("Config", {})
                network = detail.get("NetworkSettings", {})
                mounts = detail.get("Mounts", [])

                matches.append(
                    {
                        "name": c_name,
                        # Canonical host, with the raw Portainer endpoint kept as
                        # "endpoint" so the map joins with list_containers.
                        "host": resolve_host(ep_name, "portainer") or ep_name,
                        "endpoint": ep_name,
                        "id": container_id[:12] if container_id else "unknown",
                        "status": state.get("Status", "unknown"),
                        "started_at": state.get("StartedAt", ""),
                        "image": cfg.get("Image", "unknown"),
                        "env_count": len(cfg.get("Env", [])),
                        "labels": {
                            k: v for k, v in list(cfg.get("Labels", {}).items())[:10]
                        },
                        "ports": _format_ports(c.get("Ports", [])),
                        "networks": list(network.get("Networks", {}).keys()),
                        "mounts": [
                            {
                                "source": m.get("Source", ""),
                                "destination": m.get("Destination", ""),
                                "type": m.get("Type", ""),
                            }
                            for m in mounts
                        ],
                    }
                )

        if not matches:
            return {
                "error": "not_found",
                "message": f"Container '{name}' not found on any host",
            }

        if len(matches) == 1:
            matches[0]["_meta"] = build_meta("portainer")
            return matches[0]

        # Multiple matches (same name on different hosts) - return all
        return {"containers": matches, "_meta": build_meta("portainer")}

    # ---- Tool 3: restart_container (WRITE-01) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        }
    )
    async def restart_container(
        ctx: Context,
        name: Annotated[str, "Container name (e.g. 'grafana', 'sonarr')"],
        host: Annotated[
            str | None,
            "Host to disambiguate when the same container name exists on "
            "multiple hosts (e.g. 'docker-host', 'beast'). Omit if unique.",
        ] = None,
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Restart a Docker container by name via Portainer. Use dry_run=True to preview."""
        # Step 1: Check critical blocklist
        if is_critical(name):
            return {
                "error": "blocked",
                "message": f"Container '{name}' is critical and cannot be restarted. Critical containers: {', '.join(config.TOPOLOGY.get('critical_containers', []))}",
                "reason": "critical_container",
            }

        # Step 2: Find the container across all endpoints. The same name can
        # exist on several hosts (watchtower, promtail); collect every match and
        # refuse to guess which one unless the caller names the host.
        client = ctx.lifespan_context["portainer"]
        found = await find_container_matches(client, name, host=host)
        if isinstance(found, dict):  # error dict from the endpoint listing
            return found

        matches = [
            {
                "id": m["container"].get("Id"),
                "ep_name": m["ep_name"],
                "ep_id": m["ep_id"],
            }
            for m in found
        ]

        if not matches:
            return {
                "error": "not_found",
                "message": f"Container '{name}' not found on any host",
            }

        if len(matches) > 1:
            hosts = [m["ep_name"] for m in matches]
            return {
                "error": "ambiguous",
                "message": f"Container '{name}' exists on multiple hosts: "
                f"{', '.join(hosts)}. Pass host= to disambiguate.",
                "hosts": hosts,
            }

        container_id = matches[0]["id"]
        ep_name = matches[0]["ep_name"]
        ep_id = matches[0]["ep_id"]

        # Step 3: Dry run gate
        if dry_run:
            await audit_log(
                ctx,
                action="restart_container",
                target=name,
                params={"host": ep_name, "container_id": container_id[:12]},
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": "restart",
                "target": name,
                "host": resolve_host(ep_name, "portainer") or ep_name,
                "endpoint": ep_name,
                "container_id": container_id[:12],
                "would_restart": True,
                "summary": f"Would restart {name} on {ep_name}",
            }

        # Step 4: Execute restart
        result = await _post(
            ctx, f"/api/endpoints/{ep_id}/docker/containers/{container_id}/restart"
        )

        if "error" in result:
            await audit_log(
                ctx,
                action="restart_container",
                target=name,
                params={"host": ep_name, "container_id": container_id[:12]},
                result="failure",
            )
            return result

        # Step 5: Audit success and return
        await audit_log(
            ctx,
            action="restart_container",
            target=name,
            params={"host": ep_name, "container_id": container_id[:12]},
            result="success",
        )
        return {
            "action": "restart",
            "target": name,
            "host": resolve_host(ep_name, "portainer") or ep_name,
            "endpoint": ep_name,
            "container_id": container_id[:12],
            "result": "success",
            "summary": f"Restarted {name} on {ep_name}",
            "_meta": build_meta("portainer"),
        }
