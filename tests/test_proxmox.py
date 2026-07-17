"""Tests for Proxmox VE monitoring tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import proxmox

# --- Helpers ---


# --- Sample data ---

CLUSTER_RESOURCES_DATA = {
    "data": [
        {
            "type": "node",
            "node": "proxmox",
            "cpu": 0.15,
            "maxcpu": 8,
            "mem": 4294967296,
            "maxmem": 34359738368,
            "uptime": 86400,
            "status": "online",
        },
        {
            "type": "qemu",
            "vmid": 100,
            "name": "ai-vm",
            "status": "running",
            "node": "proxmox",
            "maxcpu": 16,
            "maxmem": 68719476736,
            "maxdisk": 536870912000,
            "cpu": 0.25,
            "mem": 34359738368,
        },
        {
            "type": "lxc",
            "vmid": 101,
            "name": "docker-host",
            "status": "running",
            "node": "proxmox",
            "maxcpu": 4,
            "maxmem": 8589934592,
            "maxdisk": 107374182400,
            "cpu": 0.10,
            "mem": 4294967296,
        },
        {
            "type": "storage",
            "storage": "local-lvm",
            "node": "proxmox",
            "plugintype": "lvmthin",
            "disk": 100000000000,
            "maxdisk": 500000000000,
            "status": "available",
        },
    ]
}

VM_STATUS_CURRENT = {
    "data": {
        "cpu": 0.42,
        "mem": 34359738368,
        "maxmem": 68719476736,
        "uptime": 172800,
        "netin": 1234567890,
        "netout": 987654321,
        "diskread": 5555555555,
        "diskwrite": 3333333333,
    }
}

VM_CONFIG = {
    "data": {
        "ostype": "l26",
        "boot": "order=scsi0;ide2;net0",
        "cores": 16,
        "memory": 65536,
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "scsi0": "local-lvm:vm-100-disk-0,size=500G",
    }
}


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Proxmox tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "PROXMOX_URL", "https://fake:8006"),
        patch.object(config, "PROXMOX_TOKEN_ID", "root@pam!monitoring"),
        patch.object(config, "PROXMOX_TOKEN_SECRET", "fake-uuid-1234"),
    ):
        proxmox.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When PROXMOX_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with (
        patch.object(config, "PROXMOX_URL", ""),
        patch.object(config, "PROXMOX_TOKEN_ID", "root@pam!test"),
        patch.object(config, "PROXMOX_TOKEN_SECRET", "fake-uuid"),
    ):
        proxmox.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_token_id_missing():
    """When PROXMOX_TOKEN_ID is missing, register() adds no tools."""
    app = FastMCP("test-skip-token")
    with (
        patch.object(config, "PROXMOX_URL", "https://fake:8006"),
        patch.object(config, "PROXMOX_TOKEN_ID", None),
        patch.object(config, "PROXMOX_TOKEN_SECRET", "fake-uuid"),
    ):
        proxmox.register(app)
    assert count_tools(app) == 0


def test_register_adds_3_tools():
    """When all 3 Proxmox config vars are set, register() adds 3 tools."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "PROXMOX_URL", "https://fake:8006"),
        patch.object(config, "PROXMOX_TOKEN_ID", "root@pam!test"),
        patch.object(config, "PROXMOX_TOKEN_SECRET", "fake-uuid"),
    ):
        proxmox.register(app)
    assert count_tools(app) == 3


# --- Test: get_proxmox_nodes ---


@pytest.mark.asyncio
async def test_get_proxmox_nodes_all(mcp_app, mock_client):
    """Returns dict keyed by node name with cpu_percent, ram fields, uptime, status."""
    ctx = make_mock_ctx(proxmox=mock_client)

    # get_proxmox_nodes passes ?type=node, so API returns only node entries
    node_only_data = {
        "data": [
            {
                "type": "node",
                "node": "proxmox",
                "cpu": 0.15,
                "maxcpu": 8,
                "mem": 4294967296,
                "maxmem": 34359738368,
                "uptime": 86400,
                "status": "online",
            },
        ]
    }
    mock_client.get = AsyncMock(return_value=make_response(node_only_data))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx)

    assert "proxmox" in result
    node = result["proxmox"]
    assert node["cpu_percent"] == 15.0
    assert node["ram_used_bytes"] == 4294967296
    assert node["ram_total_bytes"] == 34359738368
    assert node["ram_percent"] == 12.5
    assert node["uptime_seconds"] == 86400
    assert node["status"] == "online"


