"""Synology NAS tools for homelab MCP server."""

import asyncio
import time

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta
from lib.redact import redact_exception


class SynologyLoginStrategy:
    """Login strategy for Synology DSM session auth.

    Logs in via /webapi/auth.cgi with API v7 to get a session ID (_sid).
    auth.cgi accepts POST, so credentials go in the form body rather than the
    query string (which the DSM access log, proxies, and httpx's request logger
    would otherwise record). Session is valid for ~7 days; we refresh
    conservatively at 6 days.
    """

    SESSION_DURATION = 6 * 24 * 3600  # 6 days (conservative, actual 7)

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        resp = await client.post(
            "/webapi/auth.cgi",
            data={
                "api": "SYNO.API.Auth",
                "version": "7",
                "method": "login",
                "account": self._username,
                "passwd": self._password,
                "format": "sid",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") or not data.get("data", {}).get("sid"):
            error_code = data.get("error", {}).get("code", "unknown")
            raise RuntimeError(f"Synology auth failed (code: {error_code})")

        sid = data["data"]["sid"]
        return LoginResult(
            params={"_sid": sid},
            expires_at=time.time() + self.SESSION_DURATION,
        )

    # DSM session-auth error codes that mean "re-login": 105 no permission,
    # 106 session timeout, 107 session interrupted by duplicate login,
    # 119 SID not found (e.g. after a NAS reboot).
    SESSION_AUTH_CODES = (105, 106, 107, 119)

    def is_auth_error(self, response: httpx.Response) -> bool:
        """Synology reports auth errors in response body, not HTTP status."""
        if response.status_code != 200:
            return False
        try:
            data = response.json()
            code = data.get("error", {}).get("code")
            return not data.get("success") and code in self.SESSION_AUTH_CODES
        except Exception:
            return False


def register(mcp):
    """Register Synology tools. Skips if credentials are not configured."""
    if not (
        config.SYNOLOGY_URL and config.SYNOLOGY_USERNAME and config.SYNOLOGY_PASSWORD
    ):
        return

    async def _api(ctx: Context, api: str, version: int, method: str) -> dict:
        """Call a Synology API via the unified entry.cgi endpoint."""
        client = ctx.lifespan_context["synology"]  # SessionAuthManager
        try:
            resp = await client.get(
                "/webapi/entry.cgi",
                params={
                    "api": api,
                    "version": str(version),
                    "method": method,
                },
            )
            data = resp.json()
            if not data.get("success"):
                error_code = data.get("error", {}).get("code", "unknown")
                return {
                    "error": "api_error",
                    "message": f"Synology API {api} failed (error code: {error_code})",
                }
            return data.get("data", {})
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "Synology did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": redact_exception(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": redact_exception(e)}
        except Exception as e:
            return {"error": "connection_error", "message": redact_exception(e)}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_nas_status(ctx: Context) -> dict:
        """Get complete NAS status: system info, CPU/RAM utilization, disk health, volume usage, and RAID/pool status in one call."""
        # Fetch all three APIs in parallel
        info, util, storage = await asyncio.gather(
            _api(ctx, "SYNO.DSM.Info", 2, "getinfo"),
            _api(ctx, "SYNO.Core.System.Utilization", 1, "get"),
            _api(ctx, "SYNO.Storage.CGI.Storage", 1, "load_info"),
        )

        # System info
        if "error" in info:
            system = info
        else:
            system = {
                "model": info.get("model", "Unknown"),
                "serial": info.get("serial", "Unknown"),
                "dsm_version": info.get("version_string", "Unknown"),
                "uptime_seconds": info.get("uptime_seconds", 0),
                "temperature_c": info.get("temperature", 0),
            }

        # Utilization
        if "error" in util:
            utilization = util
        else:
            cpu = util.get("cpu", {})
            memory = util.get("memory", {})
            total_real = int(memory.get("total_real", 0))
            avail_real = int(memory.get("avail_real", 0))
            used_real = total_real - avail_real
            utilization = {
                "cpu_load_percent": cpu.get("user_load", 0) + cpu.get("system_load", 0),
                "ram_total_mb": int(total_real / 1024),
                "ram_used_mb": int(used_real / 1024),
                "ram_used_percent": round(used_real / max(total_real, 1) * 100, 1),
            }

        # Disks, volumes, storage pools (all from storage response)
        storage_error = None
        if "error" in storage:
            # Surface the storage error rather than reporting empty disks/volumes
            # (indistinguishable from a NAS with zero disks), mirroring how the
            # info/utilization errors above are propagated.
            storage_error = storage
            disks = []
            volumes = []
            storage_pools = []
        else:
            disks = [
                {
                    "id": disk.get("id", "Unknown"),
                    "name": disk.get("name", "Unknown"),
                    "model": disk.get("model", "Unknown"),
                    "size_bytes": int(disk.get("size_total", "0")),  # STRING! Pitfall 1
                    "temperature_c": disk.get("temp", 0),
                    "status": disk.get("status", "unknown"),
                    "smart_status": disk.get("smart_status", "unknown"),
                }
                for disk in storage.get("disks", [])
            ]

            volumes = [
                {
                    "id": vol.get("id", "Unknown"),
                    "status": vol.get("status", "unknown"),
                    "size_total_bytes": int(vol.get("size", {}).get("total", "0")),
                    "size_used_bytes": int(vol.get("size", {}).get("used", "0")),
                    "used_percent": round(
                        int(vol.get("size", {}).get("used", "0"))
                        / max(int(vol.get("size", {}).get("total", "1")), 1)
                        * 100,
                        1,
                    ),
                    "fs_type": vol.get("fs_type", "unknown"),
                }
                for vol in storage.get("volumes", [])
            ]

            storage_pools = [
                {
                    "id": pool.get("id", "Unknown"),
                    "status": pool.get("status", "unknown"),
                    "raid_type": pool.get("raidType", "unknown"),
                    "disk_count": len(pool.get("disks", [])),
                }
                for pool in storage.get("storagePools", storage.get("raids", []))
            ]

        result = {
            "system": system,
            "utilization": utilization,
            "disks": disks,
            "volumes": volumes,
            "storage_pools": storage_pools,
            "_meta": build_meta("synology"),
        }
        if storage_error is not None:
            result["storage_error"] = storage_error
        return result
