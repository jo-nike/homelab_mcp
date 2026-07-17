"""Tests for the periodic-refresh registry builders."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lib import refresh_registries as refresh


def make_response(json_data, status_code=200):
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "http://test"),
    )


# Real Scanopy schema: addresses on ip_addresses[*], not interfaces[*].
SCANOPY_RESPONSE = {
    "success": True,
    "data": [
        {
            "id": "id-1",
            "name": "docker-host",
            "hostname": "docker-host",
            "updated_at": "2026-04-01T00:00:00Z",
            "interfaces": [],
            "ip_addresses": [
                {"ip_address": "192.168.1.79", "mac_address": "AA:BB:CC:DD:EE:FF"},
                {"ip_address": "172.20.0.3", "mac_address": "3E:52:E2:3A:FF:1A"},
            ],
        },
        {
            "id": "id-2",
            "name": "bridge-only",
            "hostname": "",
            "updated_at": "2026-04-01T00:00:00Z",
            "interfaces": [],
            "ip_addresses": [
                {"ip_address": "172.17.0.5", "mac_address": "00:11:22:33:44:55"},
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_fetch_scanopy_hosts_parses_ip_addresses():
    """Regression: the IP is read from ip_addresses[*].ip_address as a plain
    string, not a stringified dict, so bridge filtering and merging work."""
    client = MagicMock()
    client.get = AsyncMock(return_value=make_response(SCANOPY_RESPONSE))

    results = await refresh._fetch_scanopy_hosts({"scanopy": client})

    # Only the LAN host survives (the 172.x-first host is filtered out).
    assert len(results) == 1
    host = results[0]
    assert host["ip"] == "192.168.1.79"
    assert host["mac"] == "AA:BB:CC:DD:EE:FF"
    assert host["hostname"] == "docker-host"
    assert host["last_seen"] == "2026-04-01T00:00:00Z"
    assert "{" not in host["ip"]


def test_merge_hosts_no_dead_vendor_key():
    """Discovered hosts must not carry a permanently-empty vendor field."""
    seed = {}
    live = {
        "scanopy_hosts": [
            {
                "ip": "192.168.1.200",
                "hostname": "newbox",
                "mac": "AA:BB:CC:00:11:22",
                "last_seen": "2026-04-01T00:00:00Z",
            }
        ]
    }

    merged = refresh._merge_hosts(seed, live)

    assert "newbox" in merged
    assert "vendor" not in merged["newbox"]
    assert merged["newbox"]["ip"] == "192.168.1.200"


# --- tools/refresh.py: the on-demand refresh_registries/refresh_docs tools ---

from fastmcp import FastMCP  # noqa: E402

from tools import refresh as refresh_tools  # noqa: E402


@pytest.fixture
def refresh_mcp():
    mcp = FastMCP("test")
    refresh_tools.register(mcp)
    return mcp


@pytest.fixture
def tool_ctx():
    ctx = AsyncMock()
    ctx.lifespan_context = {}
    return ctx


@pytest.mark.asyncio
async def test_refresh_registries_success(refresh_mcp, tool_ctx, monkeypatch):
    monkeypatch.setattr(
        refresh_tools,
        "refresh_registries_impl",
        AsyncMock(return_value={"services": 3}),
    )
    audit = AsyncMock()
    monkeypatch.setattr(refresh_tools, "audit_log", audit)

    tool = await refresh_mcp.get_tool("refresh_registries")
    result = await tool.fn(ctx=tool_ctx)

    assert result["result"] == "success"
    assert result["diff"] == {"services": 3}
    assert "_meta" in result
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["result"] == "success"


@pytest.mark.asyncio
async def test_refresh_registries_error_shape(refresh_mcp, tool_ctx, monkeypatch):
    monkeypatch.setattr(
        refresh_tools,
        "refresh_registries_impl",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    audit = AsyncMock()
    monkeypatch.setattr(refresh_tools, "audit_log", audit)

    tool = await refresh_mcp.get_tool("refresh_registries")
    result = await tool.fn(ctx=tool_ctx)

    # Convention: {"error": <code>, "message": <human>} -- not {"result": "error"}.
    assert result["error"] == "refresh_failed"
    assert result["message"] == "boom"
    assert "result" not in result
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["result"] == "failure"


@pytest.mark.asyncio
async def test_refresh_docs_error_shape(refresh_mcp, tool_ctx, monkeypatch):
    monkeypatch.setattr(
        refresh_tools, "refresh_docs_impl", AsyncMock(side_effect=RuntimeError("nope"))
    )
    monkeypatch.setattr(refresh_tools, "audit_log", AsyncMock())

    tool = await refresh_mcp.get_tool("refresh_docs")
    result = await tool.fn(ctx=tool_ctx)

    assert result["error"] == "refresh_failed"
    assert result["message"] == "nope"


@pytest.mark.asyncio
async def test_refresh_annotations(refresh_mcp):
    for name in ("refresh_registries", "refresh_docs"):
        tool = await refresh_mcp.get_tool(name)
        ann = tool.annotations
        assert ann.readOnlyHint is False
        assert ann.destructiveHint is False
        assert ann.idempotentHint is True
