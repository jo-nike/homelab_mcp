"""Tests for Vikunja task management tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import vikunja

# --- Helpers ---


# --- Mock data ---

MOCK_TASKS = [
    {
        "id": 1,
        "title": "Fix DNS",
        "done": False,
        "priority": 3,
        "due_date": "2026-04-15T00:00:00Z",
        "project": {"id": 1, "title": "Homelab"},
        "labels": [{"title": "urgent"}],
        "assignees": [],
        "created": "2026-04-01T00:00:00Z",
        "updated": "2026-04-02T00:00:00Z",
    },
    {
        "id": 2,
        "title": "Done task",
        "done": True,
        "priority": 1,
        "due_date": "0001-01-01T00:00:00Z",
        "project": {"id": 1, "title": "Homelab"},
        "labels": [],
        "assignees": [],
        "created": "2026-03-01T00:00:00Z",
        "updated": "2026-03-15T00:00:00Z",
    },
    {
        "id": 3,
        "title": "Setup monitoring",
        "done": False,
        "priority": 2,
        "due_date": "2026-05-01T00:00:00Z",
        "project": {"id": 2, "title": "Infrastructure"},
        "labels": [{"title": "devops"}],
        "assignees": [{"username": "jon"}],
        "created": "2026-04-01T00:00:00Z",
        "updated": "2026-04-02T00:00:00Z",
    },
]

# get_vikunja_tasks fetches /api/v1/projects first, then per-project tasks.
# Tasks 1 & 2 live in project 1 (Homelab); task 3 lives in project 2 (Infrastructure).
MOCK_PROJECTS = [
    {"id": 1, "title": "Homelab"},
    {"id": 2, "title": "Infrastructure"},
]
MOCK_PROJECT1_TASKS = MOCK_TASKS[:2]
MOCK_PROJECT2_TASKS = MOCK_TASKS[2:]

MOCK_TASK_DETAIL = {
    "id": 1,
    "title": "Fix DNS",
    "description": "Update DNS records for new services",
    "done": False,
    "priority": 3,
    "due_date": "2026-04-15T00:00:00Z",
    "project": {"id": 1, "title": "Homelab"},
    "labels": [{"title": "urgent"}],
    "assignees": [{"username": "jon"}],
    "created": "2026-04-01T00:00:00Z",
    "updated": "2026-04-02T00:00:00Z",
}


# --- Tests ---


@pytest.mark.asyncio
async def test_vikunja_conditional_registration():
    """Tools are not registered when VIKUNJA_URL is not set."""
    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", None),
        patch.object(config, "VIKUNJA_TOKEN", None),
    ):
        vikunja.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_vikunja_registers_4_tools():
    """register() creates 4 tools when credentials are set."""
    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)
    # get_vikunja_tasks, get_vikunja_task_detail, create_vikunja_task, update_vikunja_task
    assert count_tools(app) == 4


@pytest.mark.asyncio
async def test_vikunja_tasks_all():
    """get_vikunja_tasks with status='all' returns all tasks."""
    mock_client = AsyncMock()
    # First call: /api/v1/projects. Then one call per project for its tasks.
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_PROJECTS),
            make_response(MOCK_PROJECT1_TASKS),
            make_response(MOCK_PROJECT2_TASKS),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="all")

    assert result["task_count"] == 3
    assert result["tasks"][0]["title"] == "Fix DNS"
    assert result["tasks"][0]["labels"] == ["urgent"]
    assert result["tasks"][0]["project"] == "Homelab"


@pytest.mark.asyncio
async def test_vikunja_tasks_partial_failure_flagged():
    """Regression (WP5): a failed per-project fetch is counted in
    projects_failed with degraded confidence, not silently dropped."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_PROJECTS),
            make_response(MOCK_PROJECT1_TASKS),
            httpx.ConnectError("project 2 down"),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="all")

    assert result["projects_failed"] == 1
    assert result["_meta"]["confidence"] == "medium"


