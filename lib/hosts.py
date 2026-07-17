"""Canonical host-name resolution.

Every integration keys its results off its own upstream's id — Prometheus by
scrape `instance`, Proxmox by guest/node name, Portainer by endpoint name — and
those ids disagree with each other and with `data/hosts.yaml`'s canonical names
(`ai-vm-gpu`, `AI` and `docker host` are all the same iron under three names).
This is the alias layer that maps them back, so a tool can stamp a canonical
`host` on each entry and a consumer can join across tools without knowing any
upstream's dialect.

Reads the hosts.yaml SEED (config.load_hosts), deliberately NOT the live
config.HOSTS registry: lib/refresh.py injects Scanopy-discovered
`discovered-192.168.1.x` entries into HOSTS at runtime, so HOSTS is a superset
of the file and not guaranteed canonical. The seed is the source of truth for
what a host is *called*.

The parsed index is cached (the seed only changes when the file does, i.e. on
redeploy); tests that point config.DATA_DIR elsewhere call `_index.cache_clear()`.
"""

from functools import lru_cache

import config


@lru_cache(maxsize=1)
def _index() -> tuple[dict[tuple[str, str], str], dict[str, str], dict[str, str]]:
    """Parse the hosts.yaml seed into three lookup tables.

    Returns (by_kind, canonical, parents):
      by_kind    {(kind, alias.lower()): canonical_name} — one entry per alias.
      canonical  {name.lower(): canonical_name} — the names themselves.
      parents    {canonical_name: parent_name} — guests only; a host with no
                 `parent` key is absent (it IS the iron).
    """
    by_kind: dict[tuple[str, str], str] = {}
    canonical: dict[str, str] = {}
    parents: dict[str, str] = {}

    for name, host in config.load_hosts().items():
        canonical[name.lower()] = name
        parent = host.get("parent")
        if parent:
            parents[name] = parent
        for kind, raw in (host.get("aliases") or {}).items():
            for alias in raw if isinstance(raw, list) else [raw]:
                by_kind[(kind, str(alias).lower())] = name

    return by_kind, canonical, parents


def resolve_host(raw: str, kind: str | None = None) -> str | None:
    """Resolve an upstream's host id to its canonical name, or None if unknown.

    `kind` is the integration the id came from ("prometheus", "proxmox",
    "portainer"); its alias table is consulted first, then the canonical names
    themselves (an upstream that already uses the canonical name needs no
    alias). Without `kind`, every kind's aliases are searched — canonical names
    still win, so a name is never shadowed by another host's alias.

    Matching is case-insensitive throughout: Portainer's "Beast" and "VPS" are
    the same hosts as `beast` and `vps`.
    """
    if not raw:
        return None
    by_kind, canonical, _ = _index()
    key = str(raw).lower()

    if kind is not None:
        return by_kind.get((kind, key)) or canonical.get(key)

    if key in canonical:
        return canonical[key]
    for (_alias_kind, alias), name in by_kind.items():
        if alias == key:
            return name
    return None


def canonical_prometheus_host(instance: str) -> str | None:
    """Canonical host name for a Prometheus `instance` label.

    Instance labels often carry a `:port` suffix (e.g. `docker-host:9100`), which
    the alias table does not include, so strip it before resolving. Returns None
    when the instance maps to no known host.
    """
    if not instance:
        return None
    return resolve_host(str(instance).split(":")[0], "prometheus")


def prometheus_instances(raw: str) -> list[str]:
    """Prometheus scrape `instance` label values for a host.

    Resolves `raw` (any canonical name or upstream alias) to its canonical host,
    then returns that host's `prometheus` aliases from the hosts.yaml seed — the
    actual `instance` label values Prometheus scrapes it under (a host can have
    several jobs, e.g. ai-vm is scraped as both `ai-vm` and `ai-vm-gpu`).

    Returns [] when the host is unknown or carries no `prometheus` alias.
    Prometheus instance labels are hostnames, not IPs, so tools must filter
    queries by these values rather than by the host's IP address.
    """
    canonical = resolve_host(raw)
    if not canonical:
        return []
    host = config.load_hosts().get(canonical, {})
    aliases = (host.get("aliases") or {}).get("prometheus")
    if aliases is None:
        return []
    return [str(a) for a in (aliases if isinstance(aliases, list) else [aliases])]


def host_parent(name: str) -> str | None:
    """The canonical name of the physical machine a guest runs on, or None when
    the host has no parent (it is the iron) or isn't known at all."""
    if not name:
        return None
    _by_kind, _canonical, parents = _index()
    return parents.get(name)
