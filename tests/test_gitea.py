"""Tests for Gitea DevOps tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import gitea

# --- Helpers ---


# --- Mock data ---

MOCK_REPOS_SEARCH = {
    "data": [
        {
            "name": "homelab-mcp",
            "full_name": "org/homelab-mcp",
            "description": "MCP server",
            "html_url": "http://gitea/org/homelab-mcp",
            "stars_count": 0,
            "forks_count": 0,
            "open_issues_count": 2,
            "updated_at": "2026-04-01T00:00:00Z",
        },
        {
            "name": "dotfiles",
            "full_name": "user/dotfiles",
            "description": "Personal dotfiles",
            "html_url": "http://gitea/user/dotfiles",
            "stars_count": 1,
            "forks_count": 0,
            "open_issues_count": 0,
            "updated_at": "2026-03-15T00:00:00Z",
        },
    ],
    "ok": True,
}

MOCK_PRS = [
    {
        "number": 1,
        "title": "chore(deps): update dependency X",
        "state": "open",
        "user": {"login": "renovate"},
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-02T00:00:00Z",
        "labels": [{"name": "dependencies"}],
    },
    {
        "number": 2,
        "title": "feat: add new feature",
        "state": "open",
        "user": {"login": "jon"},
        "created_at": "2026-04-01T12:00:00Z",
        "updated_at": "2026-04-02T12:00:00Z",
        "labels": [],
    },
]

MOCK_CI_RUNS = {
    "workflow_runs": [
        {
            "id": 100,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-04-02T10:00:00Z",
            "updated_at": "2026-04-02T10:05:00Z",
            "event": "push",
        },
        {
            "id": 99,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-04-01T10:00:00Z",
            "updated_at": "2026-04-01T10:05:00Z",
            "event": "push",
        },
    ]
}


@pytest.fixture
def mcp_app():
    """FastMCP with Gitea tools registered (mirrors test_docker's pattern)."""
    app = FastMCP("test")
    with (
        patch.object(config, "GITEA_URL", "http://gitea:7850"),
        patch.object(config, "GITEA_TOKEN", "test-token"),
    ):
        gitea.register(app)
    return app


# --- Tests ---


@pytest.mark.asyncio
async def test_gitea_conditional_registration():
    """Tools are not registered when GITEA_URL is not set."""
    app = FastMCP("test")
    with (
        patch.object(config, "GITEA_URL", None),
        patch.object(config, "GITEA_TOKEN", None),
    ):
        gitea.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_gitea_registers_3_tools(mcp_app):
    """register() creates 3 tools when credentials are set."""
    assert count_tools(mcp_app) == 3


@pytest.mark.asyncio
async def test_gitea_repos(mcp_app):
    """get_gitea_repos returns formatted repo list."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_REPOS_SEARCH))
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_repos")
    result = await fn(ctx)

    assert result["repo_count"] == 2
    assert result["repos"][0]["name"] == "homelab-mcp"
    assert result["repos"][0]["full_name"] == "org/homelab-mcp"
    assert result["repos"][0]["url"] == "http://gitea/org/homelab-mcp"
    assert result["repos"][0]["open_issues"] == 2
    assert result["repos"][1]["name"] == "dotfiles"


@pytest.mark.asyncio
async def test_gitea_repos_api_error(mcp_app):
    """get_gitea_repos returns error dict on API failure."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "http://test"),
            response=httpx.Response(500),
        )
    )
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_repos")
    result = await fn(ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_gitea_repos_timeout(mcp_app):
    """A timeout is reported as 'timeout', not a generic connection_error.

    Regression for the shared lib.http adoption: gitea's old _get caught bare
    Exception and mislabeled every timeout as connection_error.
    """
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_repos")
    result = await fn(ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_gitea_repos_connection_error(mcp_app):
    """A transport-level failure surfaces {'error': 'connection_error'}."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_repos")
    result = await fn(ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_gitea_pull_requests_with_repo(mcp_app):
    """get_gitea_pull_requests with repo param returns PRs for that repo."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response(MOCK_PRS))
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_pull_requests")
    result = await fn(ctx, repo="org/homelab-mcp")

    assert result["pr_count"] == 2
    # Renovate PR is included
    assert result["pull_requests"][0]["title"] == "chore(deps): update dependency X"
    assert result["pull_requests"][0]["author"] == "renovate"
    assert result["pull_requests"][0]["labels"] == ["dependencies"]


@pytest.mark.asyncio
async def test_gitea_pull_requests_cross_repo(mcp_app):
    """get_gitea_pull_requests without repo param does cross-repo aggregation."""
    mock_client = AsyncMock()

    def side_effect(path, params=None):
        if "/repos/search" in path:
            return make_response(MOCK_REPOS_SEARCH)
        elif "/pulls" in path:
            return make_response(MOCK_PRS)
        return make_response([])

    mock_client.get = AsyncMock(side_effect=side_effect)
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_pull_requests")
    result = await fn(ctx)

    # 2 repos x 2 PRs each = 4 total
    assert result["pr_count"] == 4


@pytest.mark.asyncio
async def test_gitea_pull_requests_empty(mcp_app):
    """get_gitea_pull_requests returns pr_count=0 when no PRs."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=make_response([]))
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_pull_requests")
    result = await fn(ctx, repo="org/homelab-mcp")

    assert result["pr_count"] == 0
    assert result["pull_requests"] == []


@pytest.mark.asyncio
async def test_gitea_ci_runs(mcp_app):
    """get_gitea_ci_runs returns sorted runs with limit."""
    mock_client = AsyncMock()

    def side_effect(path, params=None):
        if "/repos/search" in path:
            return make_response(MOCK_REPOS_SEARCH)
        elif "/actions/runs" in path:
            return make_response(MOCK_CI_RUNS)
        return make_response({"workflow_runs": []})

    mock_client.get = AsyncMock(side_effect=side_effect)
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_ci_runs")
    result = await fn(ctx, limit=3)

    assert result["run_count"] == 3
    # Sorted by created_at desc: 2026-04-02 first
    assert result["runs"][0]["created"] == "2026-04-02T10:00:00Z"


@pytest.mark.asyncio
async def test_gitea_ci_runs_per_repo_limit_tracks_limit(mcp_app):
    """Regression (item 19): the per-repo fetch limit follows the `limit`
    argument (capped) instead of a hardcoded 5, so a busy repo's recent runs
    are not dropped before the global sort."""
    captured = {}
    mock_client = AsyncMock()

    def side_effect(path, params=None):
        if "/repos/search" in path:
            return make_response(MOCK_REPOS_SEARCH)
        elif "/actions/runs" in path:
            captured["limit"] = (params or {}).get("limit")
            return make_response(MOCK_CI_RUNS)
        return make_response({"workflow_runs": []})

    mock_client.get = AsyncMock(side_effect=side_effect)
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_ci_runs")
    await fn(ctx, limit=20)

    assert captured["limit"] == 20


@pytest.mark.asyncio
async def test_gitea_ci_runs_403_graceful(mcp_app):
    """get_gitea_ci_runs handles 403 gracefully (skip repo, don't crash)."""
    mock_client = AsyncMock()

    def side_effect(path, params=None):
        if "/repos/search" in path:
            return make_response(MOCK_REPOS_SEARCH)
        elif "/actions/runs" in path:
            raise httpx.HTTPStatusError(
                "Forbidden",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(403),
            )
        return make_response({"workflow_runs": []})

    mock_client.get = AsyncMock(side_effect=side_effect)
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_ci_runs")
    result = await fn(ctx)

    # Should not crash, returns empty runs
    assert result["run_count"] == 0
    assert result["runs"] == []


@pytest.mark.asyncio
async def test_gitea_ci_runs_custom_limit(mcp_app):
    """get_gitea_ci_runs respects custom limit parameter."""
    mock_client = AsyncMock()

    def side_effect(path, params=None):
        if "/repos/search" in path:
            return make_response(MOCK_REPOS_SEARCH)
        elif "/actions/runs" in path:
            return make_response(MOCK_CI_RUNS)
        return make_response({"workflow_runs": []})

    mock_client.get = AsyncMock(side_effect=side_effect)
    ctx = make_mock_ctx(gitea=mock_client)

    fn = get_tool_fn(mcp_app, "get_gitea_ci_runs")
    result = await fn(ctx, limit=1)

    assert result["run_count"] == 1
    assert len(result["runs"]) == 1
