"""Loki log query tools for homelab infrastructure."""

import time
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def _escape_label_value(value: str) -> str:
    """Escape backslashes and double-quotes so a caller-supplied value can be
    safely embedded inside a LogQL label matcher (`host="..."`). Without this a
    value containing `"` breaks out of the matcher and produces a malformed
    query."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _time_range_ns(minutes: int) -> tuple[str, str]:
    """Return (start_ns, end_ns) nanosecond Unix timestamp strings for the
    window of `minutes` ending now."""
    now = time.time()
    end_ns = str(int(now * 1e9))
    start_ns = str(int((now - minutes * 60) * 1e9))
    return start_ns, end_ns


def register(mcp):
    """Register Loki tools. Skips if LOKI_URL is not configured."""
    if not config.LOKI_URL:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Loki API."""
        return await service_request(
            ctx, "loki", path, params=params, display_name="Loki"
        )

    def _parse_streams(data: dict) -> list[dict]:
        """Flatten Loki streams response into a list of entries."""
        entries = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                entries.append(
                    {
                        "timestamp": ts,
                        "line": line,
                        "labels": labels,
                    }
                )
        return entries

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_recent_errors(
        ctx: Context,
        minutes: Annotated[int, "How many minutes to look back. Default 60."] = 60,
        host: Annotated[
            str | None,
            "Filter by host label (e.g. 'plex-stack', 'docker-host'). Omit for all hosts.",
        ] = None,
        limit: Annotated[int, "Maximum log entries to return. Default 100."] = 100,
    ) -> dict:
        """Get recent error, fatal, and panic log entries across all Docker hosts."""
        # Build LogQL selector
        if host:
            selector = f'{{job="docker", host="{_escape_label_value(host)}"}}'
        else:
            selector = '{job="docker"}'
        logql = f'{selector} |~ "(?i)(error|panic|fatal)"'

        # Calculate time range with nanosecond timestamps
        start_ns, end_ns = _time_range_ns(minutes)

        data = await _get(
            ctx,
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": start_ns,
                "end": end_ns,
                "limit": str(limit),
                "direction": "backward",
            },
        )

        if "error" in data:
            return data

        entries = _parse_streams(data)
        return {
            "entries": entries,
            "query": logql,
            "time_range_minutes": minutes,
            "_meta": build_meta("loki", data_window=f"{minutes}m"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_container_logs(
        ctx: Context,
        container: Annotated[
            str, "Container name to get logs for (e.g. 'grafana', 'sonarr', 'plex')"
        ],
        minutes: Annotated[int, "How many minutes to look back. Default 30."] = 30,
        limit: Annotated[int, "Maximum log entries to return. Default 50."] = 50,
    ) -> dict:
        """Get recent logs for a specific Docker container by name."""
        logql = f'{{container="{_escape_label_value(container)}"}}'

        start_ns, end_ns = _time_range_ns(minutes)

        data = await _get(
            ctx,
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": start_ns,
                "end": end_ns,
                "limit": str(limit),
                "direction": "backward",
            },
        )

        if "error" in data:
            return data

        entries = _parse_streams(data)
        return {
            "container": container,
            "entries": entries,
            "time_range_minutes": minutes,
            "_meta": build_meta("loki", data_window=f"{minutes}m"),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def query_logs(
        ctx: Context,
        query: Annotated[
            str,
            'LogQL query expression. Examples: {job="docker"} |~ "(?i)error" — regex line filter; {container="grafana"} |= "error" | json — text match then parse JSON; {job="docker"} | json | level=~"error|fatal" — filter on parsed label. Note: pipeline label filters (level, status, etc.) only work AFTER a parsing stage like | json or | logfmt.',
        ],
        start: Annotated[
            str | None,
            "Start time (RFC3339 or nanosecond Unix timestamp). Default: 1 hour ago.",
        ] = None,
        end: Annotated[
            str | None, "End time (RFC3339 or nanosecond Unix timestamp). Default: now."
        ] = None,
        limit: Annotated[int, "Maximum entries. Default 100."] = 100,
    ) -> dict:
        """Run a raw LogQL query against Loki. Returns the full API response as-is."""
        # data_window is only meaningful for the default 1h window; a caller who
        # supplies an explicit start/end gets no fixed window claim.
        custom_range = start is not None or end is not None
        default_start, default_end = _time_range_ns(60)
        if end is None:
            end = default_end
        if start is None:
            start = default_start

        # Direct passthrough per D-03
        data = await _get(
            ctx,
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": start,
                "end": end,
                "limit": str(limit),
            },
        )
        if isinstance(data, dict) and "error" not in data:
            data["_meta"] = build_meta(
                "loki", data_window=None if custom_range else "1h"
            )
        return data
