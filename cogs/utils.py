from __future__ import annotations

import logging
from typing import cast

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


async def resolve_voice(
    ctx: commands.Context,
) -> tuple[
    discord.Guild | None,
    discord.VoiceChannel | discord.StageChannel | None,
    discord.VoiceClient | None,
]:
    """
    Return (guild, author_voice_channel, bot_voice_client) for guild and DM contexts.

    Voice state is read directly from guild._voice_states rather than going through
    the Member object. The member cache is populated from GUILD_CREATE's members
    array (often incomplete for large guilds), but _voice_states is always populated
    from GUILD_CREATE's voice_states array, so users already in voice when the bot
    starts are reliably found this way.
    """
    if ctx.guild:
        guild = ctx.guild
        voice_state = guild._voice_states.get(ctx.author.id)
    else:
        guild = None
        voice_state = None
        for g in ctx.bot.guilds:
            vs = g._voice_states.get(ctx.author.id)
            if vs:
                guild = g
                voice_state = vs
                break

    if guild is None:
        return None, None, None

    raw_channel = voice_state.channel if voice_state else None
    voice_channel = cast(
        discord.VoiceChannel | discord.StageChannel | None, raw_channel
    )
    voice_client = cast(discord.VoiceClient | None, guild.voice_client)
    return guild, voice_channel, voice_client


async def ensure_voice(
    voice_channel: discord.VoiceChannel | discord.StageChannel,
    vc: discord.VoiceClient | None,
) -> discord.VoiceClient:
    """Return a voice client that is *actually connected* to ``voice_channel``.

    ``guild.voice_client`` can linger as a disconnected client after the bot is
    moved/kicked, a voice websocket drops, or a go-live teardown (e.g. the
    auto-leave handler) tears down the stream without clearing it. Code that only
    checks ``if vc:`` then trusts it skips joining and "streams" into a dead
    connection — the bot announces playback but never appears in the channel, and
    a follow-up !stop finds nothing live.

    So verify the client is genuinely connected: move it if it's connected but in
    another channel; otherwise drop the stale one and connect fresh.
    """
    if vc is not None and vc.is_connected():
        if vc.channel != voice_channel:
            log.info("moving to voice channel '%s'", voice_channel)
            await vc.move_to(voice_channel)
        return vc

    if vc is not None:
        # Stale/half-open client — tear it down so the reconnect starts clean.
        log.info("discarding stale voice client (not connected) before reconnect")
        try:
            await vc.disconnect(force=True)
        except Exception:
            log.debug("error disconnecting stale voice client", exc_info=True)

    log.info("connecting to voice channel '%s'", voice_channel)
    return await voice_channel.connect(self_deaf=True)
