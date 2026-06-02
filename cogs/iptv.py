"""
IPTV playlist source management — !add-source and !sources commands.

The Discord command layer only. Playlist parsing lives in services.m3u and the
persistence layer (SourceManager) lives in services.sources, which other cogs
(tv.py) use to pull live channel lists from enabled sources.
"""

from __future__ import annotations

import logging
import urllib.error
from typing import TYPE_CHECKING, cast

from discord.ext import commands

from permissions import Role, require_role
from services.m3u import fetch_and_parse
from services.sources import SourceManager, _data_dir

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


class IPTVCog(commands.Cog, name="IPTV"):
    def __init__(self, bot: commands.Bot, source_manager: SourceManager):
        self.bot = cast("SlopSoil", bot)
        self.sm = source_manager

    @require_role(Role.ADMIN)
    @commands.command(name="add-source")
    async def add_source(self, ctx: commands.Context, name: str, *, url: str):
        """Add or update an IPTV playlist source from an M3U URL."""
        await ctx.send(f"fetching and parsing playlist from `{url}`…")
        try:
            channels, epg_url = await fetch_and_parse(url)
        except urllib.error.URLError as exc:
            await ctx.send(f"could not fetch playlist: {exc}")
            return
        except ValueError as exc:
            await ctx.send(f"invalid playlist: {exc}")
            return
        except Exception as exc:
            log.exception("error fetching IPTV playlist: %s", exc)
            await ctx.send(f"failed to load playlist: {exc}")
            return

        self.sm.add_source(name, url, channels, epg_url=epg_url)
        epg_note = (
            f" — EPG found ({epg_url})" if epg_url else " — no EPG URL in playlist"
        )
        await ctx.send(
            f"added source **{name}** with {len(channels)} channel(s)"
            f" (enabled){epg_note}"
        )

    @require_role(Role.ADMIN)
    @commands.command(name="sources")
    async def set_source(
        self, ctx: commands.Context, action: str = "", *, name: str = ""
    ):
        """List sources, or enable/disable one by name.

        !sources                      — list all sources
        !sources enable <name>        — enable a source by name
        !sources disable <name>       — disable a source by name
        """
        has_tvh = self.bot.get_cog("TV") is not None
        iptv_sources = self.sm.get_sources()

        # ── Direct enable/disable subcommand ─────────────────────────────────
        if action.lower() in ("enable", "disable"):
            if not name:
                await ctx.send(f"usage: `!sources {action} <source name>`")
                return
            want_enabled = action.lower() == "enable"
            query = name.lower()

            # Check TVheadend first (substring match against "tvheadend")
            if has_tvh and query in "tvheadend":
                self.sm.set_tvh_enabled(want_enabled)
                label = "enabled" if want_enabled else "disabled"
                await ctx.send(f"**TVheadend** {label}")
                return

            # Search IPTV sources by case-insensitive substring
            matches = [
                (i, src) for i, src in enumerate(iptv_sources)
                if query in src["name"].lower()
            ]
            if not matches:
                await ctx.send(f'no source matching "{name}" found')
                return
            if len(matches) > 1:
                names = ", ".join(f"**{src['name']}**" for _, src in matches)
                await ctx.send(f'ambiguous name "{name}" — matches: {names}')
                return
            idx, src = matches[0]
            self.sm.set_enabled(idx, want_enabled)
            label = "enabled" if want_enabled else "disabled"
            await ctx.send(f"**{src['name']}** {label}")
            return

        # ── List sources (no arguments) ───────────────────────────────────────
        if not has_tvh and not iptv_sources:
            await ctx.send(
                "no sources configured"
                " — use `!add-source <name> <url>` to add an IPTV source"
            )
            return

        lines = ["**Sources**"]
        if has_tvh:
            status = "✓" if self.sm.tvh_enabled else "✗"
            lines.append(f"  [{status}] **TVheadend**")
        for src in iptv_sources:
            status = "✓" if src.get("enabled") else "✗"
            count = len(src.get("channels", []))
            lines.append(f"  [{status}] **{src['name']}** — {count} channel(s)")
        await ctx.send("\n".join(lines))

    @require_role(Role.ADMIN)
    @commands.command(name="delete-source")
    async def delete_source(self, ctx: commands.Context):
        """Remove an IPTV playlist source."""
        sources = self.sm.get_sources()
        if not sources:
            await ctx.send(
                "no sources added yet — use `!add-source <name> <url>` to add one"
            )
            return

        lines = ["**IPTV Sources** (reply with number to delete):"]
        for i, src in enumerate(sources, 1):
            count = len(src.get("channels", []))
            lines.append(f"  {i}. **{src['name']}** — {count} channel(s)")
        lines.append('\nReply with a number to delete, or "cancel".')
        await ctx.send("\n".join(lines))

        def _check(msg) -> bool:
            if msg.author.id != ctx.author.id or msg.channel.id != ctx.channel.id:
                return False
            text = msg.content.strip().lower()
            if text == "cancel":
                return True
            return text.isdigit() and 1 <= int(text) <= len(sources)

        try:
            msg = await self.bot.wait_for("message", check=_check, timeout=60.0)
        except TimeoutError:
            await ctx.send("no response — cancelled")
            return

        text = msg.content.strip().lower()
        if text == "cancel":
            await ctx.send("cancelled")
            return

        idx = int(text) - 1
        name = self.sm.remove_source(idx)
        await ctx.send(f"removed source **{name}**")


async def setup(bot: commands.Bot):
    sm = SourceManager(_data_dir() / "sources.json")
    bot.source_manager = sm  # type: ignore[attr-defined]
    log.info("IPTV SourceManager loaded (%d source(s))", len(sm.get_sources()))
    await bot.add_cog(IPTVCog(bot, sm))
    n = await sm.backfill_epg_urls()
    if n:
        log.info("backfilled EPG URL(s) for %d source(s)", n)
