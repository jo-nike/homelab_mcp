"""Plex media tools for homelab MCP server."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta

# Library counts move slowly; cache generously so slow-tier polls don't
# re-walk every section each cycle. In-process, lives for the process lifetime.
_STATS_TTL_SECONDS = 2700  # 45 minutes


def register(mcp):
    """Register Plex tools. Skips if credentials are not configured."""
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        return

    async def _get(
        ctx: Context,
        path: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """Execute GET request against Plex API."""
        return await service_request(
            ctx, "plex", path, params=params, headers=headers, display_name="Plex"
        )

    def _extract_subtitle(session: dict) -> str | None:
        """Extract active subtitle stream language from session, if any."""
        try:
            streams = (
                session.get("Media", [{}])[0].get("Part", [{}])[0].get("Stream", [])
            )
            for stream in streams:
                if stream.get("streamType") == 3:  # 3 = subtitle stream
                    return (
                        stream.get("displayTitle")
                        or stream.get("language")
                        or "enabled"
                    )
        except (IndexError, TypeError, KeyError):
            pass
        return None

    def _format_session(session: dict) -> dict:
        """Extract all relevant fields from a Plex session."""
        # Determine title -- for TV, grandparentTitle is the show name
        grandparent = session.get("grandparentTitle")
        title = grandparent if grandparent else session.get("title", "Unknown")
        episode_title = session.get("title") if grandparent else None

        # Player info
        player = session.get("Player", {})

        # Media info
        media = session.get("Media", [{}])[0] if session.get("Media") else {}

        # Stream type
        transcode_session = session.get("TranscodeSession")
        stream_type = "transcode" if transcode_session else "direct play"

        # Progress
        view_offset = int(session.get("viewOffset", 0))
        # Default duration to 0 (not 1): Live TV and photo sessions have no
        # duration, and a default of 1 slips past the guard and yields absurd
        # progress (viewOffset in ms * 100). 0 makes the guard return 0.
        duration = int(session.get("duration", 0))
        progress_percent = round(view_offset / duration * 100) if duration > 0 else 0

        result = {
            "title": title,
            "episode_title": episode_title,
            "media_type": session.get("type", "unknown"),
            "user": session.get("User", {}).get("title", "Unknown"),
            "progress_percent": progress_percent,
            "player_device": player.get("device", "Unknown"),
            "player_platform": player.get("platform", "Unknown"),
            "player_state": player.get("state", "unknown"),
            "stream_type": stream_type,
            "video_resolution": media.get("videoResolution", "unknown"),
            "audio_codec": media.get("audioCodec", "unknown"),
            "bandwidth_kbps": session.get("Session", {}).get("bandwidth", 0),
            "subtitle_stream": _extract_subtitle(session),
        }

        # Add transcode-specific fields
        if transcode_session:
            result["transcode_progress"] = transcode_session.get("progress")
            result["transcode_decision"] = transcode_session.get("videoDecision")

        return result

    # ---- Tool 1: get_plex_sessions (MEDA-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_plex_sessions(ctx: Context) -> dict:
        """Get active Plex streams with full session detail: title, user, progress, player, stream type, resolution, bandwidth."""
        data = await _get(ctx, "/status/sessions")

        if isinstance(data, dict) and "error" in data:
            return data

        # Extract sessions from MediaContainer
        sessions_raw = []
        if isinstance(data, dict):
            container = data.get("MediaContainer", {})
            sessions_raw = container.get("Metadata", [])

        streams = [_format_session(s) for s in sessions_raw]

        return {
            "active_streams": streams,
            "stream_count": len(streams),
            "_meta": build_meta("plex"),
        }

    # ---- Tool 2: get_plex_recently_added (MEDA-02) ----

    def _format_added_item(item: dict) -> dict:
        """Extract relevant fields from a recently added media item."""
        # Title formatting -- for episodes, include show name
        if item.get("grandparentTitle"):
            title = f"{item.get('grandparentTitle', 'Unknown')} - {item.get('title', 'Unknown')}"
        else:
            title = item.get("title", "Unknown")

        # Convert addedAt Unix timestamp to ISO string
        added_at_ts = item.get("addedAt")
        added_at = None
        if added_at_ts:
            try:
                added_at = datetime.fromtimestamp(int(added_at_ts), tz=UTC).isoformat()
            except (ValueError, TypeError, OSError):
                added_at = None

        # Rating: prefer rating, fall back to audienceRating
        rating = item.get("rating") or item.get("audienceRating")

        # Thumbnail URL
        thumb = item.get("thumb")
        thumb_url = None
        if thumb and config.PLEX_URL and config.PLEX_TOKEN:
            thumb_url = f"{config.PLEX_URL}{thumb}?X-Plex-Token={config.PLEX_TOKEN}"

        return {
            "title": title,
            "type": item.get("type", "unknown"),
            "year": item.get("year"),
            "added_at": added_at,
            "library_section": item.get("librarySectionTitle", "Unknown"),
            "summary": (item.get("summary", "") or "")[:200],
            "rating": rating,
            "thumb_url": thumb_url,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_plex_recently_added(
        ctx: Context,
        limit: Annotated[
            int, "Number of recently added items to return. Default 20."
        ] = 20,
    ) -> dict:
        """Get recently added media on Plex including individual episodes (not just seasons). Merges global library and per-section episode results."""
        # Step 1 & 2: Fetch global recently added and library sections in parallel
        global_task = _get(
            ctx,
            "/library/recentlyAdded",
            {
                "X-Plex-Container-Start": "0",
                "X-Plex-Container-Size": str(limit),
            },
        )
        sections_task = _get(ctx, "/library/sections")

        global_data, sections_data = await asyncio.gather(global_task, sections_task)

        # The global recently-added feed is the primary source: if it errored,
        # an empty list would be indistinguishable from a library with nothing
        # new, so surface the error dict instead (mirrors get_plex_sessions).
        if isinstance(global_data, dict) and "error" in global_data:
            return global_data

        # Parse global results
        all_items = []
        if isinstance(global_data, dict):
            container = global_data.get("MediaContainer", {})
            all_items.extend(container.get("Metadata", []))

        # Track partial failures across the secondary (per-section) fetches so
        # confidence can be degraded rather than reporting a confident result
        # built from incomplete data.
        section_error = False

        # Parse sections -- find TV show libraries
        show_section_keys = []
        if isinstance(sections_data, dict) and "error" not in sections_data:
            container = sections_data.get("MediaContainer", {})
            directories = container.get("Directory", [])
            for section in directories:
                if section.get("type") == "show":
                    show_section_keys.append(section.get("key"))
        else:
            section_error = True

        # Step 3: Fetch recent episodes from each TV section in parallel
        if show_section_keys:
            episode_tasks = [
                _get(
                    ctx,
                    f"/library/sections/{key}/recentlyAdded",
                    {
                        "type": "4",
                        "X-Plex-Container-Size": str(limit),
                    },
                )
                for key in show_section_keys
            ]
            episode_results = await asyncio.gather(*episode_tasks)

            for ep_data in episode_results:
                if isinstance(ep_data, dict) and "error" not in ep_data:
                    container = ep_data.get("MediaContainer", {})
                    all_items.extend(container.get("Metadata", []))
                else:
                    section_error = True

        # Step 4: Deduplicate by ratingKey, sort by addedAt descending, take limit
        seen_keys = set()
        unique_items = []
        for item in all_items:
            key = item.get("ratingKey")
            if key and key not in seen_keys:
                seen_keys.add(key)
                unique_items.append(item)
            elif not key:
                unique_items.append(item)

        unique_items.sort(key=lambda x: int(x.get("addedAt", 0)), reverse=True)
        trimmed = unique_items[:limit]

        formatted = [_format_added_item(item) for item in trimmed]

        return {
            "recently_added": formatted,
            "count": len(formatted),
            "_meta": build_meta(
                "plex", confidence="medium" if section_error else "high"
            ),
        }

    # ---- Tool 3: get_plex_library_stats ----

    def _container_total(data) -> int | None:
        """Extract totalSize (full library count) from a container-size-0 response.

        Returns None on error so the caller can lower confidence.
        """
        if isinstance(data, dict) and "error" not in data:
            container = data.get("MediaContainer", {})
            total = container.get("totalSize", container.get("size", 0))
            try:
                return int(total or 0)
            except (TypeError, ValueError):
                return 0
        return None

    # Closure-level cache: {"data": {...}, "expires": monotonic_deadline}.
    # register() runs once per process, so this survives across poll cycles.
    _stats_cache: dict[str, Any] = {"data": None, "expires": 0.0}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_plex_library_stats(ctx: Context) -> dict:
        """Get Plex library item counts (movies, shows, episodes) across all libraries. Counts come from container headers without transferring items; cached ~45min since they move slowly."""
        now = time.monotonic()
        if _stats_cache["data"] is not None and now < _stats_cache["expires"]:
            result = dict(_stats_cache["data"])
            result["_meta"] = build_meta(
                "plex", confidence=result.pop("_confidence", "high")
            )
            return result

        sections = await _get(ctx, "/library/sections")
        if isinstance(sections, dict) and "error" in sections:
            return sections

        directories = []
        if isinstance(sections, dict):
            directories = sections.get("MediaContainer", {}).get("Directory", [])

        movie_keys = [
            d.get("key")
            for d in directories
            if d.get("type") == "movie" and d.get("key")
        ]
        show_keys = [
            d.get("key")
            for d in directories
            if d.get("type") == "show" and d.get("key")
        ]

        # Container-Size=0 -> Plex returns totalSize without transferring items.
        # Passed as query params, not headers: allLeaves ignores the
        # X-Plex-Container-Size header and ships every episode record
        # (observed 45k items / ~14s vs 0.3s with params).
        count_params = {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"}

        movie_results, show_results, leaf_results = await asyncio.gather(
            asyncio.gather(
                *[
                    _get(ctx, f"/library/sections/{k}/all", count_params)
                    for k in movie_keys
                ]
            ),
            asyncio.gather(
                *[
                    _get(ctx, f"/library/sections/{k}/all", count_params)
                    for k in show_keys
                ]
            ),
            asyncio.gather(
                *[
                    _get(ctx, f"/library/sections/{k}/allLeaves", count_params)
                    for k in show_keys
                ]
            ),
        )

        totals = [
            _container_total(r) for r in (*movie_results, *show_results, *leaf_results)
        ]
        # Any sub-query error -> partial data, lower confidence.
        confidence = "medium" if any(t is None for t in totals) else "high"

        movies = sum(_container_total(r) or 0 for r in movie_results)
        shows = sum(_container_total(r) or 0 for r in show_results)
        episodes = sum(_container_total(r) or 0 for r in leaf_results)

        payload = {
            "movies": movies,
            "shows": shows,
            "episodes": episodes,
        }

        # Cache only complete results: a partial count (failed sub-query)
        # would otherwise be pinned for the full TTL; let the next poll retry.
        if confidence == "high":
            _stats_cache["data"] = {**payload, "_confidence": confidence}
            _stats_cache["expires"] = now + _STATS_TTL_SECONDS

        payload["_meta"] = build_meta("plex", confidence=confidence)
        return payload
