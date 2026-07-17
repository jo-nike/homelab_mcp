"""Shared per-service HTTP client construction.

The server lifespan and standalone scripts (scripts/bootstrap_registries.py)
both need one configured httpx client (or SessionAuthManager) per upstream
service. This module owns that wiring so it exists in exactly one place.
"""

from contextlib import AsyncExitStack
from typing import Any

import httpx

import config
from lib.auth import SessionAuthManager

# The clients dict every tool module reads from ctx.lifespan_context.
# A named alias documents that contract (httpx client OR a SessionAuthManager).
Clients = dict[str, "httpx.AsyncClient | SessionAuthManager"]


async def create_clients(
    stack: AsyncExitStack, only: set[str] | None = None
) -> Clients:
    """Build one client per configured service, keyed by service name.

    `only` restricts which client keys are attempted (None means all).
    Services whose credentials are missing are silently skipped, mirroring
    the tool modules' register() guards. Client lifetimes are owned by the
    caller's AsyncExitStack.
    """

    def wanted(name: str) -> bool:
        return only is None or name in only

    clients: Clients = {}

    # --- Plain httpx clients (header/token/no auth) ---
    # Table-driven: (client key, enabled?, AsyncClient kwargs). verify
    # defaults to True and timeout to 15.0; the four self-signed services
    # (proxmox/portainer/pbs/synology) set verify=False explicitly.
    llama_headers = {}
    if config.LLAMA_SERVER_API_KEY:
        llama_headers["Authorization"] = f"Bearer {config.LLAMA_SERVER_API_KEY}"

    plain_clients: list[tuple[str, bool, dict[str, Any]]] = [
        (
            "prometheus",
            bool(config.PROMETHEUS_URL),
            {"base_url": config.PROMETHEUS_URL},
        ),
        ("loki", bool(config.LOKI_URL), {"base_url": config.LOKI_URL}),
        (
            "proxmox",
            bool(
                config.PROXMOX_URL
                and config.PROXMOX_TOKEN_ID
                and config.PROXMOX_TOKEN_SECRET
            ),
            {
                "base_url": config.PROXMOX_URL,
                "headers": {
                    "Authorization": f"PVEAPIToken={config.PROXMOX_TOKEN_ID}={config.PROXMOX_TOKEN_SECRET}"
                },
                "verify": False,
            },
        ),
        (
            "portainer",
            bool(config.PORTAINER_URL and config.PORTAINER_API_KEY),
            {
                "base_url": config.PORTAINER_URL,
                "headers": {"X-API-Key": config.PORTAINER_API_KEY},
                "verify": False,
            },
        ),
        (
            "plex",
            bool(config.PLEX_URL and config.PLEX_TOKEN),
            {
                "base_url": config.PLEX_URL,
                "headers": {
                    "X-Plex-Token": config.PLEX_TOKEN,
                    "Accept": "application/json",
                },
            },
        ),
        (
            "sonarr",
            bool(config.SONARR_URL and config.SONARR_API_KEY),
            {
                "base_url": config.SONARR_URL,
                "headers": {"X-Api-Key": config.SONARR_API_KEY},
            },
        ),
        (
            "radarr",
            bool(config.RADARR_URL and config.RADARR_API_KEY),
            {
                "base_url": config.RADARR_URL,
                "headers": {"X-Api-Key": config.RADARR_API_KEY},
            },
        ),
        (
            "overseerr",
            bool(config.OVERSEERR_URL and config.OVERSEERR_API_KEY),
            {
                "base_url": config.OVERSEERR_URL,
                "headers": {"X-Api-Key": config.OVERSEERR_API_KEY},
            },
        ),
        (
            "pbs",
            bool(config.PBS_URL and config.PBS_TOKEN_ID and config.PBS_TOKEN_SECRET),
            {
                "base_url": config.PBS_URL,
                "headers": {
                    "Authorization": f"PBSAPIToken={config.PBS_TOKEN_ID}:{config.PBS_TOKEN_SECRET}"
                },
                "verify": False,
            },
        ),
        (
            "gitea",
            bool(config.GITEA_URL and config.GITEA_TOKEN),
            {
                "base_url": config.GITEA_URL,
                "headers": {"Authorization": f"token {config.GITEA_TOKEN}"},
            },
        ),
        (
            "scanopy",
            bool(config.SCANOPY_URL and config.SCANOPY_API_KEY),
            {
                "base_url": config.SCANOPY_URL,
                "headers": {"Authorization": f"Bearer {config.SCANOPY_API_KEY}"},
            },
        ),
        (
            "vikunja",
            bool(config.VIKUNJA_URL and config.VIKUNJA_TOKEN),
            {
                "base_url": config.VIKUNJA_URL,
                "headers": {"Authorization": f"Bearer {config.VIKUNJA_TOKEN}"},
            },
        ),
        (
            "prowlarr",
            bool(config.PROWLARR_URL and config.PROWLARR_API_KEY),
            {
                "base_url": config.PROWLARR_URL,
                "headers": {"X-Api-Key": config.PROWLARR_API_KEY},
            },
        ),
        (
            "crowdsec",
            bool(config.CROWDSEC_URL and config.CROWDSEC_API_KEY),
            {
                "base_url": config.CROWDSEC_URL,
                "headers": {"X-Api-Key": config.CROWDSEC_API_KEY},
            },
        ),
        (
            "healthchecks",
            bool(config.HEALTHCHECKS_URL and config.HEALTHCHECKS_API_KEY),
            {
                "base_url": config.HEALTHCHECKS_URL,
                "headers": {"X-Api-Key": config.HEALTHCHECKS_API_KEY},
            },
        ),
        # Tautulli takes no auth header -- its apikey is a query param.
        (
            "tautulli",
            bool(config.TAUTULLI_URL and config.TAUTULLI_API_KEY),
            {"base_url": config.TAUTULLI_URL},
        ),
        (
            "litellm",
            bool(config.LITELLM_URL and config.LITELLM_API_KEY),
            {
                "base_url": config.LITELLM_URL,
                "headers": {"Authorization": f"Bearer {config.LITELLM_API_KEY}"},
            },
        ),
        (
            "llama_server",
            bool(config.LLAMA_SERVER_URL),
            {"base_url": config.LLAMA_SERVER_URL, "headers": llama_headers},
        ),
    ]
    for name, enabled, kwargs in plain_clients:
        if wanted(name) and enabled:
            kwargs.setdefault("timeout", 15.0)
            clients[name] = await stack.enter_async_context(httpx.AsyncClient(**kwargs))

    # --- SessionAuthManager services ---
    if wanted("transmission") and config.TRANSMISSION_URL:
        from tools.transmission import TransmissionLoginStrategy

        transmission_client = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=config.TRANSMISSION_URL,
                timeout=15.0,
            )
        )
        clients["transmission"] = SessionAuthManager(
            transmission_client,
            TransmissionLoginStrategy(
                username=config.TRANSMISSION_USERNAME,
                password=config.TRANSMISSION_PASSWORD,
            ),
        )

    if (
        wanted("synology")
        and config.SYNOLOGY_URL
        and config.SYNOLOGY_USERNAME
        and config.SYNOLOGY_PASSWORD
    ):
        from tools.synology import SynologyLoginStrategy

        synology_client = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=config.SYNOLOGY_URL,
                verify=False,
                timeout=15.0,
            )
        )
        clients["synology"] = SessionAuthManager(
            synology_client,
            SynologyLoginStrategy(
                username=config.SYNOLOGY_USERNAME,
                password=config.SYNOLOGY_PASSWORD,
            ),
            refresh_margin_seconds=3600,
        )

    if wanted("backblaze") and config.BACKBLAZE_KEY_ID and config.BACKBLAZE_APP_KEY:
        from tools.backblaze import BackblazeLoginStrategy

        backblaze_client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=30.0)
        )
        clients["backblaze"] = SessionAuthManager(
            backblaze_client,
            BackblazeLoginStrategy(
                key_id=config.BACKBLAZE_KEY_ID,
                app_key=config.BACKBLAZE_APP_KEY,
            ),
            refresh_margin_seconds=3600,
        )

    # --- DNS (SessionAuthManager) ---
    if wanted("technitium") and config.TECHNITIUM_URL and config.TECHNITIUM_PASSWORD:
        from tools.technitium import TechnitiumLoginStrategy

        technitium_client = await stack.enter_async_context(
            httpx.AsyncClient(base_url=config.TECHNITIUM_URL, timeout=15.0)
        )
        clients["technitium"] = SessionAuthManager(
            technitium_client,
            TechnitiumLoginStrategy(password=config.TECHNITIUM_PASSWORD),
            refresh_margin_seconds=300,
        )

    # --- Network (SessionAuthManager) ---
    if wanted("wireguard") and config.WIREGUARD_URL and config.WIREGUARD_PASSWORD:
        from tools.wireguard import WgEasyLoginStrategy

        wireguard_client = await stack.enter_async_context(
            httpx.AsyncClient(base_url=config.WIREGUARD_URL, timeout=15.0)
        )
        clients["wireguard"] = SessionAuthManager(
            wireguard_client,
            WgEasyLoginStrategy(
                password=config.WIREGUARD_PASSWORD,
                username=config.WIREGUARD_USERNAME,
            ),
        )

    # --- Reverse Proxy (SessionAuthManager) ---
    if wanted("npm") and config.NPM_URL and config.NPM_EMAIL and config.NPM_PASSWORD:
        from tools.npm import NpmLoginStrategy

        npm_client = await stack.enter_async_context(
            httpx.AsyncClient(base_url=config.NPM_URL, timeout=15.0)
        )
        clients["npm"] = SessionAuthManager(
            npm_client,
            NpmLoginStrategy(email=config.NPM_EMAIL, password=config.NPM_PASSWORD),
            refresh_margin_seconds=3600,
        )

    # --- Speed Test (optional auth) ---
    if wanted("myspeed") and config.MYSPEED_URL:
        if config.MYSPEED_PASSWORD:
            from tools.myspeed import MySpeedLoginStrategy

            myspeed_client = await stack.enter_async_context(
                httpx.AsyncClient(base_url=config.MYSPEED_URL, timeout=15.0)
            )
            clients["myspeed"] = SessionAuthManager(
                myspeed_client,
                MySpeedLoginStrategy(password=config.MYSPEED_PASSWORD),
            )
        else:
            clients["myspeed"] = await stack.enter_async_context(
                httpx.AsyncClient(base_url=config.MYSPEED_URL, timeout=15.0)
            )

    # --- Web Search (no auth) ---
    if wanted("searxng") and config.SEARXNG_URL:
        clients["searxng"] = await stack.enter_async_context(
            httpx.AsyncClient(base_url=config.SEARXNG_URL, timeout=15.0)
        )

    # --- Page fetching (no auth, no SearXNG dependency) ---
    # fetch_page needs only this client, so create it unconditionally rather
    # than gating an unrelated capability on SEARXNG_URL.
    if wanted("web_fetch"):
        clients["web_fetch"] = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; homelab-mcp/1.0; +https://github.com/homelab-mcp)"
                },
            )
        )

    return clients
