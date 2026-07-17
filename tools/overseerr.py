"""Overseerr media request tools for homelab MCP server."""

import asyncio
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.audit import audit_log
from lib.http import service_request
from lib.meta import build_meta

# Status code mappings.
# REQUEST status (Overseerr server/constants/media.ts MediaRequestStatus):
# PENDING=1, APPROVED=2, DECLINED=3, FAILED=4, COMPLETED=5. The old 4/5 labels
# were copied from the MediaStatus enum and mislabelled failed/completed
# requests as available/partial.
REQUEST_STATUS = {
    1: "pending",
    2: "approved",
    3: "declined",
    4: "failed",
    5: "completed",
}

# MEDIA status (MediaStatus): includes DELETED=6.
MEDIA_STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially_available",
    5: "available",
    6: "deleted",
}


def register(mcp):
    """Register Overseerr tools. Skips if credentials are not configured."""
    if not (config.OVERSEERR_URL and config.OVERSEERR_API_KEY):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Overseerr API."""
        return await service_request(
            ctx, "overseerr", path, params=params, display_name="Overseerr"
        )

    async def _post(ctx: Context, path: str) -> dict:
        """Execute POST request against Overseerr API."""
        return await service_request(
            ctx, "overseerr", path, method="POST", display_name="Overseerr"
        )

    def _extract_year(detail: dict) -> int | None:
        """Pull a 4-digit year from a movie releaseDate or TV firstAirDate."""
        raw = detail.get("releaseDate") or detail.get("firstAirDate")
        if isinstance(raw, str) and len(raw) >= 4 and raw[:4].isdigit():
            return int(raw[:4])
        return None

    async def _fetch_title_year(
        ctx: Context, media_type: str, tmdb_id: int | None
    ) -> tuple[str, int | None]:
        """Resolve a tmdbId to (title, year) via Overseerr's movie/tv detail endpoint."""
        if not tmdb_id:
            return "Unknown Title", None
        endpoint = "movie" if media_type == "movie" else "tv"
        details = await _get(ctx, f"/api/v1/{endpoint}/{tmdb_id}")
        if isinstance(details, dict) and "error" in details:
            return "Unknown Title", None
        title = details.get("title") or details.get("name") or "Unknown Title"
        return title, _extract_year(details)

    def _format_request(req: dict, title: str, year: int | None = None) -> dict:
        """Format a single Overseerr request with its resolved title."""
        media = req.get("media", {})

        # Status mapping
        raw_status = req.get("status", 0)
        status = REQUEST_STATUS.get(raw_status, f"unknown({raw_status})")

        # Media availability status
        raw_media_status = media.get("status", 0)
        media_status = MEDIA_STATUS.get(
            raw_media_status, f"unknown({raw_media_status})"
        )

        # Requested by
        requested_by = req.get("requestedBy", {})
        user = (
            requested_by.get("displayName") or requested_by.get("username") or "Unknown"
        )

        return {
            "id": req.get("id"),
            "title": title,
            "year": year,
            "type": media.get("mediaType", "unknown"),
            "status": status,
            "requested_by": user,
            "requested_at": req.get("createdAt"),
            "media_status": media_status,
        }

    # ---- Tool: get_overseerr_requests (MEDA-07) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_overseerr_requests(
        ctx: Context,
        limit: Annotated[
            int, "Number of recent media requests to return. Default 10."
        ] = 10,
    ) -> dict:
        """Get recent media requests from Overseerr with resolved titles, status, and requester info. pending_count counts the pending requests in this page; pending_total is the global pending count."""
        # Fetch the recent page and the global pending total in parallel. The old
        # pending_count only saw the fetched page, so pending_total (from a cheap
        # filter=pending call's pageInfo) is the authoritative global figure.
        data, pending_data = await asyncio.gather(
            _get(
                ctx,
                "/api/v1/request",
                {"take": str(limit), "skip": "0", "sort": "added"},
            ),
            _get(
                ctx,
                "/api/v1/request",
                {"take": "1", "skip": "0", "filter": "pending"},
            ),
        )

        if isinstance(data, dict) and "error" in data:
            return data

        results = data.get("results", [])

        # Resolve titles (and release years) in parallel
        title_tasks = [
            _fetch_title_year(
                ctx,
                req.get("media", {}).get("mediaType", "movie"),
                req.get("media", {}).get("tmdbId"),
            )
            for req in results
        ]
        titles = await asyncio.gather(*title_tasks)

        # Format each request with resolved title and year
        requests = [
            _format_request(req, title, year)
            for req, (title, year) in zip(results, titles, strict=False)
        ]

        # Counts. pending_count stays page-local (kept for the consumer contract);
        # pending_total is global from the filter=pending pageInfo.
        total_count = data.get("pageInfo", {}).get("results", len(results))
        pending_count = sum(1 for r in requests if r["status"] == "pending")
        if isinstance(pending_data, dict) and "error" not in pending_data:
            pending_total = pending_data.get("pageInfo", {}).get(
                "results", pending_count
            )
        else:
            pending_total = pending_count

        return {
            "requests": requests,
            "total_count": total_count,
            "pending_count": pending_count,
            "pending_total": pending_total,
            "_meta": build_meta("overseerr"),
        }

    async def _decide_request(
        ctx: Context, request_id: int, decision: str, dry_run: bool
    ) -> dict:
        """Shared approve/decline flow: dry-run gate, POST, audit, confirm new status."""
        action = f"overseerr_{decision}_request"

        # Dry run gate. Actually GET the request so the preview can catch the
        # errors the real call would hit (missing id, already-decided request)
        # rather than fabricating success.
        if dry_run:
            current = await _get(ctx, f"/api/v1/request/{request_id}")
            if isinstance(current, dict) and "error" in current:
                await audit_log(
                    ctx,
                    action=action,
                    target=str(request_id),
                    params={"request_id": request_id},
                    result="dry_run",
                    dry_run=True,
                )
                return current

            raw_status = current.get("status", 0)
            current_status = REQUEST_STATUS.get(raw_status, f"unknown({raw_status})")
            await audit_log(
                ctx,
                action=action,
                target=str(request_id),
                params={"request_id": request_id},
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": decision,
                "request_id": request_id,
                "exists": True,
                "current_status": current_status,
                "summary": f"Would {decision} request {request_id} (currently {current_status})",
            }

        # Execute
        result = await _post(ctx, f"/api/v1/request/{request_id}/{decision}")

        if isinstance(result, dict) and "error" in result:
            await audit_log(
                ctx,
                action=action,
                target=str(request_id),
                params={"request_id": request_id},
                result="failure",
            )
            return result

        # Confirm new status from the returned request object
        raw_status = result.get("status", 0)
        status = REQUEST_STATUS.get(raw_status, f"unknown({raw_status})")

        await audit_log(
            ctx,
            action=action,
            target=str(request_id),
            params={"request_id": request_id},
            result="success",
        )
        return {
            "action": decision,
            "request_id": request_id,
            "status": status,
            "result": "success",
            "summary": f"Request {request_id} {decision}d (status: {status})",
            "_meta": build_meta("overseerr"),
        }

    # ---- Tool: overseerr_approve_request (WRITE) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        }
    )
    async def overseerr_approve_request(
        ctx: Context,
        request_id: Annotated[int, "Overseerr media request ID to approve"],
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Approve a pending Overseerr media request. Use dry_run=True to preview."""
        return await _decide_request(ctx, request_id, "approve", dry_run)

    # ---- Tool: overseerr_decline_request (WRITE) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        }
    )
    async def overseerr_decline_request(
        ctx: Context,
        request_id: Annotated[int, "Overseerr media request ID to decline"],
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Decline a pending Overseerr media request. Use dry_run=True to preview."""
        return await _decide_request(ctx, request_id, "decline", dry_run)
