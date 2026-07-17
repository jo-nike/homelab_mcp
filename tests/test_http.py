"""Tests for the shared lib.http.service_request helper."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from lib.http import service_request


def make_ctx(service_key, client):
    ctx = MagicMock()
    ctx.lifespan_context = {service_key: client}
    return ctx


def make_response(status_code=200, json_data=None, url="http://svc/api"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data)
    request = httpx.Request("GET", url)
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"Client error '{status_code}' for url '{url}'",
                request=request,
                response=httpx.Response(status_code, request=request),
            )
        )
    else:
        resp.raise_for_status = MagicMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_get_returns_parsed_json():
    client = AsyncMock()
    client.get = AsyncMock(return_value=make_response(json_data={"ok": True}))
    ctx = make_ctx("sonarr", client)

    result = await service_request(ctx, "sonarr", "/api/v3/status")

    assert result == {"ok": True}
    client.get.assert_awaited_once_with("/api/v3/status", params=None)


@pytest.mark.asyncio
async def test_post_passes_json_body():
    client = AsyncMock()
    client.post = AsyncMock(return_value=make_response(json_data={"id": 1}))
    ctx = make_ctx("vikunja", client)

    result = await service_request(
        ctx, "vikunja", "/api/v1/tasks", method="POST", json={"title": "x"}
    )

    assert result == {"id": 1}
    client.post.assert_awaited_once_with(
        "/api/v1/tasks", params=None, json={"title": "x"}
    )


@pytest.mark.asyncio
async def test_timeout_returns_error_dict():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_ctx("sonarr", client)

    result = await service_request(ctx, "sonarr", "/x", display_name="Sonarr")

    assert result == {"error": "timeout", "message": "Sonarr did not respond in time"}


@pytest.mark.asyncio
async def test_display_name_defaults_to_capitalized_key():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_ctx("prometheus", client)

    result = await service_request(ctx, "prometheus", "/x")

    assert result["message"] == "Prometheus did not respond in time"


@pytest.mark.asyncio
async def test_http_status_error_carries_status_and_redacts_query():
    url = "http://svc/api?apikey=SUPER_SECRET"
    client = AsyncMock()
    client.get = AsyncMock(return_value=make_response(status_code=401, url=url))
    ctx = make_ctx("tautulli", client)

    result = await service_request(ctx, "tautulli", "/api")

    assert result["error"] == "http_error"
    assert result["status"] == 401
    assert "SUPER_SECRET" not in result["message"]


@pytest.mark.asyncio
async def test_connection_error_preserves_reason():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
    ctx = make_ctx("gitea", client)

    result = await service_request(ctx, "gitea", "/x")

    assert result["error"] == "connection_error"
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_non_json_200_returns_invalid_response():
    """A 200 whose body is not JSON (e.g. an HTML error page from a reverse
    proxy) must become an error dict, not raise json.JSONDecodeError."""
    resp = make_response(status_code=200)
    resp.json = MagicMock(side_effect=ValueError("Expecting value"))
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    ctx = make_ctx("searxng", client)

    result = await service_request(ctx, "searxng", "/search", display_name="SearXNG")

    assert result == {
        "error": "invalid_response",
        "message": "SearXNG returned a non-JSON response",
    }