@pytest.mark.asyncio
async def test_vikunja_tasks_open_filter():
    """get_vikunja_tasks with status='open' excludes done tasks."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_PROJECTS),
            make_response(MOCK_PROJECT1_TASKS),
            make_response(MOCK_PROJECT2_TASKS),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="open")

    # Only 2 open tasks (id 1 and 3), done task (id 2) excluded
    assert result["task_count"] == 2
    assert all(not t["done"] for t in result["tasks"])


@pytest.mark.asyncio
async def test_vikunja_tasks_project_filter():
    """get_vikunja_tasks filters by project name."""
    mock_client = AsyncMock()
    # Only the matching project's tasks are fetched after the projects lookup.
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_PROJECTS),
            make_response(MOCK_PROJECT2_TASKS),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, project="Infrastructure", status="all")

    assert result["task_count"] == 1
    assert result["tasks"][0]["title"] == "Setup monitoring"


@pytest.mark.asyncio
async def test_vikunja_tasks_due_date_sentinel():
    """get_vikunja_tasks converts 0001-01-01T00:00:00Z sentinel to None."""
    mock_client = AsyncMock()
    # Projects lookup first, then the single project's tasks (all three MOCK_TASKS
    # live under one project here so the ordered due_date assertions below hold).
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"id": 1, "title": "Homelab"}]),
            make_response(MOCK_TASKS),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="all")

    assert result["task_count"] == 3
    # Task 1 has a real due date
    assert result["tasks"][0]["due_date"] == "2026-04-15T00:00:00Z"
    # Task 2 has sentinel due date -> None
    assert result["tasks"][1]["due_date"] is None
    # Task 3 has a real due date
    assert result["tasks"][2]["due_date"] == "2026-05-01T00:00:00Z"


@pytest.mark.asyncio
async def test_vikunja_tasks_empty():
    """get_vikunja_tasks returns task_count=0 on empty list."""
    mock_client = AsyncMock()
    # Projects exist but each returns no tasks.
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_PROJECTS),
            make_response([]),
            make_response([]),
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="all")

    assert result["task_count"] == 0
    assert result["tasks"] == []


@pytest.mark.asyncio
async def test_vikunja_task_detail():
    """get_vikunja_task_detail returns full task detail per D-18."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_TASK_DETAIL))
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_task_detail")
    result = await fn(ctx, task_id=1)

    assert result["id"] == 1
    assert result["title"] == "Fix DNS"
    assert result["description"] == "Update DNS records for new services"
    assert result["done"] is False
    assert result["priority"] == 3
    assert result["due_date"] == "2026-04-15T00:00:00Z"
    assert result["labels"] == ["urgent"]
    assert result["assignees"] == ["jon"]
    assert result["project"] == "Homelab"
    assert result["created"] == "2026-04-01T00:00:00Z"
    assert result["updated"] == "2026-04-02T00:00:00Z"


@pytest.mark.asyncio
async def test_vikunja_task_detail_not_found():
    """get_vikunja_task_detail returns error dict for 404."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("GET", "http://test"),
            response=httpx.Response(404),
        )
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_task_detail")
    result = await fn(ctx, task_id=999)

    assert result["error"] == "http_error"
    assert result["status"] == 404


@pytest.mark.asyncio
async def test_vikunja_tasks_follows_pagination():
    """Regression (item 27): Vikunja clamps per_page to 50, so a project with
    more than one page must be fully paginated, not truncated at page 1."""
    page1 = [
        {"id": i, "title": f"t{i}", "done": False, "priority": 0} for i in range(50)
    ]
    page2 = [{"id": 50, "title": "t50", "done": False, "priority": 0}]
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"id": 1, "title": "Homelab"}]),  # projects
            make_response(page1),  # project 1, page 1 (full)
            make_response(page2),  # project 1, page 2 (short)
        ]
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "get_vikunja_tasks")
    result = await fn(ctx, status="all")

    assert result["task_count"] == 51
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_update_vikunja_task_merges_before_post():
    """Regression (item 10): update must GET the current task and merge, so
    unsent fields (priority, due_date, description) are not wiped."""
    existing = {
        "id": 5,
        "title": "old",
        "description": "keep-me",
        "priority": 4,
        "due_date": "2026-09-01T00:00:00Z",
        "done": False,
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(existing))
    mock_client.post = AsyncMock(
        return_value=make_response({**existing, "title": "new"})
    )
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)

    fn = get_tool_fn(app, "update_vikunja_task")
    result = await fn(ctx, task_id=5, title="new")

    # The POSTed body is the merged full task, not just {"title": "new"}.
    sent = mock_client.post.call_args.kwargs["json"]
    assert sent["title"] == "new"
    assert sent["priority"] == 4
    assert sent["due_date"] == "2026-09-01T00:00:00Z"
    assert sent["description"] == "keep-me"
    assert result["result"] == "success"


@pytest.mark.asyncio
async def test_create_vikunja_task_rejects_invalid_input():
    """Regression (WP5): empty title / out-of-range priority / bad due_date
    are rejected with invalid_input before any API contact."""
    mock_client = AsyncMock()
    mock_client.put = AsyncMock()
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)
    fn = get_tool_fn(app, "create_vikunja_task")

    r_empty = await fn(ctx, title="   ", project_id=1)
    r_prio = await fn(ctx, title="ok", project_id=1, priority=9)
    r_date = await fn(ctx, title="ok", project_id=1, due_date="not-a-date")

    assert r_empty["error"] == "invalid_input"
    assert r_prio["error"] == "invalid_input"
    assert r_date["error"] == "invalid_input"
    mock_client.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_vikunja_task_rejects_invalid_input():
    """Invalid update fields are rejected before the GET/merge/POST."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock()
    mock_client.post = AsyncMock()
    ctx = make_mock_ctx(vikunja=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)
    fn = get_tool_fn(app, "update_vikunja_task")

    r = await fn(ctx, task_id=5, priority=-1)
    assert r["error"] == "invalid_input"
    mock_client.get.assert_not_awaited()
    mock_client.post.assert_not_awaited()


