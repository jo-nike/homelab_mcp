"""Tests for lib.auth SessionAuthManager."""

import asyncio
import time

import httpx
import pytest

from lib.auth import LoginResult, SessionAuthManager


class FakeStrategy:
    """Mock login strategy that tracks call counts."""

    def __init__(
        self,
        login_result: LoginResult | None = None,
        auth_error_status: int | None = None,
    ):
        self._result = login_result or LoginResult(headers={"X-Token": "tok123"})
        self._auth_error_status = auth_error_status
        self.login_count = 0

    async def login(self, client: httpx.AsyncClient) -> LoginResult:
        self.login_count += 1
        return self._result

    def is_auth_error(self, response: httpx.Response) -> bool:
        if self._auth_error_status is None:
            return False
        return response.status_code == self._auth_error_status


def make_transport(responses: list[httpx.Response] | None = None):
    """Create a mock transport returning sequential responses."""
    idx = 0

    if responses is None:
        responses = [httpx.Response(200, json={"ok": True})]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        resp = responses[idx % len(responses)]
        idx += 1
        return resp

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_lazy_login_on_first_request():
    """SessionAuthManager calls login strategy on first request, not during init."""
    strategy = FakeStrategy()
    transport = make_transport()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        assert strategy.login_count == 0, "Login should not be called during init"
        await mgr.get("/test")
        assert strategy.login_count == 1, "Login should be called on first request"


@pytest.mark.asyncio
async def test_caches_auth_across_requests():
    """SessionAuthManager caches auth and reuses on subsequent requests (login called once for two requests)."""
    strategy = FakeStrategy()
    transport = make_transport([httpx.Response(200, json={"ok": True})] * 5)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        await mgr.get("/first")
        await mgr.get("/second")
        assert strategy.login_count == 1, (
            "Login should be called only once across multiple requests"
        )


@pytest.mark.asyncio
async def test_retry_on_auth_error():
    """SessionAuthManager retries with fresh login when strategy.is_auth_error returns True."""
    strategy = FakeStrategy(auth_error_status=409)
    responses = [
        httpx.Response(409, json={"error": "auth"}),  # First attempt fails
        httpx.Response(200, json={"ok": True}),  # Retry succeeds
    ]
    transport = make_transport(responses)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        resp = await mgr.get("/test")
        assert resp.status_code == 200, "Should return the retry response"
        assert strategy.login_count == 2, "Should login once initially, once on retry"


