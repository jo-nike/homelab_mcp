"""Tests for llama-server inference status tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn, make_response
from tools import llama


def _make_app():
    app = FastMCP("test")
    with patch.object(config, "LLAMA_SERVER_URL", "http://beast:8080"):
        llama.register(app)
    return app


@pytest.mark.asyncio
async def test_router_status_non_numeric_arg_does_not_raise():
    """Regression (WP5): a non-numeric --ctx-size value ('auto') must be
    skipped, not raise ValueError out of the tool."""

    async def fake_get(path, params=None):
        if path == "/health":
            return make_response({"status": "ok"})
        if path == "/props":
            return make_response({"role": "router", "max_instances": 2})
        if path == "/models":
            return make_response(
                {
                    "data": [
                        {
                            "id": "gemma",
                            "status": {
                                "value": "loaded",
                                "args": ["--ctx-size", "auto", "--n-gpu-layers", "33"],
                            },
                        }
                    ]
                }
            )
        if path == "/slots":
            return make_response([])
        return make_response({})

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"llama_server": client}

    app = _make_app()
    fn = get_tool_fn(app, "get_llama_status")
    result = await fn(ctx)

    model = result["models"][0]
    assert "n_ctx" not in model  # 'auto' skipped, not crashed
    assert model["n_gpu_layers"] == 33


@pytest.mark.asyncio
async def test_health_error_surfaced():
    """A failed /health request is reported rather than crashing."""

    async def fake_get(path, params=None):
        if path == "/props":
            return make_response({})
        raise httpx.ConnectError("down")

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"llama_server": client}

    app = _make_app()
    fn = get_tool_fn(app, "get_llama_status")
    result = await fn(ctx)

    assert result["health"]["status"] == "error"
