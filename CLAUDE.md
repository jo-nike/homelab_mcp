<!-- GSD:project-start source:PROJECT.md -->
## Project

**Homelab MCP**

A FastMCP server that unifies the homelab infrastructure behind a single MCP endpoint. 72 tools across 26 upstream services (Prometheus, Loki, Proxmox, Portainer/Docker, Plex, Sonarr/Radarr, Overseerr, Transmission, Synology, PBS, Backblaze, Technitium DNS, Gitea, WireGuard, Scanopy, Vikunja, NPM, Prowlarr, CrowdSec, MySpeed, Healthchecks, Tautulli, LiteLLM, llama-server, SearXNG, and cross-service aggregations). Designed primarily for local LLMs (Gemma-class and similar) with summary-first tool design: every tool returns a complete, useful answer without requiring the model to compose queries.

**Core Value:** Any LLM, local or cloud, can answer questions about the homelab's current state by calling a single tool, without needing domain-specific query languages or multi-step reasoning.

### Constraints

- **Tool design**: Summary-first. Every tool group needs pre-built tools that return complete answers; raw query tools (PromQL, LogQL) are secondary.
- **Read-mostly**: The server is primarily monitoring/knowledge. A small, deliberate set of write tools exists (`restart_container`, `safe_restart_container`, `create_vikunja_task`, `update_vikunja_task`, `create_task_from_alert`, `overseerr_approve_request`, `overseerr_decline_request`). Each is audit-logged to Loki via `lib/audit.py`, carries `readOnlyHint: False` (and `destructiveHint: True` where applicable), and supports `dry_run=True`; new write tools must follow all three rules. Every write is audit-logged to Loki (best-effort via `lib/audit.py`). `refresh_registries`/`refresh_docs` also mutate server state and are audit-logged too, but carry no `dry_run` (reloading a cache has no preview) and are marked `idempotentHint: True`.
- **Consumer contract**: Crow's Nest Watch/Harbor pollers consume `get_sonarr_status`, `get_radarr_status`, `get_overseerr_requests`, `get_transmission_torrents` and related payloads. Changes to existing tool payload shapes must be additive only; never rename or remove existing fields.
- **Transport**: stdio by default; `MCP_TRANSPORT=streamable-http` (or `http`/`sse`) serves over HTTP via uvicorn on `MCP_PORT` (8000 default, 5774 in Docker/prod).
- **Self-signed certs**: Proxmox, PBS, Portainer, and Synology clients use `verify=False`. Do not widen this to other services.
- **Auth patterns**: Per-service (API keys, session logins, token+dynamic-URL). Session-based services go through `lib/auth.SessionAuthManager` with a per-service `LoginStrategy`.
- **Conditional registration**: Each tool module's `register(mcp)` returns early when its credentials are missing from the environment.
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.13 (prod image; local venv may run newer) | Runtime, `requires-python >=3.13` |
| FastMCP | `>=3.2.0` | MCP framework: `@mcp.tool`, `@mcp.resource`, lifespan, MultiAuth, custom routes |
| httpx | `>=0.28.1` | Async HTTP; one long-lived `AsyncClient` per service, created in lifespan |
| python-dotenv | `>=1.2.2` | `.env` loading for local dev (Docker uses `env_file:` instead) |
| pyyaml | `>=6.0.2` | Knowledge registries (`data/*.yaml`) |
| trafilatura | `>=2.0.0` | Page-content extraction for `fetch_page` |
| pydantic | `>=2.0` | `AnyHttpUrl` in `server.py`; declared directly since first-party code imports it (also arrives via fastmcp) |
| uvicorn | `>=0.30` | HTTP serving; `server.py` calls `uvicorn.run()` directly so `LanBypassMiddleware` can wrap outside the FastMCP auth chain. Declared directly (also transitive via fastmcp) |
| uv | 0.11 | Package manager and lockfile; Docker builds with `--locked` |
| pytest + pytest-asyncio | dev | 524 tests, `asyncio_mode = "strict"` |
| ruff | dev | Lint and format; both gate CI on every branch. Lint rules: `E4,E7,E9,F,I,UP,B` (pyflakes, import sorting, pyupgrade, bugbear) |
| ty | dev | Static type check (`uv run ty check .`); gates CI and pre-push. Parsed-JSON boundaries are typed `Any` |

Deliberately not used: pydantic-settings and typed-config layers (env vars are plain strings with "is it set?" guards), aiohttp/requests (httpx only), fastapi (FastMCP wraps Starlette directly), the raw `mcp` SDK (FastMCP API exclusively), Pydantic models for tool returns (tools return plain dicts).

### Special auth patterns (via `lib/auth.SessionAuthManager`)

| Service | Pattern |
|---------|---------|
| Transmission | 409 CSRF: retry with `X-Transmission-Session-Id` from the 409 response |
| Technitium DNS | POST login for token; cached; re-login on 401 |
| Synology NAS | Session login; `_sid` passed as query param |
| Backblaze B2 | `b2_authorize_account` returns token plus dynamic `apiUrl`; cached ~23h |
| wg-easy | Session cookie from POST `/api/session` |
| NPM, MySpeed | Session/bearer login strategies in their tool modules |
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

