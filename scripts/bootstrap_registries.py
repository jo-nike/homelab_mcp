"""Bootstrap the knowledge registries (data/*.yaml) from live APIs.

Queries whichever upstream services are configured in your .env (Proxmox,
Portainer, Scanopy, NPM, Technitium DNS, Prometheus, ...) and writes skeleton
hosts.yaml / services.yaml / baselines.yaml / topology.yaml with discovered
data, plus TODO stubs for the fields only a human knows: role prose, the
cross-service alias map, logical dependencies, and the critical-containers
safety list.

The refresh loop never writes these files -- they are read-only seeds -- so
regenerating them is always safe from the server's point of view.

Usage:
    uv run scripts/bootstrap_registries.py [--dry-run] [--force] [--out DIR]
                                           [--only hosts ...]
"""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import config  # noqa: E402
from lib.clients import create_clients  # noqa: E402
from lib.refresh_registries import _fetch_all_sources, _safe_get  # noqa: E402

# Client keys the bootstrap needs; everything else (media, tasks, ...) carries
# no host/service inventory.
NEEDED_CLIENTS = {
    "proxmox",
    "portainer",
    "scanopy",
    "npm",
    "technitium",
    "wireguard",
    "healthchecks",
    "gitea",
    "prometheus",
}

TODO_ROLE = "TODO: describe this host's purpose"
TODO_SERVICE_ROLE = "TODO: what does this service do?"
TODO_AUTH = "TODO: auth scheme (or add an mcp.auth label to the container)"

# Generic node_exporter/nvidia_smi PromQL; works on any Prometheus setup.
METRIC_QUERIES = {
    "cpu_percent": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "ram_percent": "(1 - node_memory_MemAvailable_bytes{} / node_memory_MemTotal_bytes) * 100",
    "disk_percent": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
    "gpu_vram_percent": "nvidia_smi_memory_used_bytes{} / nvidia_smi_memory_total_bytes * 100",
}


# --- Live data gathering ---


async def fetch_prometheus_instances(clients: dict) -> list[str]:
    """Return distinct Prometheus `instance` label values, ports stripped."""
    client = clients.get("prometheus")
    if client is None:
        return []
    data = await _safe_get(client, "/api/v1/label/instance/values")
    if not isinstance(data, dict):
        return []
    values = data.get("data") or []
    return sorted({str(v).rsplit(":", 1)[0] for v in values if v})


async def gather_live() -> tuple[dict, list[str]]:
    """Fetch inventory from every configured source; returns (live, sources)."""
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack, only=NEEDED_CLIENTS)
        live = await _fetch_all_sources(clients)
        live["prometheus_instances"] = await fetch_prometheus_instances(clients)
        return live, sorted(clients)


# --- Builders (pure: live dict in, registry structures out) ---


def _norm(name: str) -> str:
    """Normalize an upstream id for cross-source matching."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "")


def _match(name: str, candidates: list[str]) -> str | None:
    """Return the candidate whose normalized form equals `name`'s, if any."""
    target = _norm(name)
    for cand in candidates:
        if _norm(cand) == target:
            return cand
    return None


