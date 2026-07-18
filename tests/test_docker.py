"""Tests for Docker/Portainer container management tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import docker

# --- Helpers ---


# --- Mock data ---

MOCK_ENDPOINTS = [
    {"Id": 1, "Name": "beast", "Status": 1},
    {"Id": 2, "Name": "srv", "Status": 1},
]

MOCK_ENDPOINTS_WITH_DOWN = [
    {"Id": 1, "Name": "beast", "Status": 1},
    {"Id": 2, "Name": "srv", "Status": 2},
]

MOCK_CONTAINERS_BEAST = [
    {
        "Names": ["/grafana"],
        "State": "running",
        "Image": "grafana/grafana:latest",
        "Ports": [{"PrivatePort": 3000, "PublicPort": 3000, "Type": "tcp"}],
        "Labels": {"com.docker.compose.project": "monitoring"},
        "Id": "abc123def456",
    },
    {
        "Names": ["/prometheus"],
        "State": "running",
        "Image": "prom/prometheus:latest",
        "Ports": [{"PrivatePort": 9090, "PublicPort": 9090, "Type": "tcp"}],
        "Id": "def456ghi789",
    },
]

MOCK_CONTAINERS_SRV = [
    {
        "Names": ["/sonarr"],
        "State": "running",
        "Image": "linuxserver/sonarr:latest",
        "Ports": [{"PrivatePort": 8989, "PublicPort": 8989, "Type": "tcp"}],
        "Id": "111222333444",
    },
]

MOCK_INSPECT_GRAFANA = {
    "Id": "abc123def456",
    "State": {"Status": "running", "StartedAt": "2026-04-01T00:00:00Z"},
    "Config": {
        "Image": "grafana/grafana:latest",
        "Env": ["GF_SECURITY_ADMIN_PASSWORD=secret"],
        "Labels": {"com.docker.compose.project": "monitoring"},
    },
    "NetworkSettings": {"Networks": {"bridge": {}}},
    "Mounts": [
        {
            "Source": "/data/grafana",
            "Destination": "/var/lib/grafana",
            "Type": "bind",
        }
    ],
}


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Docker tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "PORTAINER_URL", "https://fake:9443"),
        patch.object(config, "PORTAINER_API_KEY", "ptr_fake_key"),
    ):
        docker.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When PORTAINER_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with (
        patch.object(config, "PORTAINER_URL", ""),
        patch.object(config, "PORTAINER_API_KEY", "ptr_fake"),
    ):
        docker.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_none():
    """When PORTAINER_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with (
        patch.object(config, "PORTAINER_URL", None),
        patch.object(config, "PORTAINER_API_KEY", "ptr_fake"),
    ):
        docker.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_no_api_key():
    """When PORTAINER_URL is set but PORTAINER_API_KEY is missing, register() adds no tools."""
    app = FastMCP("test-skip-no-key")
    with (
        patch.object(config, "PORTAINER_URL", "https://fake:9443"),
        patch.object(config, "PORTAINER_API_KEY", None),
    ):
        docker.register(app)
    assert count_tools(app) == 0


