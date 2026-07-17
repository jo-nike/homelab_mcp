"""Tests for Prometheus monitoring tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import prometheus

# --- Helpers ---


def prom_vector(results):
    """Build a Prometheus vector response shape."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": results,
        },
    }


def prom_result(instance, value, extra_labels=None):
    """Build a single Prometheus vector result item."""
    metric = {"instance": instance}
    if extra_labels:
        metric.update(extra_labels)
    return {"metric": metric, "value": [1712000000, str(value)]}


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Prometheus tools registered."""
    app = FastMCP("test")
    with patch.object(config, "PROMETHEUS_URL", "http://fake:9090"):
        prometheus.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Test: _inject_host_filter helper ---


def test_inject_host_filter_empty_brace():
    """Empty selector metric{} must not gain a leading comma (regression: the
    ram_percent query produced {,instance="..."} which Prometheus rejected)."""
    q = "(1 - node_memory_MemAvailable_bytes{} / node_memory_MemTotal_bytes) * 100"
    result = prometheus._inject_host_filter(q, "beast")
    assert result == (
        '(1 - node_memory_MemAvailable_bytes{instance="beast"}'
        " / node_memory_MemTotal_bytes) * 100"
    )
    assert "{," not in result


def test_inject_host_filter_existing_matcher():
    """A metric with an existing matcher gets instance appended after a comma."""
    q = '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    result = prometheus._inject_host_filter(q, "beast")
    assert '{mode="idle",instance="beast"}' in result


def test_inject_host_filter_bare_metric():
    """A bare metric name (no braces) gets a full {instance="..."} selector."""
    assert (
        prometheus._inject_host_filter("node_load1", "beast")
        == 'node_load1{instance="beast"}'
    )
    # Expression with no braces at all: selector appended to the whole thing.
    assert (
        prometheus._inject_host_filter("time() - node_boot_time_seconds", "beast")
        == 'time() - node_boot_time_seconds{instance="beast"}'
    )


def test_inject_host_filter_escapes_quotes():
    """Regression (item 30): a host value with a double quote must be escaped so
    it cannot break out of the label matcher."""
    result = prometheus._inject_host_filter("node_load1", 'be"ast')
    assert result == 'node_load1{instance="be\\"ast"}'


@pytest.mark.asyncio
async def test_get_container_stats_keys_by_name_and_host():
    """Regression (item 23): same-named containers on different hosts must not
    collide -- both survive and CPU/memory stay matched per host."""

    def _series(name, host, value):
        return {"metric": {"name": name, "instance": host}, "value": [0, str(value)]}

    cpu = {
        "data": {
            "result": [
                _series("promtail", "docker-host", 5.0),
                _series("promtail", "beast", 9.0),
            ]
        }
    }
    mem = {
        "data": {
            "result": [
                _series("promtail", "docker-host", 100),
                _series("promtail", "beast", 200),
            ]
        }
    }

    async def fake_get(path, params=None):
        q = (params or {}).get("query", "")
        return make_response(cpu if "cpu" in q else mem)

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    ctx = MagicMock()
    ctx.lifespan_context = {"prometheus": client}

    app = FastMCP("test")
    with patch.object(config, "PROMETHEUS_URL", "http://prom:9090"):
        prometheus.register(app)
        fn = get_tool_fn(app, "get_container_stats")
        result = await fn(ctx, sort_by="cpu", limit=10)

    entries = {(c["container_name"], c["host"]): c for c in result["containers"]}
    assert len(entries) == 2
    assert entries[("promtail", "docker-host")]["memory_bytes"] == 100
    assert entries[("promtail", "beast")]["memory_bytes"] == 200


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When PROMETHEUS_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "PROMETHEUS_URL", ""):
        prometheus.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_none():
    """When PROMETHEUS_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "PROMETHEUS_URL", None):
        prometheus.register(app)
    assert count_tools(app) == 0


