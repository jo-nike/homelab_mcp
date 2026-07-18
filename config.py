"""Configuration and knowledge loading for homelab-mcp."""

import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# --- Monitoring (no auth) ---
# No hardcoded defaults: like every other service these default to None so the
# conditional-registration guard (`if not config.PROMETHEUS_URL: return`) can
# actually skip the tool group when the service is absent, and the operator's network
# topology is not baked into source. Set both in .env (prod beast .env already
# does); see .env.example.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL")
LOKI_URL = os.environ.get("LOKI_URL")

# --- Infrastructure (auth required) ---
PROXMOX_URL = os.environ.get("PROXMOX_URL")  # e.g. https://192.168.1.114:8006
PROXMOX_TOKEN_ID = os.environ.get("PROXMOX_TOKEN_ID")  # e.g. root@pam!monitoring
PROXMOX_TOKEN_SECRET = os.environ.get("PROXMOX_TOKEN_SECRET")  # UUID

PORTAINER_URL = os.environ.get("PORTAINER_URL")  # e.g. https://192.168.1.79:9443
PORTAINER_API_KEY = os.environ.get("PORTAINER_API_KEY")

# --- Media (header auth) ---
PLEX_URL = os.environ.get("PLEX_URL")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN")
SONARR_URL = os.environ.get("SONARR_URL")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY")
RADARR_URL = os.environ.get("RADARR_URL")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY")
OVERSEERR_URL = os.environ.get("OVERSEERR_URL")
OVERSEERR_API_KEY = os.environ.get("OVERSEERR_API_KEY")
TRANSMISSION_URL = os.environ.get("TRANSMISSION_URL")
TRANSMISSION_USERNAME = os.environ.get("TRANSMISSION_USERNAME")
TRANSMISSION_PASSWORD = os.environ.get("TRANSMISSION_PASSWORD")

# --- Storage ---
SYNOLOGY_URL = os.environ.get("SYNOLOGY_URL")
SYNOLOGY_USERNAME = os.environ.get("SYNOLOGY_USERNAME")
SYNOLOGY_PASSWORD = os.environ.get("SYNOLOGY_PASSWORD")
PBS_URL = os.environ.get("PBS_URL")
PBS_TOKEN_ID = os.environ.get("PBS_TOKEN_ID")
PBS_TOKEN_SECRET = os.environ.get("PBS_TOKEN_SECRET")
BACKBLAZE_KEY_ID = os.environ.get("BACKBLAZE_KEY_ID")
BACKBLAZE_APP_KEY = os.environ.get("BACKBLAZE_APP_KEY")

# --- DNS ---
TECHNITIUM_URL = os.environ.get("TECHNITIUM_URL")  # http://192.168.1.79:5380
TECHNITIUM_PASSWORD = os.environ.get("TECHNITIUM_PASSWORD")

# --- DevOps ---
GITEA_URL = os.environ.get("GITEA_URL")  # http://192.168.1.79:7850
GITEA_TOKEN = os.environ.get("GITEA_TOKEN")
STACKS_REPO = os.environ.get(
    "STACKS_REPO", "youruser/docker-stacks"
)  # owner/repo for stacks
STACKS_BRANCH = os.environ.get("STACKS_BRANCH", "master")
DOCS_REPO = os.environ.get(
    "DOCS_REPO", "youruser/homelab-docs"
)  # owner/repo for docs+vault
DOCS_BRANCH = os.environ.get("DOCS_BRANCH", "master")

# --- Network ---
WIREGUARD_URL = os.environ.get("WIREGUARD_URL")  # http://192.168.1.79:51821
WIREGUARD_PASSWORD = os.environ.get("WIREGUARD_PASSWORD")
WIREGUARD_USERNAME = os.environ.get("WIREGUARD_USERNAME", "admin")
SCANOPY_URL = os.environ.get("SCANOPY_URL")  # http://192.168.1.79:60072
SCANOPY_API_KEY = os.environ.get("SCANOPY_API_KEY")

# --- Tasks ---
VIKUNJA_URL = os.environ.get("VIKUNJA_URL")  # http://192.168.1.79:3456
VIKUNJA_TOKEN = os.environ.get("VIKUNJA_TOKEN")
VIKUNJA_ALERT_PROJECT_ID = os.environ.get("VIKUNJA_ALERT_PROJECT_ID")

