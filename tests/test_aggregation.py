"""Tests for aggregation overview tools."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastmcp import FastMCP

from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import aggregation

# --- Helpers ---


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with aggregation tools registered."""
    app = FastMCP("test")
    aggregation.register(app)
    return app


# --- Mock data factories ---


def _mock_prometheus_client():
    """Mock prometheus httpx.AsyncClient returning host metrics."""
    client = AsyncMock(spec=httpx.AsyncClient)

    def _make_prom_response(query):
        """Return different results based on PromQL query."""
        if "cpu" in query.lower() or "idle" in query.lower():
            return make_response(
                {
                    "data": {
                        "result": [
                            {"metric": {"instance": "node1:9100"}, "value": [1, "25.3"]}
                        ]
                    },
                }
            )
        elif "memavailable" in query.lower() or "mem" in query.lower():
            return make_response(
                {
                    "data": {
                        "result": [
                            {"metric": {"instance": "node1:9100"}, "value": [1, "60.0"]}
                        ]
                    },
                }
            )
        elif "filesystem" in query.lower():
            return make_response(
                {
                    "data": {
                        "result": [
                            {"metric": {"instance": "node1:9100"}, "value": [1, "45.2"]}
                        ]
                    },
                }
            )
        return make_response({"data": {"result": []}})

    async def get_side_effect(path, **kwargs):
        params = kwargs.get("params", {})
        query = params.get("query", "")
        return _make_prom_response(query)

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_portainer_client():
    """Mock portainer httpx.AsyncClient returning container counts."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def get_side_effect(path, **kwargs):
        if path == "/api/endpoints":
            return make_response(
                [
                    {"Id": 1, "Name": "docker-host-1", "Status": 1},
                ]
            )
        elif "/docker/containers/json" in path:
            return make_response(
                [
                    {"State": "running"},
                    {"State": "running"},
                    {"State": "exited"},
                ]
            )
        return make_response({})

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_synology_client():
    """Mock synology SessionAuthManager."""
    client = AsyncMock()
    client.get = AsyncMock(
        return_value=make_response(
            {
                "success": True,
                "data": {
                    "volumes": [
                        {
                            "id": "volume_1",
                            "size": {
                                "total": "10000000000000",
                                "used": "6000000000000",
                            },
                        },
                    ],
                },
            }
        )
    )
    return client


def _mock_pbs_client():
    """Mock PBS httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=make_response(
            {
                "data": [
                    {
                        "store": "main",
                        "total": 1000000000000,
                        "used": 400000000000,
                        "avail": 600000000000,
                    },
                ],
            }
        )
    )
    return client


def _mock_backblaze_client():
    """Mock backblaze SessionAuthManager."""
    client = AsyncMock()
    client.ensure_auth = AsyncMock()
    client._strategy = MagicMock()
    client._strategy.account_id = "test-account"
    client.post = AsyncMock(
        return_value=make_response(
            {
                "buckets": [
                    {
                        "bucketName": "backup-1",
                        "bucketId": "id1",
                        "bucketType": "allPrivate",
                    },
                    {
                        "bucketName": "backup-2",
                        "bucketId": "id2",
                        "bucketType": "allPrivate",
                    },
                ],
            }
        )
    )
    return client


def _mock_plex_client():
    """Mock plex httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=make_response(
            {
                "MediaContainer": {
                    "size": 1,
                    "Metadata": [
                        {
                            "title": "Test Movie",
                            "type": "movie",
                            "viewOffset": 3000000,
                            "duration": 6000000,
                            "User": {"title": "Jon"},
                        }
                    ],
                },
            }
        )
    )
    return client


def _mock_loki_client():
    """Mock loki httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=make_response(
            {
                "data": {
                    "result": [
                        {
                            "stream": {"job": "containers"},
                            "values": [
                                ["1700000000000000000", "error: something failed"],
                            ],
                        },
                    ],
                },
            }
        )
    )
    return client


