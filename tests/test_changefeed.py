"""Tests for the change-feed timeline tool."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastmcp import FastMCP

from tests.conftest import get_tool_fn, make_response
from tools import changefeed


def _iso(delta_hours):
    dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=delta_hours)
    return dt.isoformat()


@pytest.mark.asyncio
async def test_non_positive_hours_rejected():
    """Regression (WP5): hours<=0 is rejected before it becomes invalid PromQL."""
    ctx = MagicMock()
    ctx.lifespan_context = {}
    app = FastMCP("test")
    changefeed.register(app)
    fn = get_tool_fn(app, "what_changed_last_24h")
    result = await fn(ctx, hours=0)
    assert result["error"] == "invalid_parameter"


@pytest.mark.asyncio
async def test_source_failure_reported_not_silently_empty():
    """Regression (WP5): when a source is unreachable the tool must flag it in
    sources_failed with degraded confidence, not report a quiet day."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    changefeed.register(app)
    fn = get_tool_fn(app, "what_changed_last_24h")
    result = await fn(ctx, hours=24)

    assert "prometheus" in result["sources_failed"]
    assert result["_meta"]["confidence"] == "medium"
    assert "could not check" in result["summary"]


@pytest.mark.asyncio
async def test_all_sources_healthy_has_no_failures():
    """A reachable source that simply has nothing to report leaves
    sources_failed empty and keeps confidence high."""
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response({"data": {"result": []}}))
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    changefeed.register(app)
    fn = get_tool_fn(app, "what_changed_last_24h")
    result = await fn(ctx, hours=24)

    assert result["sources_failed"] == []
    assert result["_meta"]["confidence"] == "high"


@pytest.mark.asyncio
async def test_crowdsec_events_honor_hours_window():
    """Regression (item 13): a ban older than the window is excluded from
    'what changed'; a recent ban is kept."""
    decisions = [
        {
            "origin": "crowdsec",
            "type": "ban",
            "value": "1.1.1.1",
            "scope": "ip",
            "scenario": "recent",
            "created_at": _iso(2),  # within 24h
        },
        {
            "origin": "crowdsec",
            "type": "ban",
            "value": "2.2.2.2",
            "scope": "ip",
            "scenario": "old",
            "created_at": _iso(120),  # 5 days ago
        },
    ]
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response(decisions))
    ctx = MagicMock()
    ctx.lifespan_context = {"crowdsec": client}

    app = FastMCP("test")
    changefeed.register(app)
    fn = get_tool_fn(app, "what_changed_last_24h")
    result = await fn(ctx, hours=24)

    entities = {e["entity"] for e in result["events"] if e["source"] == "crowdsec"}
    assert entities == {"1.1.1.1"}


@pytest.mark.asyncio
async def test_timeline_sorted_by_parsed_timestamp():
    """Regression (item 29): mixed 'Z' vs +00:00 suffixes must sort
    chronologically, newest first, not lexicographically."""
    decisions = [
        {
            "origin": "crowdsec",
            "type": "ban",
            "value": "older",
            "created_at": _iso(10).replace("+00:00", "Z"),
        },
        {
            "origin": "crowdsec",
            "type": "ban",
            "value": "newer",
            "created_at": _iso(1),
        },
    ]
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response(decisions))
    ctx = MagicMock()
    ctx.lifespan_context = {"crowdsec": client}

    app = FastMCP("test")
    changefeed.register(app)
    fn = get_tool_fn(app, "what_changed_last_24h")
    result = await fn(ctx, hours=24)

    order = [e["entity"] for e in result["events"]]
    assert order == ["newer", "older"]
