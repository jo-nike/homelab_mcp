"""Shared PromQL host-metric query strings and label-matcher injection.

The node_cpu/node_memory/node_filesystem percentage expressions were repeated in
prometheus, health (twice), and aggregation, and had already drifted (only
prometheus carried the empty-selector fix on the ram query). ``inject_host_filter``
lived in prometheus and was imported by baselines. This owns both.
"""

# Canonical host-health queries. The ram query carries an empty selector ``{}``
# on its first metric so ``inject_matcher`` has a brace to target; without it a
# matcher would be appended after ``* 100`` and produce invalid PromQL. An empty
# ``{}`` is valid PromQL when no matcher is injected.
HOST_QUERIES = {
    "cpu_percent": (
        '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    ),
    "ram_percent": (
        "(1 - node_memory_MemAvailable_bytes{} / node_memory_MemTotal_bytes) * 100"
    ),
    "disk_percent": (
        '(1 - node_filesystem_avail_bytes{mountpoint="/"}'
        ' / node_filesystem_size_bytes{mountpoint="/"}) * 100'
    ),
    "load_1m": "node_load1",
    "uptime_seconds": "time() - node_boot_time_seconds",
}


def inject_matcher(query: str, matcher: str) -> str:
    """Inject a raw label ``matcher`` into the first selector of a PromQL query.

    ``matcher`` is a bare label expression like ``instance="beast"`` or
    ``instance=~"(a|b)(:.*)?"``. An empty selector (``metric{}``) receives the
    matcher with no leading comma; a query with no braces gets ``{matcher}``
    appended. Only the first metric is filtered -- PromQL binary-op label
    matching propagates the constraint to the other operand.
    """
    idx = query.find("}")
    if idx == -1:
        return f"{query}{{{matcher}}}"
    brace_open = query.rfind("{", 0, idx)
    body = query[brace_open + 1 : idx] if brace_open != -1 else ""
    sep = "" if body.strip() == "" else ","
    return query[:idx] + sep + matcher + query[idx:]


def inject_host_filter(query: str, host: str) -> str:
    """Inject an exact ``instance="<host>"`` matcher into a PromQL query.

    The host value comes from an LLM, so backslashes and double quotes are
    escaped before interpolation -- an unescaped quote would break out of the
    label matcher and produce invalid PromQL.
    """
    safe_host = host.replace("\\", "\\\\").replace('"', '\\"')
    return inject_matcher(query, f'instance="{safe_host}"')
