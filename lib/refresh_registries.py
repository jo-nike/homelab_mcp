"""Registry refresh: fetch live service/host data from upstream APIs and merge.

Split from the former monolithic lib/refresh.py (the other half is
lib/refresh_content.py); lib/refresh.py now just composes the two in
periodic_refresh.
"""

import asyncio
import logging
import time
from typing import Any

import config
from lib.gitea import paginate_repos
from lib.hosts import resolve_host
from lib.redact import redact_exception
from lib.scanopy import extract_ips, extract_macs
from lib.wireguard import is_connected

_logger = logging.getLogger(__name__)


_DNS_MAX_ZONES = 10


def _endpoint_ip(ep_name: str) -> str:
    """Resolve a Portainer endpoint name to its canonical host's LAN IP.

    Replaces a hand-maintained name->IP table with the hosts.yaml alias layer,
    so a newly added/renamed endpoint no longer needs a code edit.
    """
    canonical = resolve_host(ep_name, "portainer")
    if not canonical:
        return ""
    return config.HOSTS.get(canonical, {}).get("ip", "")


async def _safe_get(
    client, path: str, params: dict | None = None, timeout: float = 10.0
) -> Any:
    """Safe GET that works with both httpx.AsyncClient and SessionAuthManager.

    Both return an ``httpx.Response``, so there is no non-Response branch.
    """
    try:
        resp = await client.get(path, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _logger.warning("GET %s failed: %s", path, redact_exception(e))
        return None


# --- Source fetchers ---


async def _fetch_portainer_containers(clients: dict) -> list[dict]:
    """Fetch containers from all Portainer endpoints with mcp.* labels."""
    client = clients.get("portainer")
    if client is None:
        return []
    try:
        endpoints = await _safe_get(client, "/api/endpoints")
        if not endpoints:
            return []
        containers = []
        for ep in endpoints:
            if ep.get("Status") != 1:
                continue
            ep_id = ep.get("Id")
            ep_name = ep.get("Name", "")
            ep_ip = _endpoint_ip(ep_name)
            raw = await _safe_get(
                client,
                f"/api/endpoints/{ep_id}/docker/containers/json",
                params={"all": "true"},
            )
            if not raw:
                continue
            for c in raw:
                names = c.get("Names", [])
                name = names[0].lstrip("/") if names else ""
                if name.startswith("GITEA-ACTIONS-TASK-"):
                    continue
                labels = c.get("Labels") or {}
                ports = sorted(
                    {
                        p.get("PublicPort")
                        for p in (c.get("Ports") or [])
                        if p.get("PublicPort")
                    }
                )
                containers.append(
                    {
                        "name": name,
                        # Canonical host name so a discovered service joins with
                        # the per-host tools, not Portainer's endpoint dialect.
                        "host": resolve_host(ep_name, "portainer") or ep_name,
                        "ip": ep_ip,
                        "status": c.get("State", ""),
                        "image": c.get("Image", ""),
                        "ports": ports,
                        "mcp_role": labels.get("mcp.role", ""),
                        "mcp_auth": labels.get("mcp.auth", ""),
                    }
                )
        return containers
    except Exception as e:
        _logger.warning("Portainer fetch failed: %s", e)
        return []


async def _fetch_proxmox_vms(clients: dict) -> list[dict]:
    """Fetch VMs/CTs/nodes from Proxmox cluster resources."""
    client = clients.get("proxmox")
    if client is None:
        return []
    try:
        data = await _safe_get(client, "/api2/json/cluster/resources")
        if not data:
            return []
        items = data.get("data", []) if isinstance(data, dict) else []
        results = []
        for item in items:
            rtype = item.get("type", "")
            if rtype not in ("qemu", "lxc", "node"):
                continue
            type_map = {"qemu": "vm", "lxc": "ct", "node": "node"}
            entry = {
                "name": item.get("name", ""),
                "type": type_map[rtype],
                "status": item.get("status", ""),
                "node": item.get("node", ""),
            }
            if rtype in ("qemu", "lxc"):
                entry["vmid"] = item.get("vmid")
                entry["cpu_cores"] = item.get("maxcpu")
                entry["ram_total_bytes"] = item.get("maxmem")
            results.append(entry)
        return results
    except Exception as e:
        _logger.warning("Proxmox fetch failed: %s", e)
        return []


async def _fetch_dns_records(clients: dict) -> list[dict]:
    """Fetch A/CNAME records from Technitium DNS zones."""
    client = clients.get("technitium")
    if client is None:
        return []
    try:
        zone_resp = await _safe_get(
            client,
            "/api/zones/list",
            params={"pageNumber": 1, "zonesPerPage": _DNS_MAX_ZONES},
        )
        if not zone_resp:
            return []
        resp_data = zone_resp.get("response", {}) if isinstance(zone_resp, dict) else {}
        zones = resp_data.get("zones", [])
        if len(zones) > _DNS_MAX_ZONES:
            _logger.warning(
                "DNS refresh processing first %d of %d zones",
                _DNS_MAX_ZONES,
                len(zones),
            )
        records = []
        for zone in zones[:_DNS_MAX_ZONES]:
            zone_name = zone.get("name", "")
            if not zone_name:
                continue
            rec_resp = await _safe_get(
                client,
                "/api/zones/records/get",
                params={"domain": zone_name, "listZone": "true"},
            )
            if not rec_resp:
                continue
            rec_data = (
                rec_resp.get("response", {}) if isinstance(rec_resp, dict) else {}
            )
            for rec in rec_data.get("records", []):
                rtype = rec.get("type", "")
                if rtype not in ("A", "CNAME"):
                    continue
                rdata = rec.get("rData", {})
                value = (
                    rdata.get("ipAddress", "")
                    if rtype == "A"
                    else rdata.get("cname", "")
                )
                records.append(
                    {
                        "domain": rec.get("name", ""),
                        "type": rtype,
                        "value": value,
                    }
                )
        return records
    except Exception as e:
        _logger.warning("DNS fetch failed: %s", e)
        return []


async def _fetch_npm_hosts(clients: dict) -> list[dict]:
    """Fetch proxy hosts from Nginx Proxy Manager."""
    client = clients.get("npm")
    if client is None:
        return []
    try:
        data = await _safe_get(client, "/api/nginx/proxy-hosts")
        if not data:
            return []
        results = []
        for proxy in data:
            domains = proxy.get("domain_names", [])
            results.append(
                {
                    "domain": domains[0] if domains else "",
                    "forward_host": proxy.get("forward_host", ""),
                    "forward_port": proxy.get("forward_port", 0),
                    "ssl": bool(proxy.get("ssl_forced", False)),
                }
            )
        return results
    except Exception as e:
        _logger.warning("NPM fetch failed: %s", e)
        return []


async def _fetch_scanopy_hosts(clients: dict) -> list[dict]:
    """Fetch discovered hosts from Scanopy network scanner."""
    client = clients.get("scanopy")
    if client is None:
        return []
    try:
        data = await _safe_get(client, "/api/v1/hosts", params={"limit": 500})
        if not data:
            return []
        items = data.get("data", []) if isinstance(data, dict) else data
        results = []
        for host in items:
            # Real schema: IPs/MACs live on ip_addresses[*] (see lib/scanopy).
            # The old code stringified an address dict, so `ip` became
            # "{'addr': ...}" and every merge/filter silently failed.
            ips = extract_ips(host)
            macs = extract_macs(host)
            ip = ips[0] if ips else ""
            if ip.startswith("172."):
                continue
            results.append(
                {
                    "ip": ip,
                    "hostname": host.get("hostname", "") or host.get("name", ""),
                    "id": host.get("id", ""),
                    "mac": macs[0] if macs else "",
                    "last_seen": host.get("updated_at", ""),
                }
            )
        return results
    except Exception as e:
        _logger.warning("Scanopy fetch failed: %s", e)
        return []


async def _fetch_wireguard_peers(clients: dict) -> list[dict]:
    """Fetch VPN peers from WireGuard (wg-easy)."""
    client = clients.get("wireguard")
    if client is None:
        return []
    try:
        data = await _safe_get(client, "/api/client")
        if not data:
            return []
        results = []
        for peer in data:
            handshake = peer.get("latestHandshakeAt")
            results.append(
                {
                    "name": peer.get("name", ""),
                    "address": peer.get("address") or peer.get("ipv4Address", ""),
                    "enabled": peer.get("enabled", True),
                    "connected": is_connected(
                        handshake if isinstance(handshake, str) else None
                    ),
                }
            )
        return results
    except Exception as e:
        _logger.warning("WireGuard fetch failed: %s", e)
        return []


async def _fetch_healthchecks(clients: dict) -> list[dict]:
    """Fetch check statuses from Healthchecks."""
    client = clients.get("healthchecks")
    if client is None:
        return []
    try:
        data = await _safe_get(client, "/api/v3/checks/")
        if not data:
            return []
        checks = data.get("checks", data) if isinstance(data, dict) else data
        return [
            {
                "name": c.get("name", ""),
                "status": c.get("status", ""),
                "last_ping": c.get("last_ping", ""),
            }
            for c in checks
        ]
    except Exception as e:
        _logger.warning("Healthchecks fetch failed: %s", e)
        return []


async def _fetch_gitea_repos(clients: dict) -> list[dict]:
    """Fetch repos from Gitea with pagination (capped at 5 pages)."""
    client = clients.get("gitea")
    if client is None:
        return []
    try:
        raw = await paginate_repos(
            lambda params: _safe_get(client, "/api/v1/repos/search", params),
            max_pages=5,
        )
        if isinstance(raw, dict):  # _safe_get yields None (not error dicts) on failure
            return []
        return [
            {
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "description": r.get("description", ""),
                "updated_at": r.get("updated_at", ""),
            }
            for r in raw
        ]
    except Exception as e:
        _logger.warning("Gitea fetch failed: %s", e)
        return []


async def _fetch_all_sources(clients: dict) -> dict:
    """Fetch live data from all configured API sources in parallel.

    Each fetcher owns its own error handling and returns ``[]`` on failure, so
    ``gather`` here never sees an exception -- no return_exceptions guard needed.
    """
    results = await asyncio.gather(
        _fetch_portainer_containers(clients),
        _fetch_proxmox_vms(clients),
        _fetch_dns_records(clients),
        _fetch_npm_hosts(clients),
        _fetch_scanopy_hosts(clients),
        _fetch_wireguard_peers(clients),
        _fetch_healthchecks(clients),
        _fetch_gitea_repos(clients),
    )
    keys = [
        "portainer_containers",
        "proxmox_vms",
        "dns_records",
        "npm_hosts",
        "scanopy_hosts",
        "wireguard_peers",
        "healthchecks",
        "gitea_repos",
    ]
    return dict(zip(keys, results, strict=False))


def _merge_services(seed: dict, live: dict) -> dict:
    """Deep-copy seed services and overlay dynamic fields from live data.

    Per D-03: YAML role/auth are never overridden unless mcp.* labels are present.
    """
    merged = {k: dict(v) for k, v in seed.items()}

    # Overlay Portainer containers
    for container in live.get("portainer_containers", []):
        name = container.get("name", "").lower()
        # Match by name to seed
        matched_key = None
        for key in merged:
            if key.lower() == name:
                matched_key = key
                break
        if matched_key:
            merged[matched_key]["status"] = container.get("status", "")
            if container.get("ports"):
                merged[matched_key]["ports"] = container["ports"]
            # Only override role/auth if mcp.* labels present (D-03)
            if container.get("mcp_role"):
                merged[matched_key]["role"] = container["mcp_role"]
            if container.get("mcp_auth"):
                merged[matched_key]["auth"] = container["mcp_auth"]
        else:
            # New container not in seed -- create auto-discovered entry
            merged[name] = {
                "name": container.get("name", name),
                "host": container.get("host", ""),
                "status": container.get("status", ""),
                "image": container.get("image", ""),
                "ports": container.get("ports", []),
                "role": container.get("mcp_role", ""),
                "auth": container.get("mcp_auth", "unknown"),
                "ip": container.get("ip", ""),
                "source": "portainer-discovery",
            }

    # Overlay DNS records
    for record in live.get("dns_records", []):
        if record.get("type") == "A":
            domain = record.get("domain", "")
            ip = record.get("value", "")
            for svc in merged.values():
                if svc.get("domain") == domain and ip:
                    svc["ip"] = ip

    # Overlay NPM hosts
    for proxy in live.get("npm_hosts", []):
        domain = proxy.get("domain", "")
        for svc in merged.values():
            if not svc.get("domain") and svc.get("name", "").lower() in domain.lower():
                svc["domain"] = domain

    # Overlay Healthcheck statuses
    for check in live.get("healthchecks", []):
        check_name = check.get("name", "").lower()
        for key, svc in merged.items():
            if key.lower() == check_name or svc.get("name", "").lower() == check_name:
                svc["healthcheck_status"] = check.get("status", "")
                break

    # Overlay Gitea repos
    for repo in live.get("gitea_repos", []):
        repo_name = repo.get("name", "").lower()
        for key, svc in merged.items():
            if key.lower() == repo_name:
                svc["repo"] = repo.get("full_name", "")
                break

    return merged


def _merge_hosts(seed: dict, live: dict) -> dict:
    """Deep-copy seed hosts and overlay dynamic fields from live data."""
    merged = {k: dict(v) for k, v in seed.items()}

    # Overlay Proxmox node status
    for entry in live.get("proxmox_vms", []):
        if entry.get("type") == "node":
            name = entry.get("name", "")
            for key, host in merged.items():
                if key.lower() == name.lower() or host.get("proxmox_node") == name:
                    host["proxmox_status"] = entry.get("status", "")
                    break

    # Overlay Scanopy discovered hosts
    for host_data in live.get("scanopy_hosts", []):
        ip = host_data.get("ip", "")
        found = False
        for host in merged.values():
            if host.get("ip") == ip:
                host["last_seen"] = host_data.get("last_seen", "")
                found = True
                break
        if not found and ip:
            hostname = host_data.get("hostname") or f"discovered-{ip}"
            merged[hostname] = {
                "name": hostname,
                "ip": ip,
                "role": "discovered",
                "source": "scanopy",
                "mac": host_data.get("mac", ""),
                "last_seen": host_data.get("last_seen", ""),
            }

    # Overlay WireGuard peer status
    for peer in live.get("wireguard_peers", []):
        peer_ip = peer.get("address", "").split("/")[0] if peer.get("address") else ""
        for host in merged.values():
            if host.get("ip") == peer_ip or host.get("vpn_ip") == peer_ip:
                host["vpn_connected"] = peer.get("connected", False)
                break

    return merged


def _compute_diff(
    old_services: dict, new_services: dict, old_hosts: dict, new_hosts: dict
) -> dict:
    """Compute set-based diff between old and new registries."""
    old_svc_keys = set(old_services.keys())
    new_svc_keys = set(new_services.keys())
    old_host_keys = set(old_hosts.keys())
    new_host_keys = set(new_hosts.keys())

    # Count updated: keys in both but with different content
    svc_updated = sum(
        1 for k in old_svc_keys & new_svc_keys if old_services[k] != new_services[k]
    )
    host_updated = sum(
        1 for k in old_host_keys & new_host_keys if old_hosts[k] != new_hosts[k]
    )

    return {
        "services_added": sorted(new_svc_keys - old_svc_keys),
        "services_removed": sorted(old_svc_keys - new_svc_keys),
        "services_updated": svc_updated,
        "hosts_added": sorted(new_host_keys - old_host_keys),
        "hosts_removed": sorted(old_host_keys - new_host_keys),
        "hosts_updated": host_updated,
        "total_services": len(new_services),
        "total_hosts": len(new_hosts),
    }


async def refresh_registries_impl(clients: dict) -> dict:
    """Refresh service and host registries from live API sources.

    Returns diff summary of what changed.
    """
    # Load YAML seed
    seed_services = config.load_services()
    seed_hosts = config.load_hosts()

    # Fetch live data from all sources
    live = await _fetch_all_sources(clients)

    # Merge seed + live
    new_services = _merge_services(seed_services, live)
    new_hosts = _merge_hosts(seed_hosts, live)

    # Compute diff BEFORE swap
    diff = _compute_diff(config.SERVICES, new_services, config.HOSTS, new_hosts)

    # Atomic swap
    config.SERVICES.clear()
    config.SERVICES.update(new_services)
    config.HOSTS.clear()
    config.HOSTS.update(new_hosts)
    config.IP_INDEX.clear()
    config.IP_INDEX.update(config.build_ip_index())

    # Update timestamp
    config.REFRESH_TIMESTAMPS["registries"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )

    _logger.info("Registry refresh: %s", diff)
    return diff
