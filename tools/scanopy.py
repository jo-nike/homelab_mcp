"""Scanopy network topology tools for homelab MCP server."""

import ipaddress
from typing import Annotated

from fastmcp import Context

import config
from lib.hosts import resolve_host
from lib.http import service_request
from lib.meta import build_meta
from lib.scanopy import extract_ips, extract_macs

# Docker's default bridge/overlay address space (RFC1918 172.16.0.0/12). A bare
# `ip.startswith("172.")` also matched public 172.x ranges (e.g. Cloudflare
# 172.67.x) and any legitimate 172.16/12 LAN host.
_DOCKER_BRIDGE_NET = ipaddress.ip_network("172.16.0.0/12")


def _is_bridge_ip(ip: str) -> bool:
    """True if `ip` sits in Docker's default bridge address space."""
    try:
        return ipaddress.ip_address(ip) in _DOCKER_BRIDGE_NET
    except ValueError:
        return False


def register(mcp):
    """Register Scanopy tools. Skips if credentials are not configured."""
    if not config.SCANOPY_URL or not config.SCANOPY_API_KEY:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> dict:
        """Execute GET request against Scanopy API."""
        data = await service_request(
            ctx, "scanopy", path, params=params, display_name="Scanopy"
        )
        # Scanopy signals application-level failure via {"success": false}. The
        # error text lives in 'error' (not 'message'), so prefer it before
        # falling back, otherwise real errors surfaced as 'Unknown error'.
        if isinstance(data, dict) and data.get("success") is False:
            return {
                "error": "api_error",
                "message": data.get("error") or data.get("message") or "Unknown error",
            }
        return data

    # ---- Tool: get_network_topology (NETW-02 + NETW-03) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_network_topology(
        ctx: Context,
        host_filter: Annotated[
            str | None,
            "Optional hostname or IP substring to return a single host (or the "
            "few that match). Omit for the whole network.",
        ] = None,
    ) -> dict:
        """Get network topology from Scanopy showing all discovered hosts with their IPs, MACs, open ports (compact 'port/proto service' strings) and running services. Hosts on 172.x.x.x are flagged as Docker bridge containers. Pass host_filter to focus on one host."""
        data = await _get(ctx, "/api/v1/hosts", {"limit": 500})

        if isinstance(data, dict) and "error" in data:
            return data

        hosts_raw = data.get("data", []) if isinstance(data, dict) else []
        # The API paginates (default 50, max 1000); surface has_more so a
        # >500-host network is not silently under-reported.
        pagination = (
            data.get("meta", {}).get("pagination", {}) if isinstance(data, dict) else {}
        )
        truncated = bool(pagination.get("has_more"))

        lan_hosts = []
        bridge_hosts = []

        for h in hosts_raw:
            # Real schema: IPs and MACs live on ip_addresses[*]; interfaces is
            # empty in production. See lib/scanopy.py for the shared parser.
            ips = extract_ips(h)
            mac_addresses = extract_macs(h)

            # A host is a Docker bridge host only if all of its addresses fall
            # in the bridge space -- a multi-homed host that also has a real LAN
            # address (e.g. 192.168.1.x) is a real host, not a bridge.
            bridge_ips = [ip for ip in ips if _is_bridge_ip(ip)]
            docker_bridge = bool(bridge_ips) and len(bridge_ips) == len(ips)

            # Map service names onto ports via each service's bindings.
            port_service = {}
            for svc in h.get("services") or []:
                svc_name = svc.get("name") or svc.get("service_definition") or ""
                for binding in svc.get("bindings") or []:
                    port_id = binding.get("port_id")
                    if port_id and svc_name:
                        port_service.setdefault(port_id, svc_name)

            # Compact each port to a "number/proto service" string (was a
            # three-key dict per port -- a very large payload for small local
            # LLMs, ~59 ports on docker-host alone).
            ports = []
            for p in h.get("ports") or []:
                number = p.get("number")
                # API emits 'Tcp'/'Udp'; normalize to lowercase for the label.
                proto = (p.get("protocol") or "").lower()
                svc_name = port_service.get(p.get("id"), "")
                label = f"{number}/{proto}".rstrip("/")
                if svc_name:
                    label = f"{label} {svc_name}"
                ports.append(label)

            # The API returns hostname: null for undiscovered names; dict.get's
            # default only fires on a missing key, not a null value, so coalesce
            # with `or` and fall back to the API's 'name' field.
            hostname = h.get("hostname") or h.get("name") or ""
            host_entry = {
                "id": h.get("id"),
                "hostname": hostname,
                # Canonical host so Scanopy's dialect (AI, pve) joins with the
                # per-host tools; None when the scanner name maps to no known host.
                "host": resolve_host(hostname, "scanopy"),
                "ips": ips,
                "mac_addresses": mac_addresses,
                "docker_bridge": docker_bridge,
                "ports": ports,
                "port_count": len(ports),
                "services": sorted(set(port_service.values())),
                "first_seen": h.get("created_at", ""),
                "last_seen": h.get("updated_at", ""),
            }

            # Optional host filter: match hostname, canonical host, or any IP.
            if host_filter:
                needle = host_filter.lower()
                haystack = [hostname.lower(), (host_entry["host"] or "").lower()]
                haystack += [ip.lower() for ip in ips]
                if not any(needle in field for field in haystack if field):
                    continue

            if docker_bridge:
                bridge_hosts.append(host_entry)
            else:
                lan_hosts.append(host_entry)

        return {
            "hosts": lan_hosts,
            "docker_bridge_hosts": bridge_hosts,
            "lan_host_count": len(lan_hosts),
            "bridge_host_count": len(bridge_hosts),
            "total_host_count": len(lan_hosts) + len(bridge_hosts),
            "truncated": truncated,
            "_meta": build_meta("scanopy"),
        }
