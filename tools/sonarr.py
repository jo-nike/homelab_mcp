"""Sonarr tools for homelab MCP server."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Sonarr tools. Skips if credentials are not configured."""
    if not (config.SONARR_URL and config.SONARR_API_KEY):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Sonarr API."""
        return await service_request(
            ctx, "sonarr", path, params=params, display_name="Sonarr"
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_sonarr_status(
        ctx: Context,
        days: Annotated[
            int, "Number of days to look ahead for upcoming episodes. Default 7."
        ] = 7,
    ) -> dict:
        """Get Sonarr upcoming episodes, download queue, and wanted (missing, monitored) episodes in one call. Shows what TV episodes are airing soon, what's currently downloading, and what is missing."""
        # UTC everywhere: a non-UTC host date would shift the calendar window by
        # up to a day relative to Sonarr's own expectations.
        today = datetime.now(UTC).date()
        end = today + timedelta(days=days)

        calendar_params = {
            "start": today.isoformat(),
            "end": end.isoformat(),
            "includeSeries": "true",
        }
        queue_params = {
            "includeEpisode": "true",
            "includeSeries": "true",
            "pageSize": "50",
        }
        wanted_params = {
            "pageSize": "50",
            "includeSeries": "true",
        }

        calendar_data, queue_data, wanted_data = await asyncio.gather(
            _get(ctx, "/api/v3/calendar", calendar_params),
            _get(ctx, "/api/v3/queue", queue_params),
            _get(ctx, "/api/v3/wanted/missing", wanted_params),
        )

        # Handle errors from any of the requests
        if isinstance(calendar_data, dict) and "error" in calendar_data:
            return calendar_data
        if isinstance(queue_data, dict) and "error" in queue_data:
            return queue_data
        if isinstance(wanted_data, dict) and "error" in wanted_data:
            return wanted_data

        # Parse upcoming episodes (calendar returns a list)
        upcoming = []
        if isinstance(calendar_data, list):
            for ep in calendar_data:
                if not ep.get("hasFile"):
                    upcoming.append(
                        {
                            "series": ep.get("series", {}).get("title", "Unknown"),
                            "episode_title": ep.get("title"),
                            "season": ep.get("seasonNumber"),
                            "episode": ep.get("episodeNumber"),
                            "air_date": ep.get("airDateUtc"),
                            "monitored": ep.get("monitored", False),
                        }
                    )

        # Parse download queue
        queue = []
        records = queue_data.get("records", []) if isinstance(queue_data, dict) else []
        for rec in records:
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            queue.append(
                {
                    "series": rec.get("series", {}).get("title", "Unknown"),
                    "episode_title": rec.get("episode", {}).get(
                        "title", rec.get("title")
                    ),
                    "quality": rec.get("quality", {})
                    .get("quality", {})
                    .get("name", "Unknown"),
                    "status": rec.get("status", "unknown"),
                    "progress_percent": round(((size - sizeleft) / size) * 100)
                    if size > 0
                    else 0,
                    "size_bytes": size,
                    "timeleft": rec.get("timeleft"),
                }
            )

        # Parse wanted/missing episodes
        wanted = []
        wanted_records = (
            wanted_data.get("records", []) if isinstance(wanted_data, dict) else []
        )
        for rec in wanted_records:
            wanted.append(
                {
                    "series": rec.get("series", {}).get("title", "Unknown"),
                    "season": rec.get("seasonNumber"),
                    "episode": rec.get("episodeNumber"),
                    "episode_title": rec.get("title"),
                    "air_date": rec.get("airDateUtc"),
                }
            )

        # /queue and /wanted/missing are paged (pageSize 50); surface the server's
        # totalRecords so a count of 50 is not mistaken for "exactly 50" when more
        # exist beyond the first page.
        queue_total = (
            queue_data.get("totalRecords", len(queue))
            if isinstance(queue_data, dict)
            else len(queue)
        )
        wanted_total = (
            wanted_data.get("totalRecords", len(wanted))
            if isinstance(wanted_data, dict)
            else len(wanted)
        )

        return {
            "upcoming": upcoming,
            "upcoming_count": len(upcoming),
            "queue": queue,
            "queue_count": len(queue),
            "queue_total": queue_total,
            "queue_truncated": queue_total > len(queue),
            "wanted": wanted,
            "wanted_count": len(wanted),
            "wanted_total": wanted_total,
            "wanted_truncated": wanted_total > len(wanted),
            "_meta": build_meta("sonarr", data_window=f"{days}d"),
        }
