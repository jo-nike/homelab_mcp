"""WireGuard (wg-easy) VPN tools for homelab MCP server."""

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta
from lib.wireguard import is_connected as _is_connected


class WgEasyLoginStrategy:
    """Login strategy for wg-easy session cookie acquisition.

    wg-easy uses POST /api/session with a password to establish a session.
    The response sets a connect.sid cookie that must be sent on subsequent requests.
    """

    def __init__(self, password: str, username: str = "admin"):
        self._password = password
        self._username = username

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        resp = await client.post(
            "/api/session",
            json={
                "username": self._username,
                "password": self._password,
                "remember": False,
            },
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"WireGuard login failed: {resp.status_code} {resp.text}"
            )

        # The login response already populated the client's cookie jar with the
        # session cookie, so httpx sends it automatically on subsequent
        # requests. Return an empty result rather than a frozen Cookie header:
        # httpx suppresses jar cookies whenever a Cookie header is already set,
        # so a manual header would go stale on rotation and drop duplicate
        # cookies. is_auth_error remains the refresh trigger.
        return LoginResult(expires_at=None)

    def is_auth_error(self, response: httpx.Response) -> bool:
        return response.status_code == 401


def register(mcp):
    """Register WireGuard tools. Skips if credentials are not configured."""
    if not config.WIREGUARD_URL or not config.WIREGUARD_PASSWORD:
        return

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_wireguard_peers(ctx: Context) -> dict:
        """Get WireGuard VPN connected peers with traffic stats. Shows peer name, IP address, connection status (based on last handshake within 3 minutes), and data transfer amounts."""
        client = ctx.lifespan_context["wireguard"]  # SessionAuthManager
        try:
            resp = await client.get("/api/client")
            data = resp.json()
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "WireGuard did not respond in time"}
        except Exception as e:
            return {"error": "connection_error", "message": str(e)}

        # wg-easy v14+ returns a list from /api/client
        if isinstance(data, list):
            peers_raw = data
        elif isinstance(data, dict) and data.get("error"):
            return {
                "error": "wg_api_error",
                "message": data.get("message", "unknown"),
                "status": data.get("statusCode"),
            }
        else:
            return {
                "error": "unexpected_response",
                "message": f"Expected list, got {type(data).__name__}: {str(data)[:200]}",
            }

        peers = []
        for p in peers_raw:
            address = p.get("ipv4Address") or p.get("address", "")
            if "/" in address:
                address = address.split("/")[0]
            last_handshake = p.get("latestHandshakeAt")
            connected = _is_connected(last_handshake)

            peers.append(
                {
                    "name": p.get("name", "Unknown"),
                    "address": address,
                    "enabled": p.get("enabled", False),
                    "connected": connected,
                    "last_handshake": last_handshake,
                    "transfer_rx_bytes": p.get("transferRx", 0),
                    "transfer_tx_bytes": p.get("transferTx", 0),
                }
            )

        connected_count = sum(1 for peer in peers if peer["connected"])

        return {
            "peers": peers,
            "total_peers": len(peers),
            "connected_peers": connected_count,
            "_meta": build_meta("wireguard"),
        }
