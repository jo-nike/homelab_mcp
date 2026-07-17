"""Prometheus monitoring tools for homelab infrastructure."""

import asyncio
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.hosts import canonical_prometheus_host, host_parent, resolve_host
from lib.http import service_request
from lib.meta import build_meta
from lib.promql import HOST_QUERIES
from lib.promql import inject_host_filter as _inject_host_filter


def register(mcp):
    """Register Prometheus tools. Skips if PROMETHEUS_URL is not configured."""
    if not config.PROMETHEUS_URL:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Prometheus API."""
        return await service_request(
            ctx, "prometheus", path, params=params, display_name="Prometheus"
        )

    async def _instant_query(ctx: Context, query: str) -> dict:
        """Run an instant PromQL query and return parsed response."""
        return await _get(ctx, "/api/v1/query", {"query": query})

    def _extract_vector(response: dict) -> list[dict]:
        """Extract result list from a Prometheus vector response.

        Returns empty list if response contains an error or unexpected shape.
        """
        if "error" in response:
            return []
        try:
            return response["data"]["result"]
        except (KeyError, TypeError):
            return []

    def _meta_confidence(
        responses: list, results_present: bool
    ) -> tuple[str, dict | None]:
        """Grade a batch of query responses.

        Returns (confidence, fatal_error). `fatal_error` is the first error dict
        when every query errored (so the tool can propagate it instead of an
        empty 'healthy' result); otherwise None. Confidence is 'high' when no
        query errored, else 'medium'. Distinguishing a full outage from a quiet
        result is the whole point: an empty result with all-errors is 'monitoring
        is down', not 'no hosts'.
        """
        errors = [r for r in responses if isinstance(r, dict) and "error" in r]
        if responses and len(errors) == len(responses):
            return "medium", errors[0]
        return ("medium" if errors else "high"), None

    def _build_instance_map(results: list[dict]) -> dict[str, float]:
        """Build {instance: float_value} from Prometheus vector results."""
        out = {}
        for item in results:
            instance = item.get("metric", {}).get("instance", "unknown")
            try:
                out[instance] = round(float(item["value"][1]), 1)
            except (IndexError, ValueError, TypeError):
                continue
        return out

    # ---- Tool 1: get_host_summary (PROM-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_host_summary(
        ctx: Context,
        host: Annotated[
            str | None,
            "Instance name (e.g. 'beast', 'docker-host', 'plex-stack', "
            "'ai-vm', 'proxmox', 'cron-machine'). Omit for all hosts.",
        ] = None,
    ) -> dict:
        """Get host health: CPU%, RAM%, root disk%, 1m load average, and uptime for monitored hosts."""
        queries = dict(HOST_QUERIES)

        # Apply host filter if specified
        if host:
            filtered = {}
            for key, query in queries.items():
                filtered[key] = _inject_host_filter(query, host)
            queries = filtered

        # Execute all queries in parallel (independent instant queries).
        metrics: dict[str, Any] = {}
        metric_names = list(queries.keys())
        responses = await asyncio.gather(
            *[_instant_query(ctx, q) for q in queries.values()]
        )
        for metric_name, response in zip(metric_names, responses, strict=False):
            results = _extract_vector(response)
            instance_values = _build_instance_map(results)
            for instance, value in instance_values.items():
                if instance not in metrics:
                    metrics[instance] = {}
                metrics[instance][metric_name] = value

        # If every query errored, report the outage rather than 'no hosts'.
        confidence, fatal = _meta_confidence(responses, bool(metrics))
        if fatal is not None:
            return fatal

        # A host filter that matched nothing is a wrong/typo'd name, not 'no
        # data' -- say so instead of returning a bare {_meta}.
        if host and not metrics:
            return {
                "error": "not_found",
                "message": f"No monitored host matched '{host}'",
            }

        # Stamp the canonical host on each instance's metrics (map stays keyed by
        # the raw scrape instance) so consumers can join across tools.
        for instance, values in metrics.items():
            values["host"] = canonical_prometheus_host(instance) or instance

        metrics["_meta"] = build_meta(
            "prometheus", data_window="5m", confidence=confidence
        )
        return metrics

    # ---- Tool 2: get_container_stats (PROM-02) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_container_stats(
        ctx: Context,
        sort_by: Annotated[str, "Sort by 'cpu' or 'memory'. Default 'cpu'."] = "cpu",
        limit: Annotated[int, "Number of top containers to return. Default 10."] = 10,
    ) -> dict:
        """Get top containers by CPU or memory usage across all Docker hosts."""
        # Normalize/validate sort_by so a typo ('CPU', 'mem') is not silently
        # mapped to the memory sort and mislabelled as what was asked for.
        sort_norm = sort_by.strip().lower()
        if sort_norm not in ("cpu", "memory"):
            return {
                "error": "invalid_parameter",
                "message": f"sort_by must be 'cpu' or 'memory', got '{sort_by}'",
            }

        cpu_query = (
            'sort_desc(rate(container_cpu_usage_seconds_total{name!=""}[5m]) * 100)'
        )
        mem_query = 'sort_desc(container_memory_usage_bytes{name!=""})'

        cpu_response, mem_response = await asyncio.gather(
            _instant_query(ctx, cpu_query),
            _instant_query(ctx, mem_query),
        )

        confidence, fatal = _meta_confidence([cpu_response, mem_response], True)
        if fatal is not None:
            return fatal

        cpu_results = _extract_vector(cpu_response)
        mem_results = _extract_vector(mem_response)

        # Build lookup keyed by (name, instance): the same container name runs
        # on several hosts (watchtower, promtail, cadvisor), so keying by name
        # alone would overwrite one host's entry and could attach one host's
        # memory to another host's CPU during the merge.
        containers: dict[tuple[str, str], dict] = {}

        for item in cpu_results:
            name = item.get("metric", {}).get("name", "unknown")
            host = item.get("metric", {}).get("instance", "unknown")
            try:
                cpu_val = round(float(item["value"][1]), 1)
            except (IndexError, ValueError, TypeError):
                cpu_val = 0.0
            containers[(name, host)] = {
                "container_name": name,
                "cpu_percent": cpu_val,
                "memory_bytes": 0,
                "host": canonical_prometheus_host(host) or host,
                "instance": host,
            }

        for item in mem_results:
            name = item.get("metric", {}).get("name", "unknown")
            host = item.get("metric", {}).get("instance", "unknown")
            try:
                mem_val = round(float(item["value"][1]), 1)
            except (IndexError, ValueError, TypeError):
                mem_val = 0.0
            key = (name, host)
            if key in containers:
                containers[key]["memory_bytes"] = mem_val
            else:
                containers[key] = {
                    "container_name": name,
                    "cpu_percent": 0.0,
                    "memory_bytes": mem_val,
                    "host": canonical_prometheus_host(host) or host,
                    "instance": host,
                }

        # Sort by requested field
        sort_key = "cpu_percent" if sort_norm == "cpu" else "memory_bytes"
        sorted_containers = sorted(
            containers.values(), key=lambda c: c[sort_key], reverse=True
        )

        result = {
            "containers": sorted_containers[:limit],
            "_meta": build_meta("prometheus", data_window="5m", confidence=confidence),
        }
        return result

    # ---- Tool 3: get_gpu_status (PROM-03) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_gpu_status(ctx: Context) -> dict:
        """Get GPU name, utilization, VRAM, power draw, temperature, and top processes for all hosts exposing NVIDIA GPU metrics (currently Beast and AI-VM)."""
        queries = {
            "utilization_percent": "nvidia_smi_utilization_gpu_ratio * 100",
            "vram_percent": "nvidia_smi_memory_used_bytes / nvidia_smi_memory_total_bytes * 100",
            "temperature_celsius": "nvidia_smi_temperature_gpu",
            "power_draw_watts": "nvidia_smi_power_draw_watts",
            "vram_used_bytes": "nvidia_smi_memory_used_bytes",
            "vram_total_bytes": "nvidia_smi_memory_total_bytes",
        }

        gpus: dict[str, dict] = {}
        metric_names = list(queries.keys())
        responses = await asyncio.gather(
            *[_instant_query(ctx, q) for q in queries.values()]
        )
        for metric_name, response in zip(metric_names, responses, strict=False):
            results = _extract_vector(response)
            instance_values = _build_instance_map(results)
            for instance, value in instance_values.items():
                if instance not in gpus:
                    gpus[instance] = {}
                gpus[instance][metric_name] = value

        # Every metric query errored -> Prometheus is unreachable, not 'no GPUs'.
        confidence, fatal = _meta_confidence(responses, bool(gpus))
        if fatal is not None:
            return fatal

        # GPU model name lives on the 'name' label of nvidia_smi_gpu_info. The
        # nvidia_smi_memory_* series carry only instance/job/uuid (no name), so
        # the model must be read from gpu_info and joined by instance.
        name_response = await _instant_query(ctx, "nvidia_smi_gpu_info")
        for item in _extract_vector(name_response):
            metric = item.get("metric", {})
            instance = metric.get("instance", "unknown")
            name = metric.get("name")
            if name and instance in gpus:
                gpus[instance]["name"] = name

        # Top-5 processes by VRAM. The exporter for this metric is not scraped
        # yet (companion Prometheus job), so this returns empty in prod for now;
        # degrade gracefully (no "processes" key rather than an error).
        proc_response = await _instant_query(ctx, "nvidia_gpu_process_memory_bytes")
        processes: dict[str, list[dict]] = {}
        for item in _extract_vector(proc_response):
            metric = item.get("metric", {})
            instance = metric.get("instance", "unknown")
            try:
                mem = int(float(item["value"][1]))
            except (IndexError, ValueError, TypeError):
                continue
            processes.setdefault(instance, []).append(
                {
                    "name": metric.get("name", "unknown"),
                    "container": metric.get("container", ""),
                    "type": metric.get("type", ""),
                    "memory_bytes": mem,
                }
            )
        for instance, procs in processes.items():
            if instance not in gpus:
                continue
            procs.sort(key=lambda p: p["memory_bytes"], reverse=True)
            gpus[instance]["processes"] = procs[:5]

        # Canonical identity, stamped additively — the map stays keyed by scrape
        # instance so a host with two cards could never collide on one key.
        # `host` is who USES the GPU, `physical_host` is the machine the card
        # sits in: the 3070 is scraped as `ai-vm-gpu` and used by ai-vm, but it
        # lives in the Proxmox box and is passed through to that guest.
        for instance, entry in gpus.items():
            host = resolve_host(instance, "prometheus")
            entry["host"] = host
            entry["physical_host"] = (host_parent(host) or host) if host else None

        gpus["_meta"] = build_meta("prometheus", confidence=confidence)
        return gpus

    # ---- Tool 4: get_storage_status (PROM-04) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_storage_status(ctx: Context) -> dict:
        """Get storage utilization across all hosts and NAS."""
        size_query = 'node_filesystem_size_bytes{fstype!~"tmpfs|devtmpfs|overlay"}'
        avail_query = 'node_filesystem_avail_bytes{fstype!~"tmpfs|devtmpfs|overlay"}'

        size_response, avail_response = await asyncio.gather(
            _instant_query(ctx, size_query),
            _instant_query(ctx, avail_query),
        )

        confidence, fatal = _meta_confidence([size_response, avail_response], True)
        if fatal is not None:
            return fatal

        size_results = _extract_vector(size_response)
        avail_results = _extract_vector(avail_response)

        # Build lookup: (instance, mountpoint) -> values
        storage: dict[str, Any] = {}

        # Index sizes by (instance, mountpoint)
        size_map: dict[tuple[str, str], float] = {}
        for item in size_results:
            instance = item.get("metric", {}).get("instance", "unknown")
            mountpoint = item.get("metric", {}).get("mountpoint", "/")
            try:
                # Byte counts are integers, not 0.1-byte floats.
                size_val = int(float(item["value"][1]))
            except (IndexError, ValueError, TypeError):
                continue
            size_map[(instance, mountpoint)] = size_val

        # Index available by (instance, mountpoint)
        avail_map: dict[tuple[str, str], float] = {}
        for item in avail_results:
            instance = item.get("metric", {}).get("instance", "unknown")
            mountpoint = item.get("metric", {}).get("mountpoint", "/")
            try:
                avail_val = int(float(item["value"][1]))
            except (IndexError, ValueError, TypeError):
                continue
            avail_map[(instance, mountpoint)] = avail_val

        # Merge into per-host storage lists
        all_keys = set(size_map.keys()) | set(avail_map.keys())
        for instance, mountpoint in sorted(all_keys):
            total = size_map.get((instance, mountpoint), 0)
            avail = avail_map.get((instance, mountpoint), 0)
            used = total - avail
            used_pct = round((used / total) * 100, 1) if total > 0 else 0.0

            entry = {
                "mountpoint": mountpoint,
                "host": canonical_prometheus_host(instance) or instance,
                "total_bytes": total,
                "used_bytes": used,
                "avail_bytes": avail,
                "used_percent": used_pct,
            }

            if instance not in storage:
                storage[instance] = []
            storage[instance].append(entry)

        storage["_meta"] = build_meta("prometheus", confidence=confidence)
        return storage

    # ---- Tool 5: get_prometheus_targets (PROM-05) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_prometheus_targets(ctx: Context) -> dict:
        """Get all Prometheus scrape targets with their health status."""
        response = await _get(ctx, "/api/v1/targets")

        if isinstance(response, dict) and "error" in response:
            return response

        try:
            active_targets = response["data"]["activeTargets"]
        except (KeyError, TypeError):
            return {"targets": [], "_meta": build_meta("prometheus")}

        targets = []
        for target in active_targets:
            labels = target.get("labels", {})
            targets.append(
                {
                    "job": labels.get("job", "unknown"),
                    "instance": labels.get("instance", "unknown"),
                    "health": target.get("health", "unknown"),
                    "last_scrape": target.get("lastScrape", ""),
                    "scrape_url": target.get("scrapeUrl", ""),
                }
            )

        return {"targets": targets, "_meta": build_meta("prometheus")}

    # ---- Tool 6: query_prometheus (PROM-06) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def query_prometheus(
        ctx: Context,
        query: Annotated[str, "PromQL query expression"],
        time: Annotated[
            str | None, "Evaluation timestamp (RFC3339 or Unix). Defaults to now."
        ] = None,
    ) -> dict:
        """Run a raw PromQL instant query. Returns the full Prometheus API response as-is."""
        params = {"query": query}
        if time:
            params["time"] = time
        result = await _get(ctx, "/api/v1/query", params)
        if isinstance(result, dict) and "error" not in result:
            result["_meta"] = build_meta("prometheus")
        return result

    # ---- Tool 7: query_prometheus_range (PROM-07) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def query_prometheus_range(
        ctx: Context,
        query: Annotated[str, "PromQL query expression"],
        start: Annotated[str, "Start time (RFC3339 or Unix timestamp)"],
        end: Annotated[str, "End time (RFC3339 or Unix timestamp)"],
        step: Annotated[
            str, "Query step (e.g. '15s', '1m', '5m'). Default '1m'."
        ] = "1m",
    ) -> dict:
        """Run a raw PromQL range query. Returns the full Prometheus API response as-is."""
        params = {"query": query, "start": start, "end": end, "step": step}
        result = await _get(ctx, "/api/v1/query_range", params)
        if isinstance(result, dict) and "error" not in result:
            result["_meta"] = build_meta("prometheus")
        return result
