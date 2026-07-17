"""Tests for lib.gather.safe_gather."""

import pytest

from lib.gather import error_dict, safe_gather


async def _ok(v):
    return v


async def _boom():
    raise RuntimeError("nope")


@pytest.mark.asyncio
async def test_default_on_error_yields_empty_list():
    results = await safe_gather(_ok([1, 2]), _boom(), _ok("x"))
    assert results == [[1, 2], [], "x"]


@pytest.mark.asyncio
async def test_error_dict_on_error():
    results = await safe_gather(_ok(1), _boom(), on_error=error_dict)
    assert results[0] == 1
    assert results[1]["error"] == "unreachable"
    assert "nope" in results[1]["message"]
