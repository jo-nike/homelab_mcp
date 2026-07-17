"""Proxmox VE monitoring tools for homelab infrastructure."""

from typing import Annotated, Any

from fastmcp import Context

import config
from lib.hosts import resolve_host
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Proxmox tools. Skips if credentials missing."""
    if not (
        config.PROXMOX_URL and config.PROXMOX_TOKEN_ID and config.PROXMOX_TOKEN_SECRET
    ):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Proxmox API."""
        return await service_request(
            ctx, "proxmox", path, params=params, display_name="Proxmox"
        )

    # ---- Tool 1: get_proxmox_nodes (PROX-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_proxmox_nodes(
        ctx: Context,
        node: Annotated[
            str | None,
            "Proxmox node name (e.g. 'proxmox'). Omit for all nodes.",
        ] = None,
    ) -> dict:
        """Get Proxmox node status: CPU%, RAM%, uptime for all cluster nodes."""
        data = await _get(ctx, "/api2/json/cluster/resources", {"type": "node"})
        if "error" in data:
            return data

        nodes = {}
        matched_any = False
        for r in data.get("data", []):
            node_name = r.get("node")
            if not node_name:
                # Every other field access here uses .get; skip a malformed entry
                # rather than KeyError out of the whole tool.
                continue
            if node and node_name != node:
                continue
            matched_any = True
            nodes[node_name] = {
                "name": node_name,
                # Proxmox's node name is its own id ("pve"), not a host name —
                # stamp the canonical one so a consumer can join on it.
                "host": resolve_host(node_name, "proxmox"),
                "status": r.get("status", "unknown"),
                "cpu_percent": round((r.get("cpu", 0)) * 100, 1),
                "ram_used_bytes": r.get("mem", 0),
                "ram_total_bytes": r.get("maxmem", 0),
                "ram_percent": round(
                    (r.get("mem", 0) / max(r.get("maxmem", 1), 1)) * 100, 1
                ),
                "uptime_seconds": r.get("uptime", 0),
            }
        # A node filter that matched nothing is a wrong/typo'd name, mirroring
        # get_proxmox_vm_status's not_found rather than an empty {_meta}.
        if node and not matched_any:
            return {
                "error": "not_found",
                "message": f"Proxmox node '{node}' not found",
            }
        nodes["_meta"] = build_meta("proxmox")
        return nodes

    # ---- Tool 2: get_proxmox_resources (PROX-02) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_proxmox_resources(ctx: Context) -> dict:
        """Get all Proxmox VMs, containers, and storage pools with status and allocation."""
        data = await _get(ctx, "/api2/json/cluster/resources")
        if "error" in data:
            return data

        def _guest_entry(r: dict, label: str, gtype: str) -> dict:
            """Build a guest (VM/CT) entry with allocation + live stats.

            Live cpu/mem come from cluster/resources for running guests; when
            stopped they are reported as null.
            """
            status = r.get("status", "unknown")
            maxmem = r.get("maxmem", 0)
            name = r.get("name", f"{label} {r.get('vmid')}")
            entry = {
                "vmid": r.get("vmid"),
                "name": name,
                # The guest name is Proxmox's own ("AI" is the host we call
                # ai-vm). None for a guest that isn't one of our canonical hosts
                # — the templates, mostly — which is the honest answer, not a gap.
                "host": resolve_host(name, "proxmox"),
                "type": gtype,
                "status": status,
                "node": r.get("node"),
                "cpu_cores": r.get("maxcpu", 0),
                "ram_total_bytes": maxmem,
                "disk_total_bytes": r.get("maxdisk", 0),
            }
            if status == "running":
                mem = r.get("mem", 0)
                entry["cpu_percent"] = round(r.get("cpu", 0) * 100, 1)
                entry["ram_used_bytes"] = mem
                entry["ram_percent"] = round((mem / max(maxmem, 1)) * 100, 1)
            else:
                entry["cpu_percent"] = None
                entry["ram_used_bytes"] = None
                entry["ram_percent"] = None
            return entry

        result = {"vms": [], "containers": [], "storage": []}
        for r in data.get("data", []):
            rtype = r.get("type")
            if rtype == "qemu":
                result["vms"].append(_guest_entry(r, "VM", "vm"))
            elif rtype == "lxc":
                result["containers"].append(_guest_entry(r, "CT", "ct"))
            elif rtype == "storage":
                result["storage"].append(
                    {
                        "name": r.get("storage", "unknown"),
                        "node": r.get("node"),
                        "plugin_type": r.get("plugintype", "unknown"),
                        "disk_used_bytes": r.get("disk", 0),
                        "disk_total_bytes": r.get("maxdisk", 0),
                        "status": r.get("status", "unknown"),
                    }
                )
        result["_meta"] = build_meta("proxmox")
        return result

    # ---- Tool 3: get_proxmox_vm_status (PROX-03) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_proxmox_vm_status(
        ctx: Context,
        vmid: Annotated[int, "VM or CT ID (e.g. 100, 101)"],
    ) -> dict:
        """Get detailed status of a specific Proxmox VM or container by ID."""
        # Step 1: Find node and type from cluster resources
        resources = await _get(ctx, "/api2/json/cluster/resources")
        if "error" in resources:
            return resources

        target = None
        for r in resources.get("data", []):
            if r.get("vmid") == vmid and r.get("type") in ("qemu", "lxc"):
                target = r
                break

        if not target:
            return {
                "error": "not_found",
                "message": f"VM/CT {vmid} not found in cluster",
            }

        node = target["node"]
        rtype = target["type"]  # "qemu" or "lxc"

        # Step 2: Get live status
        status_data = await _get(
            ctx, f"/api2/json/nodes/{node}/{rtype}/{vmid}/status/current"
        )
        # Step 3: Get config
        config_data = await _get(ctx, f"/api2/json/nodes/{node}/{rtype}/{vmid}/config")

        # Build response from allocation (target) + live stats + config
        result = {
            "vmid": vmid,
            "name": target.get("name", f"{rtype.upper()} {vmid}"),
            "type": "vm" if rtype == "qemu" else "ct",
            "node": node,
            "status": target.get("status", "unknown"),
            # Allocation from cluster/resources
            "cpu_cores": target.get("maxcpu", 0),
            "ram_total_bytes": target.get("maxmem", 0),
            "disk_total_bytes": target.get("maxdisk", 0),
        }

        # Track sub-request failures so a running VM whose node API errored is
        # not indistinguishable from a stopped VM with no stats.
        degraded = False

        # Add live stats if available
        if "error" in status_data:
            result["status_error"] = status_data
            degraded = True
        else:
            sd = status_data.get("data", {})
            result.update(
                {
                    "cpu_percent": round(sd.get("cpu", 0) * 100, 1),
                    "ram_used_bytes": sd.get("mem", 0),
                    "ram_percent": round(
                        (sd.get("mem", 0) / max(sd.get("maxmem", 1), 1)) * 100, 1
                    ),
                    "uptime_seconds": sd.get("uptime", 0),
                    "netin_bytes": sd.get("netin", 0),
                    "netout_bytes": sd.get("netout", 0),
                    "diskread_bytes": sd.get("diskread", 0),
                    "diskwrite_bytes": sd.get("diskwrite", 0),
                }
            )

        # Add config if available
        if "error" in config_data:
            result["config_error"] = config_data
            degraded = True
        else:
            cd = config_data.get("data", {})
            config_fields = {}
            for key in (
                "ostype",
                "boot",
                "cores",
                "memory",
                "hostname",
                "net0",
                "rootfs",
                "scsi0",
            ):
                if key in cd:
                    config_fields[key] = cd[key]
            if config_fields:
                result["config"] = config_fields

        result["_meta"] = build_meta(
            "proxmox", confidence="medium" if degraded else "high"
        )
        return result
