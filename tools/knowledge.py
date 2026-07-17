"""Knowledge lookup tools for homelab service, host, and documentation search."""

import difflib
from typing import Annotated

from fastmcp import Context

import config
from lib.meta import build_meta


def _suggest(name: str, keys) -> list[str]:
    """Return up to 10 candidate keys closest to `name` for a not-found error.

    Combines substring matches with difflib fuzzy matches. The full key list is
    deliberately not returned: after Portainer/Scanopy auto-discovery these
    registries can hold hundreds of entries, which would bloat the error string
    the small local LLMs this server targets have to read.
    """
    keys = list(keys)
    lowered = name.lower()
    substrings = [k for k in keys if lowered in k.lower() or k.lower() in lowered]
    close = difflib.get_close_matches(name, keys, n=10, cutoff=0.4)
    seen: list[str] = []
    for k in substrings + close:
        if k not in seen:
            seen.append(k)
    return seen[:10]


def register(mcp):
    """Register knowledge tools. Always available (no external credentials needed)."""

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def search_docs(
        ctx: Context,
        query: Annotated[str, "Search term to find in homelab documentation"],
        max_results: Annotated[int, "Maximum results to return. Default 10."] = 10,
    ) -> dict:
        """Full-text search across all homelab documentation. Returns matching paragraphs with source file and section."""
        # On a fresh clone data/ is unsynced and DOCS_INDEX is empty until a
        # Gitea refresh succeeds. Distinguish 'no docs loaded' from 'no match'
        # so an LLM does not conclude the topic is undocumented.
        if not config.DOCS_INDEX:
            return {
                "results": [],
                "query": query,
                "total_matches": 0,
                "matches_returned": 0,
                "message": "no documentation loaded; run refresh_docs",
                "_meta": build_meta("knowledge", confidence="medium"),
            }

        query_lower = query.lower()
        results = []
        total_matches = 0

        for filename, doc in config.DOCS_INDEX.items():
            for section in doc.get("sections", []):
                text = section.get("text", "")
                if query_lower in text.lower():
                    total_matches += 1
                    if len(results) >= max_results:
                        continue

                    # Extract the paragraph containing the match
                    paragraphs = text.split("\n\n")
                    snippet = None
                    for para in paragraphs:
                        if query_lower in para.lower():
                            snippet = para.strip()
                            break
                    if snippet is None:
                        snippet = text[:200].strip()

                    results.append(
                        {
                            "file": filename,
                            "section": section.get("heading", ""),
                            "snippet": snippet,
                        }
                    )

        return {
            "results": results,
            "query": query,
            # total_matches is now the true count across all docs; matches_returned
            # reflects the (possibly capped) number actually included.
            "total_matches": total_matches,
            "matches_returned": len(results),
            "_meta": build_meta("knowledge"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_service_info(
        ctx: Context,
        name: Annotated[
            str, "Service name (e.g. 'prometheus', 'plex', 'grafana', 'sonarr')"
        ],
    ) -> dict:
        """Look up a service by name. Returns host, port, stack, domain, role, and co-located services."""
        svc = config.SERVICES.get(name.lower())

        # Try fuzzy match if exact match fails
        if svc is None:
            for svc_name, svc_data in config.SERVICES.items():
                if name.lower() in svc_name.lower():
                    svc = svc_data
                    break

        if svc is None:
            return {
                "error": "not_found",
                "message": f"Service '{name}' not found. Closest matches: {_suggest(name, config.SERVICES.keys())}",
            }

        # Cross-reference: find co-located services on same IP (D-06)
        ip = svc.get("ip")
        co_located = []
        if ip and ip in config.IP_INDEX:
            co_located = [
                s["name"]
                for s in config.IP_INDEX[ip].get("services", [])
                if s["name"] != svc["name"]
            ]

        return {
            "name": svc["name"],
            "host": svc.get("host", ""),
            "ip": svc.get("ip", ""),
            "port": svc.get("port"),
            "stack": svc.get("stack"),
            "domain": svc.get("domain"),
            "role": svc.get("role", ""),
            "auth": svc.get("auth", "unknown"),
            "co_located_services": co_located,
            "_meta": build_meta("knowledge"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_host_info(
        ctx: Context,
        name_or_ip: Annotated[
            str,
            "Hostname (e.g. 'beast', 'docker-host') or IP address (e.g. '192.168.1.79')",
        ],
    ) -> dict:
        """Look up a host by name or IP. Returns specs, role, OS, and all services running on it."""
        host = config.HOSTS.get(name_or_ip.lower())

        # If not found by name, try by IP
        if host is None:
            ip_entry = config.IP_INDEX.get(name_or_ip)
            if ip_entry and ip_entry.get("host"):
                host = ip_entry["host"]

        if host is None:
            return {
                "error": "not_found",
                "message": f"Host '{name_or_ip}' not found. Closest matches: {_suggest(name_or_ip, config.HOSTS.keys())}",
            }

        # Cross-reference: get all services on this host's IP
        ip = host.get("ip", "")
        services = []
        if ip and ip in config.IP_INDEX:
            services = [
                {"name": s["name"], "port": s.get("port"), "role": s.get("role", "")}
                for s in config.IP_INDEX[ip].get("services", [])
            ]

        return {
            "name": host["name"],
            "ip": ip,
            "role": host.get("role", ""),
            "os": host.get("os", ""),
            "specs": host.get("specs", {}),
            "services": services,
            "_meta": build_meta("knowledge"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_ip_info(
        ctx: Context,
        ip: Annotated[
            str, "IP address to look up (e.g. '192.168.1.79' or '.79' shorthand)"
        ],
    ) -> dict:
        """Reverse-lookup an IP address. Returns the host and all services at that IP."""
        # Expand shorthand: ".79" -> "192.168.1.79"
        if ip.startswith("."):
            ip = "192.168.1" + ip

        entry = config.IP_INDEX.get(ip)
        if entry is None:
            return {
                "error": "not_found",
                "message": f"IP '{ip}' not found. Closest matches: {_suggest(ip, config.IP_INDEX.keys())}",
            }

        host_info = entry.get("host")
        services = [
            {"name": s["name"], "port": s.get("port"), "role": s.get("role", "")}
            for s in entry.get("services", [])
        ]

        return {
            "ip": ip,
            "host": host_info,
            "services": services,
            "_meta": build_meta("knowledge"),
        }
