"""Transmission tools for homelab MCP server."""

import base64
from typing import Annotated

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta

# --- Status code mapping ---

STATUS_MAP = {
    0: "stopped",
    1: "checking",
    2: "checking",
    3: "queued",
    4: "downloading",
    5: "queued",
    6: "seeding",
}

TORRENT_FIELDS = [
    "id",
    "name",
    "status",
    "percentDone",
    "rateDownload",
    "rateUpload",
    "eta",
    "totalSize",
    "downloadedEver",
    "uploadedEver",
    "uploadRatio",
    "peersConnected",
    "addedDate",
]


class TransmissionLoginStrategy:
    """Login strategy for Transmission CSRF token acquisition.

    Transmission uses a 409 CSRF pattern: first request returns 409
    with X-Transmission-Session-Id header. Subsequent requests must
    include that header.
    """

    def __init__(self, username: str | None = None, password: str | None = None):
        self._username = username
        self._password = password

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        # Compute the Basic credential once and reuse it for both the trigger
        # request and the returned auth headers.
        authorization = None
        if self._username and self._password:
            cred = base64.b64encode(
                f"{self._username}:{self._password}".encode()
            ).decode()
            authorization = f"Basic {cred}"

        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization

        # Make a dummy RPC request to trigger 409 with session ID
        resp = await client.post(
            "/transmission/rpc",
            headers=headers,
            json={"method": "session-get"},
        )

        session_id = resp.headers.get("x-transmission-session-id")
        if not session_id:
            # If we got a 200 somehow, the session ID might already be valid
            session_id = ""

        auth_headers = {"X-Transmission-Session-Id": session_id}
        if authorization:
            auth_headers["Authorization"] = authorization

        return LoginResult(headers=auth_headers, expires_at=None)

    def is_auth_error(self, response: httpx.Response) -> bool:
        return response.status_code == 409


def register(mcp):
    """Register Transmission tools. Skips if credentials are not configured."""
    if not config.TRANSMISSION_URL:
        return

    async def _rpc(ctx: Context, method: str, arguments: dict | None = None) -> dict:
        """Execute Transmission JSON-RPC call."""
        client = ctx.lifespan_context["transmission"]  # SessionAuthManager
        try:
            body = {"method": method}
            if arguments:
                body["arguments"] = arguments
            resp = await client.post("/transmission/rpc", json=body)
            # Distinguish HTTP failures (e.g. 401 from wrong Basic-auth creds,
            # which returns an HTML body) from real connection errors, instead
            # of letting the JSONDecodeError surface as a misleading
            # "connection_error".
            if resp.status_code >= 400:
                return {
                    "error": "http_error",
                    "status": resp.status_code,
                    "message": f"HTTP {resp.status_code} from Transmission",
                }
            # Transmission returns 200 for success, body has "result": "success"
            data = resp.json()
            if data.get("result") != "success":
                return {
                    "error": "rpc_error",
                    "message": data.get("result", "unknown error"),
                }
            return data.get("arguments", {})
        except httpx.TimeoutException:
            return {
                "error": "timeout",
                "message": "Transmission did not respond in time",
            }
        except Exception as e:
            return {"error": "connection_error", "message": str(e)}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_transmission_torrents(
        ctx: Context,
        limit: Annotated[int, "Max torrents to return (default 25)"] = 25,
        offset: Annotated[
            int, "Number of torrents to skip for pagination (default 0)"
        ] = 0,
    ) -> dict:
        """Get torrents from Transmission with download/upload speeds, progress, and status. Returns most recently added first, paginated (default 25). Aggregate speeds cover all torrents."""
        data = await _rpc(ctx, "torrent-get", {"fields": TORRENT_FIELDS})

        if "error" in data:
            return data

        torrents = data.get("torrents", [])

        # Aggregate speeds across ALL torrents before pagination. Transmission
        # rates are bytes/sec; dividing by 1000 gives KB/s, so the fields are
        # named *_kb_per_sec (D5: the old *_kbps names mislabelled the unit).
        total_download_kb_per_sec = round(
            sum(t.get("rateDownload", 0) for t in torrents) / 1000, 1
        )
        total_upload_kb_per_sec = round(
            sum(t.get("rateUpload", 0) for t in torrents) / 1000, 1
        )
        total_count = len(torrents)

        # Sort by most recently added, then paginate
        torrents.sort(key=lambda t: t.get("addedDate", 0), reverse=True)
        page = torrents[offset : offset + limit]

        torrent_list = []
        for t in page:
            eta_val = t.get("eta", -1)
            torrent_list.append(
                {
                    "id": t.get("id"),
                    "name": t.get("name", "Unknown"),
                    "status": STATUS_MAP.get(t.get("status", 0), "unknown"),
                    "progress_percent": round(t.get("percentDone", 0) * 100, 1),
                    "download_speed_kb_per_sec": round(
                        t.get("rateDownload", 0) / 1000, 1
                    ),
                    "upload_speed_kb_per_sec": round(t.get("rateUpload", 0) / 1000, 1),
                    "eta_seconds": eta_val if eta_val >= 0 else None,
                    "size_bytes": t.get("totalSize", 0),
                    "downloaded_bytes": t.get("downloadedEver", 0),
                    "uploaded_bytes": t.get("uploadedEver", 0),
                    "ratio": round(t.get("uploadRatio", 0), 2),
                    "peers": t.get("peersConnected", 0),
                }
            )

        return {
            "torrents": torrent_list,
            "total_count": total_count,
            "showing": len(torrent_list),
            "offset": offset,
            "has_more": offset + limit < total_count,
            "total_download_speed_kb_per_sec": total_download_kb_per_sec,
            "total_upload_speed_kb_per_sec": total_upload_kb_per_sec,
            "_meta": build_meta("transmission"),
        }
