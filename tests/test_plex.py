"""Tests for Plex media tools."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

import config
from tests.conftest import count_tools, get_tool_fn, make_mock_ctx, make_response
from tools import plex

# --- Helpers ---


# --- Fixtures ---


@pytest.fixture
def mcp_app():
    """Create a real FastMCP instance with Plex tools registered."""
    app = FastMCP("test")
    with (
        patch.object(config, "PLEX_URL", "http://fake:32400"),
        patch.object(config, "PLEX_TOKEN", "fake-token"),
    ):
        plex.register(app)
    return app


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock(spec=httpx.AsyncClient)


# --- Mock data ---


MOCK_SESSION_MOVIE = {
    "title": "Inception",
    "type": "movie",
    "viewOffset": 3600000,
    "duration": 9000000,
    "User": {"title": "Jon"},
    "Player": {"device": "Apple TV", "platform": "tvOS", "state": "playing"},
    "Media": [
        {"videoResolution": "1080", "audioCodec": "aac", "Part": [{"Stream": []}]}
    ],
    "Session": {"bandwidth": 20000},
}

MOCK_SESSION_TV = {
    "title": "The Rains of Castamere",
    "grandparentTitle": "Game of Thrones",
    "type": "episode",
    "viewOffset": 1800000,
    "duration": 3600000,
    "User": {"title": "Alice"},
    "Player": {"device": "Roku", "platform": "Roku", "state": "paused"},
    "Media": [
        {
            "videoResolution": "4k",
            "audioCodec": "dts",
            "Part": [
                {
                    "Stream": [
                        {"streamType": 1, "displayTitle": "English (AAC)"},
                        {
                            "streamType": 3,
                            "displayTitle": "English (SRT)",
                            "language": "English",
                        },
                    ]
                }
            ],
        }
    ],
    "Session": {"bandwidth": 40000},
    "TranscodeSession": {"progress": 85.0, "videoDecision": "transcode"},
}

MOCK_RECENTLY_ADDED_MOVIE = {
    "ratingKey": "1001",
    "title": "Dune: Part Two",
    "type": "movie",
    "year": 2024,
    "addedAt": 1712000200,
    "librarySectionTitle": "Movies",
    "summary": "A long summary about Dune Part Two that should be truncated if necessary.",
    "rating": 8.5,
    "thumb": "/library/metadata/1001/thumb/1712000200",
}

MOCK_RECENTLY_ADDED_SEASON = {
    "ratingKey": "2001",
    "title": "Season 2",
    "grandparentTitle": None,
    "type": "season",
    "addedAt": 1712000100,
    "librarySectionTitle": "TV Shows",
    "summary": "",
}

MOCK_EPISODE = {
    "ratingKey": "3001",
    "title": "The Bell Jar",
    "grandparentTitle": "Severance",
    "type": "episode",
    "year": 2025,
    "addedAt": 1712000300,
    "librarySectionTitle": "TV Shows",
    "summary": "Mark discovers a new floor.",
    "audienceRating": 9.2,
    "thumb": "/library/metadata/3001/thumb/1712000300",
}


# --- Test: Conditional Registration ---


def test_register_skips_when_no_url():
    """When PLEX_URL is empty/None, register() adds no tools."""
    app = FastMCP("test-skip")
    with patch.object(config, "PLEX_URL", ""):
        plex.register(app)
    assert count_tools(app) == 0


def test_register_skips_when_url_none():
    """When PLEX_URL is None, register() adds no tools."""
    app = FastMCP("test-skip-none")
    with patch.object(config, "PLEX_URL", None):
        plex.register(app)
    assert count_tools(app) == 0


def test_register_adds_tools_when_url_set():
    """When PLEX_URL is set, register() adds 3 tools."""
    app = FastMCP("test-add")
    with (
        patch.object(config, "PLEX_URL", "http://fake:32400"),
        patch.object(config, "PLEX_TOKEN", "fake-token"),
    ):
        plex.register(app)
    assert count_tools(app) == 3


# --- Test: get_plex_sessions ---


@pytest.mark.asyncio
async def test_get_plex_sessions_active(mcp_app, mock_client):
    """Returns active streams with full session detail."""
    ctx = make_mock_ctx(plex=mock_client)

    mock_client.get = AsyncMock(
        return_value=make_response(
            {
                "MediaContainer": {
                    "size": 2,
                    "Metadata": [MOCK_SESSION_MOVIE, MOCK_SESSION_TV],
                },
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_sessions")
    result = await tool_fn(ctx=ctx)

    assert result["stream_count"] == 2
    assert len(result["active_streams"]) == 2

    # Movie session
    movie = result["active_streams"][0]
    assert movie["title"] == "Inception"
    assert movie["episode_title"] is None
    assert movie["user"] == "Jon"
    assert movie["progress_percent"] == 40  # 3600000/9000000 * 100
    assert movie["player_device"] == "Apple TV"
    assert movie["player_platform"] == "tvOS"
    assert movie["player_state"] == "playing"
    assert movie["stream_type"] == "direct play"
    assert movie["video_resolution"] == "1080"
    assert movie["audio_codec"] == "aac"
    assert movie["bandwidth_kbps"] == 20000
    assert movie["media_type"] == "movie"

    # TV session
    tv = result["active_streams"][1]
    assert tv["title"] == "Game of Thrones"
    assert tv["episode_title"] == "The Rains of Castamere"
    assert tv["user"] == "Alice"
    assert tv["progress_percent"] == 50  # 1800000/3600000 * 100
    assert tv["stream_type"] == "transcode"
    assert tv["video_resolution"] == "4k"
    assert tv["transcode_progress"] == 85.0
    assert tv["transcode_decision"] == "transcode"
    assert tv["subtitle_stream"] == "English (SRT)"


@pytest.mark.asyncio
async def test_get_plex_sessions_durationless(mcp_app, mock_client):
    """Regression (item 22): a session without duration (Live TV/photo) reports
    progress 0, not an absurd viewOffset*100."""
    ctx = make_mock_ctx(plex=mock_client)
    live = {
        "title": "News",
        "type": "clip",
        "viewOffset": 1450000,
        "User": {"title": "Jon"},
        "Player": {},
    }
    mock_client.get = AsyncMock(
        return_value=make_response({"MediaContainer": {"size": 1, "Metadata": [live]}})
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_sessions")
    result = await tool_fn(ctx=ctx)

    assert result["active_streams"][0]["progress_percent"] == 0


@pytest.mark.asyncio
async def test_get_plex_sessions_empty(mcp_app, mock_client):
    """Returns empty list when no active streams."""
    ctx = make_mock_ctx(plex=mock_client)

    mock_client.get = AsyncMock(
        return_value=make_response(
            {
                "MediaContainer": {"size": 0},
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_sessions")
    result = await tool_fn(ctx=ctx)

    assert result["stream_count"] == 0
    assert result["active_streams"] == []


# get_plex_sessions timeout is covered by test_get_helper_timeout below.


# --- Test: get_plex_recently_added ---


@pytest.mark.asyncio
async def test_get_plex_recently_added_merge(mcp_app, mock_client):
    """Merges global recently added with per-section episodes, sorted by addedAt."""
    ctx = make_mock_ctx(plex=mock_client)

    # Call sequence: global recentlyAdded, sections, then episode fetch per TV section
    mock_client.get = AsyncMock(
        side_effect=[
            # Global recently added
            make_response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            MOCK_RECENTLY_ADDED_MOVIE,
                            MOCK_RECENTLY_ADDED_SEASON,
                        ],
                    },
                }
            ),
            # Library sections
            make_response(
                {
                    "MediaContainer": {
                        "Directory": [
                            {"key": "1", "type": "movie", "title": "Movies"},
                            {"key": "2", "type": "show", "title": "TV Shows"},
                        ],
                    },
                }
            ),
            # Episodes from TV section 2
            make_response(
                {
                    "MediaContainer": {
                        "Metadata": [MOCK_EPISODE],
                    },
                }
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_recently_added")
    with (
        patch.object(config, "PLEX_URL", "http://fake:32400"),
        patch.object(config, "PLEX_TOKEN", "fake-token"),
    ):
        result = await tool_fn(ctx=ctx, limit=20)

    assert result["count"] == 3
    items = result["recently_added"]

    # Sorted by addedAt desc: episode (300), movie (200), season (100)
    assert items[0]["title"] == "Severance - The Bell Jar"
    assert items[0]["type"] == "episode"
    assert items[0]["rating"] == 9.2
    assert items[0]["thumb_url"] is not None
    assert "X-Plex-Token" in items[0]["thumb_url"]

    assert items[1]["title"] == "Dune: Part Two"
    assert items[1]["type"] == "movie"
    assert items[1]["year"] == 2024

    assert items[2]["type"] == "season"


@pytest.mark.asyncio
async def test_get_plex_recently_added_global_error_propagates(mcp_app, mock_client):
    """Regression (WP5): a failed global fetch must return the error dict, not
    an empty high-confidence result."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

    tool_fn = get_tool_fn(mcp_app, "get_plex_recently_added")
    with (
        patch.object(config, "PLEX_URL", "http://fake:32400"),
        patch.object(config, "PLEX_TOKEN", "fake-token"),
    ):
        result = await tool_fn(ctx=ctx, limit=20)

    assert result["error"] == "connection_error"