def test_register_adds_tools_when_url_set():
    """When PROMETHEUS_URL is set, register() adds all 7 tools."""
    app = FastMCP("test-add")
    with patch.object(config, "PROMETHEUS_URL", "http://fake:9090"):
        prometheus.register(app)
    assert count_tools(app) == 7


# --- Test: get_host_summary ---


@pytest.mark.asyncio
async def test_get_host_summary_all(mcp_app, mock_client):
    """Returns dict with keys for each host, each containing cpu/ram/disk/load/uptime."""
    ctx = make_mock_ctx(prometheus=mock_client)

    # Mock 5 queries: cpu, ram, disk, load, uptime -- each returns results for 2 hosts
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 23.5),
                        prom_result("docker-host", 45.2),
                    ]
                )
            ),
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 60.1),
                        prom_result("docker-host", 72.3),
                    ]
                )
            ),
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 35.0),
                        prom_result("docker-host", 55.5),
                    ]
                )
            ),
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 1.5),
                        prom_result("docker-host", 3.2),
                    ]
                )
            ),
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 86400.0),
                        prom_result("docker-host", 172800.0),
                    ]
                )
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_host_summary")
    result = await tool_fn(ctx=ctx)

    assert "beast" in result
    assert "docker-host" in result
    assert result["beast"]["cpu_percent"] == 23.5
    assert result["beast"]["ram_percent"] == 60.1
    assert result["beast"]["disk_percent"] == 35.0
    assert result["beast"]["load_1m"] == 1.5
    assert result["beast"]["uptime_seconds"] == 86400.0
    assert result["docker-host"]["cpu_percent"] == 45.2


@pytest.mark.asyncio
async def test_get_host_summary_single(mcp_app, mock_client):
    """When host='beast', returns dict with only 'beast' key."""
    ctx = make_mock_ctx(prometheus=mock_client)

    # Each query returns only beast results when filtered
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(prom_vector([prom_result("beast", 23.5)])),
            make_response(prom_vector([prom_result("beast", 60.1)])),
            make_response(prom_vector([prom_result("beast", 35.0)])),
            make_response(prom_vector([prom_result("beast", 1.5)])),
            make_response(prom_vector([prom_result("beast", 86400.0)])),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_host_summary")
    result = await tool_fn(ctx=ctx, host="beast")

    assert "beast" in result
    # Only beast plus the _meta envelope
    assert set(result) == {"beast", "_meta"}
    assert result["_meta"]["source"] == "prometheus"
    # ram_percent must survive host filtering (regression: it was being dropped
    # because the injected filter produced invalid PromQL for the ram query)
    assert result["beast"]["ram_percent"] == 60.1
    assert result["beast"]["cpu_percent"] == 23.5
    assert result["beast"]["uptime_seconds"] == 86400.0


# --- Test: get_container_stats ---