def build_hosts(live: dict) -> tuple[list[dict], dict[str, list[str]]]:
    """Build hosts.yaml skeleton entries from Proxmox + Scanopy inventories,
    pre-filling aliases by normalized name match against Portainer endpoints
    and Prometheus instances. Returns (hosts, unmatched-ids-per-source)."""
    hosts: list[dict] = []
    by_norm: dict[str, dict] = {}

    def add(name: str, **fields: Any) -> dict:
        host = {"name": name, "ip": "", "role": TODO_ROLE}
        host.update(fields)
        hosts.append(host)
        by_norm[_norm(name)] = host
        return host

    proxmox_vms = live.get("proxmox_vms") or []
    for item in proxmox_vms:
        # cluster/resources node entries carry the id in `node`, not `name`.
        node_name = item.get("name") or item.get("node")
        if item.get("type") == "node" and node_name:
            add(
                node_name,
                proxmox_node=node_name,
                aliases={"proxmox": node_name},
            )
    for item in proxmox_vms:
        if item.get("type") in ("vm", "ct") and item.get("name"):
            host = add(item["name"], aliases={"proxmox": item["name"]})
            node = _match(item.get("node", ""), [h["name"] for h in hosts])
            if node:
                host["parent"] = node
            specs = {}
            if item.get("cpu_cores"):
                specs["cpu"] = f"{item['cpu_cores']} vCPU"
            if item.get("ram_total_bytes"):
                specs["ram"] = f"{round(item['ram_total_bytes'] / 1024**3)} GB"
            if specs:
                host["specs"] = specs

    # Scanopy: fills IPs/MACs on known hosts, creates entries for the rest.
    for scanned in live.get("scanopy_hosts") or []:
        name = scanned.get("hostname", "")
        ip = scanned.get("ip", "")
        host = by_norm.get(_norm(name)) if name else None
        if host is None and name:
            host = add(name)
        if host is not None:
            if ip and not host.get("ip"):
                host["ip"] = ip
            if scanned.get("mac"):
                host.setdefault("mac", scanned["mac"])

    # Alias pre-fill: exact-after-normalization matches only; anything else is
    # reported, never guessed (an alias wrong in hosts.yaml silently breaks
    # every cross-tool join).
    unmatched: dict[str, list[str]] = {"portainer": [], "prometheus": []}
    endpoint_names = sorted(
        {
            c.get("host", "")
            for c in live.get("portainer_containers") or []
            if c.get("host")
        }
    )
    for ep_name in endpoint_names:
        host = by_norm.get(_norm(ep_name))
        if host is not None:
            host.setdefault("aliases", {})["portainer"] = ep_name
        else:
            unmatched["portainer"].append(ep_name)
    for instance in live.get("prometheus_instances") or []:
        host = by_norm.get(_norm(instance))
        if host is not None:
            host.setdefault("aliases", {}).setdefault("prometheus", [])
            host["aliases"]["prometheus"].append(instance)
        else:
            unmatched["prometheus"].append(instance)

    return hosts, unmatched


def build_services(live: dict, hosts: list[dict] | None = None) -> list[dict]:
    """Build services.yaml skeleton entries: one per Portainer container,
    domain matched from NPM proxy hosts, role/auth from mcp.* labels.

    The fetcher's own endpoint IP resolution needs the runtime HOSTS registry
    (empty outside the server), so container IPs are backfilled here from the
    host entries this same bootstrap discovered.
    """
    services = []
    npm_hosts = live.get("npm_hosts") or []
    host_ips = {_norm(h["name"]): h.get("ip", "") for h in hosts or []}
    for c in sorted(
        live.get("portainer_containers") or [],
        key=lambda c: (c.get("host", ""), c.get("name", "")),
    ):
        name = c.get("name", "")
        if not name:
            continue
        domain = None
        for proxy in npm_hosts:
            if name.lower() in proxy.get("domain", "").lower():
                domain = proxy["domain"]
                break
        services.append(
            {
                "name": name,
                "host": c.get("host", ""),
                "ip": c.get("ip", "") or host_ips.get(_norm(c.get("host", "")), ""),
                "port": (c.get("ports") or [None])[0],
                "stack": None,
                "domain": domain,
                "role": c.get("mcp_role") or TODO_SERVICE_ROLE,
                "auth": c.get("mcp_auth") or TODO_AUTH,
                "image": c.get("image", ""),
            }
        )
    return services


def build_baselines(services: list[dict], live: dict) -> dict[str, dict]:
    """Freeze the current running state as the expected baseline per host."""
    baselines: dict[str, dict] = {}
    running_by_host: dict[str, list[str]] = {}
    for c in live.get("portainer_containers") or []:
        if c.get("status") == "running" and c.get("host"):
            running_by_host.setdefault(c["host"], []).append(c.get("name", ""))
    for host, names in sorted(running_by_host.items()):
        baselines[host] = {
            "expected_container_count": len(names),
            "expected_services": sorted(n for n in names if n),
        }
    return baselines


