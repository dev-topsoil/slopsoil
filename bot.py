import asyncio
import logging
import os
from typing import TYPE_CHECKING

import discord
import discord.gateway
import discord.voice_state
from discord.ext import commands
from dotenv import load_dotenv

import davey_compat
import video_compat
from permissions import Role, get_user_role

if TYPE_CHECKING:
    from cogs.golive import GoLiveConnection
    from cogs.iptv import SourceManager
    from cogs.video_player import H264VideoPlayer

# Replace discord.py-self's broken davey (Rust) with the working libdave shim.
# Must happen before any voice connections are made.
discord.voice_state.davey = davey_compat
discord.gateway.davey = davey_compat
davey_compat.patch_reinit(discord.voice_state)

# Patch discord.py-self for H.264 video streaming support.
video_compat.patch_video(discord.gateway)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)


def _load_allowed_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


class SlopSoil(commands.Bot):
    def __init__(self, allowed_ids: set[int]):
        super().__init__(command_prefix="!", help_command=None)
        self.allowed_ids = allowed_ids
        self.stream_tasks: dict[int, asyncio.Task] = {}
        self.video_players: dict[int, H264VideoPlayer] = {}
        self.live_connections: dict[int, GoLiveConnection] = {}
        self.source_manager: SourceManager | None = None

    async def setup_hook(self):
        async def has_any_role(ctx: commands.Context) -> bool:
            if self.user and ctx.author.id == self.user.id:
                return True
            return get_user_role(self, ctx.author.id) != Role.NONE

        self.add_check(has_any_role)

        for ext in ("cogs.general", "cogs.voice", "cogs.iptv"):
            await self.load_extension(ext)
            log.info("loaded extension: %s", ext)

        tvh_vars = ("TVHEADEND_URL", "TVHEADEND_USER", "TVHEADEND_PASS")
        if all(os.environ.get(v) for v in tvh_vars):
            await self.load_extension("cogs.tv")
            log.info(
                "loaded extension: cogs.tv (TVheadend: %s)", os.environ["TVHEADEND_URL"]
            )
        else:
            log.warning(
                "TVHEADEND_URL/USER/PASS not set — !play and !channels disabled"
            )

    async def close(self) -> None:
        # Cancel active stream tasks and terminate FFmpeg processes before the
        # event loop shuts down, so a single Ctrl+C exits cleanly.
        tasks = list(self.stream_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        for vp in list(self.video_players.values()):
            vp.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    async def on_ready(self):
        log.info("logged in as %s (id: %s)", self.user, self.user.id)
        log.info("allowed users: %s", self.allowed_ids or "none (only self)")
        log.info("serving %d guild(s)", len(self.guilds))

    async def on_command(self, ctx: commands.Context):
        log.info(
            "command [%s] by %s (id: %s) in #%s / %s",
            ctx.command,
            ctx.author,
            ctx.author.id,
            ctx.channel,
            ctx.guild or "DM",
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ):
        # CheckFailure means the user failed a role check — ignore silently
        if isinstance(error, commands.CheckFailure):
            log.debug(
                "command [%s] rejected for user %s (id: %s)",
                ctx.command,
                ctx.author,
                ctx.author.id,
            )
            return
        if isinstance(error, commands.CommandNotFound):
            return
        log.exception(
            "unhandled error in command [%s] by %s: %s",
            ctx.command,
            ctx.author,
            error,
            exc_info=error,
        )


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable not set")

    bot = SlopSoil(_load_allowed_ids())
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
