"""Tests for the LiteLLM status tool (stable models type)."""

from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

import config
from tools import litellm


@pytest.fixture
def registered_mcp(monkeypatch):
    monkeypatch.setattr(config, "LITELLM_URL", "http://litellm")
    monkeypatch.setattr(config, "LITELLM_API_KEY", "key")
    mcp = FastMCP("test")
    litellm.register(mcp)
    return mcp


def _ctx():
    ctx = AsyncMock()
    ctx.lifespan_context = {}
    return ctx


@pytest.mark.asyncio
async def test_models_success(registered_mcp, monkeypatch):
    async def fake_request(ctx, service, path, **kwargs):
        if "readiness" in path:
            return {"status": "healthy", "litellm_version": "1.0"}
        return {"data": [{"id": "gpt-4"}, {"id": "claude"}]}

    monkeypatch.setattr(litellm, "service_request", fake_request)
    tool = await registered_mcp.get_tool("get_litellm_status")
    result = await tool.fn(ctx=_ctx())

    assert result["models"] == [{"model_name": "gpt-4"}, {"model_name": "claude"}]
    assert result["model_count"] == 2
    assert "models_error" not in result


@pytest.mark.asyncio
async def test_models_error_keeps_list_type(registered_mcp, monkeypatch):
    async def fake_request(ctx, service, path, **kwargs):
        if "readiness" in path:
            return {"status": "healthy", "litellm_version": "1.0"}
        return {"error": "http_error", "message": "500"}

    monkeypatch.setattr(litellm, "service_request", fake_request)
    tool = await registered_mcp.get_tool("get_litellm_status")
    result = await tool.fn(ctx=_ctx())

    # `models` stays a list; the error goes in a dedicated key.
    assert result["models"] == []
    assert result["model_count"] == 0
    assert result["models_error"]["error"] == "http_error"