# --- Reverse Proxy ---
NPM_URL = os.environ.get("NPM_URL")  # http://192.168.1.17:81
NPM_EMAIL = os.environ.get("NPM_EMAIL")
NPM_PASSWORD = os.environ.get("NPM_PASSWORD")

# --- Indexers ---
PROWLARR_URL = os.environ.get("PROWLARR_URL")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY")

# --- Security ---
CROWDSEC_URL = os.environ.get("CROWDSEC_URL")  # http://192.168.1.79:8180
CROWDSEC_API_KEY = os.environ.get(
    "CROWDSEC_API_KEY"
)  # Bouncer API key from cscli bouncers add

# --- Speed Test ---
MYSPEED_URL = os.environ.get("MYSPEED_URL")
MYSPEED_PASSWORD = os.environ.get("MYSPEED_PASSWORD")

# --- Cron Monitoring ---
HEALTHCHECKS_URL = os.environ.get("HEALTHCHECKS_URL")
HEALTHCHECKS_API_KEY = os.environ.get("HEALTHCHECKS_API_KEY")

# --- Plex Analytics ---
TAUTULLI_URL = os.environ.get("TAUTULLI_URL")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY")

# --- LLM Proxy ---
LITELLM_URL = os.environ.get("LITELLM_URL")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")

# --- Inference Server ---
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL")
LLAMA_SERVER_API_KEY = os.environ.get("LLAMA_SERVER_API_KEY")  # Optional

# --- Web Search (no auth) ---
SEARXNG_URL = os.environ.get("SEARXNG_URL")  # http://192.168.1.79:8890

# fetch_page SSRF policy. Loopback, link-local, and cloud-metadata targets are
# always blocked. RFC1918/private targets are allowed only when this is true
# (default true, to preserve the ability to fetch LAN pages).
FETCH_ALLOW_PRIVATE = os.environ.get(
    "FETCH_ALLOW_PRIVATE", "true"
).strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to `default` (with a warning) on a
    non-numeric value so a typo like '10m' cannot abort startup with a
    context-free ValueError traceback."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "Invalid integer for %s=%r; using default %d", name, raw, default
        )
        return default


# --- Refresh intervals ---
REFRESH_INTERVAL_SECONDS = _env_int("REFRESH_INTERVAL_SECONDS", 600)  # 10 min
DOC_REFRESH_INTERVAL_SECONDS = _env_int("DOC_REFRESH_INTERVAL_SECONDS", 3600)  # 1 hr

# --- Server transport / auth (read here so all env access lives in config) ---
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_PORT = _env_int("MCP_PORT", 8000)
APP_VERSION = os.environ.get("APP_VERSION", "dev")
# CIDRs whose direct (unproxied, no-Origin) connections get the in-process
# LAN-bypass bearer. Empty string disables the bypass entirely.
MCP_TRUSTED_CIDRS = os.environ.get("MCP_TRUSTED_CIDRS", "192.168.1.0/24,127.0.0.0/8")
# CORS allowlist for browser clients; empty (default) mounts no CORS middleware.
MCP_CORS_ORIGINS = os.environ.get("MCP_CORS_ORIGINS", "")

# Authentik OIDC endpoints (env-overridable so a redeploy under another domain
# needs no code change). AUTHENTIK_CLIENT_ID has no default -- HTTP-mode startup
# fails with a clear message when it is missing (see server._build_auth).
AUTHENTIK_CLIENT_ID = os.environ.get("AUTHENTIK_CLIENT_ID")
AUTHENTIK_ISSUER = os.environ.get(
    "AUTHENTIK_ISSUER", "https://auth.example.com/application/o/homelab-mcp/"
)
AUTHENTIK_JWKS_URI = os.environ.get(
    "AUTHENTIK_JWKS_URI", "https://auth.example.com/application/o/homelab-mcp/jwks/"
)
MCP_RESOURCE_BASE_URL = os.environ.get(
    "MCP_RESOURCE_BASE_URL", "https://mcp.example.com"
)

# --- Knowledge paths ---
DATA_DIR = Path(__file__).parent / "data"
DOCS_DIR = Path(os.environ.get("DOCS_DIR", str(DATA_DIR / "docs")))
STACKS_DIR = Path(os.environ.get("STACKS_DIR", str(DATA_DIR / "stacks")))
VAULT_DIR = Path(os.environ.get("VAULT_DIR", str(DATA_DIR / "vault")))