def build_topology(hosts: list[dict], services: list[dict]) -> dict:
    """Build the topology skeleton: vertical stacks from the Proxmox parent
    tree; dependencies/storage/critical lists are human knowledge -> stubs."""
    services_by_host: dict[str, list[str]] = {}
    for svc in services:
        if svc.get("host"):
            services_by_host.setdefault(svc["host"], []).append(svc["name"])

    stacks = []
    for host in hosts:
        if host.get("parent"):
            continue
        children = [
            {
                "type": "vm",
                "name": guest["name"],
                "ip": guest.get("ip", ""),
                "services": sorted(services_by_host.get(guest["name"], [])),
            }
            for guest in hosts
            if guest.get("parent") == host["name"]
        ]
        own_services = sorted(services_by_host.get(host["name"], []))
        if children or own_services:
            stacks.append(
                {
                    "host": host["name"],
                    "ip": host.get("ip", ""),
                    "children": children
                    or [
                        {
                            "type": "service",
                            "name": host["name"],
                            "services": own_services,
                        }
                    ],
                }
            )
    return {
        "vertical_stacks": stacks,
        "ingress": [],
        "dependencies": [],
        "storage_mounts": [],
        "critical_containers": [],
    }


# --- YAML rendering (hand-built emitters: pyyaml cannot write comments) ---


def _s(value: Any) -> str:
    """Render a scalar as a YAML flow value (JSON is a YAML subset)."""
    if value is None:
        return "null"
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    return json.dumps(str(value))


def _emit_mapping(lines: list[str], data: dict, indent: str) -> None:
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            _emit_mapping(lines, value, indent + "  ")
        elif isinstance(value, list):
            lines.append(f"{indent}{key}: [{', '.join(_s(v) for v in value)}]")
        else:
            lines.append(f"{indent}{key}: {_s(value)}")


def _emit_entry_list(lines: list[str], entries: list[dict], indent: str) -> None:
    for entry in entries:
        first = True
        for key, value in entry.items():
            prefix = f"{indent}- " if first else f"{indent}  "
            if isinstance(value, dict):
                lines.append(f"{prefix}{key}:")
                _emit_mapping(lines, value, indent + "    ")
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                lines.append(f"{prefix}{key}:")
                _emit_entry_list(lines, value, indent + "    ")
            elif isinstance(value, list):
                lines.append(f"{prefix}{key}: [{', '.join(_s(v) for v in value)}]")
            else:
                lines.append(f"{prefix}{key}: {_s(value)}")
            first = False
        if not entry:
            lines.append(f"{indent}- {{}}")


def render_hosts_yaml(hosts: list[dict], unmatched: dict[str, list[str]]) -> str:
    lines = [
        "# Canonical host names, generated by scripts/bootstrap_registries.py.",
        "# `name` is the one true id: services.yaml joins on it, and lib/hosts.py",
        "# resolves every upstream's own id back to it via `aliases`.",
        "#",
        "# TODO after generation:",
        "#   - replace every 'TODO' role with a one-line purpose description",
        "#   - verify/complete each host's `aliases` map (prometheus/proxmox/portainer)",
        "#   - add `os:` and physical `specs:` where useful",
        "hosts:",
    ]
    if not hosts:
        lines.append("  []  # no sources configured -- add hosts by hand")
    _emit_entry_list(lines, hosts, "  ")
    pending = {k: v for k, v in unmatched.items() if v}
    if pending:
        lines.append("")
        lines.append("# TODO: unmatched upstream ids -- each belongs to one of the")
        lines.append("# hosts above; add it under that host's `aliases:` map:")
        for source, names in sorted(pending.items()):
            for name in names:
                lines.append(f"#   {source}: {name}")
    return "\n".join(lines) + "\n"


def render_services_yaml(services: list[dict]) -> str:
    lines = [
        "# Service registry, generated by scripts/bootstrap_registries.py.",
        "# One entry per discovered container. `host` must match a hosts.yaml name.",
        "#",
        "# TODO after generation:",
        "#   - fill in role/auth (or add mcp.role / mcp.auth labels to containers",
        "#     and re-run -- labeled containers bootstrap themselves)",
        "#   - set `stack:` to the compose stack name where applicable",
        "#   - remove containers you don't want the MCP server to know about",
        "services:",
    ]
    if not services:
        lines.append("  []  # no sources configured -- add services by hand")
    _emit_entry_list(lines, services, "  ")
    return "\n".join(lines) + "\n"


