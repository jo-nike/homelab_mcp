"""Shared Portainer container-lookup helpers.

The endpoint-iterate -> skip Status!=1 -> list containers (all=true) ->
case-insensitive name match walk was implemented three times
(docker.get_container_info, docker.restart_container, compound._find_container),
and ``is_critical`` was byte-identical in two modules. This owns both.
"""

import httpx

import config
from lib.hosts import resolve_host
from lib.redact import redact_exception


def is_critical(name: str) -> bool:
    """True if a container name is in the topology's critical blocklist."""
    blocked = config.TOPOLOGY.get("critical_containers", [])
    return name.lower() in [c.lower() for c in blocked]


def container_name(container: dict) -> str:
    """Portainer/Docker container display name (first Names entry, no leading /)."""
    return container.get("Names", ["/unknown"])[0].lstrip("/")


def _host_matches(ep_name: str, host: str, host_canonical: str | None) -> bool:
    return ep_name.lower() == host.lower() or (
        host_canonical is not None
        and resolve_host(ep_name, "portainer") == host_canonical
    )


async def find_container_matches(
    client, name: str, host: str | None = None
) -> list[dict] | dict:
    """Find every container named ``name`` across Portainer endpoints.

    Returns a list of ``{"ep_id", "ep_name", "container"}`` (the raw container
    JSON), one per matching endpoint whose Status == 1. When ``host`` is given,
    only that endpoint (matched by name or canonical resolution) is searched.
    On a failed endpoint listing the standard error dict is returned; a
    per-endpoint container-list failure is skipped so one bad host does not
    abort the rest.
    """
    try:
        resp = await client.get("/api/endpoints")
        resp.raise_for_status()
        endpoints = resp.json()
    except httpx.TimeoutException:
        return {"error": "timeout", "message": "Portainer did not respond in time"}
    except httpx.HTTPStatusError as e:
        return {
            "error": "http_error",
            "status": e.response.status_code,
            "message": redact_exception(e),
        }
    except httpx.HTTPError as e:
        return {"error": "connection_error", "message": redact_exception(e)}

    host_canonical = resolve_host(host) if host else None
    matches: list[dict] = []
    for ep in endpoints:
        if ep.get("Status") != 1:
            continue
        ep_id = ep.get("Id")
        ep_name = ep.get("Name", f"endpoint-{ep_id}")
        if host and not _host_matches(ep_name, host, host_canonical):
            continue
        try:
            containers_resp = await client.get(
                f"/api/endpoints/{ep_id}/docker/containers/json",
                params={"all": "true"},
            )
            containers_resp.raise_for_status()
            containers = containers_resp.json()
        except httpx.HTTPError:
            continue
        for c in containers:
            if container_name(c).lower() == name.lower():
                matches.append({"ep_id": ep_id, "ep_name": ep_name, "container": c})
                break
    return matches
