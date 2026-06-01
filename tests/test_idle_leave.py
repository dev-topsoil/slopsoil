"""Tests for the optional idle-leave-after-playback timeout."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from streaming.engine import (
    _format_idle,
    _idle_leave_now,
    _idle_leave_timeout,
    cancel_idle_leave,
    schedule_idle_leave,
)


@pytest.fixture
def fake_bot():
    bot = MagicMock()
    bot.stream_tasks = {}
    bot.video_players = {}
    bot.live_connections = {}
    bot.idle_leave_tasks = {}
    return bot


def _make_guild(guild_id=42, *, connected=True, playing=False):
    vc = MagicMock()
    vc.is_connected.return_value = connected
    vc.is_playing.return_value = playing
    vc.disconnect = AsyncMock()
    guild = MagicMock()
    guild.id = guild_id
    guild.voice_client = vc
    return guild, vc


# --- config parsing -------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [(None, 0.0), ("", 0.0), ("300", 300.0), ("0", 0.0), ("abc", 0.0), ("-5", 0.0)],
)
def test_idle_leave_timeout_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("IDLE_LEAVE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("IDLE_LEAVE_TIMEOUT", value)
    assert _idle_leave_timeout() == expected


# --- duration formatting --------------------------------------------------

@pytest.mark.parametrize(
    "seconds,text",
    [(300, "5 min"), (60, "1 min"), (30, "30 sec"), (90, "1 min 30 sec")],
)
def test_format_idle(seconds, text):
    assert _format_idle(seconds) == text


# --- scheduling / cancellation -------------------------------------------

async def test_schedule_disabled_is_noop(fake_bot):
    guild, _ = _make_guild()
    schedule_idle_leave(fake_bot, guild, AsyncMock(), 0)
    assert guild.id not in fake_bot.idle_leave_tasks


async def test_schedule_arms_and_rearm_cancels_prior(fake_bot):
    guild, _ = _make_guild()
    schedule_idle_leave(fake_bot, guild, AsyncMock(), 1000)
    first = fake_bot.idle_leave_tasks[guild.id]
    assert isinstance(first, asyncio.Task)

    schedule_idle_leave(fake_bot, guild, AsyncMock(), 1000)
    second = fake_bot.idle_leave_tasks[guild.id]
    assert second is not first

    await asyncio.sleep(0)  # let the cancelled task settle
    assert first.cancelled()
    assert fake_bot.idle_leave_tasks[guild.id] is second

    cancel_idle_leave(fake_bot, guild.id)


async def test_cancel_idle_leave_removes_task(fake_bot):
    guild, _ = _make_guild()
    schedule_idle_leave(fake_bot, guild, AsyncMock(), 1000)
    cancel_idle_leave(fake_bot, guild.id)
    assert guild.id not in fake_bot.idle_leave_tasks


def test_cancel_idle_leave_safe_when_absent(fake_bot):
    cancel_idle_leave(fake_bot, 123)  # must not raise


# --- the leave action (no sleep) -----------------------------------------

async def test_idle_leave_now_noop_when_disconnected(fake_bot):
    guild, vc = _make_guild(connected=False)
    send = AsyncMock()
    await _idle_leave_now(fake_bot, guild, send, 300)
    vc.disconnect.assert_not_called()
    send.assert_not_called()


async def test_idle_leave_now_noop_when_playing(fake_bot):
    guild, vc = _make_guild(playing=True)
    send = AsyncMock()
    await _idle_leave_now(fake_bot, guild, send, 300)
    vc.disconnect.assert_not_called()


async def test_idle_leave_now_noop_when_stream_active(fake_bot):
    guild, vc = _make_guild()
    fake_bot.stream_tasks[guild.id] = MagicMock()
    send = AsyncMock()
    await _idle_leave_now(fake_bot, guild, send, 300)
    vc.disconnect.assert_not_called()


async def test_idle_leave_now_noop_when_golive_active(fake_bot):
    guild, vc = _make_guild()
    fake_bot.live_connections[guild.id] = MagicMock()
    send = AsyncMock()
    await _idle_leave_now(fake_bot, guild, send, 300)
    vc.disconnect.assert_not_called()


async def test_idle_leave_now_disconnects_when_idle(fake_bot):
    guild, vc = _make_guild()
    send = AsyncMock()
    await _idle_leave_now(fake_bot, guild, send, 300)
    vc.disconnect.assert_awaited_once()
    send.assert_called_once()


# --- voice.py cancellation wiring ----------------------------------------

from cogs.voice import Voice  # noqa: E402


async def test_leave_cancels_idle_timer(fake_bot, monkeypatch):
    guild, vc = _make_guild(guild_id=99)
    monkeypatch.setattr("cogs.voice.cancel_stream", lambda *a: None)
    called = {}
    monkeypatch.setattr(
        "cogs.voice.cancel_idle_leave",
        lambda bot, gid: called.__setitem__("gid", gid),
    )

    cog = Voice(fake_bot)
    ctx = MagicMock()
    ctx.guild = guild
    ctx.send = AsyncMock()
    await Voice.leave.callback(cog, ctx)

    assert called["gid"] == 99


async def test_auto_leave_cancels_idle_timer(fake_bot, monkeypatch):
    monkeypatch.setattr("cogs.voice.cancel_stream", lambda *a: None)
    called = {}
    monkeypatch.setattr(
        "cogs.voice.cancel_idle_leave",
        lambda bot, gid: called.__setitem__("gid", gid),
    )
    bot_user = MagicMock()
    bot_user.id = 1000
    fake_bot.user = bot_user

    cog = Voice(fake_bot)

    vc = MagicMock()
    vc.disconnect = AsyncMock()
    vc.channel.members = [bot_user]  # only the bot remains
    before = MagicMock()
    before.channel = vc.channel
    after = MagicMock()
    after.channel = None
    member = MagicMock()
    member.guild.id = 5
    member.guild.voice_client = vc

    # Invoked the same way the existing suite calls the listener (see test_voice.py).
    await cog.on_voice_state_update(member, before, after)

    assert called["gid"] == 5
