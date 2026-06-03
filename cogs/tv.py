from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import urllib.error
from collections.abc import Callable
from datetime import datetime, timezone, tzinfo as _TZInfo
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

from cogs.utils import resolve_voice
from permissions import Role, require_role
from services.epg import fetch_xmltv_now_playing as _fetch_xmltv_now_playing
from services.hls import extract_hls_variant_url as _extract_hls_variant_url
from services.probe import probe_stream as _probe_stream
from services.tvheadend import TVheadendClient
from services.ytdlp import (
    _yt_cleanup_after_stream,
    _yt_download,
    _yt_extract_live_url,
    _yt_remove_dir,
)
from streaming.engine import start_live_stream

# {source_name: (fetched_at, {tvg_id: title})} — refreshed every 15 minutes
_epg_cache: dict[str, tuple[float, dict[str, str]]] = {}

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


def _find_channel(channels: list[dict], query: str) -> dict | None:
    if query.isdigit():
        num = int(query)
        match = next((c for c in channels if c.get("number") == num), None)
        if match:
            return match
    q = query.lower()
    return next((c for c in channels if q in c.get("name", "").lower()), None)


def _find_iptv_channel(channels: list[dict], query: str) -> dict | None:
    q = query.lower()
    return next((c for c in channels if q in c.get("name", "").lower()), None)