@pytest.mark.asyncio
async def test_get_proxmox_nodes_single(mcp_app, mock_client):
    """When node='proxmox', returns only matching node."""
    ctx = make_mock_ctx(proxmox=mock_client)

    # Include multiple nodes to verify filtering
    multi_node_data = {
        "data": [
            {
                "type": "node",
                "node": "proxmox",
                "cpu": 0.15,
                "maxcpu": 8,
                "mem": 4294967296,
                "maxmem": 34359738368,
                "uptime": 86400,
                "status": "online",
            },
            {
                "type": "node",
                "node": "proxmox2",
                "cpu": 0.30,
                "maxcpu": 4,
                "mem": 2147483648,
                "maxmem": 17179869184,
                "uptime": 43200,
                "status": "online",
            },
        ]
    }
    mock_client.get = AsyncMock(return_value=make_response(multi_node_data))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx, node="proxmox")

    assert "proxmox" in result
    assert "proxmox2" not in result
    # Only the matching node plus the _meta envelope.
    assert set(result) == {"proxmox", "_meta"}


@pytest.mark.asyncio
async def test_get_proxmox_nodes_unknown_node_returns_not_found(mcp_app, mock_client):
    """Regression (WP5): a node filter matching nothing returns not_found, not
    an empty {_meta} dict."""
    ctx = make_mock_ctx(proxmox=mock_client)
    data = {
        "data": [
            {
                "type": "node",
                "node": "proxmox",
                "cpu": 0.1,
                "maxcpu": 8,
                "mem": 1,
                "maxmem": 2,
                "uptime": 1,
                "status": "online",
            }
        ]
    }
    mock_client.get = AsyncMock(return_value=make_response(data))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx, node="does-not-exist")

    assert result["error"] == "not_found"


# --- Test: get_proxmox_resources ---


@pytest.mark.asyncio
async def test_get_proxmox_resources(mcp_app, mock_client):
    """Returns dict with vms, containers, storage keys; each entry has expected fields."""
    ctx = make_mock_ctx(proxmox=mock_client)

    mock_client.get = AsyncMock(return_value=make_response(CLUSTER_RESOURCES_DATA))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_resources")
    result = await tool_fn(ctx=ctx)

    assert "vms" in result
    assert "containers" in result
    assert "storage" in result

    # Check VM
    assert len(result["vms"]) == 1
    vm = result["vms"][0]
    assert vm["vmid"] == 100
    assert vm["name"] == "ai-vm"
    assert vm["type"] == "vm"
    assert vm["status"] == "running"
    assert vm["node"] == "proxmox"
    assert vm["cpu_cores"] == 16
    assert vm["ram_total_bytes"] == 68719476736
    assert vm["disk_total_bytes"] == 536870912000
    # Live guest stats for a running VM
    assert vm["cpu_percent"] == 25.0
    assert vm["ram_used_bytes"] == 34359738368
    assert vm["ram_percent"] == 50.0

    # Check container
    assert len(result["containers"]) == 1
    ct = result["containers"][0]
    assert ct["vmid"] == 101
    assert ct["name"] == "docker-host"
    assert ct["type"] == "ct"
    assert ct["cpu_percent"] == 10.0
    assert ct["ram_used_bytes"] == 4294967296
    assert ct["ram_percent"] == 50.0

    # Check storage
    assert len(result["storage"]) == 1
    st = result["storage"][0]
    assert st["name"] == "local-lvm"
    assert st["plugin_type"] == "lvmthin"
    assert st["disk_used_bytes"] == 100000000000
    assert st["disk_total_bytes"] == 500000000000


@pytest.mark.asyncio
async def test_get_proxmox_resources_stopped_guest(mcp_app, mock_client):
    """Stopped guests report null live cpu/ram stats but keep allocation fields."""
    ctx = make_mock_ctx(proxmox=mock_client)

    stopped_data = {
        "data": [
            {
                "type": "qemu",
                "vmid": 200,
                "name": "off-vm",
                "status": "stopped",
                "node": "proxmox",
                "maxcpu": 8,
                "maxmem": 17179869184,
                "maxdisk": 107374182400,
            },
        ]
    }
    mock_client.get = AsyncMock(return_value=make_response(stopped_data))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_resources")
    result = await tool_fn(ctx=ctx)

    assert len(result["vms"]) == 1
    vm = result["vms"][0]
    assert vm["status"] == "stopped"
    # Allocation unchanged
    assert vm["cpu_cores"] == 8
    assert vm["ram_total_bytes"] == 17179869184
    # Live stats null when stopped
    assert vm["cpu_percent"] is None
    assert vm["ram_used_bytes"] is None
    assert vm["ram_percent"] is None


# --- Test: canonical host stamping ---

