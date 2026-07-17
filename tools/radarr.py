"""Radarr tools for homelab MCP server."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Radarr tools. Skips if credentials are not configured."""
    if not (config.RADARR_URL and config.RADARR_API_KEY):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Radarr API."""
        return await service_request(
            ctx, "radarr", path, params=params, display_name="Radarr"
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_radarr_status(
        ctx: Context,
        days: Annotated[
            int, "Number of days to look ahead for upcoming movies. Default 30."
        ] = 30,
    ) -> dict:
        """Get Radarr upcoming movies, download queue, and wanted (missing, available) movies in one call. Shows what movies are releasing soon, what's currently downloading, and what is missing and grabbable now."""
        # UTC everywhere (see sonarr): keep the calendar window aligned with the
        # *arr server regardless of the host timezone.
        today = datetime.now(UTC).date()
        end = today + timedelta(days=days)

        calendar_params = {
            "start": today.isoformat(),
            "end": end.isoformat(),
        }
        queue_params = {
            "includeMovie": "true",
            "pageSize": "50",
        }
        wanted_params = {
            "pageSize": "50",
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

        # Parse upcoming movies (calendar returns a list). Pick the earliest of
        # the three release dates that falls within the queried window (the
        # calendar can return a movie for any of them); fall back to the
        # earliest overall. Reporting physicalRelease unconditionally showed a
        # far-future physical date/type for a movie whose digital release was
        # what put it in the window.
        today_iso = today.isoformat()
        end_iso = end.isoformat()

        def _pick_release(movie: dict) -> tuple[str | None, str]:
            candidates = []
            for rtype, key in (
                ("cinema", "inCinemas"),
                ("digital", "digitalRelease"),
                ("physical", "physicalRelease"),
            ):
                val = movie.get(key)
                if val:
                    candidates.append((val[:10], val, rtype))
            if not candidates:
                return None, ""
            in_window = [c for c in candidates if today_iso <= c[0] <= end_iso]
            chosen = min(in_window or candidates, key=lambda c: c[0])
            return chosen[1], chosen[2]

        upcoming = []
        if isinstance(calendar_data, list):
            for movie in calendar_data:
                if not movie.get("hasFile"):
                    release_date, release_type = _pick_release(movie)
                    upcoming.append(
                        {
                            "title": movie.get("title", "Unknown"),
                            "year": movie.get("year"),
                            "release_date": release_date,
                            "release_type": release_type,
                            "monitored": movie.get("monitored", False),
                        }
                    )

        # Soonest first; undated last. Dates are ISO-8601, so lexical == chronological.
        upcoming.sort(
            key=lambda m: (m["release_date"] is None, m["release_date"] or "")
        )

        # Parse download queue
        queue = []
        records = queue_data.get("records", []) if isinstance(queue_data, dict) else []
        for rec in records:
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            queue.append(
                {
                    "title": rec.get("movie", {}).get(
                        "title", rec.get("title", "Unknown")
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

        # Parse wanted/missing movies. Only those Radarr can actually grab now —
        # /wanted/missing also lists unreleased monitored films (sequels years out).
        wanted = []
        wanted_records = (
            wanted_data.get("records", []) if isinstance(wanted_data, dict) else []
        )
        for rec in wanted_records:
            if rec.get("isAvailable"):
                wanted.append(
                    {
                        "title": rec.get("title", "Unknown"),
                        "year": rec.get("year"),
                    }
                )

        # Paged endpoints: surface totalRecords. wanted_total is the raw missing
        # count from /wanted/missing (includes unreleased monitored films), so it
        # can exceed the isAvailable-filtered `wanted` list; wanted_truncated only
        # signals that the isAvailable filter ran on the first 50 records.
        queue_total = (
            queue_data.get("totalRecords", len(queue))
            if isinstance(queue_data, dict)
            else len(queue)
        )
        wanted_total = (
            wanted_data.get("totalRecords", len(wanted_records))
            if isinstance(wanted_data, dict)
            else len(wanted_records)
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
            "wanted_truncated": wanted_total > len(wanted_records),
            "_meta": build_meta("radarr", data_window=f"{days}d"),
        }
