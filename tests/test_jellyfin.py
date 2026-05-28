"""Tests for cogs/jellyfin.py — pure functions and JellyfinClient API calls."""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.jellyfin import Jellyfin, JellyfinClient, _fmt_item, _parse_episode_query


# ---------------------------------------------------------------------------
# Helpers shared across sections
# ---------------------------------------------------------------------------


def _make_ctx(author_id: int = 1, channel_id: int = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.id = channel_id
    ctx.send = AsyncMock()
    return ctx


def _make_jellyfin_cog(client=None) -> Jellyfin:
    bot = MagicMock()
    bot.user.id = 999
    cog = Jellyfin(bot, client)
    return cog


# ---------------------------------------------------------------------------
# _parse_episode_query
# ---------------------------------------------------------------------------


def test_parse_no_episode_code():
    show, season, episode = _parse_episode_query("inception")
    assert show == "inception"
    assert season is None
    assert episode is None


def test_parse_lowercase():
    show, season, episode = _parse_episode_query("rick and morty s02e01")
    assert show == "rick and morty"
    assert season == 2
    assert episode == 1


def test_parse_uppercase():
    show, season, episode = _parse_episode_query("Breaking Bad S03E10")
    assert show == "Breaking Bad"
    assert season == 3
    assert episode == 10


def test_parse_mixed_case():
    show, season, episode = _parse_episode_query("The Office S01e03")
    assert show == "The Office"
    assert season == 1
    assert episode == 3


def test_parse_leading_trailing_whitespace_stripped():
    show, season, episode = _parse_episode_query("  Arrested Development  s04e01  ")
    assert show == "Arrested Development"
    assert season == 4
    assert episode == 1


def test_parse_only_episode_code_gives_empty_show():
    show, season, episode = _parse_episode_query("s01e05")
    assert show == ""
    assert season == 1
    assert episode == 5


def test_parse_multi_digit_season_and_episode():
    show, season, episode = _parse_episode_query("show s12e24")
    assert show == "show"
    assert season == 12
    assert episode == 24


def test_parse_uses_first_match_only():
    """Extra sXXeYY tokens after the first are ignored."""
    show, season, episode = _parse_episode_query("show s01e01 s02e02")
    assert show == "show"
    assert season == 1
    assert episode == 1


def test_parse_code_not_on_word_boundary_ignored():
    """Substrings like 'season01ep01' should not match."""
    show, season, episode = _parse_episode_query("seas01ep01")
    assert show == "seas01ep01"
    assert season is None
    assert episode is None


# ---------------------------------------------------------------------------
# _fmt_item
# ---------------------------------------------------------------------------


def test_fmt_movie_with_year():
    item = {"Type": "Movie", "Name": "Inception", "ProductionYear": 2010}
    assert _fmt_item(item) == "Inception (2010)"


def test_fmt_movie_without_year():
    item = {"Type": "Movie", "Name": "Inception"}
    assert _fmt_item(item) == "Inception"


def test_fmt_series_with_year():
    item = {"Type": "Series", "Name": "Breaking Bad", "ProductionYear": 2008}
    assert _fmt_item(item) == "Breaking Bad (2008)"


def test_fmt_episode_with_season_and_ep():
    item = {
        "Type": "Episode",
        "Name": "Pilot",
        "SeriesName": "Breaking Bad",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
    }
    assert _fmt_item(item) == "Breaking Bad — S01E01 — Pilot"


def test_fmt_episode_zero_padded():
    item = {
        "Type": "Episode",
        "Name": "Fly",
        "SeriesName": "Breaking Bad",
        "ParentIndexNumber": 3,
        "IndexNumber": 10,
    }
    assert _fmt_item(item) == "Breaking Bad — S03E10 — Fly"


def test_fmt_episode_without_season_ep():
    item = {
        "Type": "Episode",
        "Name": "Pilot",
        "SeriesName": "Breaking Bad",
    }
    assert _fmt_item(item) == "Breaking Bad — Pilot"


# ---------------------------------------------------------------------------
# JellyfinClient HTTP methods (urlopen mocked)
# ---------------------------------------------------------------------------


def _mock_response(data: dict):
    """Return a context-manager mock that yields a response with JSON body."""
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def client():
    return JellyfinClient(url="http://jellyfin.local:8096", api_key="testkey")


@pytest.mark.asyncio
async def test_get_user_id_fetches_and_caches(client):
    users = [{"Id": "user-abc", "Name": "Admin"}]
    with patch("urllib.request.urlopen", return_value=_mock_response(users)):
        uid = await client._get_user_id()
    assert uid == "user-abc"
    # Second call should use cache, not make another request
    with patch("urllib.request.urlopen", side_effect=Exception("should not be called")):
        uid2 = await client._get_user_id()
    assert uid2 == "user-abc"


@pytest.mark.asyncio
async def test_get_user_id_returns_none_on_failure(client):
    with patch("urllib.request.urlopen", side_effect=Exception("403 Forbidden")):
        uid = await client._get_user_id()
    assert uid is None


def _make_playback_response(transcoding_url: str | None = None, direct_url: str | None = None):
    source: dict = {"Id": "source-1"}
    if transcoding_url:
        source["TranscodingUrl"] = transcoding_url
    if direct_url:
        source["DirectStreamUrl"] = direct_url
    return _mock_response({"MediaSources": [source], "PlaySessionId": "sess-1"})


@pytest.mark.asyncio
async def test_get_stream_url_uses_playback_info(client):
    client._user_id = "user-abc"  # pre-cache so no extra fetch
    pb_resp = _make_playback_response(
        transcoding_url="/Videos/item-1/main/stream.m3u8?DeviceId=slopsoil&PlaySessionId=abc"
    )
    with patch("urllib.request.urlopen", return_value=pb_resp):
        url = await client.get_stream_url("item-1")
    assert url is not None
    assert "stream.m3u8" in url
    assert "api_key=testkey" in url
    assert "SubtitleStreamIndex=-1" in url


@pytest.mark.asyncio
async def test_get_stream_url_overrides_subtitle_index(client):
    """Jellyfin may set SubtitleStreamIndex=0 in the URL from user profile settings; we must override it."""
    client._user_id = "user-abc"
    pb_resp = _make_playback_response(
        transcoding_url="/Videos/item-1/stream.m3u8?SubtitleStreamIndex=0&PlaySessionId=abc"
    )
    with patch("urllib.request.urlopen", return_value=pb_resp):
        url = await client.get_stream_url("item-1")
    assert url is not None
    assert "SubtitleStreamIndex=-1" in url
    assert "SubtitleStreamIndex=0" not in url


@pytest.mark.asyncio
async def test_get_stream_url_falls_back_to_direct_stream(client):
    client._user_id = "user-abc"
    pb_resp = _make_playback_response(direct_url="/Videos/item-1/stream?Container=mp4")
    with patch("urllib.request.urlopen", return_value=pb_resp):
        url = await client.get_stream_url("item-1")
    assert url is not None
    assert "api_key=testkey" in url


@pytest.mark.asyncio
async def test_get_stream_url_returns_none_on_empty_sources(client):
    client._user_id = "user-abc"
    with patch("urllib.request.urlopen", return_value=_mock_response({"MediaSources": []})):
        url = await client.get_stream_url("item-1")
    assert url is None


@pytest.mark.asyncio
async def test_get_stream_url_returns_none_on_request_failure(client):
    client._user_id = "user-abc"
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        url = await client.get_stream_url("item-1")
    assert url is None


@pytest.mark.asyncio
async def test_search_returns_items(client):
    payload = {
        "Items": [
            {"Id": "1", "Name": "Inception", "Type": "Movie", "ProductionYear": 2010},
            {"Id": "2", "Name": "Interstellar", "Type": "Movie", "ProductionYear": 2014},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        results = await client.search("ception")
    assert len(results) == 2
    assert results[0]["Name"] == "Inception"


@pytest.mark.asyncio
async def test_search_empty_result(client):
    with patch("urllib.request.urlopen", return_value=_mock_response({"Items": []})):
        results = await client.search("nothing")
    assert results == []


@pytest.mark.asyncio
async def test_get_seasons_sorted(client):
    payload = {
        "Items": [
            {"Id": "s2", "Name": "Season 2", "IndexNumber": 2},
            {"Id": "s1", "Name": "Season 1", "IndexNumber": 1},
            {"Id": "s3", "Name": "Season 3", "IndexNumber": 3},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        seasons = await client.get_seasons("series-abc")
    assert [s["IndexNumber"] for s in seasons] == [1, 2, 3]


@pytest.mark.asyncio
async def test_get_episodes_sorted(client):
    payload = {
        "Items": [
            {"Id": "e3", "Name": "C", "IndexNumber": 3},
            {"Id": "e1", "Name": "A", "IndexNumber": 1},
            {"Id": "e2", "Name": "B", "IndexNumber": 2},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        episodes = await client.get_episodes("series-abc", "season-1")
    assert [e["IndexNumber"] for e in episodes] == [1, 2, 3]


@pytest.mark.asyncio
async def test_find_episode_resolves_via_series(client):
    """find_episode should search for a series, then navigate seasons → episode."""
    series = [{"Id": "series-1", "Name": "Rick and Morty", "Type": "Series"}]
    seasons = [
        {"Id": "s1", "Name": "Season 1", "IndexNumber": 1},
        {"Id": "s2", "Name": "Season 2", "IndexNumber": 2},
    ]
    episodes_s2 = [
        {"Id": "e1", "Name": "Ricksy Business", "IndexNumber": 1, "ParentIndexNumber": 2},
        {"Id": "e2", "Name": "A Rickle in Time", "IndexNumber": 2, "ParentIndexNumber": 2},
    ]

    async def _search(q, limit=25):
        return series

    async def _get_seasons(sid):
        assert sid == "series-1"
        return seasons

    async def _get_episodes(sid, season_id):
        assert sid == "series-1"
        assert season_id == "s2"
        return episodes_s2

    client.search = _search
    client.get_seasons = _get_seasons
    client.get_episodes = _get_episodes

    results = await client.find_episode("rick and morty", season=2, episode=1)
    assert len(results) == 1
    assert results[0]["Id"] == "e1"


@pytest.mark.asyncio
async def test_find_episode_no_matching_season(client):
    series = [{"Id": "series-1", "Name": "Show", "Type": "Series"}]
    seasons = [{"Id": "s1", "Name": "Season 1", "IndexNumber": 1}]

    client.search = AsyncMock(return_value=series)
    client.get_seasons = AsyncMock(return_value=seasons)
    client.get_episodes = AsyncMock(return_value=[])

    results = await client.find_episode("show", season=5, episode=1)
    assert results == []
    client.get_episodes.assert_not_called()


@pytest.mark.asyncio
async def test_find_episode_no_series_found(client):
    client.search = AsyncMock(return_value=[])

    results = await client.find_episode("unknown show", season=1, episode=1)
    assert results == []


# ---------------------------------------------------------------------------
# Jellyfin cog — _wait_for_number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_number_valid_selection():
    cog = _make_jellyfin_cog()
    ctx = _make_ctx()
    reply = MagicMock()
    reply.content = "2"
    cog.bot.wait_for = AsyncMock(return_value=reply)

    result = await cog._wait_for_number(ctx, max_val=3)
    assert result == 2


@pytest.mark.asyncio
async def test_wait_for_number_out_of_range_returns_none():
    cog = _make_jellyfin_cog()
    ctx = _make_ctx()
    reply = MagicMock()
    reply.content = "99"
    cog.bot.wait_for = AsyncMock(return_value=reply)

    result = await cog._wait_for_number(ctx, max_val=3)
    assert result is None
    ctx.send.assert_awaited_once()
    assert "1 and 3" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_wait_for_number_timeout_returns_none():
    cog = _make_jellyfin_cog()
    ctx = _make_ctx()
    cog.bot.wait_for = AsyncMock(side_effect=TimeoutError)

    result = await cog._wait_for_number(ctx, max_val=5)
    assert result is None
    ctx.send.assert_awaited_once()
    assert "timed out" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_wait_for_number_command_cancels():
    """Typing a new command while a prompt is active cancels the selection."""
    cog = _make_jellyfin_cog()
    ctx = _make_ctx()
    reply = MagicMock()
    reply.content = "!play something"
    cog.bot.wait_for = AsyncMock(return_value=reply)

    result = await cog._wait_for_number(ctx, max_val=5)
    assert result is None
    ctx.send.assert_awaited_once()
    assert "cancelled" in ctx.send.call_args[0][0]


@pytest.mark.asyncio
async def test_wait_for_number_boundary_values():
    cog = _make_jellyfin_cog()
    ctx = _make_ctx()

    for value in ("1", "5"):
        ctx.send.reset_mock()
        reply = MagicMock()
        reply.content = value
        cog.bot.wait_for = AsyncMock(return_value=reply)
        result = await cog._wait_for_number(ctx, max_val=5)
        assert result == int(value)


# ---------------------------------------------------------------------------
# Jellyfin cog — unconfigured client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_command_not_configured(monkeypatch):
    """!media should send a helpful error if JELLYFIN_URL/API_KEY are not set."""
    cog = _make_jellyfin_cog(client=None)
    ctx = _make_ctx()

    # Invoke the underlying coroutine directly, bypassing the command decorator.
    await cog.media.callback(cog, ctx, query="inception")

    ctx.send.assert_awaited_once()
    msg = ctx.send.call_args[0][0]
    assert "not configured" in msg.lower()
    assert "JELLYFIN_URL" in msg
