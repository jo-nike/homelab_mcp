"""Backblaze B2 tools for homelab MCP server."""

import asyncio
import base64
import time

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta


class BackblazeLoginStrategy:
    """Login strategy for Backblaze B2 multi-step auth.

    Authenticates via b2_authorize_account with Basic auth. Response
    contains authorizationToken + dynamic apiUrl. Token valid for ~24hrs;
    we refresh conservatively at 23hrs.

    Stores account_id from auth response for use by tools (e.g. b2_list_buckets).
    """

    TOKEN_DURATION = 23 * 3600  # 23 hours (conservative, actual 24)

    def __init__(self, key_id: str, app_key: str):
        self._key_id = key_id
        self._app_key = app_key
        self.account_id: str | None = None  # Populated during login

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        cred = base64.b64encode(f"{self._key_id}:{self._app_key}".encode()).decode()

        resp = await client.get(
            "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
            headers={"Authorization": f"Basic {cred}"},
        )
        resp.raise_for_status()
        data = resp.json()

        token = data["authorizationToken"]
        api_url = data["apiInfo"]["storageApi"]["apiUrl"]
        self.account_id = data["accountId"]

        return LoginResult(
            headers={"Authorization": token},
            base_url=api_url,
            expires_at=time.time() + self.TOKEN_DURATION,
        )

    def is_auth_error(self, response: httpx.Response) -> bool:
        return response.status_code == 401


# --- File-stats cache (item 6) ---
#
# The b2_list_file_names pagination walk is expensive for large buckets (a
# ~500k-file bucket means ~50 serial B2 page fetches, well past a polling
# client's read timeout), so we cache the per-bucket stats in-process with a
# TTL — and expiry is stale-while-revalidate: expired stats are served
# immediately while one shared background task re-walks the bucket. Only a
# bucket this process has never seen makes the caller wait for a walk.
_FILE_STATS_TTL = 15 * 60  # 15 minutes
_MAX_FILE_COUNT = 10000  # B2 API max per b2_list_file_names page
_MAX_PAGES = 50  # hard cap on the pagination walk (500k files max)

# bucket_id -> {"expires_at": float, "stats": dict}
_file_stats_cache: dict[str, dict] = {}
# bucket_id -> in-flight walk, shared so concurrent callers don't stampede B2
_refresh_tasks: dict[str, asyncio.Task] = {}


def _now() -> float:
    """Wall clock for cache expiry. Isolated so tests can inject a clock."""
    return time.time()


