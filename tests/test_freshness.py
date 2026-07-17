"""Tests for the data-freshness tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_response
from tools import freshness


@pytest.mark.asyncio
async def test_session_services_not_counted_reachable():
    """Regression (item 6): a configured-but-unprobed session service (dead or
    not) is reported 'not_probed' and excluded from the reachable count."""
    crowdsec = MagicMock()
    crowdsec.get = AsyncMock(return_value=make_response(200))
    transmission = MagicMock()  # never probed

    ctx = MagicMock()
    ctx.lifespan_context = {"crowdsec": crowdsec, "transmission": transmission}

    app = FastMCP("test")
    freshness.register(app)
    fn = get_tool_fn(app, "show_data_freshness")

    with (
        patch.object(config, "REFRESH_TIMESTAMPS", {}, create=True),
        patch.object(config, "STALENESS_THRESHOLDS", {}, create=True),
    ):
        result = await fn(ctx)

    # CrowdSec is now actually probed and reachable.
    assert result["services"]["crowdsec"]["status"] == "ok"
    # Transmission (session service) is visible but not counted reachable.
    assert result["services"]["transmission"]["status"] == "not_probed"
    assert result["ok_count"] == 1
    assert result["not_probed_count"] >= 1
    # Transmission's client was never called.
    transmission.get.assert_not_called()


@pytest.mark.asyncio
async def test_backblaze_and_tautulli_are_visible():
    """Regression (item 18): backblaze and tautulli appear in the report instead
    of being invisible."""
    ctx = MagicMock()
    ctx.lifespan_context = {"backblaze": MagicMock(), "tautulli": MagicMock()}

    app = FastMCP("test")
    freshness.register(app)
    fn = get_tool_fn(app, "show_data_freshness")

    with (
        patch.object(config, "REFRESH_TIMESTAMPS", {}, create=True),
        patch.object(config, "STALENESS_THRESHOLDS", {}, create=True),
    ):
        result = await fn(ctx)

    assert "backblaze" in result["services"]
    assert "tautulli" in result["services"]
    assert result["services"]["backblaze"]["status"] == "not_probed"
    assert result["services"]["tautulli"]["status"] == "not_probed"