def _vikunja_app():
    app = FastMCP("test")
    with (
        patch.object(config, "VIKUNJA_URL", "http://vikunja:3456"),
        patch.object(config, "VIKUNJA_TOKEN", "test-token"),
    ):
        vikunja.register(app)
    return app


@pytest.mark.asyncio
async def test_create_vikunja_task_success():
    """create PUTs to /projects/{id}/tasks with the built body and returns the id."""
    mock_client = AsyncMock()
    mock_client.put = AsyncMock(
        return_value=make_response({"id": 42, "title": "Buy milk"})
    )
    ctx = make_mock_ctx(vikunja=mock_client)
    fn = get_tool_fn(_vikunja_app(), "create_vikunja_task")

    result = await fn(ctx, title="Buy milk", project_id=7, priority=4)

    assert result["result"] == "success"
    assert result["task_id"] == 42
    assert mock_client.put.await_args is not None
    assert mock_client.put.await_args.args[0] == "/api/v1/projects/7/tasks"
    sent = mock_client.put.await_args.kwargs["json"]
    assert sent == {"title": "Buy milk", "priority": 4}


@pytest.mark.asyncio
async def test_create_vikunja_task_dry_run_no_write():
    """create dry_run=True previews without issuing a PUT."""
    mock_client = AsyncMock()
    mock_client.put = AsyncMock()
    ctx = make_mock_ctx(vikunja=mock_client)
    fn = get_tool_fn(_vikunja_app(), "create_vikunja_task")

    result = await fn(ctx, title="Buy milk", project_id=7, dry_run=True)

    assert result["dry_run"] is True
    assert result["payload"]["title"] == "Buy milk"
    mock_client.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_vikunja_task_error_propagates():
    """A failed create PUT propagates the error dict."""
    mock_client = AsyncMock()
    mock_client.put = AsyncMock(
        return_value=make_response({"message": "nope"}, status_code=500)
    )
    ctx = make_mock_ctx(vikunja=mock_client)
    fn = get_tool_fn(_vikunja_app(), "create_vikunja_task")

    result = await fn(ctx, title="Buy milk", project_id=7)
    assert result["error"] == "http_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_update_vikunja_task_dry_run_no_write():
    """update dry_run=True previews without GET or POST."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock()
    mock_client.post = AsyncMock()
    ctx = make_mock_ctx(vikunja=mock_client)
    fn = get_tool_fn(_vikunja_app(), "update_vikunja_task")

    result = await fn(ctx, task_id=5, title="new", dry_run=True)

    assert result["dry_run"] is True
    assert result["updated_fields"] == ["title"]
    mock_client.get.assert_not_awaited()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_vikunja_task_no_changes():
    """update with no fields returns no_changes without touching the API."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock()
    mock_client.post = AsyncMock()
    ctx = make_mock_ctx(vikunja=mock_client)
    fn = get_tool_fn(_vikunja_app(), "update_vikunja_task")

    result = await fn(ctx, task_id=5)

    assert result["error"] == "no_changes"
    mock_client.get.assert_not_awaited()
    mock_client.post.assert_not_awaited()