def _mock_sonarr_client():
    """Mock sonarr httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def get_side_effect(path, **kwargs):
        if "calendar" in path:
            return make_response(
                [{"id": 1, "hasFile": False}, {"id": 2, "hasFile": True}]
            )
        elif "queue" in path:
            return make_response({"totalRecords": 3, "records": []})
        return make_response([])

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_radarr_client():
    """Mock radarr httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def get_side_effect(path, **kwargs):
        if "calendar" in path:
            return make_response(
                [
                    {"id": 1, "hasFile": False},
                    {"id": 2, "hasFile": False},
                    {"id": 3, "hasFile": True},
                ]
            )
        elif "queue" in path:
            return make_response({"totalRecords": 1, "records": []})
        return make_response([])

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_transmission_client():
    """Mock transmission SessionAuthManager."""
    client = AsyncMock()
    client.post = AsyncMock(
        return_value=make_response(
            {
                "result": "success",
                "arguments": {
                    "torrents": [
                        {"status": 4, "rateDownload": 5000000, "rateUpload": 100000},
                        {"status": 6, "rateDownload": 0, "rateUpload": 200000},
                    ],
                },
            }
        )
    )
    return client


def _mock_overseerr_client():
    """Mock overseerr httpx.AsyncClient.

    Total page reports 15 results; the filter=pending page reports 7 (more than
    a single 20-item window would surface), so pending_count must come from the
    filtered pageInfo, not from counting status==1 in one page.
    """
    client = AsyncMock(spec=httpx.AsyncClient)

    async def _get(path, params=None):
        if (params or {}).get("filter") == "pending":
            return make_response({"pageInfo": {"results": 7}, "results": []})
        return make_response({"pageInfo": {"results": 15}, "results": []})

    client.get = AsyncMock(side_effect=_get)
    return client


def _mock_proxmox_client():
    """Mock proxmox httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=make_response(
            {
                "data": [
                    {
                        "node": "pve1",
                        "status": "online",
                        "cpu": 0.253,
                        "mem": 8000000000,
                        "maxmem": 16000000000,
                    },
                ],
            }
        )
    )
    return client


def _mock_prowlarr_client():
    """Mock prowlarr httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def get_side_effect(path, **kwargs):
        if "/api/v1/health" in path:
            return make_response(
                [
                    {"source": "IndexerCheck", "message": "Indexer1 is down"},
                ]
            )
        # /api/v1/indexer
        return make_response(
            [
                {"id": 1, "name": "Indexer1", "enable": True},
                {"id": 2, "name": "Indexer2", "enable": True},
                {"id": 3, "name": "Indexer3", "enable": False},
            ]
        )

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_crowdsec_client():
    """Mock crowdsec httpx client with bouncer API key."""
    client = AsyncMock()

    async def get_side_effect(path, **kwargs):
        if "/v1/decisions" in path:
            return make_response(
                [
                    {"id": 1, "type": "ban", "value": "1.2.3.4", "origin": "crowdsec"},
                    {"id": 2, "type": "ban", "value": "5.6.7.8", "origin": "crowdsec"},
                    {"id": 3, "type": "ban", "value": "9.9.9.9", "origin": "CAPI"},
                ]
            )
        return make_response([])

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_npm_client():
    """Mock npm SessionAuthManager."""
    client = AsyncMock()

    async def get_side_effect(path, **kwargs):
        if "proxy-hosts" in path:
            return make_response(
                [
                    {"id": 1, "domain_names": ["app1.example.com"]},
                    {"id": 2, "domain_names": ["app2.example.com"]},
                ]
            )
        elif "dead-hosts" in path:
            return make_response(
                [
                    {"id": 1, "domain_names": ["dead.example.com"]},
                ]
            )
        elif "certificates" in path:
            return make_response(
                [
                    {
                        "id": 1,
                        "nice_name": "example.com",
                        "expires_on": "2027-01-01T00:00:00Z",
                    },
                ]
            )
        return make_response([])

    client.get = AsyncMock(side_effect=get_side_effect)
    return client


