"""Audit logging for write operations via Loki push API."""

import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)


async def audit_log(
    ctx,
    *,
    action: str,
    target: str,
    params: dict | None = None,
    result: str = "success",
    dry_run: bool = False,
) -> None:
    """Log a write operation to Loki. Best-effort: never raises.

    Auditing is best-effort by design -- if Loki is unconfigured or the push
    fails the write still proceeds. Those cases are logged locally at WARNING so
    an unaudited write is at least visible in the server's own logs.
    """
    client: httpx.AsyncClient | None = ctx.lifespan_context.get("loki")
    if client is None:
        logger.warning(
            "Audit log skipped (no Loki client): action=%s target=%s result=%s",
            action,
            target,
            result,
        )
        return

    try:
        ts_ns = str(int(time.time() * 1e9))
        log_entry = {
            "action": action,
            "target": target,
            "params": params or {},
            "result": result,
            "dry_run": dry_run,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        payload = {
            "streams": [
                {
                    "stream": {
                        "job": "homelab-mcp",
                        "level": "info",
                        "type": "audit",
                    },
                    "values": [[ts_ns, json.dumps(log_entry)]],
                }
            ]
        }
        await client.post("/loki/api/v1/push", json=payload)
    except Exception as e:
        logger.warning(
            "Audit log push to Loki failed (write not audited): "
            "action=%s target=%s result=%s error=%s",
            action,
            target,
            result,
            e,
        )