def _ms_to_iso(ms: int) -> str:
    """Convert a B2 uploadTimestamp (epoch ms) to an ISO UTC string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


async def _fetch_file_stats(post, bucket_id: str) -> dict:
    """Walk b2_list_file_names for one bucket, aggregating size/count/newest.

    `post` is an async callable (path, body) -> dict that returns either the
    parsed JSON or an error dict (never raises). Returns a stats dict with
    total_size_bytes, file_count, latest_upload_at, and truncated; or an error
    dict if a page request failed.
    """
    total_size = 0
    file_count = 0
    latest_ts = 0  # epoch ms of the newest upload seen
    start_file_name = None
    truncated = False

    for _ in range(_MAX_PAGES):
        body = {"bucketId": bucket_id, "maxFileCount": _MAX_FILE_COUNT}
        if start_file_name:
            body["startFileName"] = start_file_name

        data = await post("/b2api/v3/b2_list_file_names", body)
        if "error" in data:
            return data

        for f in data.get("files", []):
            # Only real uploads count toward size/count; skip hide markers and
            # folder placeholders that b2_list_file_names can interleave.
            if f.get("action", "upload") != "upload":
                continue
            total_size += f.get("contentLength") or 0
            file_count += 1
            ts = f.get("uploadTimestamp") or 0
            if ts > latest_ts:
                latest_ts = ts

        start_file_name = data.get("nextFileName")
        if not start_file_name:
            break
    else:
        # Loop exhausted _MAX_PAGES without reaching the end of the bucket.
        truncated = True

    return {
        "total_size_bytes": total_size,
        "file_count": file_count,
        "latest_upload_at": _ms_to_iso(latest_ts) if latest_ts else None,
        "truncated": truncated,
    }


async def _refresh_file_stats(post, bucket_id: str) -> dict:
    """Walk one bucket and cache the stats on success; returns the stats."""
    stats = await _fetch_file_stats(post, bucket_id)
    if "error" not in stats:
        _file_stats_cache[bucket_id] = {
            "expires_at": _now() + _FILE_STATS_TTL,
            "stats": stats,
        }
    return stats


def _ensure_refresh(post, bucket_id: str) -> asyncio.Task:
    """Get the in-flight walk for a bucket, starting one if none is running."""
    task = _refresh_tasks.get(bucket_id)
    if task is None or task.done():
        task = asyncio.create_task(_refresh_file_stats(post, bucket_id))
        # Retrieve the (never expected) exception so an abandoned background
        # refresh can't emit "Task exception was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        _refresh_tasks[bucket_id] = task
    return task


def register(mcp):
    """Register Backblaze B2 tools. Skips if credentials are not configured."""
    if not (config.BACKBLAZE_KEY_ID and config.BACKBLAZE_APP_KEY):
        return

    async def _post(ctx: Context, path: str, body: dict) -> dict:
        """Execute POST request via Backblaze SessionAuthManager."""
        client = ctx.lifespan_context["backblaze"]  # SessionAuthManager
        try:
            resp = await client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Backblaze did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": str(e),
            }
        except Exception as e:
            return {"error": "connection_error", "message": str(e)}

    async def _bucket_file_stats(ctx: Context, bucket_id: str) -> dict:
        """Return cached per-bucket file stats, stale-while-revalidate.

        Fresh cache → returned as is. Expired cache → returned immediately
        while a shared background task re-walks the bucket for the next
        caller. Only a never-cached bucket waits for the walk.
        """
        entry = _file_stats_cache.get(bucket_id)
        if entry and entry["expires_at"] > _now():
            return entry["stats"]

        task = _ensure_refresh(lambda path, body: _post(ctx, path, body), bucket_id)
        if entry:
            return entry["stats"]
        return await task

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_backblaze_usage(ctx: Context) -> dict:
        """Get Backblaze B2 bucket list with per-bucket file stats (total size, file count, and newest upload timestamp). File stats come from a paginated walk cached in-process for 15 minutes; on expiry the stale stats are served while the walk refreshes in the background."""
        # Get account_id from strategy (stored during auth). ensure_auth() can
        # raise (BackblazeLoginStrategy.login uses raise_for_status), so guard it
        # and return the conventional error dict instead of a raw MCP error.
        manager = ctx.lifespan_context["backblaze"]
        try:
            await manager.ensure_auth()  # Ensure we've logged in
        except Exception as e:
            return {"error": "auth_error", "message": str(e)}
        account_id = manager.strategy.account_id

        if not account_id:
            return {
                "error": "auth_error",
                "message": "Could not determine Backblaze account ID",
            }

        # List all buckets
        data = await _post(ctx, "/b2api/v3/b2_list_buckets", {"accountId": account_id})

        if "error" in data:
            return data

        buckets = []
        for bucket in data.get("buckets", []):
            bucket_id = bucket.get("bucketId")
            entry = {
                "bucket_name": bucket.get("bucketName", "Unknown"),
                "bucket_id": bucket_id,
                "bucket_type": bucket.get("bucketType", "unknown"),
                "total_size_bytes": None,
                "file_count": None,
                "latest_upload_at": None,
            }

            if bucket_id:
                stats = await _bucket_file_stats(ctx, bucket_id)
                if "error" in stats:
                    entry["stats_error"] = stats.get(
                        "message", "file stats unavailable"
                    )
                else:
                    entry["total_size_bytes"] = stats["total_size_bytes"]
                    entry["file_count"] = stats["file_count"]
                    entry["latest_upload_at"] = stats["latest_upload_at"]
                    if stats["truncated"]:
                        entry["stats_truncated"] = True

            buckets.append(entry)

        return {
            "buckets": buckets,
            "bucket_count": len(buckets),
            "_meta": build_meta("backblaze"),
        }