@pytest.mark.asyncio
async def test_get_container_stats_cpu(mcp_app, mock_client):
    """Returns list of dicts sorted by cpu_percent descending."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # CPU query
            make_response(
                prom_vector(
                    [
                        prom_result("docker-host", 5.2, {"name": "nginx"}),
                        prom_result("docker-host", 12.1, {"name": "postgres"}),
                        prom_result("docker-host", 1.3, {"name": "redis"}),
                    ]
                )
            ),
            # Memory query
            make_response(
                prom_vector(
                    [
                        prom_result("docker-host", 104857600, {"name": "nginx"}),
                        prom_result("docker-host", 524288000, {"name": "postgres"}),
                        prom_result("docker-host", 52428800, {"name": "redis"}),
                    ]
                )
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_container_stats")
    result = await tool_fn(ctx=ctx, sort_by="cpu", limit=10)

    assert result["_meta"]["source"] == "prometheus"
    containers = result["containers"]
    assert isinstance(containers, list)
    assert len(containers) == 3
    # Sorted by CPU descending
    assert containers[0]["container_name"] == "postgres"
    assert containers[0]["cpu_percent"] == 12.1
    assert containers[1]["container_name"] == "nginx"
    assert containers[2]["container_name"] == "redis"
    # Memory data is present
    assert "memory_bytes" in containers[0]


@pytest.mark.asyncio
async def test_get_container_stats_memory(mcp_app, mock_client):
    """When sort_by='memory', returns list sorted by memory_bytes descending."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # CPU query
            make_response(
                prom_vector(
                    [
                        prom_result("docker-host", 5.2, {"name": "nginx"}),
                        prom_result("docker-host", 12.1, {"name": "postgres"}),
                    ]
                )
            ),
            # Memory query
            make_response(
                prom_vector(
                    [
                        prom_result("docker-host", 104857600, {"name": "nginx"}),
                        prom_result("docker-host", 524288000, {"name": "postgres"}),
                    ]
                )
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_container_stats")
    result = await tool_fn(ctx=ctx, sort_by="memory", limit=10)

    assert result["_meta"]["source"] == "prometheus"
    containers = result["containers"]
    assert isinstance(containers, list)
    # postgres has more memory, should be first
    assert containers[0]["container_name"] == "postgres"
    assert containers[0]["memory_bytes"] == 524288000
    assert containers[1]["container_name"] == "nginx"


# --- Test: get_gpu_status ---


@pytest.mark.asyncio
async def test_get_gpu_status(mcp_app, mock_client):
    """Returns dict with GPU hosts containing utilization/vram/temp/power/name/processes."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # Utilization query
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 45.0),
                        prom_result("ai-vm", 80.0),
                    ]
                )
            ),
            # VRAM percent query
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 62.3),
                        prom_result("ai-vm", 95.1),
                    ]
                )
            ),
            # Temperature query
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 71.0),
                        prom_result("ai-vm", 82.0),
                    ]
                )
            ),
            # Power draw query
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 220.5),
                        prom_result("ai-vm", 150.0),
                    ]
                )
            ),
            # VRAM used bytes query
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 15032385536),
                        prom_result("ai-vm", 22548578304),
                    ]
                )
            ),
            # VRAM total bytes query (nvidia_smi_memory_total_bytes -- carries
            # only instance/job/uuid, no 'name' label in prod).
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 25757220864),
                        prom_result("ai-vm", 25757220864),
                    ]
                )
            ),
            # Name lookup query (nvidia_smi_gpu_info carries the 'name' label).
            make_response(
                prom_vector(
                    [
                        prom_result("beast", 1, {"name": "NVIDIA GeForce RTX 3090"}),
                        prom_result("ai-vm", 1, {"name": "NVIDIA GeForce RTX 4090"}),
                    ]
                )
            ),
            # Process memory query (top-5 by memory)
            make_response(
                prom_vector(
                    [
                        prom_result(
                            "beast",
                            8000000000,
                            {"name": "python", "container": "ollama", "type": "C"},
                        ),
                        prom_result(
                            "beast",
                            3000000000,
                            {"name": "Xorg", "container": "", "type": "G"},
                        ),
                    ]
                )
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_gpu_status")
    result = await tool_fn(ctx=ctx)

    assert "beast" in result
    assert "ai-vm" in result
    assert result["beast"]["utilization_percent"] == 45.0
    assert result["beast"]["vram_percent"] == 62.3
    assert result["beast"]["temperature_celsius"] == 71.0
    assert result["ai-vm"]["utilization_percent"] == 80.0
    # New fields
    assert result["beast"]["power_draw_watts"] == 220.5
    assert result["beast"]["vram_used_bytes"] == 15032385536
    assert result["beast"]["vram_total_bytes"] == 25757220864
    assert result["beast"]["name"] == "NVIDIA GeForce RTX 3090"
    assert result["ai-vm"]["name"] == "NVIDIA GeForce RTX 4090"
    # Processes sorted by memory descending, capped at top-5
    procs = result["beast"]["processes"]
    assert [p["name"] for p in procs] == ["python", "Xorg"]
    assert procs[0]["memory_bytes"] == 8000000000
    assert procs[0]["container"] == "ollama"
    assert procs[0]["type"] == "C"
    assert result["_meta"]["source"] == "prometheus"


@pytest.mark.asyncio
async def test_get_gpu_status_no_process_metric(mcp_app, mock_client):
    """When the process-memory metric has no data, no 'processes' key is added and no error."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(prom_vector([prom_result("beast", 45.0)])),  # utilization
            make_response(prom_vector([prom_result("beast", 62.3)])),  # vram percent
            make_response(prom_vector([prom_result("beast", 71.0)])),  # temperature
            make_response(prom_vector([prom_result("beast", 220.5)])),  # power draw
            make_response(
                prom_vector([prom_result("beast", 15032385536)])
            ),  # vram used
            make_response(
                prom_vector(
                    [  # vram total (nvidia_smi_memory_total_bytes -- no name label)
                        prom_result("beast", 25757220864),
                    ]
                )
            ),
            make_response(
                prom_vector(
                    [  # name lookup (nvidia_smi_gpu_info carries name)
                        prom_result("beast", 1, {"name": "NVIDIA GeForce RTX 3090"}),
                    ]
                )
            ),
            make_response(
                prom_vector([])
            ),  # process query: empty (exporter not scraped yet)
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_gpu_status")
    result = await tool_fn(ctx=ctx)

    assert "beast" in result
    assert "processes" not in result["beast"]
    assert result["beast"]["power_draw_watts"] == 220.5


@pytest.mark.asyncio
async def test_get_gpu_status_stamps_canonical_host(
    mcp_app, mock_client, canonical_hosts
):
    """Each GPU carries the host that uses it and the machine the card sits in.
    `ai-vm-gpu` is a scrape job, not a host: the 3070 is used by ai-vm but lives
    in the Proxmox box. Beast's own card sits in Beast, so it is its own iron.
    The map stays keyed by instance — the stamp is additive."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=(
            # utilization / vram% / temp / power / vram used / vram total
            [
                make_response(
                    prom_vector([prom_result("beast", 1), prom_result("ai-vm-gpu", 2)])
                )
            ]
            * 6
            # name lookup, then the (unscraped) process query
            + [
                make_response(prom_vector([])),
                make_response(prom_vector([])),
            ]
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_gpu_status")
    result = await tool_fn(ctx=ctx)

    assert set(result) == {"beast", "ai-vm-gpu", "_meta"}
    assert result["ai-vm-gpu"]["host"] == "ai-vm"
    assert result["ai-vm-gpu"]["physical_host"] == "proxmox"
    assert result["beast"]["host"] == "beast"
    assert result["beast"]["physical_host"] == "beast"


@pytest.mark.asyncio
async def test_get_gpu_status_unknown_instance_stamps_null(
    mcp_app, mock_client, canonical_hosts
):
    """An instance that is no canonical host gets host: None — an honest gap
    rather than a guess. The entry's metrics are untouched."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=(
            [make_response(prom_vector([prom_result("mystery-box", 45.0)]))] * 6
            + [make_response(prom_vector([])), make_response(prom_vector([]))]
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_gpu_status")
    result = await tool_fn(ctx=ctx)

    assert result["mystery-box"]["host"] is None
    assert result["mystery-box"]["physical_host"] is None
    assert result["mystery-box"]["utilization_percent"] == 45.0


# --- Test: get_storage_status ---


@pytest.mark.asyncio
async def test_get_storage_status(mcp_app, mock_client):
    """Returns dict with per-host storage entries containing mountpoint details."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            # Size query
            make_response(
                prom_vector(
                    [
                        prom_result(
                            "beast", 500000000000, {"mountpoint": "/", "fstype": "ext4"}
                        ),
                        prom_result(
                            "beast",
                            2000000000000,
                            {"mountpoint": "/data", "fstype": "ext4"},
                        ),
                    ]
                )
            ),
            # Available query
            make_response(
                prom_vector(
                    [
                        prom_result(
                            "beast", 250000000000, {"mountpoint": "/", "fstype": "ext4"}
                        ),
                        prom_result(
                            "beast",
                            1000000000000,
                            {"mountpoint": "/data", "fstype": "ext4"},
                        ),
                    ]
                )
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_storage_status")
    result = await tool_fn(ctx=ctx)

    assert "beast" in result
    beast_storage = result["beast"]
    assert isinstance(beast_storage, list)
    assert len(beast_storage) == 2

    # Find root mountpoint
    root = next(s for s in beast_storage if s["mountpoint"] == "/")
    assert root["total_bytes"] == 500000000000
    assert root["used_bytes"] == 250000000000  # 500B - 250B
    assert root["used_percent"] == 50.0


# --- Test: get_prometheus_targets ---


@pytest.mark.asyncio
async def test_get_prometheus_targets(mcp_app, mock_client):
    """Returns list of dicts with job, instance, health, last_scrape."""
    ctx = make_mock_ctx(prometheus=mock_client)

    mock_client.get = AsyncMock(
        return_value=make_response(
            {
                "status": "success",
                "data": {
                    "activeTargets": [
                        {
                            "labels": {"job": "node", "instance": "beast:9100"},
                            "health": "up",
                            "lastScrape": "2026-04-02T12:00:00Z",
                            "scrapeUrl": "http://beast:9100/metrics",
                        },
                        {
                            "labels": {
                                "job": "cadvisor",
                                "instance": "docker-host:8080",
                            },
                            "health": "down",
                            "lastScrape": "2026-04-02T11:59:00Z",
                            "scrapeUrl": "http://docker-host:8080/metrics",
                        },
                    ],
                },
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_prometheus_targets")
    result = await tool_fn(ctx=ctx)

    assert result["_meta"]["source"] == "prometheus"
    targets = result["targets"]
    assert isinstance(targets, list)
    assert len(targets) == 2
    assert targets[0]["job"] == "node"
    assert targets[0]["instance"] == "beast:9100"
    assert targets[0]["health"] == "up"
    assert targets[0]["last_scrape"] == "2026-04-02T12:00:00Z"
    assert targets[1]["health"] == "down"


# --- Test: query_prometheus ---


@pytest.mark.asyncio
async def test_query_prometheus(mcp_app, mock_client):
    """Passes query to /api/v1/query, returns raw response dict."""
    ctx = make_mock_ctx(prometheus=mock_client)
    raw_response = prom_vector([prom_result("beast", 42.0)])

    mock_client.get = AsyncMock(return_value=make_response(raw_response))

    tool_fn = get_tool_fn(mcp_app, "query_prometheus")
    result = await tool_fn(ctx=ctx, query="up")

    # Raw Prometheus payload passes through, plus the _meta envelope
    assert result["status"] == raw_response["status"]
    assert result["data"] == raw_response["data"]
    assert result["_meta"]["source"] == "prometheus"
    mock_client.get.assert_called_once()
    call_args = mock_client.get.call_args
    assert call_args[0][0] == "/api/v1/query"


# --- Test: query_prometheus_range ---


@pytest.mark.asyncio
async def test_query_prometheus_range(mcp_app, mock_client):
    """Passes query, start, end, step to /api/v1/query_range, returns raw response."""
    ctx = make_mock_ctx(prometheus=mock_client)
    raw_response = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"instance": "beast"},
                    "values": [[1712000000, "1.0"], [1712000060, "2.0"]],
                }
            ],
        },
    }

    mock_client.get = AsyncMock(return_value=make_response(raw_response))

    tool_fn = get_tool_fn(mcp_app, "query_prometheus_range")
    result = await tool_fn(
        ctx=ctx,
        query="up",
        start="2026-04-01T00:00:00Z",
        end="2026-04-02T00:00:00Z",
        step="1m",
    )

    assert result["status"] == raw_response["status"]
    assert result["data"] == raw_response["data"]
    assert result["_meta"]["source"] == "prometheus"
    mock_client.get.assert_called_once()


# --- Test: _get helper error handling ---


@pytest.mark.asyncio
async def test_get_helper_timeout(mcp_app, mock_client):
    """When httpx.TimeoutException raised, returns error dict."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    # Use query_prometheus as a vehicle to test _get timeout handling
    tool_fn = get_tool_fn(mcp_app, "query_prometheus")
    result = await tool_fn(ctx=ctx, query="up")

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_helper_http_error(mcp_app, mock_client):
    """When 500 returned, returns error dict with status."""
    ctx = make_mock_ctx(prometheus=mock_client)
    error_response = httpx.Response(
        status_code=500,
        request=httpx.Request("GET", "http://test"),
    )
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "http://test"),
            response=error_response,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "query_prometheus")
    result = await tool_fn(ctx=ctx, query="up")

    assert result["error"] == "http_error"
    assert result["status"] == 500
    assert "message" in result