def test_register_adds_tools_when_config_set():
    """When both Portainer config vars are set, register() adds 3 tools."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "PORTAINER_URL", "https://fake:9443"),
        patch.object(config, "PORTAINER_API_KEY", "ptr_fake_key"),
    ):
        docker.register(app)
    # list_containers, get_container_info, restart_container
    assert count_tools(app) == 3


# --- Test: list_containers ---


@pytest.mark.asyncio
async def test_list_containers_non_json_body_returns_error(mcp_app, mock_client):
    """A 200 with a non-JSON body (e.g. an HTML error page from a reverse
    proxy) must surface an invalid_response error, never crash the tool."""
    ctx = make_mock_ctx(portainer=mock_client)
    html_resp = httpx.Response(
        status_code=200,
        content=b"<html><body>502 Bad Gateway</body></html>",
        request=httpx.Request("GET", "http://test"),
    )
    mock_client.get = AsyncMock(return_value=html_resp)

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "invalid_response"


@pytest.mark.asyncio
async def test_list_containers_endpoint_error_flattened(mcp_app, mock_client):
    """Regression (WP5): a per-endpoint container-fetch error is flattened to
    {error: code, message: ...}, not a whole error dict nested under 'error'."""
    ctx = make_mock_ctx(portainer=mock_client)
    err = httpx.Response(status_code=500, request=httpx.Request("GET", "http://test"))
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 1, "Name": "beast", "Status": 1}]),
            httpx.HTTPStatusError("500", request=err.request, response=err),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["beast"]["status"] == "error"
    assert isinstance(result["beast"]["error"], str)
    assert "message" in result["beast"]


@pytest.mark.asyncio
async def test_list_containers_all_hosts(mcp_app, mock_client):
    """list_containers() with no filter returns dict keyed by endpoint name."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # /api/endpoints
            make_response(MOCK_ENDPOINTS),
            # /api/endpoints/1/docker/containers/json (beast)
            make_response(MOCK_CONTAINERS_BEAST),
            # /api/endpoints/2/docker/containers/json (srv)
            make_response(MOCK_CONTAINERS_SRV),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert "beast" in result
    assert "srv" in result

    # Beast has 2 containers
    assert result["beast"]["status"] == "up"
    assert result["beast"]["running"] == 2
    assert result["beast"]["total"] == 2
    assert len(result["beast"]["containers"]) == 2

    # Srv has 1 container
    assert result["srv"]["status"] == "up"
    assert result["srv"]["running"] == 1
    assert result["srv"]["total"] == 1


@pytest.mark.asyncio
async def test_list_containers_name_no_slash(mcp_app, mock_client):
    """Container names have no leading slash."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS[:1]),  # Only beast
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    for container in result["beast"]["containers"]:
        assert not container["name"].startswith("/")
        assert container["name"] in ("grafana", "prometheus")


@pytest.mark.asyncio
async def test_list_containers_has_required_fields(mcp_app, mock_client):
    """Containers have name, status, image, ports fields."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS[:1]),
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    for container in result["beast"]["containers"]:
        assert "name" in container
        assert "status" in container
        assert "image" in container
        assert "ports" in container
        assert "stack" in container

    by_name = {c["name"]: c for c in result["beast"]["containers"]}
    # Compose-managed container reports its project as the stack...
    assert by_name["grafana"]["stack"] == "monitoring"
    # ...and a standalone container (no compose labels) has stack None.
    assert by_name["prometheus"]["stack"] is None


@pytest.mark.asyncio
async def test_list_containers_filter_by_host(mcp_app, mock_client):
    """list_containers(host='beast') returns only the matching host."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS),
            # Only beast containers fetched (srv skipped due to filter)
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx, host="beast")

    assert "beast" in result
    assert "srv" not in result
    assert result["beast"]["total"] == 2


@pytest.mark.asyncio
async def test_list_containers_down_endpoint(mcp_app, mock_client):
    """Endpoint with Status=2 (down) returns {"status": "down", "containers": []}."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS_WITH_DOWN),
            # Only beast containers fetched (srv is down)
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["beast"]["status"] == "up"
    assert result["srv"]["status"] == "down"
    assert result["srv"]["containers"] == []


# Portainer's own endpoint names — a space in one, a capital in another, and one
# ("srv") that is no host of ours.
MOCK_ENDPOINTS_CANONICAL = [
    {"Id": 1, "Name": "docker host", "Status": 1},
    {"Id": 2, "Name": "Beast", "Status": 2},
    {"Id": 3, "Name": "srv", "Status": 2},
]


@pytest.mark.asyncio
async def test_list_containers_stamps_canonical_host(
    mcp_app, mock_client, canonical_hosts
):
    """Every endpoint carries the canonical host it runs on — including a down
    one (unreachable is not unknown) — and None when Portainer's endpoint is no
    host of ours. The map stays keyed by Portainer's name."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS_CANONICAL),
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["docker host"]["host"] == "docker-host"
    assert result["docker host"]["status"] == "up"
    assert result["Beast"]["host"] == "beast"
    assert result["Beast"]["status"] == "down"
    assert result["srv"]["host"] is None


@pytest.mark.asyncio
async def test_list_containers_per_endpoint_error(mcp_app, mock_client):
    """When a per-endpoint API call fails, returns {"status": "error"} for that endpoint."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS),
            # beast containers succeed
            make_response(MOCK_CONTAINERS_BEAST),
            # srv containers fail with HTTP error
            httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(
                    500, request=httpx.Request("GET", "http://test")
                ),
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["beast"]["status"] == "up"
    assert result["srv"]["status"] == "error"
    assert result["srv"]["containers"] == []


