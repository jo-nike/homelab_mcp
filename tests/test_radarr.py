"""Tests for Radarr movie tools."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import radarr

# --- Helpers ---


# --- Mock data ---

# Dates are relative to today so they land inside the queried window; the
# calendar can return a movie for any of its three dates.
_TODAY = date.today()


def _d(offset_days):
    return (_TODAY + timedelta(days=offset_days)).isoformat()


MOCK_CALENDAR = [
    {
        # Digital release is what puts it in the window; the physical release is
        # months out. The tool must report the in-window digital date, not the
        # far-future physical one (item 24).
        "title": "The Matrix Reloaded",
        "year": 2026,
        "physicalRelease": _d(200),
        "digitalRelease": _d(5),
        "inCinemas": _d(-30),
        "monitored": True,
        "hasFile": False,
    },
    {
        "title": "Inception 2",
        "year": 2026,
        "physicalRelease": None,
        "digitalRelease": _d(10),
        "inCinemas": _d(-60),
        "monitored": True,
        "hasFile": False,
    },
    {
        "title": "Old Movie",
        "year": 2025,
        "physicalRelease": None,
        "digitalRelease": None,
        "inCinemas": _d(-200),
        "monitored": False,
        "hasFile": True,
    },
]

MOCK_QUEUE = {
    "records": [
        {
            "movie": {"title": "The Matrix Reloaded"},
            "quality": {"quality": {"name": "Remux-1080p"}},
            "status": "downloading",
            "size": 4000,
            "sizeleft": 1000,
            "timeleft": "01:30:00",
        },
        {
            "movie": {"title": "Blade Runner 2099"},
            "quality": {"quality": {"name": "HDTV-720p"}},
            "status": "queued",
            "size": 1500,
            "sizeleft": 1500,
            "timeleft": None,
        },
    ]
}

MOCK_WANTED = {
    "records": [
        {"title": "Dune: Part Three", "year": 2026, "isAvailable": False},
        {"title": "The Lost Film", "year": 2019, "isAvailable": True},
    ]
}


# --- Tests ---


@pytest.mark.asyncio
async def test_radarr_combined_status():
    """Test combined upcoming + queue + wanted response with mock data."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_CALENDAR),
            make_response(MOCK_QUEUE),
            make_response(MOCK_WANTED),
        ]
    )
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)

    # "Old Movie" is already on disk (hasFile), so it drops out — matching Sonarr.
    assert result["upcoming_count"] == 2
    assert [m["title"] for m in result["upcoming"]] == [
        "The Matrix Reloaded",
        "Inception 2",
    ]
    # In-window digital date wins over the far-future physical one.
    assert result["upcoming"][0]["release_type"] == "digital"
    assert result["upcoming"][0]["release_date"] == _d(5)
    assert result["upcoming"][1]["release_type"] == "digital"
    assert result["upcoming"][1]["release_date"] == _d(10)

    # _meta data_window reflects the days argument, not a hardcoded 90d.
    assert result["_meta"]["data_window"] == "30d"

    # Queue
    assert result["queue_count"] == 2
    assert result["queue"][0]["title"] == "The Matrix Reloaded"
    assert result["queue"][0]["quality"] == "Remux-1080p"
    assert result["queue"][0]["progress_percent"] == 75
    assert result["queue"][1]["progress_percent"] == 0

    # Wanted/missing — "Dune: Part Three" isn't released yet, so it isn't wantable.
    assert result["wanted_count"] == 1
    assert result["wanted"][0]["title"] == "The Lost Film"
    assert result["wanted"][0]["year"] == 2019


@pytest.mark.asyncio
async def test_radarr_empty_calendar_and_queue():
    """Test empty calendar and empty queue."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([]),
            make_response({"records": []}),
            make_response({"records": []}),
        ]
    )
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)

    assert result["upcoming_count"] == 0
    assert result["upcoming"] == []
    assert result["queue_count"] == 0
    assert result["queue"] == []
    assert result["wanted_count"] == 0
    assert result["wanted"] == []


@pytest.mark.asyncio
async def test_radarr_upcoming_sorted_soonest_first():
    """Upcoming is sorted by release date, with undated movies last."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                [
                    {
                        "title": "Later",
                        "physicalRelease": "2026-09-01",
                        "hasFile": False,
                    },
                    {"title": "Undated", "hasFile": False},
                    {
                        "title": "Sooner",
                        "digitalRelease": "2026-04-02",
                        "hasFile": False,
                    },
                ]
            ),
            make_response({"records": []}),
            make_response({"records": []}),
        ]
    )
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)

    assert [m["title"] for m in result["upcoming"]] == ["Sooner", "Later", "Undated"]
    assert result["upcoming"][2]["release_date"] is None


@pytest.mark.asyncio
async def test_radarr_queue_progress_zero_size():
    """Test queue progress with size=0 (no division error)."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([]),
            make_response(
                {
                    "records": [
                        {
                            "movie": {"title": "Zero Size Movie"},
                            "quality": {"quality": {"name": "HD"}},
                            "status": "downloading",
                            "size": 0,
                            "sizeleft": 0,
                            "timeleft": None,
                        },
                    ]
                }
            ),
            make_response({"records": []}),
        ]
    )
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)

    assert result["queue"][0]["progress_percent"] == 0


@pytest.mark.asyncio
async def test_radarr_timeout_returns_error_dict():
    """A timed-out upstream call surfaces {'error': 'timeout'}."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)
    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_radarr_endpoint_error_propagates():
    """A 500 on one of the three gathered endpoints propagates the error dict."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_CALENDAR),
            make_response(MOCK_QUEUE),
            make_response({"message": "boom"}, status_code=500),
        ]
    )
    ctx = make_mock_ctx(radarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)

    fn = get_tool_fn(app, "get_radarr_status")
    result = await fn(ctx, days=30)
    assert result["error"] == "http_error"
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_radarr_conditional_registration():
    """Test that tools are not registered when RADARR_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "RADARR_URL", None):
        radarr.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_radarr_registers_when_configured():
    """Test that tools are registered when RADARR_URL is set."""
    app = FastMCP("test")
    with (
        patch.object(config, "RADARR_URL", "http://radarr:7878"),
        patch.object(config, "RADARR_API_KEY", "fake-api-key"),
    ):
        radarr.register(app)
    assert count_tools(app) == 1
