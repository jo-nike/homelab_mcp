"""Tests for compound orchestration tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_mock_ctx, make_response
from tools import compound

_TOPOLOGY = {"critical_containers": [], "dependencies": [], "vertical_stacks": []}


def _make_app():
    app = FastMCP("test")
    with (
        patch.object(config, "PORTAINER_URL", "https://fake:9443"),
        patch.object(config, "PORTAINER_API_KEY", "ptr_key"),
    ):
        compound.register(app)
    return app


def _portainer_ctx():
    endpoints = [{"Id": 1, "Name": "docker-host", "Status": 1}]
    containers = [{"Names": ["/grafana"], "State": "running", "Id": "cid123"}]
    healthy = {"State": {"Status": "running", "Health": {"Status": "healthy"}}}

    async def fake_get(path, params=None):
        if path == "/api/endpoints":
            return make_response(endpoints)
        if path.endswith("/containers/json"):
            return make_response(containers)
        return make_response(healthy)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=make_response(status_code=204))
    ctx = MagicMock()
    ctx.lifespan_context = {"portainer": client}
    return ctx, client


def _tool_names(app):
    return {
        k[len("tool:") : k.index("@")]
        for k in app._local_provider._components
        if k.startswith("tool:")
    }


@pytest.mark.asyncio
async def test_create_task_from_alert_registers_without_portainer():
    """Item 47: create_task_from_alert needs only Vikunja, so it must register
    even when Portainer is unconfigured (safe_restart_container does not)."""
    app = FastMCP("test")
    with (
        patch.object(config, "PORTAINER_URL", None),
        patch.object(config, "PORTAINER_API_KEY", None),
        patch.object(config, "VIKUNJA_URL", "http://vikunja"),
        patch.object(config, "VIKUNJA_TOKEN", "tok"),
    ):
        compound.register(app)

    names = _tool_names(app)
    assert "create_task_from_alert" in names
    assert "safe_restart_container" not in names


@pytest.mark.asyncio
async def test_safe_restart_carries_last_status_on_timeout(monkeypatch):
    """Regression (item 14): when a container never becomes healthy, the poll
    reports the last observed status (e.g. 'restarting'), not 'unknown', and
    the check count derives from timeout/interval (not a hardcoded 6)."""
    monkeypatch.setattr(compound.asyncio, "sleep", AsyncMock())

    endpoints = [{"Id": 1, "Name": "docker-host", "Status": 1}]
    containers = [{"Names": ["/grafana"], "State": "running", "Id": "cid123"}]
    # Inspect always shows it stuck restarting with a failing healthcheck.
    inspect = {"State": {"Status": "restarting", "Health": {"Status": "starting"}}}

    async def fake_get(path, params=None):
        if path == "/api/endpoints":
            return make_response(endpoints)
        if path.endswith("/containers/json"):
            return make_response(containers)
        return make_response(inspect)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=make_response(status_code=204))
    ctx = MagicMock()
    ctx.lifespan_context = {"portainer": client}

    with patch.object(
        config,
        "TOPOLOGY",
        {"critical_containers": [], "dependencies": [], "vertical_stacks": []},
    ):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana")

    health = result["health_check"]
    assert health["healthy"] is False
    assert health["status"] == "restarting"
    assert health["health_status"] == "starting"
    # timeout=30, interval=5 -> at most 30//5 + 1 = 7 checks (not the old cap 6).
    assert health["checks"] == 7


@pytest.mark.asyncio
async def test_safe_restart_timeout_reported_as_timeout():
    """A timed-out restart POST must report error=timeout, not connection_error."""
    endpoints = [{"Id": 1, "Name": "docker-host", "Status": 1}]
    containers = [{"Names": ["/grafana"], "State": "running", "Id": "cid123"}]

    async def fake_get(path, params=None):
        if path == "/api/endpoints":
            return make_response(endpoints)
        return make_response(containers)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = MagicMock()
    ctx.lifespan_context = {"portainer": client}

    with patch.object(
        config,
        "TOPOLOGY",
        {"critical_containers": [], "dependencies": [], "vertical_stacks": []},
    ):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana")

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_safe_restart_audits_failed_restart(monkeypatch):
    """Item 11: a failed restart (non-204) is audit-logged result='failure',
    matching docker.restart_container -- not only the success path."""
    endpoints = [{"Id": 1, "Name": "docker-host", "Status": 1}]
    containers = [{"Names": ["/grafana"], "State": "running", "Id": "cid123"}]

    async def fake_get(path, params=None):
        if path == "/api/endpoints":
            return make_response(endpoints)
        return make_response(containers)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.post = AsyncMock(return_value=make_response(status_code=500))
    ctx = MagicMock()
    ctx.lifespan_context = {"portainer": client}

    audit = AsyncMock()
    monkeypatch.setattr(compound, "audit_log", audit)

    with patch.object(
        config,
        "TOPOLOGY",
        {"critical_containers": [], "dependencies": [], "vertical_stacks": []},
    ):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana")

    assert result["error"] == "restart_failed"
    assert audit.await_args is not None
    assert audit.await_args.kwargs["result"] == "failure"


@pytest.mark.asyncio
async def test_safe_restart_success(monkeypatch):
    """A healthy restart POSTs, polls healthy, and reports result=success."""
    monkeypatch.setattr(compound.asyncio, "sleep", AsyncMock())
    ctx, client = _portainer_ctx()
    with patch.object(config, "TOPOLOGY", _TOPOLOGY):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana")

    assert result["restart_result"] == "success"
    assert result["health_check"]["healthy"] is True
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_safe_restart_dry_run_no_post(monkeypatch):
    """dry_run=True previews the plan and issues no restart POST."""
    monkeypatch.setattr(compound.asyncio, "sleep", AsyncMock())
    ctx, client = _portainer_ctx()
    with patch.object(config, "TOPOLOGY", _TOPOLOGY):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana", dry_run=True)

    assert result["dry_run"] is True
    assert result["would_restart"] is True
    assert client.post.await_count == 0


@pytest.mark.asyncio
async def test_safe_restart_blocked_critical():
    """A critical container is refused with error 'blocked' and no POST."""
    ctx, client = _portainer_ctx()
    with patch.object(
        config, "TOPOLOGY", {**_TOPOLOGY, "critical_containers": ["grafana"]}
    ):
        app = _make_app()
        fn = get_tool_fn(app, "safe_restart_container")
        result = await fn(ctx, name="grafana")

    assert result["error"] == "blocked"
    assert result["reason"] == "critical_container"
    assert client.post.await_count == 0


def _vikunja_app():
    app = FastMCP("test")
    with (
        patch.object(config, "PORTAINER_URL", None),
        patch.object(config, "PORTAINER_API_KEY", None),
        patch.object(config, "VIKUNJA_URL", "http://vikunja"),
        patch.object(config, "VIKUNJA_TOKEN", "tok"),
        patch.object(config, "VIKUNJA_ALERT_PROJECT_ID", None),
    ):
        compound.register(app)
    return app


@pytest.mark.asyncio
async def test_create_task_from_alert_success():
    """A configured project_id creates the task via PUT and returns its id."""
    client = AsyncMock()
    client.put = AsyncMock(return_value=make_response({"id": 99, "title": "t"}))
    ctx = make_mock_ctx(vikunja=client)
    fn = get_tool_fn(_vikunja_app(), "create_task_from_alert")

    result = await fn(
        ctx,
        entity="sonarr",
        severity="critical",
        category="availability",
        message="down",
        project_id=3,
    )

    assert result["result"] == "success"
    assert result["task_id"] == 99
    assert result["priority"] == 4  # critical -> 4
    assert client.put.await_args is not None
    assert client.put.await_args.args[0] == "/api/v1/projects/3/tasks"


@pytest.mark.asyncio
async def test_create_task_from_alert_dry_run_no_write():
    """dry_run=True previews the task and issues no PUT."""
    client = AsyncMock()
    client.put = AsyncMock()
    ctx = make_mock_ctx(vikunja=client)
    fn = get_tool_fn(_vikunja_app(), "create_task_from_alert")

    result = await fn(
        ctx,
        entity="sonarr",
        severity="warning",
        category="availability",
        message="slow",
        project_id=3,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["priority"] == 3  # warning -> 3
    client.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_from_alert_error_propagates():
    """A failed create PUT returns task_creation_failed with the status code."""
    client = AsyncMock()
    client.put = AsyncMock(
        return_value=make_response({"message": "no"}, status_code=500)
    )
    ctx = make_mock_ctx(vikunja=client)
    fn = get_tool_fn(_vikunja_app(), "create_task_from_alert")

    result = await fn(
        ctx,
        entity="sonarr",
        severity="info",
        category="availability",
        message="fyi",
        project_id=3,
    )

    assert result["error"] == "task_creation_failed"
    assert result["status_code"] == 500
