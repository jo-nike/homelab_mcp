"""Homelab MCP server entry point."""

import asyncio
import ipaddress
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal, cast

from fastmcp import FastMCP
from fastmcp.server.auth import (
    AccessToken,
    MultiAuth,
    RemoteAuthProvider,
    TokenVerifier,
)
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
import resources
from lib.clients import Clients, create_clients
from lib.refresh import periodic_refresh
from tools import (
    aggregation,
    backblaze,
    baselines,
    changefeed,
    compound,
    crowdsec,
    docker,
    freshness,
    gitea,
    graph,
    health,
    healthchecks,
    knowledge,
    litellm,
    llama,
    loki,
    myspeed,
    npm,
    overseerr,
    pbs,
    plex,
    prometheus,
    prowlarr,
    proxmox,
    radarr,
    scanopy,
    searxng,
    sonarr,
    synology,
    tautulli,
    technitium,
    transmission,
    vikunja,
    wireguard,
)
from tools import refresh as refresh_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# httpx/httpcore log every request at INFO, including the full URL. Many of our
# request URLs carry secrets in the query string (Technitium ?user/?pass and
# ?token, Synology ?account/?passwd and ?_sid, Tautulli ?apikey), which would
# otherwise be written to container stdout -> Loki -> readable back through this
# server's own log tools. Silence them below WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# Per-process synthetic bearer used only by LanBypassMiddleware to satisfy
# FastMCP's auth chain for trusted-CIDR direct LAN traffic. Never persisted,
# never returned to any client; regenerated on every process start.
LAN_BYPASS_TOKEN = secrets.token_urlsafe(32)


