"""Entity graph tools for homelab MCP server."""

from typing import Annotated

from fastmcp import Context

import config
from lib.meta import build_meta


def register(mcp):
    """Register entity graph tools. Always registers (reads from in-memory topology)."""

    def _find_entity(name: str) -> dict:
        """Search topology for entity by name across all relationship types."""
        topo = config.TOPOLOGY
        if not topo:
            return {"error": "no_topology", "message": "Topology not loaded"}

        result = {
            "entity": name,
            "found_in": [],
            "vertical_stack": None,
            "ingress": [],
            "depends_on": [],
            "depended_by": [],
            "storage_mounts": [],
        }

        # Search vertical stacks
        for stack in topo.get("vertical_stacks", []):
            host_name = stack.get("host", "")
            if host_name == name:
                result["found_in"].append("vertical_stack:host")
                result["vertical_stack"] = {
                    "role": "host",
                    "host": host_name,
                    "ip": stack.get("ip"),
                    "children": stack.get("children", []),
                }
            for child in stack.get("children", []):
                child_name = child.get("name", "")
                if child_name == name:
                    result["found_in"].append(
                        f"vertical_stack:{child.get('type', 'child')}"
                    )
                    result["vertical_stack"] = {
                        "role": child.get("type"),
                        "name": child_name,
                        "ip": child.get("ip"),
                        "parent_host": host_name,
                        "services": child.get("services", []),
                    }
                if name in child.get("services", []):
                    result["found_in"].append("vertical_stack:service")
                    # Prefer the infrastructure (host/child) view when a name is
                    # both a stack child and a service of the same name (npm,
                    # portainer, pbs, ...). Only fill the service representation
                    # if nothing more specific already claimed vertical_stack,
                    # so it no longer self-references ('npm runs on npm').
                    if result["vertical_stack"] is None:
                        result["vertical_stack"] = {
                            "role": "service",
                            "service": name,
                            "runs_on": child_name,
                            "host": host_name,
                            "host_ip": stack.get("ip"),
                            "vm_ip": child.get("ip"),
                        }

        # Search ingress
        for ing in topo.get("ingress", []):
            if ing.get("target") == name or ing.get("domain") == name:
                result["ingress"].append(ing)

        # Search dependencies (both directions)
        for dep in topo.get("dependencies", []):
            if dep.get("from") == name:
                result["depends_on"].extend(dep.get("to", []))
            if name in dep.get("to", []):
                result["depended_by"].append(dep.get("from"))

        # Search storage mounts
        for mount in topo.get("storage_mounts", []):
            source = mount.get("source", "")
            share = mount.get("share", "")
            if source == name or share == name:
                result["storage_mounts"].append(mount)
            else:
                for consumer in mount.get("consumers", []):
                    if (
                        name in consumer.get("services", [])
                        or consumer.get("host") == name
                    ):
                        result["storage_mounts"].append(
                            {
                                "source": source,
                                "share": share,
                                "mount": consumer.get("mount"),
                            }
                        )

        if not result["found_in"]:
            return {
                "error": "not_found",
                "message": f"Entity '{name}' not found in topology",
            }

        # Return the full fixed key set (empty lists included) so the response
        # schema is stable call-to-call, rather than dropping empty fields and
        # making keys appear/disappear -- schema instability the small local LLMs
        # this server targets cope badly with.
        return result

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def show_dependency_chain(
        ctx: Context,
        entity: Annotated[
            str,
            "Name of entity to look up: service name (e.g., 'plex'), host name (e.g., 'docker-host'), or domain (e.g., 'plex.example.com')",
        ],
    ) -> dict:
        """Show the full dependency chain for any homelab entity. Returns what it runs on, what depends on it, what it depends on, how it is accessed (ingress), and what storage it uses."""
        result = _find_entity(entity)
        if "error" in result:
            return result

        # Build summary
        parts = []
        if result.get("vertical_stack"):
            vs = result["vertical_stack"]
            role = vs.get("role", "entity")
            if role == "service":
                parts.append(
                    f"{entity} runs on {vs.get('runs_on', '?')} ({vs.get('host', '?')})"
                )
            elif role == "host":
                children = vs.get("children", [])
                parts.append(f"{entity} hosts {len(children)} VMs/containers")
            else:
                parts.append(f"{entity} is a {role} on {vs.get('parent_host', '?')}")

        if result.get("depends_on"):
            parts.append(f"depends on: {', '.join(result['depends_on'])}")
        if result.get("depended_by"):
            parts.append(f"depended on by: {', '.join(result['depended_by'])}")
        if result.get("ingress"):
            domains = [i.get("domain", "?") for i in result["ingress"]]
            parts.append(f"accessible via: {', '.join(domains)}")
        if result.get("storage_mounts"):
            shares = [m.get("share", "?") for m in result["storage_mounts"]]
            parts.append(f"storage: {', '.join(shares)}")

        result["summary"] = "; ".join(parts) if parts else f"Found {entity} in topology"
        result["_meta"] = build_meta("topology")
        return result