- **Tool module pattern**: each `tools/<service>.py` exposes `register(mcp)` that returns early if config is missing, defines a private `_get` (and `_post` where needed) helper, then declares tools with `@mcp.tool`.
- **Never raise from tools**: helpers catch `httpx.TimeoutException` / `HTTPStatusError` / `HTTPError` and return error dicts shaped `{"error": "<code>", "message": "<human-readable>"}` (plus `"status"` for HTTP errors). Callers check `isinstance(x, dict) and "error" in x` and propagate the error dict as the tool result.
- **Return dicts, not strings**: FastMCP serializes to JSON. Summary responses include pre-computed counts (`*_count`) alongside lists.
- **`_meta` on responses**: tools stamp `lib.meta.build_meta(source, data_window=..., confidence=...)` so consumers see freshness and staleness.
- **Canonical host names**: anything that returns per-host data resolves upstream ids (Prometheus instance, Proxmox node/guest, Portainer endpoint) through `lib/hosts.py` so results join across tools.
- **Parameters**: `Annotated[type, "description"]` with sensible defaults; the string becomes the MCP parameter description. First tool param is `ctx: Context`; clients come from `ctx.lifespan_context["<service>"]`.
- **Annotations**: read tools declare `annotations={"readOnlyHint": True}`; writes declare `readOnlyHint: False` and `destructiveHint: True` when they change infrastructure state.
- **Fan-out**: parallel upstream calls use `asyncio.gather`.
- **Docstrings are the product**: the tool docstring is the MCP description that small local LLMs use for tool selection. First sentence says what it returns; keep it concrete.
- **Tests**: one `tests/test_<service>.py` per tool module, mocked httpx (no live calls), happy path plus error paths. Known gap: 10 modules currently have no test file (baselines, changefeed, compound, freshness, graph, health, healthchecks, litellm, llama, refresh). `make test`, `make lint`, `make format`; git hooks via `make hooks` (`.githooks/`).
- **Config**: all env access lives in `config.py` as module constants; tool modules import `config`, never `os.environ` directly.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

- **`server.py`**: builds the FastMCP app. Lifespan creates one httpx client (or `SessionAuthManager`) per configured service into a `clients` dict, loads knowledge registries, and spawns the `periodic_refresh` background task. Registers all tool modules and resources. HTTP mode runs uvicorn directly with `LanBypassMiddleware` wrapped outside the FastMCP auth chain.
- **Auth (HTTP transport only)**: `MultiAuth` = Authentik OIDC (`RemoteAuthProvider` + RS256 `JWTVerifier`, issuer `auth.example.com`, resource `mcp.example.com`) plus `LanBypassVerifier`. `LanBypassMiddleware` injects a per-process synthetic bearer for requests from `MCP_TRUSTED_CIDRS` that carry no proxy headers, so direct LAN traffic skips OIDC while proxied public traffic cannot. stdio mode has no auth.
- **`config.py`**: env constants, knowledge paths, mutable registries (`SERVICES`, `HOSTS`, `IP_INDEX`, `DOCS_INDEX`, `TOPOLOGY`, `BASELINES`, `STACKS_INDEX`, `VAULT_INDEX`) populated during lifespan, and `build_instructions()` which generates the MCP instructions string from the YAML registries.
- **`lib/`**: `auth.py` (SessionAuthManager + LoginStrategy protocol), `hosts.py` (canonical host-name resolution from the hosts.yaml seed), `meta.py` (`build_meta` + `staleness` freshness metadata), `audit.py` (fire-and-forget write audit to Loki), `http.py` (`service_request` shared error ladder), `redact.py` (query-string secret scrubber), and shared upstream parsers/helpers (`certs.py`, `crowdsec.py`, `healthchecks.py`, `gitea.py`, `portainer.py`, `promql.py`, `scanopy.py`, `wireguard.py`, `gather.py`). Refresh is split across `refresh_registries.py` (live-API service/host refresh), `refresh_content.py` (Gitea docs/stacks/vault sync), and `refresh.py` (facade owning the `periodic_refresh` background task).
- **`tools/`**: 35 modules, one per service plus cross-cutting modules (`aggregation`, `compound`, `health`, `changefeed`, `baselines`, `freshness`, `graph`, `knowledge`, `refresh`).
- **`resources.py`**: MCP resources `homelab://docs/{name}`, `homelab://stacks/{name}`, `homelab://vault/{path}`.
- **Knowledge data**: `data/` holds docs/stacks/vault plus YAML registries. Local dev syncs it with `make sync-data`; the Docker image strips `data/docs|stacks|vault` at build and fetches them live from Gitea (`DOCS_REPO`, `STACKS_REPO`) via the refresh loop.
- **Deployment**: Gitea Actions CI (`.gitea/workflows/ci.yml`) runs ruff + pytest on every branch; a master push builds the image (CalVer+SHA tag), pushes to the Gitea registry, renders `stack/docker-compose.yaml`, deploys to beast (192.168.1.119) over SSH, and polls `/health` for the new `APP_VERSION`. Secrets live only in the host-managed `.env` on beast.
<!-- GSD:architecture-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd:quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd:debug` for investigation and bug fixing
- `/gsd:execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