# --- Mutable registries (populated during lifespan) ---
SERVICES: dict[str, dict] = {}
HOSTS: dict[str, dict] = {}
IP_INDEX: dict[str, dict] = {}
DOCS_INDEX: dict[str, dict] = {}
TOPOLOGY: dict = {}
BASELINES: dict = {}
STACKS_INDEX: dict[str, str] = {}  # {stack_name: compose_yaml_content}
VAULT_INDEX: dict[str, dict] = {}  # {filename: {"content": str, "sections": [...]}}

# --- Refresh tracking ---
REFRESH_TIMESTAMPS: dict[
    str, str
] = {}  # {"registries": "2026-...", "docs": "2026-..."}
STALENESS_THRESHOLDS: dict[str, int] = {
    "default_registries": 1800,  # 30 minutes
    "default_docs": 7200,  # 2 hours
}


def load_services() -> dict[str, dict]:
    """Load services from data/services.yaml, keyed by service name."""
    services_path = DATA_DIR / "services.yaml"
    if not services_path.exists():
        return {}
    with open(services_path) as f:
        data = yaml.safe_load(f)
    if not data or "services" not in data:
        return {}
    # Skip (and log) entries lacking 'name' rather than crashing startup with a
    # bare KeyError: data/ is synced from other repos, so one malformed entry
    # must not abort the whole server (build_instructions runs at import).
    services = {}
    for svc in data["services"]:
        if not isinstance(svc, dict) or not svc.get("name"):
            logger.warning("Skipping services.yaml entry with no 'name': %r", svc)
            continue
        services[svc["name"]] = svc
    return services


def load_hosts() -> dict[str, dict]:
    """Load hosts from data/hosts.yaml, keyed by host name."""
    hosts_path = DATA_DIR / "hosts.yaml"
    if not hosts_path.exists():
        return {}
    with open(hosts_path) as f:
        data = yaml.safe_load(f)
    if not data or "hosts" not in data:
        return {}
    # Skip (and log) entries lacking 'name' -- see load_services rationale.
    hosts = {}
    for host in data["hosts"]:
        if not isinstance(host, dict) or not host.get("name"):
            logger.warning("Skipping hosts.yaml entry with no 'name': %r", host)
            continue
        hosts[host["name"]] = host
    return hosts


def build_ip_index() -> dict[str, dict]:
    """Cross-reference HOSTS and SERVICES by IP address.

    For each host IP, creates {"host": host_dict, "services": [matching_svc_dicts]}.
    For service IPs not matching a host, creates entry with {"host": None, "services": [svc]}.
    """
    index: dict[str, dict] = {}

    # Index all hosts by IP
    for host in HOSTS.values():
        ip = host.get("ip")
        if ip:
            index[ip] = {"host": host, "services": []}

    # Attach services to their host IP
    for svc in SERVICES.values():
        ip = svc.get("ip")
        if not ip:
            continue
        if ip in index:
            index[ip]["services"].append(svc)
        else:
            index[ip] = {"host": None, "services": [svc]}

    return index


def load_baselines() -> dict:
    """Load non-metric baselines from data/baselines.yaml."""
    baselines_path = DATA_DIR / "baselines.yaml"
    if not baselines_path.exists():
        return {}
    with open(baselines_path) as f:
        data = yaml.safe_load(f)
    return data or {}


def load_docs_index() -> dict[str, dict]:
    """Scan DOCS_DIR for .md files and index their content and sections.

    Returns {filename: {"path": str, "content": str, "sections": [{"heading": str, "text": str}]}}.
    """
    index: dict[str, dict] = {}

    if not DOCS_DIR.exists():
        return index

    for md_file in sorted(DOCS_DIR.glob("*.md")):
        content = md_file.read_text(errors="replace")
        sections = parse_sections(content)
        index[md_file.name] = {
            "path": str(md_file),
            "content": content,
            "sections": sections,
        }

    return index


def load_topology() -> dict:
    """Load entity graph from data/topology.yaml."""
    topo_path = DATA_DIR / "topology.yaml"
    if not topo_path.exists():
        return {}
    with open(topo_path) as f:
        data = yaml.safe_load(f)
    return data or {}


