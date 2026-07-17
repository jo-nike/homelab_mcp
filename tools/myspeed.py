"""MySpeed tools for homelab MCP server."""

import time

import httpx
from fastmcp import Context

import config
from lib.auth import LoginResult
from lib.meta import build_meta


class MySpeedLoginStrategy:
    """MySpeed session auth via POST /api/login."""

    def __init__(self, password: str):
        self.password = password

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        resp = await client.post(
            "/api/login",
            json={
                "password": self.password,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token", "")
        # Default session duration unknown; refresh every 12 hours
        expires_at = time.time() + 43200
        return LoginResult(
            headers={"Authorization": f"Bearer {token}"},
            expires_at=expires_at,
        )

    def is_auth_error(self, response: httpx.Response) -> bool:
        return response.status_code in (401, 403)


def _compute_trend(tests: list[dict]) -> str:
    """Compute trend from speed test download values.

    Compares latest download to average of previous 4. Failed tests record a
    null download, so coalesce to 0 to avoid a TypeError.
    Returns 'improving', 'degrading', or 'stable'.
    """
    if len(tests) < 5:
        return "unknown"

    latest_dl = tests[0].get("download") or 0
    prev_avg = sum((t.get("download") or 0) for t in tests[1:5]) / 4

    if prev_avg == 0:
        return "unknown"

    ratio = latest_dl / prev_avg
    if ratio > 1.10:
        return "improving"
    elif ratio < 0.90:
        return "degrading"
    return "stable"


def _process_test(test: dict) -> dict:
    """Convert a raw speed test result to display format.

    MySpeed stores failed tests with null download/upload/ping/jitter, so every
    numeric field is coalesced through `or 0` (test.get(k, 0) still yields None
    when the key is present with value None).
    """
    return {
        "download_mbps": round(test.get("download") or 0, 1),
        "upload_mbps": round(test.get("upload") or 0, 1),
        "ping_ms": test.get("ping") or 0,
        "jitter_ms": test.get("jitter") or 0,
        "timestamp": test.get("created", ""),
        "failed": bool(test.get("error")),
    }


def register(mcp):
    """Register MySpeed tools. Skips if URL is not configured."""
    if not config.MYSPEED_URL:
        return

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_myspeed_status(ctx: Context) -> dict:
        """Get MySpeed status: latest speed test result and the 25 most recent tests for trend detection."""
        client = ctx.lifespan_context["myspeed"]
        try:
            resp = await client.get("/api/speedtests", params={"limit": 25})
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            return {"error": "timeout", "message": "MySpeed did not respond in time"}
        except httpx.HTTPStatusError as e:
            return {
                "error": "http_error",
                "status": e.response.status_code,
                "message": str(e),
            }
        except httpx.HTTPError as e:
            return {"error": "connection_error", "message": str(e)}
        except ValueError:
            return {
                "error": "invalid_response",
                "message": "MySpeed returned a non-JSON response",
            }

        if not data:
            return {
                "summary": "No speed tests recorded",
                "latest": None,
                "history": [],
                "history_count": 0,
                "trend": "unknown",
                "_meta": build_meta("myspeed"),
            }

        # Process all tests
        history = [_process_test(t) for t in data]
        latest = history[0]

        # Compute trend from raw download values
        trend = _compute_trend(data)

        summary = (
            f"Download: {latest['download_mbps']} Mbps, "
            f"Upload: {latest['upload_mbps']} Mbps, "
            f"Ping: {latest['ping_ms']}ms ({trend})"
        )

        return {
            "summary": summary,
            "latest": latest,
            "history": history,
            "history_count": len(history),
            "trend": trend,
            # Point-in-time snapshot of the 25 most recent tests -- no fixed
            # duration window, so leave data_window unset rather than claim "24h".
            "_meta": build_meta("myspeed"),
        }
