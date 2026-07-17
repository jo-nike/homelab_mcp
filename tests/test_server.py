"""Tests for server wiring: LanBypassVerifier, the lifespan, /health, and
register-guard/lifespan-guard parity.

Import server under stdio transport so module load does not try to build the
Authentik auth chain (which needs AUTHENTIK_CLIENT_ID). config may already hold
a non-stdio MCP_TRANSPORT from a parent .env, so force it before importing.
"""

import os

os.environ["MCP_TRANSPORT"] = "stdio"

import config  # noqa: E402

config.MCP_TRANSPORT = "stdio"

import httpx  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402
from tests.conftest import count_tools  # noqa: E402

# Every service-gating config constant. A parity/lifespan test zeroes all of
# these, then re-enables only the service under test.
ALL_GATES = [
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
    "PBS_URL",
    "PBS_TOKEN_ID",
    "PBS_TOKEN_SECRET",
    "GITEA_URL",
    "GITEA_TOKEN",
    "SCANOPY_URL",
    "SCANOPY_API_KEY",
    "VIKUNJA_URL",
    "VIKUNJA_TOKEN",
    "PROWLARR_URL",
    "PROWLARR_API_KEY",
    "CROWDSEC_URL",
    "CROWDSEC_API_KEY",
    "HEALTHCHECKS_URL",
    "HEALTHCHECKS_API_KEY",
    "TAUTULLI_URL",
    "TAUTULLI_API_KEY",
    "LITELLM_URL",
    "LITELLM_API_KEY",
    "LLAMA_SERVER_URL",
    "LLAMA_SERVER_API_KEY",
    "BACKBLAZE_KEY_ID",
    "BACKBLAZE_APP_KEY",
    "TECHNITIUM_URL",
    "TECHNITIUM_PASSWORD",
    "SYNOLOGY_URL",
    "SYNOLOGY_USERNAME",
    "SYNOLOGY_PASSWORD",
    "TRANSMISSION_URL",
    "TRANSMISSION_USERNAME",
    "TRANSMISSION_PASSWORD",
    "NPM_URL",
    "NPM_EMAIL",
    "NPM_PASSWORD",
    "MYSPEED_URL",
    "MYSPEED_PASSWORD",
    "WIREGUARD_URL",
    "WIREGUARD_USERNAME",
    "WIREGUARD_PASSWORD",
    "SEARXNG_URL",
]

# (module attr on server, {config attrs to enable}, expected lifespan client key)
PARITY = [
    ("prometheus", ["PROMETHEUS_URL"], "prometheus"),
    ("loki", ["LOKI_URL"], "loki"),
    (
        "proxmox",
        ["PROXMOX_URL", "PROXMOX_TOKEN_ID", "PROXMOX_TOKEN_SECRET"],
        "proxmox",
    ),
    ("docker", ["PORTAINER_URL", "PORTAINER_API_KEY"], "portainer"),
    ("plex", ["PLEX_URL", "PLEX_TOKEN"], "plex"),
    ("sonarr", ["SONARR_URL", "SONARR_API_KEY"], "sonarr"),
    ("radarr", ["RADARR_URL", "RADARR_API_KEY"], "radarr"),
    ("overseerr", ["OVERSEERR_URL", "OVERSEERR_API_KEY"], "overseerr"),
    ("pbs", ["PBS_URL", "PBS_TOKEN_ID", "PBS_TOKEN_SECRET"], "pbs"),
    ("gitea", ["GITEA_URL", "GITEA_TOKEN"], "gitea"),
    ("scanopy", ["SCANOPY_URL", "SCANOPY_API_KEY"], "scanopy"),
    ("vikunja", ["VIKUNJA_URL", "VIKUNJA_TOKEN"], "vikunja"),
    ("prowlarr", ["PROWLARR_URL", "PROWLARR_API_KEY"], "prowlarr"),
    ("crowdsec", ["CROWDSEC_URL", "CROWDSEC_API_KEY"], "crowdsec"),
    ("healthchecks", ["HEALTHCHECKS_URL", "HEALTHCHECKS_API_KEY"], "healthchecks"),
    ("tautulli", ["TAUTULLI_URL", "TAUTULLI_API_KEY"], "tautulli"),
    ("litellm", ["LITELLM_URL", "LITELLM_API_KEY"], "litellm"),
    ("llama", ["LLAMA_SERVER_URL"], "llama_server"),
    ("backblaze", ["BACKBLAZE_KEY_ID", "BACKBLAZE_APP_KEY"], "backblaze"),
    ("technitium", ["TECHNITIUM_URL", "TECHNITIUM_PASSWORD"], "technitium"),
    (
        "synology",
        ["SYNOLOGY_URL", "SYNOLOGY_USERNAME", "SYNOLOGY_PASSWORD"],
        "synology",
    ),
    (
        "transmission",
        ["TRANSMISSION_URL", "TRANSMISSION_USERNAME", "TRANSMISSION_PASSWORD"],
        "transmission",
    ),
    ("npm", ["NPM_URL", "NPM_EMAIL", "NPM_PASSWORD"], "npm"),
    (
        "wireguard",
        ["WIREGUARD_URL", "WIREGUARD_USERNAME", "WIREGUARD_PASSWORD"],
        "wireguard",
    ),
]


