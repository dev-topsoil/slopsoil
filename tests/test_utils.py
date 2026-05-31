"""Tests for cogs/utils.py resolve_voice() and ensure_voice()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.utils import ensure_voice, resolve_voice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    in_guild: bool = True,
    author_in_voice: bool = True,
    bot_in_voice: bool = False,
    author_id: int = 111,
) -> MagicMock:
    ctx = MagicMock()
    ctx.author.id = author_id

    voice_state = MagicMock() if author_in_voice else None
    voice_channel = MagicMock() if author_in_voice else None
    if voice_state:
        voice_state.channel = voice_channel

    if in_guild:
        guild = MagicMock()
        guild._voice_states = {author_id: voice_state} if author_in_voice else {}
        guild.voice_client = MagicMock() if bot_in_voice else None
        ctx.guild = guild
    else:
        ctx.guild = None
        # Build guilds list for DM path
        guild = MagicMock()
        guild._voice_states = {author_id: voice_state} if author_in_voice else {}
        guild.voice_client = MagicMock() if bot_in_voice else None
        ctx.bot.guilds = [guild]

    return ctx


# ---------------------------------------------------------------------------
# Guild context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guild_author_in_voice_returns_channel():
    ctx = _make_ctx(in_guild=True, author_in_voice=True)
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is ctx.guild
    assert channel is not None
    assert voice_client is None


@pytest.mark.asyncio
async def test_guild_author_not_in_voice_channel_is_none():
    ctx = _make_ctx(in_guild=True, author_in_voice=False)
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is ctx.guild
    assert channel is None


@pytest.mark.asyncio
async def test_guild_bot_in_voice_client_returned():
    ctx = _make_ctx(in_guild=True, author_in_voice=True, bot_in_voice=True)
    guild, channel, voice_client = await resolve_voice(ctx)
    assert voice_client is ctx.guild.voice_client


@pytest.mark.asyncio
async def test_guild_bot_not_in_voice_client_is_none():
    ctx = _make_ctx(in_guild=True, author_in_voice=True, bot_in_voice=False)
    _, _, voice_client = await resolve_voice(ctx)
    assert voice_client is None


# ---------------------------------------------------------------------------
# DM context (no guild on ctx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_author_in_guild_voice_found():
    ctx = _make_ctx(in_guild=False, author_in_voice=True)
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is not None
    assert channel is not None


@pytest.mark.asyncio
async def test_dm_author_not_in_any_guild_voice():
    ctx = _make_ctx(in_guild=False, author_in_voice=False)
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is None
    assert channel is None
    assert voice_client is None


@pytest.mark.asyncio
async def test_dm_no_guilds_returns_none_triple():
    ctx = MagicMock()
    ctx.guild = None
    ctx.bot.guilds = []
    ctx.author.id = 1
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is None
    assert channel is None
    assert voice_client is None


@pytest.mark.asyncio
async def test_dm_author_found_in_second_guild():
    """Author not in first guild's voice states but found in second."""
    ctx = MagicMock()
    ctx.guild = None
    ctx.author.id = 42

    voice_state = MagicMock()
    voice_state.channel = MagicMock()

    guild1 = MagicMock()
    guild1._voice_states = {}

    guild2 = MagicMock()
    guild2._voice_states = {42: voice_state}
    guild2.voice_client = None

    ctx.bot.guilds = [guild1, guild2]
    guild, channel, voice_client = await resolve_voice(ctx)
    assert guild is guild2
    assert channel is voice_state.channel


# ---------------------------------------------------------------------------
# ensure_voice
# ---------------------------------------------------------------------------


def _make_vc(channel, *, connected: bool = True) -> MagicMock:
    vc = MagicMock()
    vc.channel = channel
    vc.is_connected.return_value = connected
    vc.move_to = AsyncMock()
    vc.disconnect = AsyncMock()
    return vc


def _make_channel() -> tuple[MagicMock, MagicMock]:
    """Return (channel, the_vc_its_connect_yields)."""
    channel = MagicMock()
    fresh_vc = _make_vc(channel)
    channel.connect = AsyncMock(return_value=fresh_vc)
    return channel, fresh_vc


@pytest.mark.asyncio
async def test_ensure_voice_connects_when_no_client():
    channel, fresh_vc = _make_channel()
    result = await ensure_voice(channel, None)
    assert result is fresh_vc
    channel.connect.assert_awaited_once_with(self_deaf=True)


@pytest.mark.asyncio
async def test_ensure_voice_stays_when_already_in_channel():
    channel, _ = _make_channel()
    vc = _make_vc(channel, connected=True)
    result = await ensure_voice(channel, vc)
    assert result is vc
    vc.move_to.assert_not_awaited()
    vc.disconnect.assert_not_awaited()
    channel.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_voice_moves_when_connected_elsewhere():
    channel, _ = _make_channel()
    other = MagicMock()
    vc = _make_vc(other, connected=True)
    result = await ensure_voice(channel, vc)
    assert result is vc
    vc.move_to.assert_awaited_once_with(channel)
    channel.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_voice_reconnects_stale_client_same_channel():
    """A stale (disconnected) client in the same channel must NOT be trusted —
    this is the bug: the bot 'plays' without actually being connected."""
    channel, fresh_vc = _make_channel()
    stale = _make_vc(channel, connected=False)
    result = await ensure_voice(channel, stale)
    assert result is fresh_vc
    stale.disconnect.assert_awaited_once_with(force=True)
    stale.move_to.assert_not_awaited()
    channel.connect.assert_awaited_once_with(self_deaf=True)


@pytest.mark.asyncio
async def test_ensure_voice_reconnects_stale_client_other_channel():
    channel, fresh_vc = _make_channel()
    other = MagicMock()
    stale = _make_vc(other, connected=False)
    result = await ensure_voice(channel, stale)
    assert result is fresh_vc
    stale.disconnect.assert_awaited_once_with(force=True)
    stale.move_to.assert_not_awaited()  # never move a dead client
    channel.connect.assert_awaited_once_with(self_deaf=True)
