"""Baseline and anomaly detection tools for homelab infrastructure.

Compares current metric values against historical baselines using PromQL
avg_over_time/stddev_over_time for metric baselines, and YAML-defined
expected values for non-metric baselines (container counts, expected services).
"""

import asyncio
from typing import Annotated

import httpx
from fastmcp import Context

import config
from lib.hosts import resolve_host
from lib.meta import build_meta
from lib.promql import inject_host_filter as _inject_host_filter


def register(mcp):
    """Register baseline tools. Metric baselines need Prometheus; the
    container_count path needs only Portainer, so register when either is
    configured and gate the PromQL paths on Prometheus at call time."""
    if not (
        config.PROMETHEUS_URL or (config.PORTAINER_URL and config.PORTAINER_API_KEY)
    ):
        return

    async def _prom_query(ctx: Context, query: str) -> list[dict]:
        """Execute an instant PromQL query and return result vector."""
        client = ctx.lifespan_context.get("prometheus")
        if client is None:
            return []
        try:
            resp = await client.get("/api/v1/query", params={"query": query})
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("result", [])
        except (httpx.HTTPError, httpx.TimeoutException):
            return []

    def _extract_scalar(results: list[dict]) -> float | None:
        """Extract a single scalar value from Prometheus results."""
        if not results:
            return None
        try:
            return float(results[0]["value"][1])
        except (IndexError, KeyError, ValueError, TypeError):
            return None

    async def _get_metric_baseline(
        ctx: Context, entity: str, metric: str, metric_query: str, window_days: int
    ) -> dict:
        """Get current value, average, and stddev for a metric on an entity."""
        # Metric baselines need Prometheus; when it is unconfigured the module may
        # still be registered for the Portainer container_count path, so return
        # not_configured rather than KeyError or a misleading 'insufficient data'.
        if "prometheus" not in ctx.lifespan_context:
            return {
                "metric": metric,
                "current": None,
                "avg": None,
                "stddev": None,
                "deviation": None,
                "is_normal": None,
                "error": "not_configured",
                "message": "Prometheus is not configured",
            }

        # Build filtered query for this entity. Reuse prometheus.py's comma- and
        # empty-selector-aware injector: the old local version appended
        # {instance=~...} after brace-less queries (ram_percent, gpu_vram_percent),
        # producing invalid PromQL (e.g. "... * 100{instance=~...}") that
        # Prometheus rejects with 400.
        current_q = _inject_host_filter(metric_query, entity)

        # Use Prometheus subquery syntax for avg/stddev over time window
        avg_q = f"avg_over_time(({current_q})[{window_days}d:1h])"
        stddev_q = f"stddev_over_time(({current_q})[{window_days}d:1h])"

        # Query current, avg, stddev in parallel
        current_r, avg_r, stddev_r = await asyncio.gather(
            _prom_query(ctx, current_q),
            _prom_query(ctx, avg_q),
            _prom_query(ctx, stddev_q),
        )

        current = _extract_scalar(current_r)
        avg = _extract_scalar(avg_r)
        stddev = _extract_scalar(stddev_r)

        if current is None or avg is None:
            return {
                "metric": metric,
                "current": current,
                "avg": avg,
                "stddev": stddev,
                "deviation": None,
                "is_normal": None,
                "error": "insufficient_data",
                "message": f"not enough baseline history for {metric} on {entity}",
            }

        # Calculate deviation in standard deviations
        # Use max(stddev, 0.01) to avoid division by zero
        effective_stddev = max(stddev or 0.0, 0.01)
        deviation = abs(current - avg) / effective_stddev

        return {
            "metric": metric,
            "current": round(current, 2),
            "avg": round(avg, 2),
            "stddev": round(effective_stddev, 2),
            "deviation": round(deviation, 2),
            "is_normal": deviation < 2,
        }

    async def _get_container_count(ctx: Context, entity: str) -> dict | None:
        """Get current container count for an entity via Portainer, if available."""
        baselines = config.BASELINES.get("baselines", {}).get(entity, {})
        expected = baselines.get("expected_container_count")
        if expected is None:
            return None

        # Try to get current container count from Portainer
        if "portainer" not in ctx.lifespan_context:
            return {
                "expected": expected,
                "current": None,
                "is_normal": None,
                "error": "portainer_not_configured",
                "message": "Portainer is not configured",
            }

        client: httpx.AsyncClient = ctx.lifespan_context["portainer"]
        try:
            resp = await client.get("/api/endpoints")
            resp.raise_for_status()
            endpoints = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException):
            return {
                "expected": expected,
                "current": None,
                "is_normal": None,
                "error": "portainer_query_failed",
                "message": "Portainer endpoints query failed",
            }

        # Find the endpoint matching the entity and count running containers.
        # Portainer endpoint names use their own dialect ('docker host'), so
        # resolve both sides to canonical host names rather than substring
        # matching (which never matched 'docker host' against 'docker-host').
        target = resolve_host(entity)
        current = None
        for ep in endpoints:
            ep_name = ep.get("Name", "")
            resolved = resolve_host(ep_name, "portainer")
            if (resolved and target and resolved == target) or (
                ep_name.lower() == entity.lower()
            ):
                snapshots = ep.get("Snapshots", [])
                if snapshots:
                    current = snapshots[0].get("RunningContainerCount", 0)
                break

        if current is None:
            return {
                "expected": expected,
                "current": None,
                "is_normal": None,
                "error": "endpoint_not_found",
                "message": f"endpoint '{entity}' not found in portainer",
            }

        # Allow 20% tolerance for container count
        tolerance = max(expected * 0.2, 2)
        is_normal = abs(current - expected) <= tolerance

        return {
            "expected": expected,
            "current": current,
            "is_normal": is_normal,
        }

    # ---- Tool 1: is_this_normal ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def is_this_normal(
        ctx: Context,
        entity: Annotated[str, "Host or service name (e.g., 'docker-host', 'beast')"],
        metric: Annotated[
            str,
            "Metric to check: 'cpu_percent', 'ram_percent', 'disk_percent', "
            "'gpu_vram_percent', or 'container_count'",
        ],
        window_days: Annotated[
            int, "Baseline comparison window in days. Default 7."
        ] = 7,
    ) -> dict:
        """Check if a metric's current value is normal compared to its baseline. Returns yes/no with deviation details."""
        # A non-positive window makes the '[Nd:1h]' subquery invalid, which
        # Prometheus rejects and the tool would misreport as 'insufficient data'.
        if window_days <= 0:
            return {
                "error": "invalid_parameter",
                "message": "window_days must be a positive integer",
                "entity": entity,
                "metric": metric,
                "is_normal": None,
                "_meta": build_meta("baselines"),
            }

        # Handle non-metric check: container_count
        if metric == "container_count":
            result = await _get_container_count(ctx, entity)
            if result is None:
                return {
                    "summary": f"{entity} has no container_count baseline defined",
                    "entity": entity,
                    "metric": "container_count",
                    "is_normal": None,
                    "error": "no_baseline_defined",
                    "message": f"{entity} has no container_count baseline defined",
                    "_meta": build_meta("baselines"),
                }

            if result.get("is_normal") is None:
                status = "UNKNOWN"
            elif result["is_normal"]:
                status = "NORMAL"
            else:
                status = "ANOMALOUS"

            summary = f"{entity} container_count is {status}"
            if result.get("current") is not None and result.get("expected") is not None:
                summary += f" ({result['current']} vs {result['expected']} expected)"

            response = {
                "summary": summary,
                "entity": entity,
                "metric": "container_count",
                "is_normal": result.get("is_normal"),
                "current_value": result.get("current"),
                "expected_value": result.get("expected"),
                "_meta": build_meta("baselines"),
            }
            # Omit the error key entirely on the success path rather than
            # emitting "error": None, which reads as a malformed error dict.
            if result.get("error"):
                response["error"] = result["error"]
                response["message"] = result.get("message", "")
            return response

        # Handle metric check via PromQL
        metric_queries = config.BASELINES.get("metric_queries", {})
        if metric not in metric_queries:
            return {
                "summary": f"Unknown metric '{metric}'. Available: {', '.join(metric_queries.keys())}, container_count",
                "entity": entity,
                "metric": metric,
                "is_normal": None,
                "error": "unknown_metric",
                "message": f"Unknown metric '{metric}'. Available: {', '.join(metric_queries.keys())}, container_count",
                "_meta": build_meta("baselines"),
            }

        result = await _get_metric_baseline(
            ctx, entity, metric, metric_queries[metric], window_days
        )

        if result.get("error"):
            return {
                "summary": f"{entity} {metric}: {result.get('message', result['error'])}",
                "entity": entity,
                "metric": metric,
                "is_normal": None,
                "error": result["error"],
                "message": result.get("message", ""),
                "_meta": build_meta("baselines", data_window=f"{window_days}d"),
            }

        status = "NORMAL" if result["is_normal"] else "ANOMALOUS"
        summary = (
            f"{entity} {metric} is {status} "
            f"({result['current']}% vs {result['avg']}% avg, "
            f"{result['deviation']} std devs)"
        )

        return {
            "summary": summary,
            "entity": entity,
            "metric": metric,
            "is_normal": result["is_normal"],
            "current_value": result["current"],
            "baseline_avg": result["avg"],
            "baseline_stddev": result["stddev"],
            "deviation_stddevs": result["deviation"],
            "window_days": window_days,
            "_meta": build_meta("baselines", data_window=f"{window_days}d"),
        }

    # ---- Tool 2: compare_to_baseline ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def compare_to_baseline(
        ctx: Context,
        entity: Annotated[str, "Host or service name (e.g., 'docker-host', 'beast')"],
        window_days: Annotated[
            int, "Baseline comparison window in days. Default 7."
        ] = 7,
    ) -> dict:
        """Compare all metrics for an entity against their baselines. Returns a full comparison table with deviations."""
        if window_days <= 0:
            return {
                "error": "invalid_parameter",
                "message": "window_days must be a positive integer",
                "entity": entity,
                "_meta": build_meta("baselines"),
            }

        metric_queries = config.BASELINES.get("metric_queries", {})

        # Query all metric baselines in parallel
        metric_tasks = [
            _get_metric_baseline(ctx, entity, name, query, window_days)
            for name, query in metric_queries.items()
        ]
        metric_results = await asyncio.gather(*metric_tasks)

        # Filter out metrics with no data (entity might not have GPU, etc.)
        metrics = [r for r in metric_results if r.get("current") is not None]

        # Check non-metric baselines
        non_metric = {}
        container_result = await _get_container_count(ctx, entity)
        if container_result is not None:
            non_metric["container_count"] = container_result

        # Check expected services
        entity_baselines = config.BASELINES.get("baselines", {}).get(entity, {})
        expected_services = entity_baselines.get("expected_services", [])
        if expected_services:
            non_metric["expected_services"] = {
                "expected": expected_services,
                "count": len(expected_services),
            }

        # Count anomalies
        anomaly_count = sum(1 for m in metrics if m.get("is_normal") is False)
        if container_result and container_result.get("is_normal") is False:
            anomaly_count += 1

        # Build summary
        total_checked = len(metrics) + (1 if container_result else 0)
        anomaly_names = [m["metric"] for m in metrics if m.get("is_normal") is False]
        if container_result and container_result.get("is_normal") is False:
            anomaly_names.append("container_count")

        if anomaly_count == 0:
            summary = f"{entity}: {total_checked} metrics checked, all normal"
        else:
            summary = (
                f"{entity}: {total_checked} metrics checked, "
                f"{anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'} "
                f"({', '.join(anomaly_names)})"
            )

        return {
            "summary": summary,
            "entity": entity,
            "window_days": window_days,
            "metrics": metrics,
            "non_metric": non_metric,
            "anomaly_count": anomaly_count,
            "_meta": build_meta("baselines", data_window=f"{window_days}d"),
        }
