"""Tests for lib.gitea.paginate_repos."""

import pytest

from lib.gitea import paginate_repos


@pytest.mark.asyncio
async def test_stops_on_short_page():
    pages = {1: {"data": [{"i": n} for n in range(50)]}, 2: {"data": [{"i": 99}]}}

    async def fetch(params):
        return pages[params["page"]]

    repos = await paginate_repos(fetch)
    assert len(repos) == 51


@pytest.mark.asyncio
async def test_respects_max_pages():
    async def fetch(params):
        return {"data": [{"i": n} for n in range(50)]}  # always full -> never short

    repos = await paginate_repos(fetch, max_pages=3)
    assert len(repos) == 150


@pytest.mark.asyncio
async def test_propagates_error_dict():
    async def fetch(params):
        return {"error": "http_error", "status": 500}

    result = await paginate_repos(fetch)
    assert isinstance(result, dict)
    assert result["error"] == "http_error"


@pytest.mark.asyncio
async def test_none_page_stops():
    async def fetch(params):
        return None

    assert await paginate_repos(fetch) == []
