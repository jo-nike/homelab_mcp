from unittest.mock import MagicMock

import httpx
import pytest

import config
from lib import hosts as hosts_lib

# --- Test hermeticity: null every service credential/URL before each test ---
#
# config.py freezes these constants from the environment at import time. A local
# .env populates many of them; a CI runner has none. Without this reset the suite
# behaves differently depending on whether a .env happens to be present: a test
# that patches only a service's URL (not its credential) silently borrows the
# credential from the developer's .env, then fails on CI where the guard's
# credential half is None and register() skips the tool group.
#
# Nulling all of them before every test makes the suite behave identically with
# or without a .env. A test that needs a service registered must patch its FULL
# register() guard (URL *and* credential) itself.
#
# This lists only the service constants that gate tool registration -- it
# deliberately excludes the transport/auth constants (MCP_TRANSPORT,
# MCP_RESOURCE_BASE_URL, AUTHENTIK_*) and the repo/branch defaults (STACKS_REPO,
# DOCS_REPO, ...), which other tests pin to specific values and which do not gate
# a register() guard.
_SERVICE_CONFIG_CONSTANTS = [
    "PROMETHEUS_URL",
    "LOKI_URL",
    "PROXMOX_URL",
    "PROXMOX_TOKEN_ID",
    "PROXMOX_TOKEN_SECRET",
    "PORTAINER_URL",
    "PORTAINER_API_KEY",
    "PLEX_URL",
    "PLEX_TOKEN",
    "SONARR_URL",
    "SONARR_API_KEY",
    "RADARR_URL",
    "RADARR_API_KEY",
    "OVERSEERR_URL",
    "OVERSEERR_API_KEY",
    "TRANSMISSION_URL",
    "TRANSMISSION_USERNAME",
    "TRANSMISSION_PASSWORD",
    "SYNOLOGY_URL",
    "SYNOLOGY_USERNAME",
    "SYNOLOGY_PASSWORD",
    "PBS_URL",
    "PBS_TOKEN_ID",
    "PBS_TOKEN_SECRET",
    "BACKBLAZE_KEY_ID",
    "BACKBLAZE_APP_KEY",
    "TECHNITIUM_URL",
    "TECHNITIUM_PASSWORD",
    "GITEA_URL",
    "GITEA_TOKEN",
    "WIREGUARD_URL",
    "WIREGUARD_PASSWORD",
    "WIREGUARD_USERNAME",
    "SCANOPY_URL",
    "SCANOPY_API_KEY",
    "VIKUNJA_URL",
    "VIKUNJA_TOKEN",
    "VIKUNJA_ALERT_PROJECT_ID",
    "NPM_URL",
    "NPM_EMAIL",
    "NPM_PASSWORD",
    "PROWLARR_URL",
    "PROWLARR_API_KEY",
    "CROWDSEC_URL",
    "CROWDSEC_API_KEY",
    "MYSPEED_URL",
    "MYSPEED_PASSWORD",
    "HEALTHCHECKS_URL",
    "HEALTHCHECKS_API_KEY",
    "TAUTULLI_URL",
    "TAUTULLI_API_KEY",
    "LITELLM_URL",
    "LITELLM_API_KEY",
    "LLAMA_SERVER_URL",
    "LLAMA_SERVER_API_KEY",
    "SEARXNG_URL",
]


@pytest.fixture(autouse=True)
def _null_service_config(monkeypatch):
    """Reset every service credential/URL constant to None before each test.

    Autouse so it runs before any test body or module fixture; monkeypatch so the
    original values are restored on teardown. A test that patches a constant on
    top of this (patch.object / monkeypatch.setattr) still works: its restore
    lands on None, and this fixture's teardown then restores the real value."""
    for name in _SERVICE_CONFIG_CONSTANTS:
        monkeypatch.setattr(config, name, None, raising=False)


# --- Shared test helpers ---
#
# These four helpers were previously copy-pasted into ~23 test files. They live
# here so a FastMCP internals change (get_tool_fn/count_tools reach into private
# attributes) is a one-file edit. Import them with
# `from tests.conftest import get_tool_fn, count_tools, make_response, make_mock_ctx`.


def get_tool_fn(app, name):
    """Return a registered tool's underlying function from a FastMCP app."""
    key = f"tool:{name}@"
    tool = app._local_provider._components[key]
    return tool.fn


def count_tools(app):
    """Count registered tools in a FastMCP app."""
    return sum(1 for k in app._local_provider._components if k.startswith("tool:"))


