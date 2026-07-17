"""Proxmox Backup Server tools for homelab MCP server."""

import asyncio
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.http import service_request
from lib.meta import build_meta


def register(mcp):
    """Register PBS tools. Skips if credentials are not configured."""
    if not (config.PBS_URL and config.PBS_TOKEN_ID and config.PBS_TOKEN_SECRET):
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against PBS API."""
        data = await service_request(
            ctx, "pbs", path, params=params, display_name="PBS"
        )
        # PBS wraps responses in {"data": ...}; error dicts pass through unchanged.
        if isinstance(data, dict):
            return data.get("data", data)
        return data

    async def _snapshot_summary(
        ctx: Context, store: str, backup_type: str, backup_id: str
    ) -> tuple[str, int | None, int | None]:
        """Summarize a group's most recent snapshot.

        Returns (verify_state, last_verified, last_backup_size_bytes):
        - verify_state: "ok" / "failed" from the newest snapshot's verification,
          or "none" when it has never been verified (or lookup failed).
        - last_verified: the newest snapshot's backup-time when it carries a
          verification result, else None.
        - last_backup_size_bytes: the newest snapshot's on-disk size, or None.
        """
        snap_data = await _get(
            ctx,
            f"/api2/json/admin/datastore/{store}/snapshots",
            params={"backup-type": backup_type, "backup-id": backup_id},
        )
        if not isinstance(snap_data, list) or not snap_data:
            return "none", None, None

        snapshots: list[dict[str, Any]] = snap_data

        def _backup_time(s: dict[str, Any]) -> int:
            return s.get("backup-time", 0)

        # ty (0.0.60) can't resolve the keyed `max` overload; the guard above
        # guarantees a non-empty list, so this is safe at runtime.
        latest = max(snapshots, key=_backup_time)  # ty: ignore[no-matching-overload]
        size = latest.get("size")
        verification = latest.get("verification")
        if isinstance(verification, dict) and verification.get("state"):
            return verification["state"], latest.get("backup-time"), size
        return "none", None, size

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_pbs_status(
        ctx: Context,
        backup_limit: Annotated[
            int, "Number of recent backups to show per datastore. Default 5."
        ] = 5,
    ) -> dict:
        """Get PBS datastore usage and recent backup status. Shows disk usage per datastore plus the most recent backup groups sorted by last backup time. Note: each group's `last_verified` field is the backup-time of the newest verified snapshot, not the time verification ran."""
        # Step 1: Fetch datastore usage
        ds_data = await _get(ctx, "/api2/json/status/datastore-usage")

        if isinstance(ds_data, dict) and "error" in ds_data:
            return ds_data

        if not isinstance(ds_data, list):
            ds_data = []

        datastores = []
        for ds in ds_data:
            name = ds.get("store", "Unknown")
            total = ds.get("total", 0)
            used = ds.get("used", 0)
            avail = ds.get("avail", 0)
            used_percent = round(used / max(total, 1) * 100, 1)

            # Step 2: Fetch backup groups for this datastore
            groups_data = await _get(ctx, f"/api2/json/admin/datastore/{name}/groups")

            if isinstance(groups_data, dict) and "error" in groups_data:
                # Include error but don't fail the whole tool
                recent_backups = []
                groups_error = groups_data.get(
                    "message", "Failed to fetch backup groups"
                )
                total_groups = 0
            else:
                if not isinstance(groups_data, list):
                    groups_data = []

                # Sort by last-backup descending, take backup_limit
                sorted_groups = sorted(
                    groups_data,
                    key=lambda g: g.get("last-backup", 0),
                    reverse=True,
                )

                top_groups = sorted_groups[:backup_limit]
                # Enrich each group from its most recent snapshot (verification
                # state, verified timestamp, on-disk size). PBS carries these
                # per-snapshot, so fetch this group's snapshots -- in parallel
                # across the shown groups rather than one round trip at a time.
                summaries = await asyncio.gather(
                    *[
                        _snapshot_summary(
                            ctx,
                            name,
                            group.get("backup-type", "unknown"),
                            group.get("backup-id", "unknown"),
                        )
                        for group in top_groups
                    ]
                )

                recent_backups = []
                for group, (verify_state, last_verified, last_backup_size) in zip(
                    top_groups, summaries, strict=False
                ):
                    recent_backups.append(
                        {
                            "backup_type": group.get("backup-type", "unknown"),
                            "backup_id": group.get("backup-id", "unknown"),
                            "last_backup": group.get("last-backup"),
                            # PBS serializes the group count as "backup-count"
                            # (kebab-case); "count" does not exist.
                            "backup_count": group.get("backup-count", 0),
                            "comment": group.get("comment"),
                            "verify_state": verify_state,
                            "last_verified": last_verified,
                            "last_backup_size_bytes": last_backup_size,
                        }
                    )
                groups_error = None
                total_groups = len(groups_data)

            ds_entry = {
                "name": name,
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": avail,
                "used_percent": used_percent,
                "recent_backups": recent_backups,
                "total_backup_groups": total_groups,
            }

            if groups_error:
                ds_entry["groups_error"] = groups_error

            datastores.append(ds_entry)

        return {"datastores": datastores, "_meta": build_meta("pbs")}
