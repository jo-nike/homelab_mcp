"""Tests for Sonarr TV series tools."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import sonarr

# --- Helpers ---


# --- Mock data ---

MOCK_CALENDAR = [
    {
        "title": "Episode Title 1",
        "seasonNumber": 3,
        "episodeNumber": 5,
        "airDateUtc": "2026-04-05T01:00:00Z",
        "hasFile": False,
        "monitored": True,
        "series": {"title": "Breaking Bad"},
    },
    {
        "title": "Episode Title 2",
        "seasonNumber": 1,
        "episodeNumber": 1,
        "airDateUtc": "2026-04-06T01:00:00Z",
        "hasFile": True,
        "monitored": True,
        "series": {"title": "The Wire"},
    },
    {
        "title": "Episode Title 3",
        "seasonNumber": 2,
        "episodeNumber": 10,
        "airDateUtc": "2026-04-07T01:00:00Z",
        "hasFile": False,
        "monitored": False,
        "series": {"title": "Better Call Saul"},
    },
]

MOCK_QUEUE = {
    "records": [
        {
            "series": {"title": "Breaking Bad"},
            "episode": {"title": "Ozymandias"},
            "quality": {"quality": {"name": "HDTV-1080p"}},
            "status": "downloading",
            "size": 1000,
            "sizeleft": 250,
            "timeleft": "00:15:00",
        },
        {
            "series": {"title": "The Sopranos"},
            "episode": {"title": "Pine Barrens"},
            "quality": {"quality": {"name": "Bluray-720p"}},
            "status": "queued",
            "size": 2000,
            "sizeleft": 2000,
            "timeleft": None,
        },
    ]
}

MOCK_WANTED = {
    "records": [
        {
            "seriesId": 1,
            "seasonNumber": 2,
            "episodeNumber": 4,
            "title": "Missing Ep 1",
            "airDateUtc": "2026-01-01T01:00:00Z",
            "series": {"title": "Breaking Bad"},
        },
        {
            "seriesId": 1,
            "seasonNumber": 2,
            "episodeNumber": 5,
            "title": "Missing Ep 2",
            "airDateUtc": "2026-01-08T01:00:00Z",
            "series": {"title": "Breaking Bad"},
        },
        {
            "seriesId": 2,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "title": "Pilot",
            "airDateUtc": "2026-02-01T01:00:00Z",
            "series": {"title": "The Wire"},
        },
    ]
}


# --- Tests ---


@pytest.mark.asyncio
async def test_sonarr_combined_status():
    """Test combined upcoming + queue response with mock data."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_CALENDAR),
            make_response(MOCK_QUEUE),
            make_response(MOCK_WANTED),
        ]
    )
    ctx = make_mock_ctx(sonarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)

    fn = get_tool_fn(app, "get_sonarr_status")
    result = await fn(ctx, days=14)

    # _meta data_window reflects the days argument, not a hardcoded 7d (item 25).
    assert result["_meta"]["data_window"] == "14d"

    # Only 2 upcoming (hasFile=True is filtered out)
    assert result["upcoming_count"] == 2
    assert result["upcoming"][0]["series"] == "Breaking Bad"
    assert result["upcoming"][0]["season"] == 3
    assert result["upcoming"][0]["episode"] == 5
    assert result["upcoming"][1]["series"] == "Better Call Saul"

    # Queue
    assert result["queue_count"] == 2
    assert result["queue"][0]["series"] == "Breaking Bad"
    assert result["queue"][0]["episode_title"] == "Ozymandias"
    assert result["queue"][0]["quality"] == "HDTV-1080p"
    assert result["queue"][0]["progress_percent"] == 75
    assert result["queue"][1]["progress_percent"] == 0


@pytest.mark.asyncio
async def test_sonarr_empty_calendar_and_queue():
    """Test empty calendar and empty queue."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([]),
            make_response({"records": []}),
            make_response({"records": []}),
        ]
    )
    ctx = make_mock_ctx(sonarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)

    fn = get_tool_fn(app, "get_sonarr_status")
    result = await fn(ctx, days=7)

    assert result["upcoming_count"] == 0
    assert result["upcoming"] == []
    assert result["queue_count"] == 0
    assert result["queue"] == []
    assert result["wanted_count"] == 0
    assert result["wanted"] == []


@pytest.mark.asyncio
async def test_sonarr_queue_progress_calculation():
    """Test queue progress calculation including zero-size edge case."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([]),
            make_response(
                {
                    "records": [
                        {
                            "series": {"title": "Show A"},
                            "episode": {"title": "Ep"},
                            "quality": {"quality": {"name": "HD"}},
                            "status": "downloading",
                            "size": 100,
                            "sizeleft": 25,
                            "timeleft": "00:05:00",
                        },
                        {
                            "series": {"title": "Show B"},
                            "episode": {"title": "Ep"},
                            "quality": {"quality": {"name": "SD"}},
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
    ctx = make_mock_ctx(sonarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)

    fn = get_tool_fn(app, "get_sonarr_status")
    result = await fn(ctx, days=7)

    assert result["queue"][0]["progress_percent"] == 75
    # size=0 should not cause division error, returns 0
    assert result["queue"][1]["progress_percent"] == 0


@pytest.mark.asyncio
async def test_sonarr_wanted_missing():
    """Test wanted/missing list is parsed with load-bearing series field."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response([]),
            make_response({"records": []}),
            make_response(MOCK_WANTED),
        ]
    )
    ctx = make_mock_ctx(sonarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)

    fn = get_tool_fn(app, "get_sonarr_status")
    result = await fn(ctx, days=7)

    assert result["wanted_count"] == 3
    assert result["wanted"][0]["series"] == "Breaking Bad"
    assert result["wanted"][0]["season"] == 2
    assert result["wanted"][0]["episode"] == 4
    assert result["wanted"][0]["episode_title"] == "Missing Ep 1"
    assert result["wanted"][0]["air_date"] == "2026-01-01T01:00:00Z"
    assert result["wanted"][2]["series"] == "The Wire"


@pytest.mark.asyncio
async def test_sonarr_wanted_error_propagates():
    """Test that an error from the wanted endpoint is returned."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[
            make_response(MOCK_CALENDAR),
            make_response(MOCK_QUEUE),
            make_response({"error": "boom"}, status_code=500),
        ]
    )
    ctx = make_mock_ctx(sonarr=mock_client)

    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)

    fn = get_tool_fn(app, "get_sonarr_status")
    result = await fn(ctx, days=7)

    assert result["error"] == "http_error"


@pytest.mark.asyncio
async def test_sonarr_conditional_registration():
    """Test that tools are not registered when SONARR_URL is not set."""
    app = FastMCP("test")
    with patch.object(config, "SONARR_URL", None):
        sonarr.register(app)
    assert count_tools(app) == 0


@pytest.mark.asyncio
async def test_sonarr_registers_when_configured():
    """Test that tools are registered when SONARR_URL is set."""
    app = FastMCP("test")
    with (
        patch.object(config, "SONARR_URL", "http://sonarr:8989"),
        patch.object(config, "SONARR_API_KEY", "fake-api-key"),
    ):
        sonarr.register(app)
    assert count_tools(app) == 1
