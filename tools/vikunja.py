"""Vikunja task management tools for homelab MCP server."""

from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context

import config
from lib.audit import audit_log
from lib.http import service_request
from lib.meta import build_meta


def _validate_due_date(due_date: str | None) -> bool:
    """True if due_date is None or a parseable ISO 8601 string."""
    if due_date is None:
        return True
    try:
        datetime.fromisoformat(due_date.replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        return False


def register(mcp):
    """Register Vikunja tools. Skips if credentials are not configured."""
    if not config.VIKUNJA_URL or not config.VIKUNJA_TOKEN:
        return

    async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
        """Execute GET request against Vikunja API."""
        return await service_request(
            ctx, "vikunja", path, params=params, display_name="Vikunja"
        )

    async def _put(ctx: Context, path: str, json_data: dict) -> dict:
        """Execute PUT request against Vikunja API."""
        return await service_request(
            ctx, "vikunja", path, method="PUT", json=json_data, display_name="Vikunja"
        )

    async def _post_json(ctx: Context, path: str, json_data: dict) -> dict:
        """Execute POST request against Vikunja API."""
        return await service_request(
            ctx, "vikunja", path, method="POST", json=json_data, display_name="Vikunja"
        )

    def _normalize_due_date(due_date: str | None) -> str | None:
        """Convert Vikunja's sentinel date to None."""
        if not due_date or due_date == "0001-01-01T00:00:00Z":
            return None
        return due_date

    # ---- Tool 1: get_vikunja_tasks (TASK-01) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_vikunja_tasks(
        ctx: Context,
        project: Annotated[
            str | None,
            "Filter by project name or ID. If omitted, returns tasks across all projects.",
        ] = None,
        status: Annotated[
            str,
            "Filter by status: 'open' (default), 'done', or 'all'",
        ] = "open",
    ) -> dict:
        """List tasks from Vikunja. Filter by project and/or status (open/done/all)."""
        # Fetch all projects first
        projects = await _get(ctx, "/api/v1/projects")
        if isinstance(projects, dict) and "error" in projects:
            return projects

        # Build project lookup
        project_map = {p["id"]: p["title"] for p in projects}

        # Filter to specific project if requested
        if project:
            target_ids = [
                pid
                for pid, title in project_map.items()
                if str(pid) == project or title.lower() == project.lower()
            ]
            if not target_ids:
                return {
                    "error": "not_found",
                    "message": f"No project matching '{project}'",
                }
        else:
            target_ids = list(project_map.keys())

        # Fetch tasks from each project, following pagination. Vikunja clamps
        # per_page to its maxitemsperpage (default 50), so requesting 200 still
        # returns one 50-item page; loop until a short page (mirroring gitea's
        # _get_all_repos) so large projects are not silently truncated.
        per_page = 50
        max_pages = 50
        truncated = False
        projects_failed = 0
        all_tasks: list[dict] = []
        for pid in target_ids:
            page = 1
            while page <= max_pages:
                data = await _get(
                    ctx,
                    f"/api/v1/projects/{pid}/tasks",
                    {"page": page, "per_page": per_page},
                )
                # A failed per-project fetch must not be silently swallowed as
                # 'no tasks': count it so the caller can flag partial results.
                if isinstance(data, dict) and "error" in data:
                    projects_failed += 1
                    break
                if not isinstance(data, list) or not data:
                    break
                for t in data:
                    t["_project_title"] = project_map.get(pid, "")
                all_tasks.extend(data)
                if len(data) < per_page:
                    break
                page += 1
            else:
                truncated = True

        # Client-side status filter
        if status == "open":
            all_tasks = [t for t in all_tasks if not t.get("done", False)]
        elif status == "done":
            all_tasks = [t for t in all_tasks if t.get("done", False)]

        task_list = [
            {
                "id": t.get("id"),
                "title": t.get("title", ""),
                "done": t.get("done", False),
                "priority": t.get("priority", 0),
                "due_date": _normalize_due_date(t.get("due_date")),
                "project": t.get("_project_title", ""),
                "labels": [lbl.get("title", "") for lbl in t.get("labels", []) or []],
            }
            for t in all_tasks
        ]

        return {
            "tasks": task_list,
            "task_count": len(task_list),
            "truncated": truncated,
            "projects_failed": projects_failed,
            "_meta": build_meta(
                "vikunja", confidence="medium" if projects_failed else "high"
            ),
        }

    # ---- Tool 2: get_vikunja_task_detail (TASK-02) ----

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_vikunja_task_detail(
        ctx: Context,
        task_id: Annotated[int, "Vikunja task ID"],
    ) -> dict:
        """Get full details of a specific Vikunja task including description, labels, assignees, and dates."""
        t = await _get(ctx, f"/api/v1/tasks/{task_id}")

        if isinstance(t, dict) and "error" in t:
            return t

        return {
            "id": t.get("id"),
            "title": t.get("title", ""),
            "description": t.get("description", ""),
            "done": t.get("done", False),
            "priority": t.get("priority", 0),
            "due_date": _normalize_due_date(t.get("due_date")),
            "labels": [lbl.get("title", "") for lbl in t.get("labels", []) or []],
            "assignees": [a.get("username", "") for a in t.get("assignees", []) or []],
            "project": t.get("project", {}).get("title", ""),
            "created": t.get("created", ""),
            "updated": t.get("updated", ""),
            "_meta": build_meta("vikunja"),
        }

    # ---- Tool 3: create_vikunja_task (WRITE-04a) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        }
    )
    async def create_vikunja_task(
        ctx: Context,
        title: Annotated[str, "Task title"],
        project_id: Annotated[int, "Vikunja project ID to create the task in"],
        description: Annotated[str, "Task description (markdown supported)"] = "",
        priority: Annotated[
            int, "Priority: 0=unset, 1=low, 2=medium, 3=high, 4=urgent, 5=critical"
        ] = 0,
        due_date: Annotated[
            str | None,
            "Due date in ISO 8601 format (e.g. '2026-04-10T00:00:00Z'). Omit for no due date.",
        ] = None,
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Create a new task in Vikunja. Use dry_run=True to preview."""
        # Validate up front so small local LLMs get an actionable message
        # instead of an opaque Vikunja 400 (and a misleading dry_run preview).
        if not title or not title.strip():
            return {"error": "invalid_input", "message": "title must not be empty"}
        if not 0 <= priority <= 5:
            return {
                "error": "invalid_input",
                "message": "priority must be between 0 and 5",
            }
        if not _validate_due_date(due_date):
            return {
                "error": "invalid_input",
                "message": "due_date must be ISO 8601 (e.g. '2026-04-10T00:00:00Z')",
            }

        # Build payload
        body: dict = {"title": title}
        if description:
            body["description"] = description
        if priority > 0:
            body["priority"] = priority
        if due_date is not None:
            body["due_date"] = due_date

        # Dry run gate
        if dry_run:
            await audit_log(
                ctx,
                action="create_vikunja_task",
                target="",
                params={"title": title, "project_id": project_id},
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": "create_task",
                "project_id": project_id,
                "payload": body,
                "summary": f"Would create task '{title}' in project {project_id}",
            }

        # Execute: Vikunja uses PUT for create
        result = await _put(ctx, f"/api/v1/projects/{project_id}/tasks", body)

        if "error" in result:
            await audit_log(
                ctx,
                action="create_vikunja_task",
                target="",
                params={"title": title, "project_id": project_id},
                result="failure",
            )
            return result

        # Audit success
        await audit_log(
            ctx,
            action="create_vikunja_task",
            target=str(result.get("id", "")),
            params={"title": title, "project_id": project_id},
            result="success",
        )
        return {
            "action": "create_task",
            "task_id": result.get("id"),
            "title": result.get("title"),
            "project_id": project_id,
            "result": "success",
            "summary": f"Created task '{title}' (ID: {result.get('id')})",
            "_meta": build_meta("vikunja"),
        }

    # ---- Tool 4: update_vikunja_task (WRITE-04b) ----

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def update_vikunja_task(
        ctx: Context,
        task_id: Annotated[int, "Vikunja task ID to update"],
        title: Annotated[str | None, "New title (omit to keep current)"] = None,
        description: Annotated[
            str | None, "New description (omit to keep current)"
        ] = None,
        done: Annotated[
            bool | None,
            "Mark task done (True) or undone (False). Omit to keep current.",
        ] = None,
        priority: Annotated[
            int | None,
            "New priority: 0=unset, 1=low, 2=medium, 3=high, 4=urgent, 5=critical. Omit to keep current.",
        ] = None,
        due_date: Annotated[
            str | None,
            "New due date in ISO 8601 (e.g. '2026-04-10T00:00:00Z'). Omit to keep current.",
        ] = None,
        dry_run: Annotated[bool, "Preview what would happen without executing"] = False,
    ) -> dict:
        """Update an existing Vikunja task. Only provided fields are changed. Use dry_run=True to preview."""
        # Validate provided fields up front (see create_vikunja_task rationale).
        if title is not None and not title.strip():
            return {"error": "invalid_input", "message": "title must not be empty"}
        if priority is not None and not 0 <= priority <= 5:
            return {
                "error": "invalid_input",
                "message": "priority must be between 0 and 5",
            }
        if not _validate_due_date(due_date):
            return {
                "error": "invalid_input",
                "message": "due_date must be ISO 8601 (e.g. '2026-04-10T00:00:00Z')",
            }

        # Build payload from provided fields
        body: dict = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if done is not None:
            body["done"] = done
        if priority is not None:
            body["priority"] = priority
        if due_date is not None:
            body["due_date"] = due_date

        if not body:
            return {
                "error": "no_changes",
                "message": "No fields to update. Provide at least one of: title, description, done, priority, due_date",
            }

        # Dry run gate
        if dry_run:
            await audit_log(
                ctx,
                action="update_vikunja_task",
                target=str(task_id),
                params=body,
                result="dry_run",
                dry_run=True,
            )
            return {
                "dry_run": True,
                "action": "update_task",
                "task_id": task_id,
                "updated_fields": list(body.keys()),
                "payload": body,
                "summary": f"Would update task {task_id}: {', '.join(body.keys())}",
            }

        # Execute: Vikunja binds POST /tasks/{id} into a FULL task model, so any
        # field omitted from the body is persisted as its zero value (verified
        # live: sending only `title` wipes priority, due_date and description).
        # GET the current task, merge the provided fields, then POST the whole
        # object so unsent fields survive.
        existing = await _get(ctx, f"/api/v1/tasks/{task_id}")
        if isinstance(existing, dict) and "error" in existing:
            await audit_log(
                ctx,
                action="update_vikunja_task",
                target=str(task_id),
                params=body,
                result="failure",
            )
            return existing

        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(body)
        result = await _post_json(ctx, f"/api/v1/tasks/{task_id}", merged)

        if "error" in result:
            await audit_log(
                ctx,
                action="update_vikunja_task",
                target=str(task_id),
                params=body,
                result="failure",
            )
            return result

        # Audit success
        await audit_log(
            ctx,
            action="update_vikunja_task",
            target=str(task_id),
            params=body,
            result="success",
        )
        return {
            "action": "update_task",
            "task_id": task_id,
            "updated_fields": list(body.keys()),
            "result": "success",
            "summary": f"Updated task {task_id}: {', '.join(body.keys())}",
            "_meta": build_meta("vikunja"),
        }
