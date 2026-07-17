"""Tests for lib.portainer shared container-lookup helpers."""

from unittest.mock import AsyncMock

import httpx
import pytest

import config
from lib.portainer import container_name, find_container_matches, is_critical


def _resp(json_data, status_code=200):
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "http://portainer/test"),
    )


def test_is_critical(monkeypatch):
    monkeypatch.setitem(config.TOPOLOGY, "critical_containers", ["Traefik", "npm"])
    assert is_critical("traefik") is True
    assert is_critical("NPM") is True
    assert is_critical("grafana") is False


def test_container_name_strips_slash():
    assert container_name({"Names": ["/grafana"]}) == "grafana"
    assert container_name({}) == "unknown"


@pytest.mark.asyncio
async def test_find_container_matches_across_endpoints():
    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[
            _resp([{"Id": 1, "Name": "beast", "Status": 1}]),
            _resp([{"Id": "abc", "Names": ["/grafana"], "State": "running"}]),
        ]
    )
    matches = await find_container_matches(client, "grafana")
    assert len(matches) == 1
    assert matches[0]["ep_name"] == "beast"
    assert matches[0]["container"]["Id"] == "abc"


@pytest.mark.asyncio
async def test_find_container_matches_skips_down_endpoint():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[_resp([{"Id": 2, "Name": "srv", "Status": 2}])])
    matches = await find_container_matches(client, "grafana")
    assert matches == []


@pytest.mark.asyncio
async def test_find_container_matches_endpoint_error_returns_dict():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    result = await find_container_matches(client, "grafana")
    assert isinstance(result, dict)
    assert result["error"] == "timeout"
