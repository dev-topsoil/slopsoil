from discord.ext import commands

from permissions import Role, get_user_role, require_role


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @require_role(Role.VIEWER)
    @commands.command()
    async def ping(self, ctx: commands.Context):
        await ctx.send("pong!")

    @require_role(Role.ADMIN)
    @commands.command()
    async def hello(self, ctx: commands.Context):
        await ctx.send("Hello, world!")

    @require_role(Role.VIEWER)
    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context):
        lines = [
            "**General**",
            "  `!ping` — check if the bot is alive",
            "  `!help` — show this message",
            "",
            "**Voice**",
            "  `!join` — join your current voice channel",
            "  `!leave` — disconnect from voice",
            "  `!stop` — stop the current stream",
        ]

        if self.bot.get_cog("TV"):
            lines += [
                "",
                "**Streaming**",
                "  `!channels` — list all channels (TVheadend + IPTV)"
                " with now-playing info (paginated)",
                "  `!play <number, name, or url>` — stream a TVheadend/IPTV"
                " channel or a yt-dlp URL into voice",
                "  `!search <show title>` — find a show in the EPG;"
                " plays now or schedules",
            ]

        if self.bot.get_cog("Jellyfin"):
            lines += [
                "",
                "**Jellyfin**",
                "  `!media <title>` — search Jellyfin for a movie, series,"
                " or episode and select it for playback",
            ]

        if self.bot.get_cog("IPTV") and (
            get_user_role(self.bot, ctx.author.id) == Role.ADMIN  # type: ignore[arg-type]
        ):
            lines += [
                "",
                "**IPTV**",
                "  `!add-source <name> <url>` — add an M3U playlist source",
                "  `!sources` — list all sources and their enabled/disabled state",
                "  `!sources enable/disable <name>` — enable or"
                " disable a source by name",
                "  `!delete-source` — remove an IPTV source",
            ]

        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
