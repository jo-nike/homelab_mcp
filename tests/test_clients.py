"""Tests for lib/clients.py -- shared per-service client construction."""

from contextlib import AsyncExitStack

import httpx
import pytest

import config
from lib.auth import SessionAuthManager
from lib.clients import create_clients


@pytest.mark.asyncio
async def test_no_config_builds_only_web_fetch():
    """With every service constant nulled, only the unconditional web_fetch
    client is created."""
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack)
    assert set(clients) == {"web_fetch"}
    assert isinstance(clients["web_fetch"], httpx.AsyncClient)


@pytest.mark.asyncio
async def test_plain_client_built_when_configured(monkeypatch):
    monkeypatch.setattr(config, "PROMETHEUS_URL", "http://prom.test:9090")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack)
    assert isinstance(clients["prometheus"], httpx.AsyncClient)
    assert str(clients["prometheus"].base_url) == "http://prom.test:9090"


@pytest.mark.asyncio
async def test_partial_guard_skips_service(monkeypatch):
    """URL alone is not enough when the guard also requires credentials."""
    monkeypatch.setattr(config, "PROXMOX_URL", "https://pve.test:8006")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack)
    assert "proxmox" not in clients


@pytest.mark.asyncio
async def test_session_auth_services(monkeypatch):
    monkeypatch.setattr(config, "NPM_URL", "http://npm.test:81")
    monkeypatch.setattr(config, "NPM_EMAIL", "admin@test")
    monkeypatch.setattr(config, "NPM_PASSWORD", "pw")
    monkeypatch.setattr(config, "TECHNITIUM_URL", "http://dns.test:5380")
    monkeypatch.setattr(config, "TECHNITIUM_PASSWORD", "pw")
    monkeypatch.setattr(config, "WIREGUARD_URL", "http://wg.test:51821")
    monkeypatch.setattr(config, "WIREGUARD_PASSWORD", "pw")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack)
    for name in ("npm", "technitium", "wireguard"):
        assert isinstance(clients[name], SessionAuthManager), name


@pytest.mark.asyncio
async def test_only_filters_keys(monkeypatch):
    monkeypatch.setattr(config, "PROMETHEUS_URL", "http://prom.test:9090")
    monkeypatch.setattr(config, "NPM_URL", "http://npm.test:81")
    monkeypatch.setattr(config, "NPM_EMAIL", "admin@test")
    monkeypatch.setattr(config, "NPM_PASSWORD", "pw")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack, only={"npm"})
    assert set(clients) == {"npm"}


@pytest.mark.asyncio
async def test_only_does_not_create_unconfigured(monkeypatch):
    """`only` restricts attempts; it cannot conjure an unconfigured service."""
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack, only={"prometheus"})
    assert clients == {}


@pytest.mark.asyncio
async def test_myspeed_auth_is_optional(monkeypatch):
    monkeypatch.setattr(config, "MYSPEED_URL", "http://speed.test:5216")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack, only={"myspeed"})
    assert isinstance(clients["myspeed"], httpx.AsyncClient)

    monkeypatch.setattr(config, "MYSPEED_PASSWORD", "pw")
    async with AsyncExitStack() as stack:
        clients = await create_clients(stack, only={"myspeed"})
    assert isinstance(clients["myspeed"], SessionAuthManager)
