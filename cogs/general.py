from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx: commands.Context):
        await ctx.send("pong!")

    @commands.command()
    async def hello(self, ctx: commands.Context):
        await ctx.send("Hello, world!")

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context):
        lines = [
            "**Commands**",
            "",
            "**General**",
            "  `!ping` — check if the bot is alive",
            "  `!help` — show this message",
            "",
            "**Voice**",
            "  `!join` — join your current voice channel",
            "  `!leave` — disconnect from voice",
            "  `!stop` — stop the current stream",
        ]

        if self.bot.get_cog("YtDlp"):
            lines += [
                "",
                "**Video**",
                "  `!yt <url>` — download a video with yt-dlp and stream it to voice",
            ]

        if self.bot.get_cog("TV"):
            lines += [
                "",
                "**TV (TVheadend)**",
                "  `!channels` — list all channels (TVheadend + IPTV)"
                " with now-playing info (paginated)",
                "  `!play <number or name>` — stream a TVheadend or IPTV"
                " channel into voice",
                "  `!search <show title>` — find a show in the EPG;"
                " plays now or schedules",
            ]

        allowed: set[int] = getattr(self.bot, "allowed_ids", set())
        if self.bot.get_cog("IPTV") and (not allowed or ctx.author.id in allowed):
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