@pytest.fixture
def zeroed_config(monkeypatch):
    """Zero every service gate and stub the knowledge loaders + refresh task so
    the lifespan builds only the clients the test explicitly enables."""
    for attr in ALL_GATES:
        monkeypatch.setattr(config, attr, None, raising=False)
    for loader in (
        "load_services",
        "load_hosts",
        "load_docs_index",
        "load_topology",
        "load_baselines",
    ):
        monkeypatch.setattr(config, loader, dict, raising=False)
    monkeypatch.setattr(config, "build_ip_index", dict, raising=False)

    async def _never():
        import asyncio

        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "periodic_refresh", lambda clients: _never())
    return monkeypatch


# --- LanBypassVerifier ---


@pytest.mark.asyncio
async def test_verifier_accepts_bypass_token():
    token = await server.LanBypassVerifier().verify_token(server.LAN_BYPASS_TOKEN)
    assert token is not None
    assert token.client_id == "lan-bypass"


@pytest.mark.asyncio
async def test_verifier_rejects_other_token():
    assert await server.LanBypassVerifier().verify_token("not-the-token") is None


# --- LanBypassMiddleware edge cases ---


@pytest.mark.asyncio
async def test_non_http_scope_passes_through():
    """A non-HTTP (e.g. lifespan/websocket) scope is forwarded untouched."""
    mw = server.LanBypassMiddleware(None, ["127.0.0.0/8"])
    seen = {}

    async def inner(scope, receive, send):
        seen["scope"] = scope

    mw.app = inner
    scope = {"type": "lifespan"}
    await mw(scope, None, None)
    assert seen["scope"] is scope


@pytest.mark.asyncio
async def test_malformed_client_addr_no_bypass():
    """A scope with an unparseable client address is not trusted."""
    mw = server.LanBypassMiddleware(None, ["127.0.0.0/8"])
    captured = {}

    async def inner(scope, receive, send):
        captured["headers"] = scope.get("headers", [])

    mw.app = inner
    scope = {"type": "http", "client": ("not-an-ip", 1), "headers": []}
    await mw(scope, lambda: None, lambda m: None)
    expected = f"Bearer {server.LAN_BYPASS_TOKEN}".encode()
    assert not any(v == expected for _, v in captured["headers"])


# --- /health custom route ---


@pytest.mark.asyncio
async def test_health_route_ok_without_auth():
    """/health returns 200 with status+version and needs no bearer token."""
    app = server.mcp.http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == config.APP_VERSION


# --- lifespan wiring ---


@pytest.mark.asyncio
async def test_lifespan_builds_only_enabled_clients(zeroed_config):
    """With only PROMETHEUS_URL set, lifespan builds prometheus (+ unconditional
    web_fetch) and no client for any unset service."""
    zeroed_config.setattr(config, "PROMETHEUS_URL", "http://prom:9090")
    async with server.lifespan(None) as clients:
        assert "prometheus" in clients
        assert "web_fetch" in clients
        assert "loki" not in clients
        assert "plex" not in clients
        assert "gitea" not in clients


@pytest.mark.asyncio
async def test_lifespan_session_service_gets_manager(zeroed_config):
    """A session-auth service is wired as a SessionAuthManager, not a bare client."""
    from lib.auth import SessionAuthManager

    zeroed_config.setattr(config, "TECHNITIUM_URL", "http://dns:5380")
    zeroed_config.setattr(config, "TECHNITIUM_PASSWORD", "pw")
    async with server.lifespan(None) as clients:
        assert isinstance(clients["technitium"], SessionAuthManager)


@pytest.mark.asyncio
async def test_lifespan_cancels_refresh_task(zeroed_config, monkeypatch):
    """On context exit the knowledge-refresh task is cancelled and awaited."""
    import asyncio

    holder = {}

    async def _sleeper(clients):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            holder["cancelled"] = True
            raise

    monkeypatch.setattr(server, "periodic_refresh", _sleeper)
    async with server.lifespan(None):
        await asyncio.sleep(0)  # let the task start
    assert holder.get("cancelled") is True


# --- register-guard / lifespan-guard parity ---


@pytest.mark.asyncio
@pytest.mark.parametrize("mod_name, gates, client_key", PARITY)
async def test_guard_parity(zeroed_config, mod_name, gates, client_key):
    """With only <service>'s credentials set: register() adds tools AND lifespan
    creates its client (both, or neither) -- the mismatch class that silently
    dropped whole services."""
    from fastmcp import FastMCP

    module = getattr(server, mod_name)

    # Disabled: no config -> no tools, no client.
    app_off = FastMCP("off")
    module.register(app_off)
    assert count_tools(app_off) == 0, f"{mod_name} registered tools with no config"
    async with server.lifespan(None) as clients:
        assert client_key not in clients

    # Enabled: set this service's gates -> tools AND client present.
    for attr in gates:
        zeroed_config.setattr(config, attr, "http://x" if attr.endswith("URL") else "x")
    app_on = FastMCP("on")
    module.register(app_on)
    assert count_tools(app_on) > 0, f"{mod_name} registered no tools when configured"
    async with server.lifespan(None) as clients:
        assert client_key in clients, (
            f"{mod_name} tools registered but no {client_key} client"
        )


def test_module_registration_list_covers_registered_modules():
    """_TOOL_MODULES is what create_server iterates; guard against silent drops."""
    assert server.prometheus in server._TOOL_MODULES
    assert len(server._TOOL_MODULES) == 35
