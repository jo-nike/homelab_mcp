"""Tautulli Plex analytics tools for homelab MCP server."""

import asyncio

import httpx
from fastmcp import Context

import config
from lib.meta import build_meta
from lib.redact import redact_exception


def register(mcp):
    """Register Tautulli tools. Skips if not configured."""
    if not (config.TAUTULLI_URL and config.TAUTULLI_API_KEY):
        return

    async def _tautulli_cmd(ctx, cmd, extra_params=None, timeout=None):
        """Execute Tautulli API command. Auth via apikey query param (NOT header).

        timeout overrides the client default for this request so one slow
        sub-query (get_history/get_home_stats have been observed hanging) can
        fail fast instead of stalling the whole fan-out.
        """
        client = ctx.lifespan_context["tautulli"]
        params = {"apikey": config.TAUTULLI_API_KEY, "cmd": cmd}
        if extra_params:
            params.update(extra_params)
        kwargs = {"params": params}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await client.get("/api/v2", **kwargs)
            resp.raise_for_status()
            data = resp.json()
            # Tautulli returns HTTP 200 with response.result=='error' for
            # API-level failures (e.g. invalid apikey); raise_for_status never
            # fires, so check the envelope or a bad key looks like an idle server.
            response = data.get("response", {})
            if response.get("result") != "success":
                return {
                    "error": "api_error",
                    "message": response.get("message") or "Tautulli API error",
                }
            return response.get("data", {})
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Tautulli did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": redact_exception(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": redact_exception(e)}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_tautulli_activity(ctx: Context) -> dict:
        """Get Plex viewing activity: current streams, recent history, and most-watched content over the last 30 days."""
        # Fan out to 3 commands in parallel
        activity_data, history_data, stats_data = await asyncio.gather(
            _tautulli_cmd(ctx, "get_activity"),
            _tautulli_cmd(ctx, "get_history", {"length": "10"}, timeout=8.0),
            _tautulli_cmd(
                ctx,
                "get_home_stats",
                {"time_range": "30", "stats_type": "duration"},
                timeout=8.0,
            ),
        )

        # Process current activity
        if isinstance(activity_data, dict) and "error" in activity_data:
            current_activity = activity_data
        else:
            sessions = []
            for s in activity_data.get("sessions") or []:
                sessions.append(
                    {
                        "user": s.get("friendly_name", s.get("user", "")),
                        "title": s.get("full_title", s.get("title", "")),
                        "state": s.get("state", ""),
                        # Tautulli's activity sessions expose 'progress_percent';
                        # there is no 'progress' key (that always read 0).
                        "progress_percent": int(s.get("progress_percent", 0)),
                    }
                )

            total_bw = int(activity_data.get("total_bandwidth", 0))
            wan_bw = int(activity_data.get("wan_bandwidth", 0))
            lan_bw = int(activity_data.get("lan_bandwidth", 0))

            current_activity = {
                "stream_count": int(activity_data.get("stream_count", 0)),
                "total_bandwidth_mbps": round(total_bw / 1000, 1),
                "wan_bandwidth_mbps": round(wan_bw / 1000, 1),
                "lan_bandwidth_mbps": round(lan_bw / 1000, 1),
                "sessions": sessions,
            }

        # Process recent history
        if isinstance(history_data, dict) and "error" in history_data:
            recent_history = history_data
        else:
            history_list = (
                history_data.get("data") or [] if isinstance(history_data, dict) else []
            )
            recent_history = []
            for h in history_list:
                started = h.get("started") or h.get("date")
                duration_sec = int(h.get("duration", 0))
                recent_history.append(
                    {
                        "title": h.get("full_title", h.get("title", "")),
                        "user": h.get("friendly_name", h.get("user", "")),
                        "date": started,
                        "duration_minutes": round(duration_sec / 60),
                        "percent_complete": int(h.get("percent_complete", 0)),
                    }
                )

        # Process top stats
        if isinstance(stats_data, dict) and "error" in stats_data:
            top_stats = stats_data
        else:
            top_stats = {"top_movies": [], "top_tv": [], "top_users": []}
            # Each top-stats bucket is the same rows[:10] projection; only the
            # label field differs ("title" for media, the user name for users).
            label_fields = {
                "top_movies": "title",
                "top_tv": "title",
                "top_users": "user",
            }

            def _project_rows(rows, label_field):
                projected = []
                for r in rows[:10]:
                    if label_field == "user":
                        label = {"user": r.get("friendly_name", r.get("user", ""))}
                    else:
                        label = {"title": r.get("title", "")}
                    projected.append(
                        {
                            **label,
                            "total_duration": r.get("total_duration", 0),
                            "total_plays": r.get("total_plays", 0),
                        }
                    )
                return projected

            stat_entries = stats_data if isinstance(stats_data, list) else []
            for entry in stat_entries:
                stat_id = entry.get("stat_id", "")
                rows = entry.get("rows", [])
                for bucket, label_field in label_fields.items():
                    if bucket in stat_id:
                        top_stats[bucket] = _project_rows(rows, label_field)
                        break

        # Build summary
        stream_count = (
            current_activity.get("stream_count", 0)
            if isinstance(current_activity, dict) and "error" not in current_activity
            else 0
        )
        history_count = len(recent_history) if isinstance(recent_history, list) else 0
        summary = f"{stream_count} active stream{'s' if stream_count != 1 else ''}, {history_count} plays in last 10 sessions"

        return {
            "summary": summary,
            "current_activity": current_activity,
            "recent_history": recent_history,
            "top_stats": top_stats,
            "_meta": build_meta("tautulli", data_window="30d"),
        }
