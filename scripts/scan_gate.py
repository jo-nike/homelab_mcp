#!/usr/bin/env python3
"""Pre-push mcp-scan gate: alert (never block) on high/critical findings.

Runs Snyk Agent Scan against scan/mcp-scan-config.json and prints a prominent
warning if any tool-description finding is severity high or critical. Always
exits 0 — this is an informational alert, not a gate. Skips quietly (with a
clear message) when there is no SNYK_TOKEN or the scan cannot complete
(offline, Snyk unavailable), so it never wedges a push.

Token resolution: prefer an exported $SNYK_TOKEN, else the SNYK_TOKEN line from
the gitignored .env. Stdlib only; run as `python3 scripts/scan_gate.py` from the
repo root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = "scan/mcp-scan-config.json"
ALERT_SEVERITIES = {"high", "critical"}


def _load_token() -> str | None:
    token = os.environ.get("SNYK_TOKEN")
    if token:
        return token.strip()
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith("SNYK_TOKEN="):
            return line[len("SNYK_TOKEN=") :].strip().strip('"')
    return None


def _run_scan(token: str) -> dict | None:
    env = {**os.environ, "SNYK_TOKEN": token}
    try:
        proc = subprocess.run(
            [
                "uvx",
                "snyk-agent-scan@latest",
                CONFIG,
                "--dangerously-run-mcp-servers",
                "--json",
                "--suppress-mcpserver-io=true",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _collect_alerts(obj: object, out: list[tuple[str, str, str]]) -> None:
    """Walk the report and collect (severity, code, message) for high/critical."""
    if isinstance(obj, dict):
        extra = obj.get("extra_data")
        sev = extra.get("severity") if isinstance(extra, dict) else None
        code = obj.get("code")
        if code and isinstance(sev, str) and sev in ALERT_SEVERITIES:
            out.append((sev, str(code), str(obj.get("message", "")).strip()))
        for value in obj.values():
            _collect_alerts(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect_alerts(value, out)


def main() -> int:
    token = _load_token()
    if not token:
        print("  ↳ mcp-scan skipped: no SNYK_TOKEN (export it or add it to .env)")
        return 0

    report = _run_scan(token)
    if report is None:
        print(
            "  ↳ mcp-scan skipped: scan did not complete (offline / Snyk unavailable)"
        )
        return 0

    alerts: list[tuple[str, str, str]] = []
    _collect_alerts(report, alerts)

    if not alerts:
        print("  ↳ mcp-scan ok: no high/critical findings")
        return 0

    print("")
    print("  ⚠️  mcp-scan: HIGH/CRITICAL tool-description findings")
    for sev, code, message in alerts:
        print(f"     [{sev.upper()} {code}] {message}")
    print("  (non-blocking alert — push continues; run `make scan` for full detail)")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
