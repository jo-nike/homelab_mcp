"""Healthchecks cron job monitoring tools for homelab MCP server."""

import asyncio

from fastmcp import Context

import config
from lib.healthchecks import check_uuid, unwrap_checks, unwrap_flips
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Healthchecks tools. Skips if not configured."""
    if not (config.HEALTHCHECKS_URL and config.HEALTHCHECKS_API_KEY):
        return

    async def _get(ctx, path, params=None):
        return await service_request(
            ctx, "healthchecks", path, params=params, display_name="Healthchecks"
        )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_healthchecks_status(ctx: Context) -> dict:
        """Get all cron job monitors with their current status. Highlights failed and late checks with recent status transition history."""
        checks_data = await _get(ctx, "/api/v3/checks/")

        # If error, return directly
        if isinstance(checks_data, dict) and "error" in checks_data:
            return checks_data

        # Handle both list and dict-with-checks response shapes
        checks = unwrap_checks(checks_data)

        # Count statuses
        counts = {"up": 0, "down": 0, "grace": 0, "new": 0, "paused": 0}
        problem_checks = []
        all_checks = []

        for check in checks:
            status = check.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1

            entry = {
                "name": check.get("name", ""),
                "status": status,
                "last_ping": check.get("last_ping"),
                "next_ping": check.get("next_ping"),
                "tags": check.get("tags", ""),
                "desc": check.get("desc", ""),
            }
            all_checks.append(entry)

            # Track problem checks for flip fetching
            if status in ("down", "grace"):
                uuid = check_uuid(check)
                if uuid:
                    problem_checks.append((entry, uuid))

        # Fetch flips for problem checks (down/grace only), cap at 10 concurrent
        if problem_checks:
            semaphore = asyncio.Semaphore(10)

            async def fetch_flips(uuid):
                async with semaphore:
                    return await _get(
                        ctx, f"/api/v3/checks/{uuid}/flips/", params={"seconds": 86400}
                    )

            flip_results = await asyncio.gather(
                *[fetch_flips(uuid) for _, uuid in problem_checks],
                return_exceptions=True,
            )

            for (entry, _uuid), flips_data in zip(
                problem_checks, flip_results, strict=False
            ):
                if isinstance(flips_data, Exception):
                    # Standard {error, message} shape: the exception text is the
                    # message, not the error code.
                    entry["flips"] = [
                        {"error": "flips_error", "message": str(flips_data)}
                    ]
                elif isinstance(flips_data, dict) and "error" in flips_data:
                    entry["flips"] = [flips_data]
                else:
                    raw_flips = unwrap_flips(flips_data)
                    entry["flips"] = [
                        {"timestamp": f.get("timestamp"), "up": f.get("up")}
                        for f in raw_flips[:20]  # Cap flip history
                    ]

        # Build summary: "N checks" plus a ": up, down, ..." suffix only when any
        # status counts are nonzero (avoids a dangling "0 checks:").
        count_parts = [
            f"{counts.get(s, 0)} {s}"
            for s in ("up", "down", "grace", "paused", "new")
            if counts.get(s, 0) > 0
        ]
        summary = f"{len(checks)} check{'s' if len(checks) != 1 else ''}"
        if count_parts:
            summary += ": " + ", ".join(count_parts)

        return {
            "summary": summary,
            "total_checks": len(checks),
            "up": counts.get("up", 0),
            "down": counts.get("down", 0),
            "grace": counts.get("grace", 0),
            "paused": counts.get("paused", 0),
            "new": counts.get("new", 0),
            "checks": all_checks,
            "_meta": build_meta("healthchecks"),
        }