def parse_sections(content: str) -> list[dict]:
    """Parse markdown content into sections by heading.

    Public (not `_`-prefixed) because lib/refresh_content imports it: it is de
    facto shared API between load_docs_index and the Gitea doc fetchers.
    """
    sections: list[dict] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        if line.startswith("# ") or line.startswith("## "):
            # Save previous section
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append({"heading": current_heading, "text": text})
            current_heading = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save final section
    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append({"heading": current_heading, "text": text})

    return sections


def build_instructions() -> str:
    """Generate MCP instructions string from YAML registries.

    Reads services.yaml and hosts.yaml directly (not from mutable registries)
    so this can be called at module level before lifespan populates registries.
    """
    services = load_services()
    hosts = load_hosts()

    # Build compact topology table
    lines = [
        "Homelab MCP Server - monitoring, knowledge, and management tools.",
        "",
        "TOPOLOGY (192.168.1.0/24):",
        f"{'Host':<16} {'IP':<8} {'Role':<45} {'Key Services'}",
        "-" * 100,
    ]

    for host in hosts.values():
        ip = host.get("ip", "")
        # Use shorthand for 192.168.1.x addresses
        if ip.startswith("192.168.1."):
            ip_short = "." + ip.split(".")[-1]
        else:
            ip_short = ip

        # Find services on this host
        host_services = [
            svc["name"]
            for svc in services.values()
            if svc.get("host") == host["name"] or svc.get("ip") == host.get("ip")
        ]
        svc_str = ", ".join(host_services[:5])
        if len(host_services) > 5:
            svc_str += f" (+{len(host_services) - 5} more)"

        lines.append(
            f"{host['name']:<16} {ip_short:<8} {host.get('role', ''):<45} {svc_str}"
        )

    lines.extend(
        [
            "",
            "TOOL CATEGORIES:",
            "- Prometheus: host health, container stats, GPU status, storage usage, raw PromQL",
            "- Loki: recent errors, container logs, raw LogQL",
            "- Proxmox: node status, VM/CT listing, VM/CT detail",
            "- Docker: container listing across hosts, container detail by name",
            "- Media: Plex streams/recently added, Sonarr/Radarr upcoming/queue, Overseerr requests, Transmission torrents",
            "- Storage: Synology NAS status, PBS backup status, Backblaze B2 usage",
            "- DNS: Technitium DNS lookup, zone listing, zone records",
            "- DevOps: Gitea repos, pull requests, CI/CD runs",
            "- Network: WireGuard VPN peers, Scanopy network topology",
            "- Tasks: Vikunja task listing, task detail",
            "- Task Management: create and update Vikunja tasks",
            "- Reverse Proxy: NPM proxy hosts, SSL certificates, redirections, dead hosts",
            "- Indexers: Prowlarr indexer status and health",
            "- Security: CrowdSec alerts, blocking decisions, bouncer and machine status",
            "- Speed Test: MySpeed latest result and recent history",
            "- Cron Monitoring: Healthchecks check status and recent pings",
            "- Plex Analytics: Tautulli viewing history and stats",
            "- LLM Proxy: LiteLLM model list, health, usage stats",
            "- Inference: llama-server model status, slots, metrics",
            "- Aggregation: homelab overview (all services + speed test), media overview (Plex/Sonarr/Radarr/Transmission/Overseerr/Prowlarr), infrastructure overview (Proxmox/Docker/storage/NPM/CrowdSec)",
            "- Actions: container restart, dependency-aware safe restart, create task from alert",
            "- Web Search: search the web (general, code, academic, news), fetch and extract page content",
            "- Knowledge: search docs, service/host/IP lookup",
            "- Refresh: force refresh of service/host registries or documentation from live sources",
            "",
            "Write tools: restart_container, create_vikunja_task, update_vikunja_task, safe_restart_container, create_task_from_alert, overseerr_approve_request, overseerr_decline_request, apply_image_update. Writes are audit-logged to Loki on a best-effort basis (a Loki outage is logged locally but does not block the write). Use dry_run=True to preview any write action.",
        ]
    )

    lines.extend(
        [
            "",
            "Knowledge data (services, hosts, docs, stacks) refreshes automatically from live APIs on a configurable interval. Call refresh_registries or refresh_docs to force an immediate update. All knowledge responses include staleness metadata.",
        ]
    )

    return "\n".join(lines)