@pytest.mark.asyncio
async def test_list_containers_filter_by_canonical_host(
    mcp_app, mock_client, canonical_hosts
):
    """Regression (item 5): filtering by the canonical name 'docker-host' must
    match Portainer's 'docker host' endpoint, not just its raw name."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS_CANONICAL),
            make_response(MOCK_CONTAINERS_BEAST),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx, host="docker-host")

    assert "docker host" in result
    assert "Beast" not in result
    assert "srv" not in result


@pytest.mark.asyncio
async def test_list_containers_dedups_dual_stack_ports(mcp_app, mock_client):
    """Regression (item 16): dual-stack bindings (one entry per bind IP) render
    once, not twice."""
    ctx = make_mock_ctx(portainer=mock_client)
    dual_stack = [
        {
            "Names": ["/app"],
            "State": "running",
            "Image": "app:latest",
            "Ports": [
                {"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                {"IP": "::", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
            ],
            "Id": "aaa111",
        }
    ]
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 1, "Name": "beast", "Status": 1}]),
            make_response(dual_stack),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["beast"]["containers"][0]["ports"] == ["8080:80/tcp"]


@pytest.mark.asyncio
async def test_restart_container_ambiguous_multiple_hosts(mcp_app, mock_client):
    """Regression (item 17): a name present on multiple hosts returns an
    'ambiguous' error listing the hosts instead of restarting an arbitrary one."""
    ctx = make_mock_ctx(portainer=mock_client)
    watchtower = [
        {"Names": ["/watchtower"], "State": "running", "Image": "w", "Id": "w1"}
    ]
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                [
                    {"Id": 1, "Name": "beast", "Status": 1},
                    {"Id": 2, "Name": "srv", "Status": 1},
                ]
            ),
            make_response(watchtower),
            make_response(watchtower),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "restart_container")
    result = await tool_fn(ctx=ctx, name="watchtower")

    assert result["error"] == "ambiguous"
    assert set(result["hosts"]) == {"beast", "srv"}
    # No restart POST was issued.
    assert mock_client.post.await_count == 0


@pytest.mark.asyncio
async def test_restart_container_blocked_critical(mcp_app, mock_client):
    """A critical container returns error 'blocked' and never touches Portainer."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock()
    with patch.object(config, "TOPOLOGY", {"critical_containers": ["prometheus"]}):
        tool_fn = get_tool_fn(mcp_app, "restart_container")
        result = await tool_fn(ctx=ctx, name="Prometheus")

    assert result["error"] == "blocked"
    assert result["reason"] == "critical_container"
    assert mock_client.get.await_count == 0
    assert mock_client.post.await_count == 0


@pytest.mark.asyncio
async def test_restart_container_dry_run_no_post(mcp_app, mock_client):
    """dry_run=True returns a preview and issues no restart POST."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 2, "Name": "srv", "Status": 1}]),
            make_response(MOCK_CONTAINERS_SRV),
        ]
    )
    with patch.object(config, "TOPOLOGY", {"critical_containers": []}):
        tool_fn = get_tool_fn(mcp_app, "restart_container")
        result = await tool_fn(ctx=ctx, name="sonarr", dry_run=True)

    assert result["dry_run"] is True
    assert result["would_restart"] is True
    assert result["target"] == "sonarr"
    assert mock_client.post.await_count == 0


@pytest.mark.asyncio
async def test_restart_container_success_posts(mcp_app, mock_client):
    """A unique container is restarted: a POST is issued and a summary returned."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 2, "Name": "srv", "Status": 1}]),
            make_response(MOCK_CONTAINERS_SRV),
        ]
    )
    mock_client.post = AsyncMock(return_value=make_response(None, status_code=204))
    with patch.object(config, "TOPOLOGY", {"critical_containers": []}):
        tool_fn = get_tool_fn(mcp_app, "restart_container")
        result = await tool_fn(ctx=ctx, name="sonarr")

    assert result["result"] == "success"
    assert result["target"] == "sonarr"
    assert "Restarted sonarr" in result["summary"]
    assert mock_client.post.await_count == 1
    assert mock_client.post.await_args is not None
    posted_path = mock_client.post.await_args.args[0]
    assert "111222333444" in posted_path and posted_path.endswith("/restart")