# --- Regression (WP5): summary tools must not report empty-success on outage ---


@pytest.mark.asyncio
async def test_get_host_summary_all_queries_error_returns_error(mcp_app, mock_client):
    """When every Prometheus query fails, get_host_summary returns the error
    dict, not an empty (implicitly 'no hosts') result at high confidence."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

    tool_fn = get_tool_fn(mcp_app, "get_host_summary")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_get_storage_status_all_queries_error_returns_error(mcp_app, mock_client):
    """A total Prometheus outage surfaces as an error dict from storage."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

    tool_fn = get_tool_fn(mcp_app, "get_storage_status")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_get_gpu_status_all_queries_error_returns_error(mcp_app, mock_client):
    """A total Prometheus outage surfaces as an error dict from gpu status."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

    tool_fn = get_tool_fn(mcp_app, "get_gpu_status")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_get_container_stats_partial_error_degrades_confidence(
    mcp_app, mock_client
):
    """One failed query (cpu ok, memory down) keeps data but lowers confidence
    to medium rather than reporting a confident partial result."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(prom_vector([prom_result("beast", 12.0)])),
            httpx.ConnectError("down"),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_container_stats")
    result = await tool_fn(ctx=ctx)

    assert result["_meta"]["confidence"] == "medium"


@pytest.mark.asyncio
async def test_get_host_summary_unknown_host_returns_not_found(mcp_app, mock_client):
    """Regression (WP5): filtering by an unknown host returns not_found, not an
    empty {_meta} dict that reads as 'no data'."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(prom_vector([])))

    tool_fn = get_tool_fn(mcp_app, "get_host_summary")
    result = await tool_fn(ctx=ctx, host="does-not-exist")

    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_get_container_stats_invalid_sort_by_returns_error(mcp_app, mock_client):
    """Regression (WP5): an unrecognized sort_by returns invalid_parameter
    rather than silently sorting by memory and labelling it as asked."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(prom_vector([])))

    tool_fn = get_tool_fn(mcp_app, "get_container_stats")
    result = await tool_fn(ctx=ctx, sort_by="mem")

    assert result["error"] == "invalid_parameter"


@pytest.mark.asyncio
async def test_get_container_stats_sort_by_is_case_insensitive(mcp_app, mock_client):
    """'CPU' is accepted (normalized), not rejected or silently memory-sorted."""
    ctx = make_mock_ctx(prometheus=mock_client)
    mock_client.get = AsyncMock(return_value=make_response(prom_vector([])))

    tool_fn = get_tool_fn(mcp_app, "get_container_stats")
    result = await tool_fn(ctx=ctx, sort_by="CPU")

    assert "error" not in result
    assert "containers" in result