def _get_display_tz() -> _TZInfo:
    tz_name = os.environ.get("TIMEZONE", "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            log.warning("TIMEZONE=%r is not a valid IANA timezone; falling back to local", tz_name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def _fmt_time(ts: float) -> str:
    """Format a Unix timestamp as a human-readable time in the configured timezone."""
    tz = _get_display_tz()
    return datetime.fromtimestamp(ts, tz=tz).strftime("%I:%M %p").lstrip("0")


class TV(commands.Cog):
    def __init__(self, bot: commands.Bot, tvh: TVheadendClient | None):
        self.bot = cast("SlopSoil", bot)
        self.tvh = tvh
        self._schedule_tasks: dict[int, asyncio.Task] = {}

    def _tvh_active(self) -> bool:
        """Whether TVheadend should be queried for the current request.

        True only when a client is configured (credentials present) and the
        source toggle (set via !sources enable/disable tvheadend) is on. Every
        TVheadend-specific code path is guarded by this, so !play/!channels/
        !search degrade to yt-dlp and IPTV when TVheadend is absent.
        """
        if self.tvh is None:
            return False
        sm = getattr(self.bot, "source_manager", None)
        return sm.tvh_enabled if sm else True

    # ── Stream / schedule lifecycle ───────────────────────────────────────────

    def _cancel_schedule(self, guild_id: int) -> None:
        task = self._schedule_tasks.pop(guild_id, None)
        if task and not task.done():
            log.info("cancelling scheduled play for guild %s", guild_id)
            task.cancel()

    async def _start_stream(
        self,
        send: Callable,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        vc: discord.VoiceClient | None,
        name: str,
        url: str,
        subtitle: str = "",
        live: bool | None = True,
        audio: bool = True,
        probe_size: int = 2_000_000,
    ) -> None:
        await start_live_stream(
            self.bot,
            send,
            guild,
            voice_channel,
            vc,
            title=name,
            url=url,
            subtitle=subtitle,
            live=live,
            audio=audio,
            probe_size=probe_size,
        )

    async def _start_iptv_stream(
        self,
        send: Callable,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        vc: discord.VoiceClient | None,
        name: str,
        url: str,
        subtitle: str = "",
    ) -> None:
        """Probe, validate, and start an IPTV stream.

        Always transcodes to baseline H.264 (live=True) so Discord receives a
        compatible bitstream regardless of the source profile or codec.  Bitstream
        copy is not used because IPTV sources often encode with B-frames (Main/High
        profile) which, passed through unchanged, cause Discord to display only a
        single static frame due to out-of-display-order B-frames conflicting with
        our max_num_reorder_frames=0 SPS patch.
        """
        await send("checking stream…")

        # For HLS master playlists, resolve to the variant URL first.
        # thetvapp.to-style streams declare a separate audio rendition group
        # (mono.m3u8) in the master that frequently returns 500; the TS segments
        # themselves carry embedded audio (tracks-v1a1 = video+audio in one mux).
        # Giving FFmpeg the variant URL directly bypasses the rendition groups.
        stream_url = await _extract_hls_variant_url(url)

        info = await _probe_stream(stream_url)
        if info is None:
            await send(
                f"could not reach the stream for **{name}** — "
                "the URL may be down or protected"
            )
            return

        codec = info["codec"]
        profile = info.get("profile", "unknown")
        res = f"{info['width']}x{info['height']}" if info.get("width") else "unknown"
        fps = info["fps"]
        has_audio = info.get("has_audio", True)
        log.info(
            "IPTV probe: '%s' → codec=%s profile=%s"
            " %s %.3ffps b_frames=%s audio=%s(%s)",
            name, codec, profile, res, fps, info["has_b_frames"],
            has_audio, info.get("audio_codec") or "none",
        )

        if codec not in ("h264", "hevc", "mpeg2video", "mpeg4", "mjpeg"):
            await send(
                f"unsupported video codec `{codec}` in **{name}** — cannot stream"
            )
            return

        if not has_audio:
            log.info("IPTV stream '%s' has no audio — injecting silence", name)

        await self._start_stream(
            send, guild, voice_channel, vc, name, stream_url, subtitle,
            live=True, audio=has_audio, probe_size=10_000_000,
        )

    # ── Commands ──────────────────────────────────────────────────────────────

    @require_role(Role.VIEWER)
    @commands.command()
    async def channels(self, ctx: commands.Context):
        """List all enabled channels (TVheadend + IPTV) with what's currently airing."""
        log.info("fetching channel list for %s in guild '%s'", ctx.author, ctx.guild)

        sm = getattr(self.bot, "source_manager", None)

        lines: list[str] = []

        if self._tvh_active():
            # Fetch TVheadend channel list and now-playing EPG in parallel.
            results = await asyncio.gather(
                self.tvh.get_channels(),
                self.tvh.get_now_playing(),
                return_exceptions=True,
            )

            if isinstance(results[0], BaseException):
                # TVheadend is unreachable, but IPTV may still have channels —
                # warn and fall through to the IPTV listing instead of bailing.
                exc = results[0]
                if isinstance(exc, urllib.error.URLError):
                    log.warning(
                        "could not reach TVheadend at %s: %s", self.tvh.base_url, exc
                    )
                    await ctx.send(f"could not reach TVheadend (showing IPTV only): {exc}")
                else:
                    log.warning("unexpected error fetching channels: %s", exc)
                    await ctx.send(f"failed to fetch TVheadend channels (showing IPTV only): {exc}")
            else:
                chs: list[dict] = results[0]

                if isinstance(results[1], BaseException):
                    log.warning(
                        "EPG fetch failed, showing channels without now-playing: %s",
                        results[1],
                    )
                    now_playing: dict[str, str] = {}
                else:
                    now_playing = results[1]

                log.info(
                    "sending channel list (%d TVH channels, %d with now-playing) to %s",
                    len(chs),
                    len(now_playing),
                    ctx.author,
                )

                for c in chs:
                    num = c.get("number")
                    name = c.get("name", "(unnamed)")
                    title = now_playing.get(c.get("uuid", ""), "")
                    if num is not None:
                        prefix = f"{num:>4}  "
                    else:
                        prefix = ""
                    if title:
                        lines.append(f"{prefix}{name[:25]:<25}  ▶ {title[:35]}")
                    else:
                        lines.append(f"{prefix}{name}")

        iptv_channels = sm.get_iptv_channels() if sm else []
        if iptv_channels:
            # Build a {tvg_id: title} map from cached/fresh XMLTV EPG for all
            # enabled sources that advertise a url-tvg in their M3U header.
            iptv_now_playing: dict[str, str] = {}
            if sm:
                for src_name, epg_url in sm.get_epg_sources():
                    cached_ts, cached_data = _epg_cache.get(src_name, (0.0, {}))
                    if time.time() - cached_ts < 900:
                        iptv_now_playing.update(cached_data)
                    else:
                        try:
                            data = await _fetch_xmltv_now_playing(epg_url)
                            _epg_cache[src_name] = (time.time(), data)
                            iptv_now_playing.update(data)
                            log.info(
                                "EPG refreshed for '%s': %d current programmes",
                                src_name, len(data),
                            )
                        except Exception as exc:
                            log.warning(
                                "failed to fetch EPG for '%s': %s", src_name, exc
                            )
                            if cached_data:
                                iptv_now_playing.update(cached_data)

            by_source: dict[str, list[dict]] = {}
            for ch in iptv_channels:
                by_source.setdefault(ch["source"], []).append(ch)
            for source_name, source_chs in by_source.items():
                lines.append(f"--- {source_name} ---")
                for ch in source_chs:
                    group = ch.get("group", "")
                    tvg_id = ch.get("tvg_id", "")
                    now = iptv_now_playing.get(tvg_id, "") if tvg_id else ""
                    suffix = f"  [{group}]" if group else ""
                    if now:
                        lines.append(f"  {ch['name'][:25]:<25}  ▶ {now[:35]}{suffix}")
                    else:
                        lines.append(f"  {ch['name']}{suffix}")

        if not lines:
            await ctx.send("no channels found")
            return

        # Split lines into 1800-char pages.
        pages: list[str] = []
        chunk: list[str] = []
        chunk_chars = 0
        for line in lines:
            if chunk_chars + len(line) + 1 > 1800 and chunk:
                pages.append("```\n" + "\n".join(chunk) + "\n```")
                chunk = []
                chunk_chars = 0
            chunk.append(line)
            chunk_chars += len(line) + 1
        if chunk:
            pages.append("```\n" + "\n".join(chunk) + "\n```")

        await ctx.send(pages[0])

        for page in pages[1:]:
            prompt_msg = await ctx.send(
                "more channels available — see next page? (yes/no)"
            )

            def is_reply(m: discord.Message) -> bool:
                return m.author == ctx.author and m.channel == ctx.channel

            try:
                reply = await self.bot.wait_for("message", check=is_reply, timeout=30)
            except TimeoutError:
                await prompt_msg.edit(content="channels: timed out waiting for reply")
                return

            # Any non-yes reply (including another command) stops pagination.
            if reply.content.strip().lower() not in ("yes", "y"):
                await ctx.send("ok, stopping here")
                return

            await ctx.send(page)

    @require_role(Role.FRIEND)
    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str):
        """
        Stream a channel or URL into your voice channel.
        Pass a URL to download and stream via yt-dlp (!play https://...).
        Match a TVheadend/IPTV channel by number or name (!play BBC One).
        """
        guild, voice_channel, vc = await resolve_voice(ctx)

        if not voice_channel:
            log.debug(
                "play rejected: %s is not in a voice channel (guild: %s)",
                ctx.author,
                guild,
            )
            await ctx.send("you're not in a voice channel")
            return

        assert guild is not None

        if query.startswith(("http://", "https://")):
            log.info(
                "play: URL detected (requested by %s in guild '%s')",
                ctx.author,
                guild,
            )
            status = await ctx.send("checking…")

            # CGI URLs are direct HTTP video feeds — probe and stream immediately.
            parsed_path = urlparse(query).path.lower()
            if parsed_path.endswith(".cgi"):
                title = parsed_path.rsplit("/", 1)[-1]
                log.info("play: CGI stream '%s' (requested by %s)", title, ctx.author)
                self._cancel_schedule(guild.id)
                await self._start_iptv_stream(
                    ctx.send, guild, voice_channel, vc, title, query
                )
                return

            # Check for a live broadcast before attempting a full download.
            try:
                live_result = await _yt_extract_live_url(query)
            except Exception as exc:
                log.warning("live-check failed for %r: %s", query, exc)
                live_result = None

            if live_result is not None:
                stream_url, title = live_result
                log.info("play: live stream '%s' (requested by %s)", title, ctx.author)
                await status.edit(content=f"starting **{title}**…")
                self._cancel_schedule(guild.id)
                await self._start_iptv_stream(
                    ctx.send, guild, voice_channel, vc, title, stream_url
                )
                return

            # Not a live broadcast: download then play.
            await status.edit(content="downloading…")
            tmp_dir = tempfile.mkdtemp(prefix="slopsoil_yt_")
            try:
                try:
                    file_path, title = await _yt_download(query, tmp_dir)
                except Exception as exc:
                    log.exception("yt-dlp download failed for %r: %s", query, exc)
                    await status.edit(content=f"download failed: {exc}")
                    _yt_remove_dir(tmp_dir)
                    return

                log.info("yt-dlp downloaded '%s' → %s", title, file_path)
                await status.edit(content=f"starting **{title}**…")

                await start_live_stream(
                    self.bot, ctx.send, guild, voice_channel, vc,
                    title=title, url=file_path, live=False, audio=True,
                    probe_size=2_000_000,
                )

                stream_task = self.bot.stream_tasks.get(guild.id)
                if stream_task:
                    asyncio.create_task(
                        _yt_cleanup_after_stream(stream_task, tmp_dir),
                        name=f"yt-cleanup-{guild.id}",
                    )
                else:
                    _yt_remove_dir(tmp_dir)
            except Exception:
                _yt_remove_dir(tmp_dir)
                raise
            return

        log.info(
            "looking up channel for query %r (requested by %s in guild '%s')",
            query,
            ctx.author,
            guild,
        )

        sm = getattr(self.bot, "source_manager", None)

        chs: list[dict] = []
        if self._tvh_active():
            # TVheadend errors here are non-fatal: leave chs empty and fall
            # through to the IPTV lookup, which may still match the query.
            try:
                chs = await self.tvh.get_channels()
            except urllib.error.URLError as exc:
                log.warning(
                    "could not reach TVheadend at %s: %s", self.tvh.base_url, exc
                )
            except Exception as exc:
                log.warning("unexpected error fetching TVheadend channels: %s", exc)

            channel = _find_channel(chs, query)
            if channel:
                name = channel.get("name", "?")
                number = channel.get("number", "?")
                uuid = channel["uuid"]
                log.info(
                    "matched TVH channel: '%s' (#%s, uuid: %s)", name, number, uuid
                )
                self._cancel_schedule(guild.id)
                url = self.tvh.stream_url(uuid)
                await self._start_stream(
                    ctx.send, guild, voice_channel, vc, name, url, f"#{number}"
                )
                return

        iptv_ch = _find_iptv_channel(sm.get_iptv_channels() if sm else [], query)
        if iptv_ch:
            name = iptv_ch.get("name", "?")
            source = iptv_ch.get("source", "IPTV")
            log.info("matched IPTV channel: '%s' (source: %s)", name, source)
            self._cancel_schedule(guild.id)
            await self._start_iptv_stream(
                ctx.send, guild, voice_channel, vc, name, iptv_ch["stream_url"], source
            )
            return

        log.info(
            "no channel matched query %r (searched %d TVH + IPTV)", query, len(chs)
        )
        await ctx.send(
            f"channel not found: `{query}`"
            " — use `!channels` to see what's available"
        )

    @require_role(Role.FRIEND)
    @commands.command()
    async def search(self, ctx: commands.Context, *, query: str):
        """
        Search the TV guide by show title.
        If the show is on now, switches to it immediately.
        If it's coming up (within 24 h), offers to schedule the stream
        to start 30 seconds before airtime.
        """
        guild, voice_channel, vc = await resolve_voice(ctx)

        if not voice_channel:
            await ctx.send("you're not in a voice channel")
            return

        assert guild is not None
        log.info("EPG search for %r by %s in guild '%s'", query, ctx.author, guild)

        sm = getattr(self.bot, "source_manager", None)

        events: list[dict] = []
        if self._tvh_active():
            # TVheadend EPG errors are non-fatal: leave events empty and fall
            # through to the IPTV lookup below.
            try:
                events = await self.tvh.get_epg_events(query)
            except urllib.error.URLError as exc:
                log.warning("could not reach TVheadend EPG: %s", exc)
            except Exception as exc:
                log.warning("unexpected error searching TVheadend EPG: %s", exc)

        if not events:
            iptv_ch = _find_iptv_channel(sm.get_iptv_channels() if sm else [], query)
            if iptv_ch:
                name = iptv_ch.get("name", "?")
                source = iptv_ch.get("source", "IPTV")
                log.info(
                    "EPG: no results; matched IPTV channel '%s' (source: %s)",
                    name, source,
                )
                self._cancel_schedule(guild.id)
                await self._start_iptv_stream(
                    ctx.send, guild, voice_channel, vc, name,
                    iptv_ch["stream_url"], source,
                )
                return
            await ctx.send(f"nothing found in the TV guide for `{query}`")
            return

        now = time.time()
        horizon = now + 24 * 3600

        airing = [e for e in events if e.get("start", 0) <= now < e.get("stop", 0)]
        upcoming = sorted(
            [e for e in events if now < e.get("start", 0) <= horizon],
            key=lambda e: e["start"],
        )

        # ── Currently airing: play immediately ────────────────────────────────
        if airing:
            event = airing[0]
            ch_name = event.get("channelName", "?")
            ch_number = event.get("channelNumber", "?")
            uuid = event.get("channelUuid", "")
            show = event.get("title", query)
            if not uuid:
                await ctx.send(
                    f"**{show}** is airing on **{ch_name}** but its channel UUID"
                    f" is missing — try `!play {ch_name}` instead"
                )
                return
            log.info("EPG: '%s' is airing now on '%s'", show, ch_name)
            self._cancel_schedule(guild.id)
            url = self.tvh.stream_url(uuid)
            await self._start_stream(
                ctx.send, guild, voice_channel, vc, ch_name, url, f"#{ch_number}"
            )
            return

        # ── Nothing in the next 24 h ──────────────────────────────────────────
        if not upcoming:
            await ctx.send(f"**{query}** isn't in the guide for the next 24 hours.")
            return

        # ── Upcoming: ask to schedule ─────────────────────────────────────────
        event = upcoming[0]
        show = event.get("title", query)
        ch_name = event.get("channelName", "?")
        ch_number = event.get("channelNumber", "?")
        uuid = event.get("channelUuid", "")
        start_ts: float = event["start"]
        start_str = _fmt_time(start_ts)

        if not uuid:
            await ctx.send(
                f"**{show}** is on **{ch_name}** at {start_str} but its channel UUID "
                f"is missing — try `!play {ch_name}` manually when the time comes"
            )
            return

        await ctx.send(
            f"**{show}** is on **{ch_name}** (#{ch_number}) at {start_str}. "
            f"Schedule a viewing? (y/n)"
        )

        def _yn_check(msg: discord.Message) -> bool:
            content = msg.content.strip()
            return (
                msg.author.id == ctx.author.id
                and msg.channel.id == ctx.channel.id
                and (content.lower() in ("y", "yes", "n", "no") or content.startswith("!"))
            )

        try:
            reply = await self.bot.wait_for("message", check=_yn_check, timeout=30.0)
        except TimeoutError:
            await ctx.send("no response — schedule cancelled")
            return

        content = reply.content.strip()
        if content.startswith("!"):
            await ctx.send("schedule cancelled")
            return
        if content.lower() not in ("y", "yes"):
            await ctx.send("ok, not scheduling")
            return

        # Start 30 s early so the stream is stable when the show begins
        delay = max(0.0, start_ts - 30 - time.time())
        stream_url = self.tvh.stream_url(uuid)

        self._cancel_schedule(guild.id)

        # If the window has already passed, start right now
        if delay < 5:
            await self._start_stream(
                ctx.send, guild, voice_channel, vc, ch_name, stream_url, f"#{ch_number}"
            )
            return

        mins, secs = divmod(int(delay), 60)
        wait_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        await ctx.send(
            f"Scheduled! I'll switch to **{ch_name}** for **{show}** in {wait_str}."
        )
        log.info(
            "scheduled '%s' on '%s' in %.0f s for guild %s",
            show,
            ch_name,
            delay,
            guild.id,
        )

        text_channel = ctx.channel
        user_id = ctx.author.id
        guild_id = guild.id

        async def _scheduled_play() -> None:
            try:
                await asyncio.sleep(delay)
                self._schedule_tasks.pop(guild_id, None)

                g = self.bot.get_guild(guild_id)
                if not g:
                    return

                member = g.get_member(user_id)
                if not member or not member.voice or not member.voice.channel:
                    log.warning(
                        "scheduled play for guild %s: user %s not in a voice channel",
                        guild_id,
                        user_id,
                    )
                    try:
                        await text_channel.send(
                            "Scheduled stream cancelled"
                            " — you're not in a voice channel."
                        )
                    except Exception:
                        pass
                    return

                vc_now = cast(discord.VoiceClient | None, g.voice_client)
                vc_channel_now = cast(
                    discord.VoiceChannel | discord.StageChannel,
                    member.voice.channel,
                )

                async def _send(content: str, **kwargs) -> None:
                    try:
                        await text_channel.send(content, **kwargs)
                    except Exception:
                        pass

                log.info(
                    "scheduled play firing: '%s' on '%s' for guild %s",
                    show,
                    ch_name,
                    guild_id,
                )
                await self._start_stream(
                    _send, g, vc_channel_now, vc_now,
                    ch_name, stream_url, f"#{ch_number}",
                )
            except asyncio.CancelledError:
                log.info("scheduled play cancelled for guild %s", guild_id)

        task = asyncio.create_task(_scheduled_play())
        self._schedule_tasks[guild_id] = task


async def setup(bot: commands.Bot):
    tvh = TVheadendClient.from_env()
    if tvh is not None:
        log.info("TVheadend client configured for %s", tvh.base_url)
    else:
        log.info(
            "TVheadend not configured — !play/!channels/!search use yt-dlp + IPTV only"
        )
    await bot.add_cog(TV(bot, tvh))
