"""Tests for Healthchecks cron-monitoring tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_response
from tools import healthchecks


def _make_app():
    app = FastMCP("test")
    with (
        patch.object(config, "HEALTHCHECKS_URL", "http://hc:8000"),
        patch.object(config, "HEALTHCHECKS_API_KEY", "key"),
    ):
        healthchecks.register(app)
    return app


@pytest.mark.asyncio
async def test_status_counts_and_summary():
    checks = [
        {"name": "backup", "status": "up"},
        {"name": "sync", "status": "up"},
    ]
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response(checks))
    ctx = MagicMock()
    ctx.lifespan_context = {"healthchecks": client}

    app = _make_app()
    fn = get_tool_fn(app, "get_healthchecks_status")
    result = await fn(ctx)

    assert result["total_checks"] == 2
    assert result["up"] == 2


@pytest.mark.asyncio
async def test_error_propagates():
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    ctx = MagicMock()
    ctx.lifespan_context = {"healthchecks": client}

    app = _make_app()
    fn = get_tool_fn(app, "get_healthchecks_status")
    result = await fn(ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_flip_fetch_exception_uses_standard_error_shape(monkeypatch):
    """Regression (WP5): a raised flip fetch is recorded as {error, message},
    not {error: <exception text>} with the message in the code slot."""
    checks = [{"name": "backup", "status": "down", "unique_key": "abc"}]
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response(checks))
    ctx = MagicMock()
    ctx.lifespan_context = {"healthchecks": client}

    async def fake_gather(*coros, return_exceptions=False):
        for c in coros:
            c.close()  # avoid 'coroutine never awaited' warnings
        return [RuntimeError("boom")]

    monkeypatch.setattr(healthchecks.asyncio, "gather", fake_gather)

    app = _make_app()
    fn = get_tool_fn(app, "get_healthchecks_status")
    result = await fn(ctx)

    flips = result["checks"][0]["flips"]
    assert flips[0]["error"] == "flips_error"
    assert "boom" in flips[0]["message"]