# Proxmox's own names, not ours: the node is "pve", the ai-vm guest is "AI", and
# the templates are nobody.
CANONICAL_RESOURCES_DATA = {
    "data": [
        {"type": "node", "node": "pve", "status": "online", "cpu": 0.07, "mem": 1},
        {"type": "qemu", "vmid": 107, "name": "AI", "status": "running", "node": "pve"},
        {
            "type": "qemu",
            "vmid": 900,
            "name": "temp-debian-12",
            "status": "stopped",
            "node": "pve",
        },
        {
            "type": "lxc",
            "vmid": 104,
            "name": "docker-host",
            "status": "running",
            "node": "pve",
        },
    ]
}


@pytest.mark.asyncio
async def test_get_proxmox_resources_stamps_canonical_host(
    mcp_app, mock_client, canonical_hosts
):
    """Every guest carries the canonical host it is, joined by Proxmox's name for
    it — and None when it isn't one of our hosts (a template)."""
    ctx = make_mock_ctx(proxmox=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(CANONICAL_RESOURCES_DATA))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_resources")
    result = await tool_fn(ctx=ctx)

    by_vmid = {g["vmid"]: g for g in result["vms"] + result["containers"]}
    assert by_vmid[107]["host"] == "ai-vm"
    assert by_vmid[104]["host"] == "docker-host"
    assert by_vmid[900]["host"] is None
    # Additive — the guest's own identity is untouched.
    assert by_vmid[107]["name"] == "AI"


@pytest.mark.asyncio
async def test_get_proxmox_nodes_stamps_canonical_host(
    mcp_app, mock_client, canonical_hosts
):
    """The node keeps Proxmox's key ("pve") and gains the canonical name."""
    ctx = make_mock_ctx(proxmox=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(CANONICAL_RESOURCES_DATA))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx)

    assert result["pve"]["host"] == "proxmox"
    assert result["pve"]["name"] == "pve"


# --- Test: get_proxmox_vm_status ---


@pytest.mark.asyncio
async def test_get_proxmox_vm_status_found(mcp_app, mock_client):
    """Returns merged dict with allocation, live stats, and config for a known VMID."""
    ctx = make_mock_ctx(proxmox=mock_client)

    # Three sequential calls: cluster/resources, status/current, config
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(CLUSTER_RESOURCES_DATA),
            make_response(VM_STATUS_CURRENT),
            make_response(VM_CONFIG),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_vm_status")
    result = await tool_fn(ctx=ctx, vmid=100)

    # Allocation fields
    assert result["vmid"] == 100
    assert result["name"] == "ai-vm"
    assert result["type"] == "vm"
    assert result["node"] == "proxmox"
    assert result["status"] == "running"
    assert result["cpu_cores"] == 16
    assert result["ram_total_bytes"] == 68719476736

    # Live stats
    assert result["cpu_percent"] == 42.0
    assert result["ram_used_bytes"] == 34359738368
    assert result["uptime_seconds"] == 172800
    assert result["netin_bytes"] == 1234567890
    assert result["netout_bytes"] == 987654321

    # Config
    assert "config" in result
    assert result["config"]["cores"] == 16
    assert result["config"]["ostype"] == "l26"


@pytest.mark.asyncio
async def test_get_proxmox_vm_status_sub_request_error_attached(mcp_app, mock_client):
    """Regression (WP5): a failed status/current sub-request is attached as
    status_error with degraded confidence, not silently dropped."""
    ctx = make_mock_ctx(proxmox=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(CLUSTER_RESOURCES_DATA),
            httpx.ConnectError("node offline"),  # status/current fails
            make_response(VM_CONFIG),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_vm_status")
    result = await tool_fn(ctx=ctx, vmid=100)

    assert "status_error" in result
    assert result["_meta"]["confidence"] == "medium"


@pytest.mark.asyncio
async def test_get_proxmox_vm_status_not_found(mcp_app, mock_client):
    """Returns error dict when VMID doesn't exist."""
    ctx = make_mock_ctx(proxmox=mock_client)

    mock_client.get = AsyncMock(return_value=make_response(CLUSTER_RESOURCES_DATA))

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_vm_status")
    result = await tool_fn(ctx=ctx, vmid=999)

    assert result["error"] == "not_found"


# --- Test: _get helper error handling ---


@pytest.mark.asyncio
async def test_get_helper_timeout(mcp_app, mock_client):
    """When httpx.TimeoutException raised, returns error dict."""
    ctx = make_mock_ctx(proxmox=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    # Use get_proxmox_nodes as a vehicle to test _get timeout handling
    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_helper_http_error_403(mcp_app, mock_client):
    """When 403 returned, returns error dict with status 403."""
    ctx = make_mock_ctx(proxmox=mock_client)
    error_response = httpx.Response(
        status_code=403,
        request=httpx.Request("GET", "https://test"),
    )
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("GET", "https://test"),
            response=error_response,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_proxmox_nodes")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 403
    assert "message" in result
