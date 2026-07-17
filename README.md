# homelab-mcp

A [FastMCP](https://gofastmcp.com/) server that puts an entire homelab behind a single MCP endpoint. It exposes 72 tools across 26 services (Prometheus, Loki, Proxmox, Portainer, Plex, Sonarr/Radarr, Synology, PBS, Backblaze, Technitium DNS, Gitea, WireGuard, and more) so that any LLM — a small local model or a cloud one — can answer "what's going on in the lab?" with one tool call, no PromQL, no multi-step reasoning, no per-service API knowledge.

## Key features

- **Summary-first tool design.** Every tool returns a complete, pre-digested answer (e.g. `get_homelab_overview`, `what_needs_attention`, `explain_host_health`). Raw query tools (`query_prometheus`, `query_logs`) exist but are secondary. Tool docstrings double as MCP descriptions, tuned so small local LLMs pick the right tool.
- **72 tools across 26 services**, organized into 35 tool modules plus a built-in knowledge base (services, hosts, IPs, docs, compose stacks, topology graph).
- **Conditional registration.** Each tool module checks its env vars at startup and skips registration when credentials are missing — a partial `.env` yields a smaller but fully working server, never a broken one.
- **Dual transport.** stdio by default (for local MCP clients), streamable-http via `MCP_TRANSPORT` (for network clients). HTTP mode adds an OIDC + trusted-LAN auth chain.
- **Mostly read-only.** Seven write tools exist (container restarts, Vikunja tasks, Overseerr approvals); writes are audit-logged to Loki and support `dry_run=True`.

## Architecture

```
server.py            entrypoint: lifespan, auth chain, /health route, tool registration
  └─ lifespan        one long-lived httpx.AsyncClient per configured service,
                     created inside an AsyncExitStack; session-auth services
                     (Transmission, Synology, Technitium, wg-easy, NPM, B2, MySpeed)
                     are wrapped in lib/auth.py's SessionAuthManager
  └─ tools/*.py      one module per service; each exposes register(mcp) and
                     self-skips when its config.py env vars are unset
  └─ config.py       env-driven config (python-dotenv) + knowledge loaders
  └─ data/           services.yaml, hosts.yaml, topology.yaml, baselines.yaml,
                     docs/, stacks/, vault/ — synced from other repos via
                     `make sync-data` locally; fetched live from Gitea in prod,
                     refreshed by a background task on a configurable interval
```

Tools return plain dicts (FastMCP serializes to JSON); errors are never raised to the client, they come back as `{"error": <code>, "message": <human-readable>}`.

## Quickstart

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                    # install deps from uv.lock
cp .env.example .env       # fill in URLs/keys for the services you have
make dev                   # stdio transport (for Claude Desktop / MCP clients)
make http                  # streamable-http on port 8000 (override with MCP_PORT)
```

Only the services you configure get registered — an empty `.env` still starts, with just the credential-free tool groups.

The knowledge tools (service/host/IP lookup, topology, baselines) read four seed files: `data/hosts.yaml`, `data/services.yaml`, `data/topology.yaml`, `data/baselines.yaml`. The server boots fine without them — the knowledge tools just start empty. To create them for your own infrastructure:

```bash
uv run scripts/bootstrap_registries.py   # or --dry-run to preview
```

This discovers hosts and services from whatever you configured in `.env` (Proxmox, Portainer, Scanopy, NPM, Prometheus) and writes skeleton YAMLs with `TODO` markers for the fields only you know: role descriptions, the cross-service `aliases` map (which upstream name maps to which host — cross-tool joins depend on it), dependency edges, and the never-restart container list. Alternatively, copy the `data/*.example.yaml` files (shipped in the public mirror) and edit by hand.

## Docker / production

```bash
docker compose up -d       # builds the image, serves streamable-http on :5774
curl localhost:5774/health # {"status": "ok", "version": "..."}
```

The image (multi-stage `uv` build on `python:3.13-slim`) defaults to `MCP_TRANSPORT=streamable-http` and `MCP_PORT=5774`, with a Docker `HEALTHCHECK` polling `/health`. `data/stacks`, `data/docs`, and `data/vault` are stripped at build time and fetched live from Gitea at runtime instead.

Production runs on the `beast` host via Gitea CI (`.gitea/workflows/ci.yml`): quality gates on every branch, and on a master push it builds a CalVer+SHA-tagged image, pushes to the Gitea registry, renders `stack/docker-compose.yaml` with the pinned tag, deploys over SSH, and polls `/health` until the new version reports live. Secrets stay in the host-managed `.env` on beast.

In HTTP mode the server authenticates via a MultiAuth chain: Authentik OIDC (JWT verification) for external traffic, plus a LAN bypass that trusts direct, un-proxied requests from `MCP_TRUSTED_CIDRS` (default `192.168.1.0/24,127.0.0.0/8`).

## Configuration

All configuration is environment variables, loaded by python-dotenv from `.env` (local dev) or injected via `env_file:` (Docker). See `.env.example` (with `config.py` as the authoritative list) — roughly 75 vars, almost all of the form `<SERVICE>_URL` + `<SERVICE>_API_KEY`/token/password.

There is no validation layer on purpose: a missing var simply means that service's tools are not registered. Non-service knobs include `MCP_TRANSPORT`, `MCP_PORT`, `MCP_TRUSTED_CIDRS`, `REFRESH_INTERVAL_SECONDS`, and `DOC_REFRESH_INTERVAL_SECONDS`.

## Tool catalog

| Area | Modules | What you get |
|---|---|---|
| Monitoring | prometheus, loki, healthchecks, myspeed | Host/container/GPU/storage health, recent errors, container logs, cron check status, speed tests, raw PromQL/LogQL |
| Infrastructure | proxmox, docker, npm, crowdsec | Proxmox nodes and VMs/CTs, containers across hosts (Portainer), reverse-proxy hosts and certs, CrowdSec alerts and decisions |
| Media | plex, tautulli, sonarr, radarr, overseerr, transmission, prowlarr | Streams, library stats, upcoming/wanted, requests, torrents, indexer health |
| Storage | synology, pbs, backblaze | NAS status, backup jobs and datastores, B2 usage |
| DNS / Network | technitium, wireguard, scanopy | DNS lookups and zone records, VPN peers, network topology and IP info |
| DevOps / AI | gitea, litellm, llama, searxng | Repos, PRs, CI runs, LLM proxy status, llama-server slots/metrics, web search and page fetch |
| Knowledge | knowledge, graph, freshness, baselines, refresh | Service/host/IP lookup, docs search, dependency chains, "is this normal?" baseline comparison, forced registry/doc refresh |
| Aggregation | aggregation, compound, health, changefeed | One-call overviews (homelab/media/infra), `what_needs_attention`, `what_changed_last_24h`, health explainers |
| Write ops | docker, compound, vikunja, overseerr | `restart_container`, `safe_restart_container` (dependency-aware), `create_vikunja_task`, `update_vikunja_task`, `create_task_from_alert`, Overseerr approve/decline |

To see exactly what's registered against *your* `.env`, connect any MCP client and issue a `tools/list` request — the set varies with configured credentials.

## Development

```bash
make test        # pytest, 541 tests
make lint        # ruff check + format check
make format      # ruff format + autofix
make hooks       # point git at .githooks (pre-commit: ruff on staged Python files;
                 #                         pre-push: full lint + test suite)
make sync-data   # rsync docs/stacks/vault from sibling repos into data/
```

Tests mock httpx transports; nothing in the suite touches the real lab.

## Project layout

```
server.py           entrypoint, lifespan, auth, tool registration
config.py           env config + knowledge loaders
resources.py        MCP resources
lib/                auth (session managers), hosts, audit, meta, http (shared request ladder),
                    redact, refresh facade (refresh_registries/refresh_content), per-service helpers
tools/              35 tool modules, one register(mcp) each
scripts/            bootstrap_registries.py (registry generator) + publish tooling
tests/              pytest suite (541 tests)
data/               knowledge base (synced/fetched, not source code)
docs/               operational references (e.g. the Crow's Nest tool rename map)
stack/              production compose file rendered by CI
Dockerfile          multi-stage uv build
docker-compose.yml  local container run
.gitea/workflows/   CI + deploy pipeline
.githooks/          pre-commit / pre-push hooks
```

## Honest notes

- **Self-signed certs:** the Proxmox, PBS, Portainer, and Synology clients use `verify=False`. Fine for a LAN homelab, not a pattern to copy anywhere TLS actually matters.
- **Write tools:** deliberately few, all audit-logged to Loki, all previewable with `dry_run=True`. Still — an LLM with this server can restart your containers. Configure only the credentials you're comfortable delegating.
- **Scope:** built for a single user on a trusted LAN (`192.168.1.0/24`). The LAN-bypass auth grants full access to any un-proxied client in the trusted CIDRs, and there is no per-tool authorization. External access is expected to come through a reverse proxy with OIDC in front.
- **Knowledge freshness:** registry/doc data refreshes on a timer and can drift between refreshes; responses include staleness metadata, and `refresh_registries` / `refresh_docs` force an update.