def make_response(
    json_data=None, status_code=200, headers=None, content_type="application/json"
):
    """Create a real httpx.Response.

    ``json_data`` may be a dict/list (serialized as JSON), a ``str`` (sent as the
    raw body under ``content_type``), or ``None`` (an explicit JSON ``null`` body,
    so callers doing ``resp.json() or []`` see ``None`` rather than a decode error).
    """
    resp_headers = {"content-type": content_type}
    if headers:
        resp_headers.update(headers)
    req = httpx.Request("GET", "http://test")
    if isinstance(json_data, str):
        return httpx.Response(
            status_code=status_code, text=json_data, headers=resp_headers, request=req
        )
    if json_data is None:
        body = b"null" if content_type == "application/json" else b""
        return httpx.Response(
            status_code=status_code, content=body, headers=resp_headers, request=req
        )
    return httpx.Response(
        status_code=status_code, json=json_data, headers=resp_headers, request=req
    )


def make_mock_ctx(clients=None, **kwargs):
    """Create a mock Context whose lifespan_context maps service name -> client.

    Accepts a dict positionally (``make_mock_ctx({"loki": c})``), keyword
    ``service=client`` pairs (``make_mock_ctx(prometheus=c)``), or both.
    """
    if clients is not None and not isinstance(clients, dict):
        raise TypeError(
            "make_mock_ctx positional argument must be a dict of service->client"
        )
    ctx = MagicMock()
    ctx.lifespan_context = {**(clients or {}), **kwargs}
    return ctx


@pytest.fixture
def restore_config_registries():
    """Snapshot the mutable config registries and restore them on teardown.

    Tests that clear/repopulate config.SERVICES/HOSTS/etc. otherwise leak sample
    data into every subsequently-run test in the session."""
    import copy

    names = [
        "SERVICES",
        "HOSTS",
        "IP_INDEX",
        "DOCS_INDEX",
        "TOPOLOGY",
        "BASELINES",
        "STACKS_INDEX",
        "VAULT_INDEX",
    ]
    snapshot = {
        n: copy.deepcopy(getattr(config, n)) for n in names if hasattr(config, n)
    }
    yield
    for n, saved in snapshot.items():
        reg = getattr(config, n)
        reg.clear()
        reg.update(saved)


@pytest.fixture
def canonical_hosts(sample_hosts_yaml, monkeypatch):
    """Resolve canonical host names against the sample seed rather than the real
    data/hosts.yaml. The parsed index is cached for the life of the process, so
    it is dropped either side of the test — otherwise the seed leaks between
    tests in both directions."""
    monkeypatch.setattr(config, "DATA_DIR", sample_hosts_yaml.parent)
    hosts_lib._index.cache_clear()
    yield
    hosts_lib._index.cache_clear()


@pytest.fixture
def sample_services_yaml(tmp_path):
    """Create a minimal services.yaml for testing."""
    content = """
services:
  - name: prometheus
    host: docker-host
    ip: "192.168.1.79"
    port: 9090
    stack: dh_grafana_stack
    domain: null
    role: metrics collection
    auth: none
  - name: plex
    host: plex-stack
    ip: "192.168.1.108"
    port: 32400
    stack: plex_media
    domain: null
    role: media server
    auth: token
  - name: grafana
    host: docker-host
    ip: "192.168.1.79"
    port: 3000
    stack: dh_grafana_stack
    domain: null
    role: dashboards
    auth: none
"""
    p = tmp_path / "services.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def sample_hosts_yaml(tmp_path):
    """Create a minimal hosts.yaml for testing."""
    content = """
hosts:
  - name: proxmox
    ip: "192.168.1.114"
    role: hypervisor
    os: Proxmox VE
    proxmox_node: pve
    aliases:
      prometheus: proxmox
      proxmox: pve
  - name: docker-host
    ip: "192.168.1.79"
    role: primary Docker host
    os: Ubuntu 24.04
    parent: proxmox
    aliases:
      prometheus: docker-host
      proxmox: docker-host
      portainer: docker host
  - name: plex-stack
    ip: "192.168.1.108"
    role: media server stack
    os: Ubuntu 24.04
    parent: proxmox
    aliases:
      prometheus: plex-stack
      proxmox: plex-stack
      portainer: plex-stack
  - name: ai-vm
    ip: "192.168.1.54"
    role: AI/ML workloads
    os: Ubuntu 24.04
    parent: proxmox
    aliases:
      prometheus: [ai-vm, ai-vm-gpu]
      proxmox: AI
      portainer: AI
  - name: beast
    ip: "192.168.1.119"
    role: workstation
    os: Ubuntu 24.04
    aliases:
      prometheus: beast
      portainer: Beast
"""
    p = tmp_path / "hosts.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def sample_docs_dir(tmp_path):
    """Create a minimal docs directory for testing."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "test.md").write_text(
        "# Test Doc\n\nThis is a test document.\n\n"
        "## Section One\n\nSome content about monitoring.\n\n"
        "## Section Two\n\nMore content about storage."
    )
    return docs
