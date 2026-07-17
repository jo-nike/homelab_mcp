"""Tests for baseline/anomaly-detection tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_response
from tools import baselines


def _scalar(value):
    return {"data": {"result": [{"metric": {}, "value": [0, str(value)]}]}}


@pytest.mark.asyncio
async def test_ram_percent_produces_valid_promql():
    """Regression: the brace-less ram_percent query used to yield invalid
    PromQL ('... * 100{instance=...}'). With an empty {} selector the injected
    matcher lands inside a selector and the query stays valid."""
    captured = []

    async def fake_get(path, params=None):
        captured.append((params or {}).get("query", ""))
        return make_response(_scalar(42.0))

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    with (
        patch.object(config, "PROMETHEUS_URL", "http://prom:9090"),
        patch.object(
            config,
            "BASELINES",
            {
                "metric_queries": {
                    "ram_percent": "(1 - node_memory_MemAvailable_bytes{}"
                    " / node_memory_MemTotal_bytes) * 100"
                }
            },
        ),
    ):
        baselines.register(app)
        fn = get_tool_fn(app, "is_this_normal")
        result = await fn(ctx, "docker-host", "ram_percent")

    assert captured
    # No matcher tacked on after the closing paren / literal.
    assert all("* 100{instance" not in q for q in captured)
    # The matcher landed inside the first metric's selector.
    assert any(
        'node_memory_MemAvailable_bytes{instance="docker-host"}' in q for q in captured
    )
    assert result["is_normal"] is True


@pytest.mark.asyncio
async def test_container_count_matches_portainer_alias(canonical_hosts):
    """Regression: 'docker host' (Portainer name, space) must match entity
    'docker-host' (hyphen) via canonical resolution, not substring compare."""
    client = MagicMock()
    client.get = AsyncMock(
        return_value=make_response(
            [
                {
                    "Name": "docker host",
                    "Snapshots": [{"RunningContainerCount": 29}],
                }
            ]
        )
    )
    ctx = MagicMock()
    ctx.lifespan_context = {"portainer": client}

    app = FastMCP("test")
    with (
        patch.object(config, "PROMETHEUS_URL", "http://prom:9090"),
        patch.object(
            config,
            "BASELINES",
            {"baselines": {"docker-host": {"expected_container_count": 30}}},
        ),
    ):
        baselines.register(app)
        fn = get_tool_fn(app, "is_this_normal")
        result = await fn(ctx, "docker-host", "container_count")

    assert result["current_value"] == 29
    assert result["is_normal"] is True
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_unknown_metric_error_uses_snake_code_with_message():
    """Regression (WP5): error codes are snake_case with a separate message."""
    client = MagicMock()
    client.get = AsyncMock()
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    with (
        patch.object(config, "PROMETHEUS_URL", "http://prom:9090"),
        patch.object(config, "BASELINES", {"metric_queries": {"cpu_percent": "x"}}),
    ):
        baselines.register(app)
        fn = get_tool_fn(app, "is_this_normal")
        result = await fn(ctx, "docker-host", "bogus_metric")

    assert result["error"] == "unknown_metric"
    assert result["message"]


@pytest.mark.asyncio
async def test_insufficient_data_error_uses_snake_code_with_message():
    """A metric with no baseline history returns insufficient_data + message."""

    async def fake_get(path, params=None):
        return make_response({"data": {"result": []}})

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    with (
        patch.object(config, "PROMETHEUS_URL", "http://prom:9090"),
        patch.object(
            config, "BASELINES", {"metric_queries": {"cpu_percent": "node_x"}}
        ),
    ):
        baselines.register(app)
        fn = get_tool_fn(app, "is_this_normal")
        result = await fn(ctx, "docker-host", "cpu_percent")

    assert result["error"] == "insufficient_data"
    assert result["message"]


@pytest.mark.asyncio
async def test_non_positive_window_days_rejected():
    """Regression (WP5): window_days<=0 is rejected, not silently 'insufficient'."""
    client = MagicMock()
    client.get = AsyncMock()
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    with (
        patch.object(config, "PROMETHEUS_URL", "http://prom:9090"),
        patch.object(config, "BASELINES", {"metric_queries": {"cpu_percent": "x"}}),
    ):
        baselines.register(app)
        normal_fn = get_tool_fn(app, "is_this_normal")
        compare_fn = get_tool_fn(app, "compare_to_baseline")
        r1 = await normal_fn(ctx, "docker-host", "cpu_percent", window_days=0)
        r2 = await compare_fn(ctx, "docker-host", window_days=-1)

    assert r1["error"] == "invalid_parameter"
    assert r2["error"] == "invalid_parameter"
    client.get.assert_not_called()