class LanBypassVerifier(TokenVerifier):
    """Recognizes the in-process LAN_BYPASS_TOKEN. No network calls."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(token, LAN_BYPASS_TOKEN):
            return AccessToken(
                token=token,
                client_id="lan-bypass",
                scopes=["openid"],
                expires_at=None,
            )
        return None


class LanBypassMiddleware:
    """Pure-ASGI outer middleware: inject LAN_BYPASS_TOKEN as a Bearer header
    when the source IP is in trusted CIDRs AND the request carries no proxy
    headers AND no Origin header. Public traffic always carries XFF (Caddy adds
    it on the VPS), so this cannot be triggered remotely even with a spoofed
    source IP. The Origin refusal blocks the browser-CSRF vector: a malicious
    web page opened in a LAN browser makes a cross-origin request that arrives
    from a trusted CIDR with no proxy headers but always carries Origin, so
    without this check it would be auto-authenticated and its response read back
    by the attacker's JS. Real MCP clients do not send Origin.
    """

    def __init__(self, app, trusted_cidrs: list[str]) -> None:
        self.app = app
        self._nets = [ipaddress.ip_network(c, strict=False) for c in trusted_cidrs]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            client = scope.get("client") or ("", 0)
            try:
                addr = ipaddress.ip_address(client[0])
                in_cidr = any(addr in n for n in self._nets)
            except ValueError:
                in_cidr = False
            headers = scope.get("headers", [])
            proxied = any(
                name in (b"x-forwarded-for", b"x-real-ip", b"via", b"forwarded")
                for name, _ in headers
            )
            has_origin = any(name == b"origin" for name, _ in headers)
            if in_cidr and not proxied and not has_origin:
                new_headers = [
                    (n, v) for n, v in headers if n.lower() != b"authorization"
                ]
                new_headers.append(
                    (b"authorization", f"Bearer {LAN_BYPASS_TOKEN}".encode())
                )
                scope = {**scope, "headers": new_headers}
        await self.app(scope, receive, send)


def _build_auth() -> MultiAuth:
    """Construct the MultiAuth chain: RemoteAuthProvider (Authentik OIDC) +
    LanBypassVerifier (in-process synthetic bearer). Endpoints come from config
    (env-overridable) so a redeploy under another domain needs no code change."""
    client_id = config.AUTHENTIK_CLIENT_ID
    if not client_id:
        raise RuntimeError(
            "AUTHENTIK_CLIENT_ID must be set to build the HTTP-transport auth chain"
        )
    issuer = config.AUTHENTIK_ISSUER

    jwt_verifier = JWTVerifier(
        jwks_uri=config.AUTHENTIK_JWKS_URI,
        issuer=issuer,
        algorithm="RS256",
        audience=client_id,
        required_scopes=["openid"],
    )

    remote_auth = RemoteAuthProvider(
        token_verifier=jwt_verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=config.MCP_RESOURCE_BASE_URL,
        resource_name="Homelab MCP",
        scopes_supported=["openid", "email", "profile", "offline_access"],
    )

    return MultiAuth(server=remote_auth, verifiers=[LanBypassVerifier()])


@asynccontextmanager
async def lifespan(mcp_server):
    """Create httpx clients for configured services and load knowledge indexes."""
    async with AsyncExitStack() as stack:
        clients: Clients = await create_clients(stack)

        # Load knowledge indexes at startup
        config.SERVICES.update(config.load_services())
        config.HOSTS.update(config.load_hosts())
        config.DOCS_INDEX.update(config.load_docs_index())
        config.IP_INDEX.update(config.build_ip_index())
        config.TOPOLOGY.update(config.load_topology())
        config.BASELINES.update(config.load_baselines())

        logging.info(
            "Loaded %d services, %d hosts, %d IPs, %d docs, topology: %s",
            len(config.SERVICES),
            len(config.HOSTS),
            len(config.IP_INDEX),
            len(config.DOCS_INDEX),
            "loaded" if config.TOPOLOGY else "not found",
        )

        # Spawn background refresh task
        refresh_task = asyncio.create_task(
            periodic_refresh(clients),
            name="knowledge-refresh",
        )

        try:
            yield clients
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass


# Tool modules in registration order (FOUN-03: each module's register() is a
# no-op when its credentials are missing).
_TOOL_MODULES = [
    prometheus,
    loki,
    knowledge,
    proxmox,
    docker,
    plex,
    sonarr,
    radarr,
    overseerr,
    transmission,
    synology,
    pbs,
    backblaze,
    technitium,
    gitea,
    wireguard,
    scanopy,
    vikunja,
    npm,
    prowlarr,
    crowdsec,
    myspeed,
    healthchecks,
    tautulli,
    litellm,
    llama,
    graph,
    freshness,
    changefeed,
    health,
    baselines,
    aggregation,
    compound,
    searxng,
    refresh_mod,
]


def create_server(transport: str | None = None) -> FastMCP:
    """Build the FastMCP app: instructions, optional auth, and all registrations.

    ``transport`` selects whether the HTTP auth chain is built (it needs
    AUTHENTIK_CLIENT_ID); it defaults to ``MCP_TRANSPORT`` (``stdio``). Wrapping
    construction in a factory lets tests vary env-dependent registration without
    importlib.reload gymnastics.
    """
    transport = transport or config.MCP_TRANSPORT
    instructions = config.build_instructions()
    # Auth chain is only meaningful for HTTP transport; stdio mode skips it.
    auth = _build_auth() if transport in ("http", "streamable-http", "sse") else None

    server = FastMCP("homelab", instructions=instructions, lifespan=lifespan, auth=auth)

    @server.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        """Health check endpoint (FOUN-06)."""
        return JSONResponse({"status": "ok", "version": config.APP_VERSION})

    for module in _TOOL_MODULES:
        module.register(server)
    resources.register(server)
    return server


mcp = create_server()


def build_app(transport: str):
    """Assemble the HTTP ASGI app with the load-bearing middleware ordering.

    LanBypassMiddleware must wrap OUTSIDE the FastMCP auth chain: passing it via
    the http_app ``middleware`` arg would put it inside AuthenticationMiddleware,
    too late to affect token verification. CORS goes inside (it is order-neutral
    vs auth) and is only mounted when MCP_CORS_ORIGINS names an allowlist -- a
    wildcard on a LAN-bypass-authenticated endpoint would let any web page probe
    it. MCP_TRUSTED_CIDRS is parsed here so the wiring is testable.
    """
    from starlette.middleware import Middleware

    trusted_cidrs_list = [
        c.strip() for c in config.MCP_TRUSTED_CIDRS.split(",") if c.strip()
    ]

    cors_origins = [o.strip() for o in config.MCP_CORS_ORIGINS.split(",") if o.strip()]
    inner_middleware = []
    if cors_origins:
        inner_middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        )

    app = mcp.http_app(
        # build_app is only reached for the three HTTP transports (guarded at
        # the __main__ call site), so this narrowing is sound.
        transport=cast(Literal["http", "streamable-http", "sse"], transport),
        stateless_http=True,
        middleware=inner_middleware,
    )
    app.add_middleware(LanBypassMiddleware, trusted_cidrs=trusted_cidrs_list)
    return app


if __name__ == "__main__":
    if config.MCP_TRANSPORT in ("http", "streamable-http", "sse"):
        import uvicorn

        uvicorn.run(
            build_app(config.MCP_TRANSPORT),
            host="0.0.0.0",
            port=config.MCP_PORT,
        )
    else:
        mcp.run()
