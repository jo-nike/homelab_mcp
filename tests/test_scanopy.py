"""Tests for Scanopy network topology tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import scanopy

# --- Helpers ---


# --- Mock data ---

# Real Scanopy /api/v1/hosts schema (captured live 2026-07-16): IPs and MACs
# live on ip_addresses[*], ports use `number`, services carry `bindings` that
# reference port ids, and timestamps are created_at/updated_at.
MOCK_HOSTS_RESPONSE = {
    "success": True,
    "data": [
        {
            "id": "id-1",
            "name": "docker-host",
            "hostname": "docker-host",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-04-01T00:00:00Z",
            "interfaces": [],
            "ip_addresses": [
                {
                    "id": "ip-1",
                    "ip_address": "192.168.1.79",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                },
            ],
            "ports": [
                {"id": "p-1", "number": 9090, "protocol": "Tcp", "type": "Custom"},
                {"id": "p-2", "number": 22, "protocol": "Tcp", "type": "Ssh"},
            ],
            "services": [
                {
                    "name": "prometheus",
                    "bindings": [{"type": "Port", "port_id": "p-1"}],
                },
                {
                    "name": "ssh",
                    "bindings": [{"type": "Port", "port_id": "p-2"}],
                },
            ],
        },
        {
            "id": "id-2",
            "name": "gitea",
            # Real Scanopy emits hostname: null for undiscovered names.
            "hostname": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-04-01T00:00:00Z",
            "interfaces": [],
            "ip_addresses": [
                {"id": "ip-2", "ip_address": "172.17.0.2", "mac_address": ""},
            ],
            "ports": [],
            "services": [],
        },
        {
            "id": "id-3",
            "name": "nas",
            "hostname": "nas",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-04-01T00:00:00Z",
            "interfaces": [],
            "ip_addresses": [
                {
                    "id": "ip-3",
                    "ip_address": "192.168.1.10",
                    "mac_address": "11:22:33:44:55:66",
                },
            ],
            "ports": [
                {"id": "p-3", "number": 5000, "protocol": "Tcp", "type": "Http"},
            ],
            "services": [
                {"name": "http", "bindings": [{"type": "Port", "port_id": "p-3"}]},
            ],
        },
    ],
    "meta": {"total": 3},
}


# --- Tests ---


@pytest.mark.asyncio
async def test_scanopy_conditional_registration():
    """Tools are not registered when SCANOPY_URL is not set."""
    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", None),
        patch.object(config, "SCANOPY_API_KEY", None),
    ):
        scanopy.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_scanopy_registers_1_tool():
    """register() creates 1 tool when credentials are set."""
    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)
    assert count_tools(app) == 1


@pytest.mark.asyncio
async def test_network_topology_separates_lan_and_bridge():
    """get_network_topology returns hosts separated into LAN and docker bridge."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_HOSTS_RESPONSE))
    ctx = make_mock_ctx(scanopy=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)

    fn = get_tool_fn(app, "get_network_topology")
    result = await fn(ctx)

    # 2 LAN hosts (192.168.x.x), 1 bridge host (172.x.x.x)
    assert result["lan_host_count"] == 2
    assert result["bridge_host_count"] == 1
    assert result["total_host_count"] == 3

    # LAN hosts
    lan = result["hosts"]
    assert len(lan) == 2
    assert lan[0]["hostname"] == "docker-host"
    assert "192.168.1.79" in lan[0]["ips"]
    assert "AA:BB:CC:DD:EE:FF" in lan[0]["mac_addresses"]
    assert lan[0]["docker_bridge"] is False
    assert len(lan[0]["ports"]) == 2
    assert lan[0]["port_count"] == 2
    # Ports are compact "number/proto service" strings (summary-first payload).
    assert set(lan[0]["ports"]) == {"9090/tcp prometheus", "22/tcp ssh"}
    assert lan[0]["services"] == ["prometheus", "ssh"]
    assert lan[0]["first_seen"] == "2026-01-01T00:00:00Z"
    assert lan[0]["last_seen"] == "2026-04-01T00:00:00Z"

    # Bridge host
    bridge = result["docker_bridge_hosts"]
    assert len(bridge) == 1
    assert bridge[0]["docker_bridge"] is True
    assert "172.17.0.2" in bridge[0]["ips"]
    # hostname was null in the API response -> parser falls back to `name`.
    assert bridge[0]["hostname"] == "gitea"


@pytest.mark.asyncio
async def test_network_topology_host_filter():
    """host_filter narrows to the matching host by hostname/IP."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_HOSTS_RESPONSE))
    ctx = make_mock_ctx(scanopy=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)

    fn = get_tool_fn(app, "get_network_topology")
    result = await fn(ctx, host_filter="docker-host")

    assert result["total_host_count"] == 1
    assert result["hosts"][0]["hostname"] == "docker-host"


@pytest.mark.asyncio
async def test_network_topology_empty():
    """get_network_topology returns host_count=0 on empty response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response({"success": True, "data": [], "meta": {"total": 0}})
    )
    ctx = make_mock_ctx(scanopy=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)

    fn = get_tool_fn(app, "get_network_topology")
    result = await fn(ctx)

    assert result["lan_host_count"] == 0
    assert result["bridge_host_count"] == 0
    assert result["total_host_count"] == 0


@pytest.mark.asyncio
async def test_network_topology_success_false_uses_error_key():
    """Regression (WP5): Scanopy's {success:false, error:...} envelope surfaces
    the real error text from the 'error' key, not 'Unknown error'."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        return_value=make_response({"success": False, "error": "Resource not found"})
    )
    ctx = make_mock_ctx(scanopy=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)

    fn = get_tool_fn(app, "get_network_topology")
    result = await fn(ctx)

    assert result["error"] == "api_error"
    assert result["message"] == "Resource not found"


@pytest.mark.asyncio
async def test_network_topology_api_error():
    """get_network_topology returns error dict on API failure."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "http://test"),
            response=httpx.Response(500),
        )
    )
    ctx = make_mock_ctx(scanopy=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SCANOPY_URL", "http://scanopy:60072"),
        patch.object(config, "SCANOPY_API_KEY", "scp_u_test-key"),
    ):
        scanopy.register(app)

    fn = get_tool_fn(app, "get_network_topology")
    result = await fn(ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 500