def render_baselines_yaml(baselines: dict[str, dict]) -> str:
    lines = [
        "# Non-metric baselines: expected values for anomaly detection, generated",
        "# by scripts/bootstrap_registries.py from the current running state.",
        "# Used by is_this_normal and compare_to_baseline.",
        "#",
        "# TODO after generation: prune expected_services down to the containers",
        "# that MATTER per host (the ones whose absence is an incident).",
        "baselines:",
    ]
    if not baselines:
        lines.append("  {}  # no sources configured -- add baselines by hand")
    for host, data in baselines.items():
        lines.append(f"  {host}:")
        _emit_mapping(lines, data, "    ")
    lines += [
        "",
        "# Metric baselines are queried dynamically via PromQL (generic",
        "# node_exporter / nvidia_smi expressions; adjust to your exporters):",
        "metric_queries:",
    ]
    for key, query in METRIC_QUERIES.items():
        lines.append(f"  {key}: {_s(query)}")
    return "\n".join(lines) + "\n"


def render_topology_yaml(topology: dict) -> str:
    lines = [
        "# Entity graph: relationships between homelab entities, generated by",
        "# scripts/bootstrap_registries.py. Loaded into config.TOPOLOGY at startup.",
        "#",
        "# vertical_stacks below is discovered; the remaining sections are logical",
        "# knowledge no API exposes -- fill them in by hand:",
        "#   ingress:  [{domain: ..., proxy: ..., target: ...}]",
        "#   dependencies:  [{from: ..., to: [...]}]  # e.g. sonarr -> transmission",
        "#   storage_mounts:  [{source: ..., share: ..., consumers: [...]}]",
        "#   critical_containers:  containers MCP write tools must NEVER restart",
        "vertical_stacks:",
    ]
    if not topology.get("vertical_stacks"):
        lines.append("  []  # no sources configured -- add stacks by hand")
    _emit_entry_list(lines, topology.get("vertical_stacks", []), "  ")
    lines += [
        "",
        "ingress: []  # TODO",
        "",
        "dependencies: []  # TODO",
        "",
        "storage_mounts: []  # TODO",
        "",
        "critical_containers: []  # TODO: e.g. the MCP server's own container",
    ]
    return "\n".join(lines) + "\n"


# --- Output ---

RENDERERS = ["hosts", "services", "baselines", "topology"]


def render_all(live: dict, only: list[str] | None = None) -> dict[str, str]:
    """Run builders + renderers; returns {filename: yaml_text}."""
    selected = only or RENDERERS
    hosts, unmatched = build_hosts(live)
    services = build_services(live, hosts)
    rendered = {}
    if "hosts" in selected:
        rendered["hosts.yaml"] = render_hosts_yaml(hosts, unmatched)
    if "services" in selected:
        rendered["services.yaml"] = render_services_yaml(services)
    if "baselines" in selected:
        rendered["baselines.yaml"] = render_baselines_yaml(
            build_baselines(services, live)
        )
    if "topology" in selected:
        rendered["topology.yaml"] = render_topology_yaml(
            build_topology(hosts, services)
        )
    # Every rendered file must parse back to the shape config.load_* expects.
    for filename, text in rendered.items():
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict), f"{filename} did not render a mapping"
    return rendered


def write_files(out_dir: Path, rendered: dict[str, str], force: bool) -> list[Path]:
    existing = [f for f in rendered if (out_dir / f).exists()]
    if existing and not force:
        raise SystemExit(
            f"refusing to overwrite {', '.join(sorted(existing))} in {out_dir} "
            "(pass --force to allow)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, text in rendered.items():
        path = out_dir / filename
        path.write_text(text)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=config.DATA_DIR,
        help="output directory (default: data/)",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing YAML files"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print to stdout, write nothing"
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=RENDERERS,
        help="generate only these files (repeatable; default: all)",
    )
    args = parser.parse_args(argv)

    live, sources = asyncio.run(gather_live())
    print(
        f"configured sources: {', '.join(sources) or 'none'}",
        file=sys.stderr,
    )
    for key in sorted(k for k in live if isinstance(live[k], list)):
        print(f"  {key}: {len(live[key])} items", file=sys.stderr)
    if not any(live[k] for k in live):
        print(
            "no live data found -- writing pure TODO templates "
            "(configure services in .env and re-run to auto-fill)",
            file=sys.stderr,
        )

    rendered = render_all(live, args.only)
    if args.dry_run:
        for filename, text in rendered.items():
            print(f"# ===== {filename} =====")
            print(text)
        return 0
    written = write_files(args.out, rendered, args.force)
    for path in written:
        print(f"wrote {path}", file=sys.stderr)
    print(
        "now: review the TODO markers, especially hosts.yaml `aliases` -- "
        "cross-tool joins depend on them",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
