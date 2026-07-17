"""Schema guard for tool annotations.

mcp.types.ToolAnnotations is configured with extra='allow', so a misspelled hint
key (e.g. 'readonlyHint') validates silently and the real safety hint stays
unset. This test registers every tool module and asserts the annotation keys are
a known subset, readOnlyHint is always present, and the write tools declare
readOnlyHint False -- catching a future typo that would strip a hint.
"""

import os

# Force stdio before server's module-level create_server() runs, so it does not
# try to build the Authentik auth chain (config may hold a non-stdio value).
os.environ["MCP_TRANSPORT"] = "stdio"

import config  # noqa: E402

config.MCP_TRANSPORT = "stdio"

import pytest  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

import server  # noqa: E402

ALLOWED_KEYS = {
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
    "title",
}

# The deliberate write tools (readOnlyHint False).
WRITE_TOOLS = {
    "restart_container",
    "safe_restart_container",
    "create_vikunja_task",
    "update_vikunja_task",
    "create_task_from_alert",
    "overseerr_approve_request",
    "overseerr_decline_request",
    "refresh_registries",
    "refresh_docs",
}

# searxng web tools legitimately interact with an open world; every other tool
# queries a fixed set of homelab services and declares openWorldHint False.
OPEN_WORLD_TOOLS = {
    "web_search",
    "search_code",
    "search_academic",
    "search_news",
    "fetch_page",
}

_CRED_SUFFIXES = (
    "_URL",
    "_TOKEN",
    "_TOKEN_ID",
    "_TOKEN_SECRET",
    "_API_KEY",
    "_KEY_ID",
    "_APP_KEY",
    "_PASSWORD",
    "_USERNAME",
    "_EMAIL",
    "_SECRET",
    "_ID",
)


@pytest.fixture
def all_tools(monkeypatch):
    """Register every tool module onto one FastMCP with all credentials set."""
    for name in dir(config):
        if not name.isupper():
            continue
        if any(name.endswith(s) for s in _CRED_SUFFIXES):
            monkeypatch.setattr(config, name, "x", raising=False)

    mcp = FastMCP("test")
    for module in server._TOOL_MODULES:
        module.register(mcp)
    server.resources.register(mcp)
    return mcp


@pytest.mark.asyncio
async def test_all_tools_have_valid_annotations(all_tools):
    tools = await all_tools.list_tools()
    assert tools, "no tools registered"

    for tool in tools:
        name = tool.name
        ann = tool.annotations
        assert ann is not None, f"{name} has no annotations"
        # Only the known hint keys may appear (guards against typos).
        keys = {k for k, v in ann.model_dump(exclude_none=True).items()}
        assert keys <= ALLOWED_KEYS, f"{name} has unexpected annotation keys: {keys}"
        assert ann.readOnlyHint is not None, f"{name} missing readOnlyHint"


@pytest.mark.asyncio
async def test_write_tools_marked_not_readonly(all_tools):
    tools = {t.name: t for t in await all_tools.list_tools()}
    for name in WRITE_TOOLS:
        assert name in tools, f"expected write tool {name} not registered"
        assert tools[name].annotations.readOnlyHint is False, name
    # And nothing else claims to be a write.
    for name, tool in tools.items():
        if tool.annotations.readOnlyHint is False:
            assert name in WRITE_TOOLS, f"unexpected write tool: {name}"


@pytest.mark.asyncio
async def test_internal_tools_declare_closed_world(all_tools):
    tools = await all_tools.list_tools()
    for tool in tools:
        name = tool.name
        if name in OPEN_WORLD_TOOLS:
            continue
        assert tool.annotations.openWorldHint is False, (
            f"{name} should declare openWorldHint False"
        )