def _mock_myspeed_client():
    """Mock myspeed httpx.AsyncClient — API returns Mbps directly."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=make_response(
            [
                {"download": 862.2, "upload": 883.3, "ping": 8},
            ]
        )
    )
    return client


# --- Test: Registration ---


def test_aggregation_always_registers(mcp_app):
    """Aggregation tools register without config guards -- always 3 tools."""
    assert count_tools(mcp_app) == 3


# --- Test: get_homelab_overview ---


@pytest.mark.asyncio
async def test_homelab_overview_all_sections(mcp_app):
    """Returns all 6 sections when all clients are available."""
    ctx = make_mock_ctx(
        prometheus=_mock_prometheus_client(),
        portainer=_mock_portainer_client(),
        synology=_mock_synology_client(),
        pbs=_mock_pbs_client(),
        backblaze=_mock_backblaze_client(),
        plex=_mock_plex_client(),
        loki=_mock_loki_client(),
        myspeed=_mock_myspeed_client(),
    )

    tool_fn = get_tool_fn(mcp_app, "get_homelab_overview")
    result = await tool_fn(ctx=ctx)

    assert "hosts" in result
    assert "containers" in result
    assert "storage" in result
    assert "media" in result
    assert "errors" in result
    assert "speed_test" in result

    # hosts should have instance data with the exact parsed metric values.
    assert "error" not in result["hosts"]
    node = result["hosts"]["node1:9100"]
    assert node["cpu_percent"] == 25.3
    assert node["ram_percent"] == 60.0
    assert node["disk_percent"] == 45.2

    # containers should have endpoint data with the parsed running/total counts.
    assert "error" not in result["containers"]

    # storage should have sub-sections
    assert "nas" in result["storage"]
    assert "pbs" in result["storage"]
    assert "backblaze" in result["storage"]

    # speed_test should have metrics
    assert "download_mbps" in result["speed_test"]


@pytest.mark.asyncio
async def test_homelab_overview_empty_context(mcp_app):
    """All 6 sections return error dicts when no clients configured."""
    ctx = make_mock_ctx()  # empty lifespan_context

    tool_fn = get_tool_fn(mcp_app, "get_homelab_overview")
    result = await tool_fn(ctx=ctx)

    assert result["hosts"]["error"] == "not_configured"
    assert result["containers"]["error"] == "not_configured"
    # storage has sub-sections, each with error
    assert result["storage"]["nas"]["error"] == "not_configured"
    assert result["storage"]["pbs"]["error"] == "not_configured"
    assert result["storage"]["backblaze"]["error"] == "not_configured"
    assert result["media"]["error"] == "not_configured"
    assert result["errors"]["error"] == "not_configured"
    assert result["speed_test"]["error"] == "not_configured"


@pytest.mark.asyncio
async def test_homelab_overview_timeout(mcp_app):
    """When prometheus times out, hosts section shows unreachable error."""
    timeout_client = AsyncMock(spec=httpx.AsyncClient)
    timeout_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    ctx = make_mock_ctx(prometheus=timeout_client)

    tool_fn = get_tool_fn(mcp_app, "get_homelab_overview")
    result = await tool_fn(ctx=ctx)

    assert result["hosts"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_homelab_overview_section_unreachable_on_unexpected_exception(mcp_app):
    """safe_gather converts a non-httpx exception in one section into an
    {'error': 'unreachable'} marker instead of failing the whole overview."""
    boom_client = AsyncMock(spec=httpx.AsyncClient)
    boom_client.get = AsyncMock(side_effect=KeyError("unexpected shape"))

    ctx = make_mock_ctx(prometheus=boom_client)

    tool_fn = get_tool_fn(mcp_app, "get_homelab_overview")
    result = await tool_fn(ctx=ctx)

    assert result["hosts"]["error"] == "unreachable"


@pytest.mark.asyncio
async def test_npm_summary_flags_soon_expiring_cert():
    """A cert expiring inside the 30-day window is reported in expiring_certs."""
    soon = (datetime.now(UTC) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    async def get_side_effect(path, **kwargs):
        if "proxy-hosts" in path:
            return make_response([{"id": 1, "domain_names": ["app.example.com"]}])
        if "dead-hosts" in path:
            return make_response([])
        if "certificates" in path:
            return make_response(
                [{"id": 1, "nice_name": "app.example.com", "expires_on": soon}]
            )
        return make_response([])

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get_side_effect)
    ctx = make_mock_ctx(npm=client)

    result = await aggregation._npm_summary(ctx)

    assert "expiring_certs" in result
    assert result["expiring_certs"][0]["domain"] == "app.example.com"
    assert result["expiring_certs"][0]["expires_on"] == soon


@pytest.mark.asyncio
async def test_homelab_overview_http_error_not_empty_success(mcp_app):
    """Regression (WP5): a 5xx JSON error body from Prometheus must surface an
    error, not parse to an empty 'no hosts monitored' result."""
    err_client = AsyncMock(spec=httpx.AsyncClient)
    err_client.get = AsyncMock(
        return_value=make_response({"error": "server"}, status_code=500)
    )

    ctx = make_mock_ctx(prometheus=err_client)

    tool_fn = get_tool_fn(mcp_app, "get_homelab_overview")
    result = await tool_fn(ctx=ctx)

    assert "error" in result["hosts"]


# --- Test: get_media_overview ---


@pytest.mark.asyncio
async def test_media_overview_all_sections(mcp_app):
    """Returns all 6 sections when all media clients available."""
    ctx = make_mock_ctx(
        plex=_mock_plex_client(),
        sonarr=_mock_sonarr_client(),
        radarr=_mock_radarr_client(),
        transmission=_mock_transmission_client(),
        overseerr=_mock_overseerr_client(),
        prowlarr=_mock_prowlarr_client(),
    )

    tool_fn = get_tool_fn(mcp_app, "get_media_overview")
    result = await tool_fn(ctx=ctx)

    assert "plex" in result
    assert "sonarr" in result
    assert "radarr" in result
    assert "transmission" in result
    assert "overseerr" in result
    assert result["overseerr"]["total_count"] == 15
    assert result["overseerr"]["pending_count"] == 7
    assert "prowlarr" in result

    # Plex should have the exact active-stream count from the single mock session.
    assert result["plex"]["active_streams"] == 1

    # Sonarr should have counts
    assert "upcoming_count" in result["sonarr"]
    assert "queue_count" in result["sonarr"]

    # Radarr counts skip movies already on disk, like Sonarr (3 in calendar, 1 has a file)
    assert result["radarr"]["upcoming_count"] == 2
    assert result["radarr"]["queue_count"] == 1

    # Transmission should have torrent counts
    assert "torrent_count" in result["transmission"]

    # Prowlarr should have indexer counts
    assert "indexer_count" in result["prowlarr"]
    assert result["prowlarr"]["enabled_count"] == 2
    assert result["prowlarr"]["disabled_count"] == 1


@pytest.mark.asyncio
async def test_media_overview_empty_context(mcp_app):
    """All 6 sections return error dicts when no media clients configured."""
    ctx = make_mock_ctx()

    tool_fn = get_tool_fn(mcp_app, "get_media_overview")
    result = await tool_fn(ctx=ctx)

    assert result["plex"]["error"] == "not_configured"
    assert result["sonarr"]["error"] == "not_configured"
    assert result["radarr"]["error"] == "not_configured"
    assert result["transmission"]["error"] == "not_configured"
    assert result["overseerr"]["error"] == "not_configured"
    assert result["prowlarr"]["error"] == "not_configured"


# --- Test: get_infra_overview ---


@pytest.mark.asyncio
async def test_infra_overview_all_sections(mcp_app):
    """Returns all 5 sections when all infra clients available."""
    ctx = make_mock_ctx(
        proxmox=_mock_proxmox_client(),
        portainer=_mock_portainer_client(),
        synology=_mock_synology_client(),
        pbs=_mock_pbs_client(),
        backblaze=_mock_backblaze_client(),
        crowdsec=_mock_crowdsec_client(),
        npm=_mock_npm_client(),
    )

    tool_fn = get_tool_fn(mcp_app, "get_infra_overview")
    result = await tool_fn(ctx=ctx)

    assert "proxmox" in result
    assert "docker" in result
    assert "storage" in result
    assert "crowdsec" in result
    assert "npm" in result

    # Proxmox nodes carry the exact rounded cpu/ram percentages.
    assert "nodes" in result["proxmox"]
    pve1 = result["proxmox"]["nodes"]["pve1"]
    assert pve1["cpu_percent"] == 25.3  # 0.253 * 100
    assert pve1["ram_percent"] == 50.0  # 8e9 / 16e9

    # Docker should have endpoint data
    assert "error" not in result["docker"]

    # CrowdSec should have ban and alert counts
    assert result["crowdsec"]["local_ban_count"] == 2
    assert result["crowdsec"]["community_blocklist_count"] == 1

    # NPM should have proxy and dead host counts
    assert result["npm"]["proxy_host_count"] == 2
    assert result["npm"]["dead_host_count"] == 1


@pytest.mark.asyncio
async def test_infra_overview_empty_context(mcp_app):
    """All 5 sections return error dicts when no infra clients configured."""
    ctx = make_mock_ctx()

    tool_fn = get_tool_fn(mcp_app, "get_infra_overview")
    result = await tool_fn(ctx=ctx)

    assert result["proxmox"]["error"] == "not_configured"
    assert result["docker"]["error"] == "not_configured"
    assert result["storage"]["nas"]["error"] == "not_configured"
    assert result["storage"]["pbs"]["error"] == "not_configured"
    assert result["storage"]["backblaze"]["error"] == "not_configured"
    assert result["crowdsec"]["error"] == "not_configured"
    assert result["npm"]["error"] == "not_configured"