@pytest.mark.asyncio
async def test_restart_container_audits_success_to_loki(mcp_app, mock_client):
    """A successful restart pushes an audit entry to Loki (action + result)."""
    loki = AsyncMock()
    ctx = make_mock_ctx(portainer=mock_client, loki=loki)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 2, "Name": "srv", "Status": 1}]),
            make_response(MOCK_CONTAINERS_SRV),
        ]
    )
    mock_client.post = AsyncMock(return_value=make_response(None, status_code=204))
    with patch.object(config, "TOPOLOGY", {"critical_containers": []}):
        tool_fn = get_tool_fn(mcp_app, "restart_container")
        await tool_fn(ctx=ctx, name="sonarr")

    loki.post.assert_awaited_once()
    pushed = loki.post.await_args.kwargs["json"]
    entry = pushed["streams"][0]["values"][0][1]
    assert '"action": "restart_container"' in entry
    assert '"result": "success"' in entry


@pytest.mark.asyncio
async def test_restart_container_post_failure_propagates(mcp_app, mock_client):
    """A failed restart POST propagates the _post error dict unchanged."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([{"Id": 2, "Name": "srv", "Status": 1}]),
            make_response(MOCK_CONTAINERS_SRV),
        ]
    )
    mock_client.post = AsyncMock(
        return_value=make_response({"message": "boom"}, status_code=500)
    )
    with patch.object(config, "TOPOLOGY", {"critical_containers": []}):
        tool_fn = get_tool_fn(mcp_app, "restart_container")
        result = await tool_fn(ctx=ctx, name="sonarr")

    assert result["error"] == "http_error"
    assert result["status"] == 500


# --- Test: get_container_info ---


@pytest.mark.asyncio
async def test_get_container_info_found(mcp_app, mock_client):
    """get_container_info(name='grafana') returns match with host context."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # /api/endpoints
            make_response(MOCK_ENDPOINTS),
            # beast containers (find_container_matches scans all endpoints first)
            make_response(MOCK_CONTAINERS_BEAST),
            # srv containers (no match)
            make_response(MOCK_CONTAINERS_SRV),
            # grafana inspect (get_container_info inspects matches afterwards)
            make_response(MOCK_INSPECT_GRAFANA),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_container_info")
    result = await tool_fn(ctx=ctx, name="grafana")

    assert result["name"] == "grafana"
    assert result["host"] == "beast"
    assert result["status"] == "running"
    assert result["started_at"] == "2026-04-01T00:00:00Z"
    assert result["image"] == "grafana/grafana:latest"
    assert result["id"] == "abc123def456"[:12]
    assert "bridge" in result["networks"]
    assert len(result["mounts"]) == 1
    assert result["mounts"][0]["source"] == "/data/grafana"


@pytest.mark.asyncio
async def test_get_container_info_not_found(mcp_app, mock_client):
    """get_container_info(name='nonexistent') returns {"error": "not_found"} dict."""
    ctx = make_mock_ctx(portainer=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_ENDPOINTS),
            make_response([]),  # beast: no containers
            make_response([]),  # srv: no containers
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_container_info")
    result = await tool_fn(ctx=ctx, name="nonexistent")

    assert result["error"] == "not_found"
    assert "message" in result


# --- Test: _get helper error handling ---


@pytest.mark.asyncio
async def test_get_helper_timeout(mcp_app, mock_client):
    """When httpx.TimeoutException raised, returns error dict."""
    ctx = make_mock_ctx(portainer=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    # Use list_containers as a vehicle to test _get timeout handling
    tool_fn = get_tool_fn(mcp_app, "list_containers")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"
    assert "message" in result
