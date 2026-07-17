"""Session-based authentication manager for homelab services.

Provides SessionAuthManager for services requiring login flows,
CSRF tokens, or multi-step authorization (Transmission, Synology, Backblaze).
"""

import asyncio
import time
from typing import Protocol

import httpx


class LoginResult:
    """Result of a login strategy execution.

    Carries auth credentials in whatever form the service requires:
    - headers: Auth headers (Transmission session ID, Backblaze bearer token)
    - params: Auth query params (Synology _sid)
    - base_url: Dynamic API URL (Backblaze)
    - expires_at: Unix timestamp for proactive refresh, None means no expiry
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        base_url: str | None = None,
        expires_at: float | None = None,
    ):
        self.headers = headers or {}
        self.params = params or {}
        self.base_url = base_url
        self.expires_at = expires_at


class LoginStrategy(Protocol):
    """Protocol for service-specific login implementations.

    Each service (Transmission, Synology, Backblaze) implements this protocol
    to handle its unique authentication flow.
    """

    async def login(self, client: httpx.AsyncClient) -> LoginResult: ...
    def is_auth_error(self, response: httpx.Response) -> bool: ...


class SessionAuthManager:
    """Wraps httpx.AsyncClient with automatic auth management.

    Features:
    - Lazy login: first request triggers authentication
    - Token caching: subsequent requests reuse credentials
    - Proactive refresh: refreshes before expiry to avoid failed round trips
    - Retry on auth failure: safety net if proactive refresh misses
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        strategy: LoginStrategy,
        refresh_margin_seconds: float = 300,
    ):
        self._client = client
        self._strategy = strategy
        self._refresh_margin = refresh_margin_seconds
        self._auth: LoginResult | None = None
        self._authenticated: bool = False
        self._lock = asyncio.Lock()

    @property
    def strategy(self) -> LoginStrategy:
        """The login strategy, for tools that need strategy-held state (e.g.
        Backblaze's account_id captured during login)."""
        return self._strategy

    async def ensure_auth(self) -> None:
        """Login if not authenticated or proactively refresh if near expiry.

        Guarded by a lock so concurrent callers (user tool calls and the
        background periodic_refresh share managers) don't run duplicate logins
        or interleave sessions for services that invalidate the prior session
        on a new login. The re-checks inside the lock make this a
        double-checked lock: a coroutine that waited on the lock sees the
        session the winner just established and skips its own login.
        """
        async with self._lock:
            now = time.time()
            if self._auth and self._auth.expires_at:
                if now >= self._auth.expires_at - self._refresh_margin:
                    self._auth = await self._strategy.login(self._client)
                    self._authenticated = True
                    return
            if not self._authenticated:
                self._auth = await self._strategy.login(self._client)
                self._authenticated = True

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make authenticated request with retry on auth failure."""
        await self.ensure_auth()
        # ensure_auth always sets _auth (or raised); narrow LoginResult | None so
        # the dereferences below are well-typed.
        assert self._auth is not None

        # Extract caller headers/params once. kwargs is drained here, so the
        # retry path below must reuse these locals rather than pop again (an
        # already-emptied kwargs would send only the auth values, dropping the
        # caller's query params/headers on every 401-retry).
        # `or {}`: callers (e.g. lib/http.service_request) may pass an explicit
        # None, which plain httpx clients accept — tolerate it the same way.
        caller_headers = kwargs.pop("headers", None) or {}
        caller_params = kwargs.pop("params", None) or {}

        # Build merged headers and params (caller values + auth values)
        merged_headers = {**caller_headers, **self._auth.headers}
        merged_params = {**caller_params, **self._auth.params}

        # Use dynamic base_url if set (Backblaze)
        url = f"{self._auth.base_url}{path}" if self._auth.base_url else path

        resp = await self._client.request(
            method, url, headers=merged_headers, params=merged_params, **kwargs
        )

        # Retry once on auth error
        if self._strategy.is_auth_error(resp):
            self._authenticated = False
            await self.ensure_auth()
            merged_headers = {**caller_headers, **self._auth.headers}
            merged_params = {**caller_params, **self._auth.params}
            url = f"{self._auth.base_url}{path}" if self._auth.base_url else path
            resp = await self._client.request(
                method, url, headers=merged_headers, params=merged_params, **kwargs
            )

        return resp

    async def get(self, path: str, **kwargs) -> httpx.Response:
        """Make authenticated GET request."""
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        """Make authenticated POST request."""
        return await self.request("POST", path, **kwargs)
