from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from discord.ext import commands

from cogs.stream import cancel_stream
from cogs.utils import resolve_voice
from permissions import Role, require_role

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = cast("SlopSoil", bot)

    @require_role(Role.FRIEND)
    @commands.command()
    async def join(self, ctx: commands.Context):
        guild, voice_channel, vc = await resolve_voice(ctx)

        if not voice_channel:
            log.debug(
                "join rejected: %s is not in a voice channel (guild: %s)",
                ctx.author,
                guild,
            )
            await ctx.send("you're not in a voice channel")
            return

        if vc:
            log.info("moving to voice channel '%s' in guild '%s'", voice_channel, guild)
            await vc.move_to(voice_channel)
        else:
            log.info(
                "connecting to voice channel '%s' in guild '%s'", voice_channel, guild
            )
            await voice_channel.connect(self_deaf=True)

        await ctx.send("joined!")

    @require_role(Role.FRIEND)
    @commands.command()
    async def leave(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            for g in ctx.bot.guilds:
                if g.voice_client:
                    guild = g
                    break

        if guild is None:
            await ctx.send("not in a voice channel")
            return

        vc = guild.voice_client
        if not vc:
            log.debug("leave rejected: not in a voice channel (guild: %s)", guild)
            await ctx.send("not in a voice channel")
            return

        log.info("disconnecting from voice in guild '%s'", guild)
        cancel_stream(self.bot, guild.id)
        await vc.disconnect(force=False)
        await ctx.send("left!")

    @require_role(Role.FRIEND)
    @commands.command()
    async def stop(self, ctx: commands.Context):
        guild, _, vc = await resolve_voice(ctx)

        if not guild or not vc:
            log.debug("stop rejected: nothing playing (guild: %s)", guild)
            await ctx.send("nothing is playing")
            return

        if not vc.is_playing() and guild.id not in self.bot.stream_tasks:
            log.debug("stop rejected: nothing playing (guild: %s)", guild)
            await ctx.send("nothing is playing")
            return

        log.info("stopping stream in guild '%s'", guild)
        cancel_stream(self.bot, guild.id)
        if vc.is_playing():
            vc.stop()

        await ctx.send("stopped")


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
