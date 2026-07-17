"""Tests for entity graph tools."""

from unittest.mock import MagicMock, patch

import pytest
from fastmcp import FastMCP

import config
from tests.conftest import get_tool_fn
from tools import graph

TOPOLOGY = {
    "vertical_stacks": [
        {
            "host": "proxmox",
            "ip": "192.168.1.114",
            "children": [
                {
                    "type": "lxc",
                    "name": "npm",
                    "ip": "192.168.1.17",
                    "services": ["npm"],
                },
                {
                    "type": "vm",
                    "name": "docker-host",
                    "ip": "192.168.1.79",
                    "services": ["grafana"],
                },
            ],
        }
    ]
}


@pytest.mark.asyncio
async def test_same_named_child_and_service_prefers_child_view():
    """Regression (item 20): npm is both a stack child and a service named 'npm';
    the child (infrastructure) view must win, not a self-referential service."""
    app = FastMCP("test")
    graph.register(app)
    ctx = MagicMock()

    with patch.object(config, "TOPOLOGY", TOPOLOGY):
        fn = get_tool_fn(app, "show_dependency_chain")
        result = await fn(ctx, "npm")

    vs = result["vertical_stack"]
    assert vs["role"] == "lxc"
    assert vs["name"] == "npm"
    assert vs["ip"] == "192.168.1.17"
    # Both roles are still recorded in found_in.
    assert "vertical_stack:lxc" in result["found_in"]
    assert "vertical_stack:service" in result["found_in"]
    # No self-referential 'runs_on' leaked from the service representation.
    assert "runs_on" not in vs


@pytest.mark.asyncio
async def test_show_dependency_chain_stamps_meta_and_stable_keys():
    """Item 15/50: stamp _meta(source=topology); item 14: keep the full fixed
    key set (empty lists included) so the schema is stable call-to-call."""
    app = FastMCP("test")
    graph.register(app)
    ctx = MagicMock()

    with patch.object(config, "TOPOLOGY", TOPOLOGY):
        fn = get_tool_fn(app, "show_dependency_chain")
        result = await fn(ctx, "docker-host")

    assert result["_meta"]["source"] == "topology"
    for key in (
        "found_in",
        "vertical_stack",
        "ingress",
        "depends_on",
        "depended_by",
        "storage_mounts",
    ):
        assert key in result