@pytest.mark.asyncio
async def test_get_plex_recently_added_section_error_degrades_confidence(
    mcp_app, mock_client
):
    """Regression (WP5): if a section sub-query fails but the global feed
    succeeds, confidence drops to medium instead of staying high."""
    ctx = make_mock_ctx(plex=mock_client)

    async def fake_get(path, params=None, headers=None):
        if path == "/library/recentlyAdded":
            return make_response(
                {"MediaContainer": {"Metadata": [MOCK_RECENTLY_ADDED_MOVIE]}}
            )
        if path == "/library/sections":
            raise httpx.ConnectError("sections down")
        return make_response({"MediaContainer": {"Metadata": []}})

    mock_client.get = AsyncMock(side_effect=fake_get)

    tool_fn = get_tool_fn(mcp_app, "get_plex_recently_added")
    with (
        patch.object(config, "PLEX_URL", "http://fake:32400"),
        patch.object(config, "PLEX_TOKEN", "fake-token"),
    ):
        result = await tool_fn(ctx=ctx, limit=20)

    assert result["_meta"]["confidence"] == "medium"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_get_plex_recently_added_limit(mcp_app, mock_client):
    """Respects the limit parameter."""
    ctx = make_mock_ctx(plex=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            MOCK_RECENTLY_ADDED_MOVIE,
                            MOCK_RECENTLY_ADDED_SEASON,
                        ],
                    },
                }
            ),
            make_response(
                {
                    "MediaContainer": {
                        "Directory": [
                            {"key": "2", "type": "show", "title": "TV Shows"},
                        ],
                    },
                }
            ),
            make_response(
                {
                    "MediaContainer": {
                        "Metadata": [MOCK_EPISODE],
                    },
                }
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_recently_added")
    result = await tool_fn(ctx=ctx, limit=2)

    assert result["count"] == 2


@pytest.mark.asyncio
async def test_get_plex_recently_added_no_tv_sections(mcp_app, mock_client):
    """Works correctly when there are no TV show sections."""
    ctx = make_mock_ctx(plex=mock_client)

    mock_client.get = AsyncMock(
        side_effect=[
            make_response(
                {
                    "MediaContainer": {
                        "Metadata": [MOCK_RECENTLY_ADDED_MOVIE],
                    },
                }
            ),
            make_response(
                {
                    "MediaContainer": {
                        "Directory": [
                            {"key": "1", "type": "movie", "title": "Movies"},
                        ],
                    },
                }
            ),
        ]
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_recently_added")
    result = await tool_fn(ctx=ctx, limit=20)

    assert result["count"] == 1
    assert result["recently_added"][0]["title"] == "Dune: Part Two"


# --- Test: get_plex_library_stats ---


MOCK_STATS_SECTIONS = {
    "MediaContainer": {
        "Directory": [
            {"key": "1", "type": "movie", "title": "Movies"},
            {"key": "2", "type": "show", "title": "TV Shows"},
            {"key": "3", "type": "artist", "title": "Music"},
        ],
    },
}


def make_path_dispatch(mapping):
    """Return a side_effect function that dispatches responses by request path."""

    def _dispatch(path, params=None, headers=None):
        if path in mapping:
            return mapping[path]
        raise AssertionError(f"unexpected path: {path}")

    return _dispatch


@pytest.mark.asyncio
async def test_get_plex_library_stats_counts(mcp_app, mock_client):
    """Sums totalSize per section into movies/shows/episodes counts."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(
        side_effect=make_path_dispatch(
            {
                "/library/sections": make_response(MOCK_STATS_SECTIONS),
                "/library/sections/1/all": make_response(
                    {"MediaContainer": {"size": 0, "totalSize": 500}}
                ),
                "/library/sections/2/all": make_response(
                    {"MediaContainer": {"size": 0, "totalSize": 80}}
                ),
                "/library/sections/2/allLeaves": make_response(
                    {"MediaContainer": {"size": 0, "totalSize": 3000}}
                ),
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_library_stats")
    result = await tool_fn(ctx=ctx)

    assert result["movies"] == 500
    assert result["shows"] == 80
    assert result["episodes"] == 3000
    assert result["_meta"]["confidence"] == "high"
    assert "_confidence" not in result

    # Counts must ride as query params: allLeaves ignores the
    # X-Plex-Container-Size *header* and would ship every episode record.
    leaf_calls = [
        c
        for c in mock_client.get.call_args_list
        if c.args and c.args[0].endswith("/allLeaves")
    ]
    assert leaf_calls
    for c in leaf_calls:
        params = c.kwargs.get("params") or (c.args[1] if len(c.args) > 1 else None)
        assert params and params.get("X-Plex-Container-Size") == "0"


@pytest.mark.asyncio
async def test_get_plex_library_stats_sums_multiple_sections(mcp_app, mock_client):
    """Counts across multiple movie/show sections are summed."""
    ctx = make_mock_ctx(plex=mock_client)
    sections = {
        "MediaContainer": {
            "Directory": [
                {"key": "1", "type": "movie", "title": "Movies"},
                {"key": "4", "type": "movie", "title": "Kids Movies"},
                {"key": "2", "type": "show", "title": "TV Shows"},
            ],
        },
    }
    mock_client.get = AsyncMock(
        side_effect=make_path_dispatch(
            {
                "/library/sections": make_response(sections),
                "/library/sections/1/all": make_response(
                    {"MediaContainer": {"totalSize": 500}}
                ),
                "/library/sections/4/all": make_response(
                    {"MediaContainer": {"totalSize": 120}}
                ),
                "/library/sections/2/all": make_response(
                    {"MediaContainer": {"totalSize": 80}}
                ),
                "/library/sections/2/allLeaves": make_response(
                    {"MediaContainer": {"totalSize": 3000}}
                ),
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_library_stats")
    result = await tool_fn(ctx=ctx)

    assert result["movies"] == 620
    assert result["shows"] == 80
    assert result["episodes"] == 3000


@pytest.mark.asyncio
async def test_get_plex_library_stats_cached(mcp_app, mock_client):
    """Second call within the TTL is served from cache without new requests."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(
        side_effect=make_path_dispatch(
            {
                "/library/sections": make_response(MOCK_STATS_SECTIONS),
                "/library/sections/1/all": make_response(
                    {"MediaContainer": {"totalSize": 500}}
                ),
                "/library/sections/2/all": make_response(
                    {"MediaContainer": {"totalSize": 80}}
                ),
                "/library/sections/2/allLeaves": make_response(
                    {"MediaContainer": {"totalSize": 3000}}
                ),
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_library_stats")
    first = await tool_fn(ctx=ctx)
    calls_after_first = mock_client.get.call_count

    second = await tool_fn(ctx=ctx)

    assert mock_client.get.call_count == calls_after_first
    assert second["movies"] == first["movies"] == 500
    assert second["episodes"] == first["episodes"] == 3000
    assert second["_meta"]["confidence"] == "high"
    assert "_confidence" not in second


@pytest.mark.asyncio
async def test_get_plex_library_stats_partial_error(mcp_app, mock_client):
    """A failing sub-query yields a 0 for that count and lowers confidence."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(
        side_effect=make_path_dispatch(
            {
                "/library/sections": make_response(MOCK_STATS_SECTIONS),
                # Movie /all returns 500 -> _get returns an error dict.
                "/library/sections/1/all": make_response({}, status_code=500),
                "/library/sections/2/all": make_response(
                    {"MediaContainer": {"totalSize": 80}}
                ),
                "/library/sections/2/allLeaves": make_response(
                    {"MediaContainer": {"totalSize": 3000}}
                ),
            }
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_library_stats")
    result = await tool_fn(ctx=ctx)

    assert result["movies"] == 0
    assert result["shows"] == 80
    assert result["episodes"] == 3000
    assert result["_meta"]["confidence"] == "medium"

    # Partial results are not cached: once the sub-query recovers, the next
    # call re-fetches and returns complete counts instead of the pinned 0.
    mock_client.get = AsyncMock(
        side_effect=make_path_dispatch(
            {
                "/library/sections": make_response(MOCK_STATS_SECTIONS),
                "/library/sections/1/all": make_response(
                    {"MediaContainer": {"totalSize": 500}}
                ),
                "/library/sections/2/all": make_response(
                    {"MediaContainer": {"totalSize": 80}}
                ),
                "/library/sections/2/allLeaves": make_response(
                    {"MediaContainer": {"totalSize": 3000}}
                ),
            }
        )
    )
    retry = await tool_fn(ctx=ctx)

    assert mock_client.get.call_count > 0
    assert retry["movies"] == 500
    assert retry["_meta"]["confidence"] == "high"


@pytest.mark.asyncio
async def test_get_plex_library_stats_sections_error(mcp_app, mock_client):
    """When the sections listing fails, returns an error dict."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_plex_library_stats")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_get_helper_timeout(mcp_app, mock_client):
    """When httpx.TimeoutException raised, returns error dict."""
    ctx = make_mock_ctx(plex=mock_client)
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    tool_fn = get_tool_fn(mcp_app, "get_plex_sessions")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "timeout"
    assert "message" in result


@pytest.mark.asyncio
async def test_get_helper_http_error(mcp_app, mock_client):
    """When 500 returned, returns error dict with status."""
    ctx = make_mock_ctx(plex=mock_client)
    error_response = httpx.Response(
        status_code=500,
        request=httpx.Request("GET", "http://test"),
    )
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "http://test"),
            response=error_response,
        )
    )

    tool_fn = get_tool_fn(mcp_app, "get_plex_sessions")
    result = await tool_fn(ctx=ctx)

    assert result["error"] == "http_error"
    assert result["status"] == 500
