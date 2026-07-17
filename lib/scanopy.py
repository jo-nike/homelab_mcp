"""Shared parsing for the Scanopy network-scanner API.

The GET /api/v1/hosts response (captured live 2026-07-16) carries a host's
addresses in `ip_addresses[*]` — each entry has both `ip_address` and
`mac_address` — while the `interfaces` array is empty on this deployment.
An earlier parser read an imagined `interfaces[*].addresses[*].addr` shape and
produced empty results in production. These helpers read the real schema with a
defensive fallback to the old interfaces shape in case a future host populates
it.
"""


def extract_ips(host: dict) -> list[str]:
    """All IPv4/IPv6 addresses for a Scanopy host, in discovery order."""
    ips: list[str] = []
    for entry in host.get("ip_addresses") or []:
        ip = entry.get("ip_address")
        if ip and ip not in ips:
            ips.append(ip)
    # Fallback: the historical interfaces[*].addresses[*].addr shape.
    for iface in host.get("interfaces") or []:
        for addr in iface.get("addresses") or []:
            ip = addr.get("addr") if isinstance(addr, dict) else addr
            if ip and ip not in ips:
                ips.append(ip)
    return ips


def extract_macs(host: dict) -> list[str]:
    """All MAC addresses for a Scanopy host, de-duplicated in order."""
    macs: list[str] = []
    for entry in host.get("ip_addresses") or []:
        mac = entry.get("mac_address")
        if mac and mac not in macs:
            macs.append(mac)
    # Fallback: MACs lived on interfaces in the historical shape.
    for iface in host.get("interfaces") or []:
        mac = iface.get("mac_address") or iface.get("mac")
        if mac and mac not in macs:
            macs.append(mac)
    return macs