@pytest.mark.asyncio
async def test_retry_preserves_caller_params_and_headers():
    """On a 401-retry the caller's params/headers must survive into the second attempt.

    Regression for the auth-retry bug: kwargs was drained on the first attempt,
    so the retry re-popped from an empty kwargs and dropped the caller's query
    params/headers, sending only the auth values.
    """
    strategy = FakeStrategy(
        login_result=LoginResult(
            headers={"X-Token": "tok123"}, params={"token": "authtok"}
        ),
        auth_error_status=401,
    )
    requests_made = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        # First attempt fails auth, second succeeds
        status = 401 if len(requests_made) == 1 else 200
        return httpx.Response(status, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        resp = await mgr.get(
            "/api/resolve",
            params={"domain": "example.com"},
            headers={"X-Caller": "caller-value"},
        )

    assert resp.status_code == 200
    assert len(requests_made) == 2, "Should have made an initial attempt plus one retry"
    retry = requests_made[1]
    # Caller params survive alongside the auth param
    assert retry.url.params.get("domain") == "example.com"
    assert retry.url.params.get("token") == "authtok"
    # Caller headers survive alongside the auth header
    assert retry.headers.get("X-Caller") == "caller-value"
    assert retry.headers.get("X-Token") == "tok123"


@pytest.mark.asyncio
async def test_explicit_none_params_and_headers_tolerated():
    """request() must accept params=None/headers=None like plain httpx does.

    Regression for the prod NPM crash: lib/http.service_request forwards
    params=None for bare GETs; merging with {**None} raised TypeError
    ("'NoneType' object is not a mapping") for every session-auth GET.
    """
    strategy = FakeStrategy(
        login_result=LoginResult(
            headers={"X-Token": "tok123"}, params={"token": "authtok"}
        )
    )
    requests_made = []

    async def handler(request):
        requests_made.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        resp = await mgr.request("GET", "/hosts", params=None, headers=None)

    assert resp.status_code == 200
    # Auth values still applied despite the None caller values.
    assert requests_made[0].url.params.get("token") == "authtok"
    assert requests_made[0].headers.get("X-Token") == "tok123"


@pytest.mark.asyncio
async def test_concurrent_requests_login_once():
    """Concurrent first requests must trigger only a single login.

    Regression for the unlocked check-then-login race: without a lock two
    coroutines both see _authenticated=False and both run strategy.login.
    """
    login_calls = 0

    class SlowStrategy:
        async def login(self, client):
            nonlocal login_calls
            login_calls += 1
            # Yield control so a second coroutine can race into the check.
            await asyncio.sleep(0)
            return LoginResult(headers={"X-Token": "tok"})

        def is_auth_error(self, response):
            return False

    transport = make_transport([httpx.Response(200, json={"ok": True})] * 5)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, SlowStrategy())
        await asyncio.gather(mgr.get("/a"), mgr.get("/b"), mgr.get("/c"))

    assert login_calls == 1, "Concurrent first requests should log in exactly once"


@pytest.mark.asyncio
async def test_proactive_refresh_before_expiry():
    """SessionAuthManager proactively refreshes when token is within refresh_margin_seconds of expiry."""
    # Create an already-near-expiry token (expires in 10 seconds, margin is 300)
    near_expiry_result = LoginResult(
        headers={"X-Token": "old"},
        expires_at=time.time() + 10,  # Expires in 10s, margin is 300s
    )
    fresh_result = LoginResult(
        headers={"X-Token": "fresh"},
        expires_at=time.time() + 86400,
    )

    call_count = 0

    class RefreshStrategy:
        async def login(self, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return near_expiry_result
            return fresh_result

        def is_auth_error(self, response):
            return False

    strategy = RefreshStrategy()
    transport = make_transport([httpx.Response(200, json={"ok": True})] * 5)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy, refresh_margin_seconds=300)
        # First request triggers initial login (near-expiry token)
        await mgr.get("/first")
        assert call_count == 1

        # Second request should trigger proactive refresh (10s < 300s margin)
        await mgr.get("/second")
        assert call_count == 2, "Should proactively refresh near-expiry token"


def test_login_result_fields():
    """LoginResult carries headers, params, base_url, and expires_at fields."""
    result = LoginResult(
        headers={"Authorization": "Bearer abc"},
        params={"_sid": "session123"},
        base_url="https://api.example.com",
        expires_at=1700000000.0,
    )
    assert result.headers == {"Authorization": "Bearer abc"}
    assert result.params == {"_sid": "session123"}
    assert result.base_url == "https://api.example.com"
    assert result.expires_at == 1700000000.0


def test_login_result_defaults():
    """LoginResult has sensible defaults for all fields."""
    result = LoginResult()
    assert result.headers == {}
    assert result.params == {}
    assert result.base_url is None
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_get_and_post_delegate_to_request():
    """SessionAuthManager.get() and .post() delegate to .request() correctly."""
    strategy = FakeStrategy()
    requests_made = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append((request.method, str(request.url)))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mgr = SessionAuthManager(client, strategy)
        await mgr.get("/api/get-endpoint")
        await mgr.post("/api/post-endpoint")

    assert len(requests_made) == 2
    assert requests_made[0][0] == "GET"
    assert "/api/get-endpoint" in requests_made[0][1]
    assert requests_made[1][0] == "POST"
    assert "/api/post-endpoint" in requests_made[1][1]
