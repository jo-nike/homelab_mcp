"""Prowlarr tools for homelab MCP server."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register Prowlarr tools. Skips if credentials are not configured."""
    if not (config.PROWLARR_URL and config.PROWLARR_API_KEY):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Prowlarr API."""
        return await service_request(
            ctx, "prowlarr", path, params=params, display_name="Prowlarr"
        )

    def _derive_status(status_entry: dict | None) -> str:
        """Map an /api/v1/indexerstatus entry to a health state.

        No entry means the indexer is not failing (healthy). An entry that is
        currently backing off (disabledTill in the future) is an error;
        otherwise the indexer has recorded failures but is not disabled (warning).
        """
        if status_entry is None:
            return "healthy"
        disabled_till = status_entry.get("disabledTill")
        if disabled_till:
            try:
                till = datetime.fromisoformat(disabled_till.replace("Z", "+00:00"))
                if till > datetime.now(UTC):
                    return "error"
            except (ValueError, AttributeError):
                pass
        return "warning"

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_prowlarr_status(ctx: Context) -> dict:
        """Get Prowlarr indexer status and health. Shows all configured indexers with their health state and any system warnings."""
        indexer_data, health_data, status_data = await asyncio.gather(
            _get(ctx, "/api/v1/indexer"),
            _get(ctx, "/api/v1/health"),
            _get(ctx, "/api/v1/indexerstatus"),
        )

        # Handle errors from the indexer/health requests (the status endpoint is
        # intentionally non-fatal, handled below).
        if isinstance(indexer_data, dict) and "error" in indexer_data:
            return indexer_data
        if isinstance(health_data, dict) and "error" in health_data:
            return health_data

        # Index failing/backing-off indexers by id (non-fatal: a status-endpoint
        # error just means we have no known failures to report). Track whether
        # it errored so the caller can distinguish 'all healthy' from 'health
        # data unavailable' via degraded confidence.
        status_by_id: dict[int, dict] = {}
        status_unavailable = not isinstance(status_data, list)
        if isinstance(status_data, list):
            for entry in status_data:
                indexer_id = entry.get("indexerId")
                if indexer_id is not None:
                    status_by_id[indexer_id] = entry

        # Process indexers
        indexers = []
        if isinstance(indexer_data, list):
            for idx in indexer_data:
                indexers.append(
                    {
                        "name": idx.get("name", "Unknown"),
                        "enabled": idx.get("enable", False),
                        "protocol": idx.get("protocol", "unknown"),
                        "status": _derive_status(status_by_id.get(idx.get("id"))),
                    }
                )

        # Process health issues
        health_issues = []
        if isinstance(health_data, list):
            for issue in health_data:
                health_issues.append(
                    {
                        "source": issue.get("source", "Unknown"),
                        "type": issue.get("type", "unknown"),
                        "message": issue.get("message", ""),
                    }
                )

        # Compute counts
        indexer_count = len(indexers)
        enabled_count = sum(1 for i in indexers if i["enabled"])
        disabled_count = indexer_count - enabled_count
        health_issue_count = len(health_issues)

        summary = (
            f"{indexer_count} indexers ({enabled_count} enabled, {disabled_count} disabled), "
            f"{health_issue_count} health warning{'s' if health_issue_count != 1 else ''}"
        )

        result = {
            "summary": summary,
            "indexers": indexers,
            "indexer_count": indexer_count,
            "enabled_count": enabled_count,
            "health_issues": health_issues,
            "health_issue_count": health_issue_count,
            "_meta": build_meta(
                "prowlarr", confidence="medium" if status_unavailable else "high"
            ),
        }
        if status_unavailable:
            # Indexer health could not be fetched: every indexer is labelled
            # 'healthy' by default, so flag that this is not a confirmed clean bill.
            result["indexer_status_unavailable"] = True
        return result
