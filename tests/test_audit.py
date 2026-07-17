"""Tests for best-effort write audit logging."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.audit import audit_log


def make_ctx(loki_client):
    ctx = MagicMock()
    ctx.lifespan_context = {"loki": loki_client} if loki_client is not None else {}
    return ctx


@pytest.mark.asyncio
async def test_missing_loki_logs_warning(caplog):
    """An unaudited write (no Loki client) is visible in the server log."""
    ctx = make_ctx(None)
    with caplog.at_level(logging.WARNING, logger="lib.audit"):
        await audit_log(ctx, action="restart_container", target="grafana")
    assert any("no Loki client" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_push_failure_logs_warning(caplog):
    """A failed Loki push logs a warning instead of silently no-op'ing."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=RuntimeError("loki down"))
    ctx = make_ctx(client)
    with caplog.at_level(logging.WARNING, logger="lib.audit"):
        await audit_log(ctx, action="restart_container", target="grafana")
    assert any("push to Loki failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_push_payload_shape():
    """The Loki push carries the action/target/params/result/dry_run fields."""
    client = AsyncMock()
    ctx = make_ctx(client)
    await audit_log(
        ctx,
        action="restart_container",
        target="grafana",
        params={"host": "docker-host"},
        result="success",
        dry_run=False,
    )
    payload = client.post.await_args.kwargs["json"]
    assert payload["streams"][0]["stream"]["type"] == "audit"
    ts, line = payload["streams"][0]["values"][0]
    assert ts.isdigit()
    entry = json.loads(line)
    assert entry["action"] == "restart_container"
    assert entry["target"] == "grafana"
    assert entry["params"] == {"host": "docker-host"}
    assert entry["result"] == "success"
    assert entry["dry_run"] is False
    assert "timestamp" in entry


@pytest.mark.asyncio
async def test_dry_run_flag_recorded():
    """A dry-run audit entry carries dry_run=True and result='dry_run'."""
    client = AsyncMock()
    ctx = make_ctx(client)
    await audit_log(
        ctx,
        action="restart_container",
        target="grafana",
        result="dry_run",
        dry_run=True,
    )
    entry = json.loads(
        client.post.await_args.kwargs["json"]["streams"][0]["values"][0][1]
    )
    assert entry["dry_run"] is True
    assert entry["result"] == "dry_run"


@pytest.mark.asyncio
async def test_never_raises_on_push_failure():
    """audit_log swallows push exceptions (best-effort contract, returns None)."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=RuntimeError("loki down"))
    ctx = make_ctx(client)
    assert await audit_log(ctx, action="x", target="y") is None


@pytest.mark.asyncio
async def test_successful_push_logs_nothing(caplog):
    """A healthy push does not emit a warning."""
    client = AsyncMock()
    client.post = AsyncMock()
    ctx = make_ctx(client)
    with caplog.at_level(logging.WARNING, logger="lib.audit"):
        await audit_log(ctx, action="restart_container", target="grafana")
    assert not caplog.records
    client.post.assert_awaited_once()
