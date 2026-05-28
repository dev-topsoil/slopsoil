"""Tests for cogs/voice.py — on_voice_state_update auto-leave logic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.voice import Voice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOT_USER_ID = 999


def _make_cog():
    bot = MagicMock()
    bot.user.id = BOT_USER_ID
    cog = Voice(bot)
    return cog


def _make_member(guild, user_id: int = 1):
    member = MagicMock()
    member.guild = guild
    member.id = user_id
    member.bot = False  # selfbot accounts always have bot=False
    return member


def _make_voice_state(channel=MagicMock()):
    vs = MagicMock()
    vs.channel = channel
    return vs


def _make_guild(vc_channel, members_in_channel: list, bot_id: int = BOT_USER_ID):
    """Build a guild whose voice client sits in vc_channel with given members."""
    vc = MagicMock()
    vc.channel = vc_channel
    vc.disconnect = AsyncMock()

    for m in members_in_channel:
        m.id = getattr(m, "id", 1)

    vc_channel.members = members_in_channel
    guild = MagicMock()
    guild.id = 42
    guild.voice_client = vc
    return guild


# ---------------------------------------------------------------------------
# Auto-leave triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaves_when_last_human_disconnects():
    """Bot disconnects when the only human leaves its channel."""
    cog = _make_cog()
    channel = MagicMock()

    # Only the bot itself remains after the human leaves.
    bot_member = MagicMock()
    bot_member.id = BOT_USER_ID

    guild = _make_guild(channel, members_in_channel=[bot_member])
    member = _make_member(guild)

    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=None)  # left entirely

    live_conn = AsyncMock()
    cog.bot.live_connections = {guild.id: live_conn}

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_called_once_with(cog.bot, guild.id)
    live_conn.disconnect.assert_awaited_once()
    assert guild.id not in cog.bot.live_connections
    guild.voice_client.disconnect.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_auto_leave_no_golive_connection():
    """Auto-leave works cleanly when no go-live stream is active."""
    cog = _make_cog()
    channel = MagicMock()

    bot_member = MagicMock()
    bot_member.id = BOT_USER_ID
    guild = _make_guild(channel, members_in_channel=[bot_member])
    member = _make_member(guild)
    cog.bot.live_connections = {}  # no go-live active

    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=None)

    with patch("cogs.voice.cancel_stream"):
        await cog.on_voice_state_update(member, before, after)

    guild.voice_client.disconnect.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_stays_when_other_humans_remain():
    """Bot stays if at least one non-bot member is still in the channel."""
    cog = _make_cog()
    channel = MagicMock()

    human1 = MagicMock()
    human1.id = 100
    human2 = MagicMock()
    human2.id = 200

    guild = _make_guild(channel, members_in_channel=[human1, human2])
    member = _make_member(guild, user_id=300)  # a third human leaving

    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=None)

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_not_called()
    guild.voice_client.disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# Auto-leave NOT triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignores_mute_deafen_events():
    """Voice state changes that keep the member in the same channel are ignored."""
    cog = _make_cog()
    channel = MagicMock()
    guild = _make_guild(channel, members_in_channel=[])
    member = _make_member(guild)

    # before and after both point to the same channel (mute/deafen event)
    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=channel)

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_not_called()
    guild.voice_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_join_events():
    """Member joining (before.channel is None) should not trigger auto-leave."""
    cog = _make_cog()
    channel = MagicMock()
    guild = _make_guild(channel, members_in_channel=[])
    member = _make_member(guild)

    before = _make_voice_state(channel=None)  # was not in any channel
    after = _make_voice_state(channel=channel)  # joined

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_ignores_leave_from_different_channel():
    """Member leaving a channel the bot is NOT in should be ignored."""
    cog = _make_cog()
    bot_channel = MagicMock()
    other_channel = MagicMock()

    guild = _make_guild(bot_channel, members_in_channel=[])
    member = _make_member(guild)

    before = _make_voice_state(channel=other_channel)  # left a different channel
    after = _make_voice_state(channel=None)

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_not_called()
    guild.voice_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_when_bot_not_in_voice():
    """If the bot has no active voice client, do nothing."""
    cog = _make_cog()
    channel = MagicMock()

    guild = MagicMock()
    guild.voice_client = None  # bot not in voice
    member = _make_member(guild)

    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=None)

    with patch("cogs.voice.cancel_stream") as mock_cancel:
        await cog.on_voice_state_update(member, before, after)

    mock_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_bot_id_used_not_bot_flag():
    """Uses member ID to identify the bot, not member.bot, because selfbot accounts have bot=False."""
    cog = _make_cog()
    channel = MagicMock()

    # The remaining member IS the bot, but bot=False (selfbot account)
    bot_member = MagicMock()
    bot_member.id = BOT_USER_ID
    bot_member.bot = False  # selfbot — this flag cannot be trusted

    guild = _make_guild(channel, members_in_channel=[bot_member])
    member = _make_member(guild, user_id=100)

    before = _make_voice_state(channel=channel)
    after = _make_voice_state(channel=None)

    with patch("cogs.voice.cancel_stream"):
        await cog.on_voice_state_update(member, before, after)

    # Should disconnect because the only remaining member IS the bot (by ID)
    guild.voice_client.disconnect.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_member_moves_to_another_channel_no_leave():
    """Member moving between channels (before and after both non-None, different) is a leave from before.channel."""
    cog = _make_cog()
    bot_channel = MagicMock()
    other_channel = MagicMock()

    # Bot is alone in bot_channel after the human moves away
    bot_member = MagicMock()
    bot_member.id = BOT_USER_ID
    guild = _make_guild(bot_channel, members_in_channel=[bot_member])
    member = _make_member(guild)

    before = _make_voice_state(channel=bot_channel)
    after = _make_voice_state(channel=other_channel)  # moved, not fully left

    with patch("cogs.voice.cancel_stream"):
        await cog.on_voice_state_update(member, before, after)

    # before.channel != after.channel, so it IS treated as a leave from bot_channel
    guild.voice_client.disconnect.assert_awaited_once_with(force=False)
