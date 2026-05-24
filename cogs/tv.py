from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, urlparse, urlunparse

import discord
from discord.ext import commands

from cogs.iptv import extract_hls_variant_url as _extract_hls_variant_url
from cogs.iptv import fetch_xmltv_now_playing as _fetch_xmltv_now_playing
from cogs.iptv import probe_stream as _probe_stream

# {source_name: (fetched_at, {tvg_id: title})} — refreshed every 15 minutes
_epg_cache: dict[str, tuple[float, dict[str, str]]] = {}
from cogs.stream import start_live_stream
from cogs.utils import resolve_voice

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


class TVheadendClient:
    def __init__(self, url: str, user: str, password: str):
        self.base_url = url.rstrip("/")
        self.user = user
        self.password = password
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {creds}"
        self._now_playing_cache: tuple[float, dict[str, str]] = (0.0, {})

    async def get_channels(self) -> list[dict]:
        def _fetch():
            endpoint = f"{self.base_url}/api/channel/grid?limit=99999"
            log.debug("fetching channel grid from %s", endpoint)
            req = urllib.request.Request(
                endpoint, headers={"Authorization": self._auth}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            all_entries = data.get("entries", [])
            enabled = [e for e in all_entries if e.get("enabled", True)]
            log.debug(
                "TVheadend returned %d total channels, %d enabled",
                len(all_entries),
                len(enabled),
            )
            return enabled

        entries = await asyncio.to_thread(_fetch)
        if entries:
            log.info("sample channel entry (first result): %s", entries[0])
        return sorted(entries, key=lambda c: c.get("number", 999_999))

    async def get_epg_events(self, query: str, limit: int = 100) -> list[dict]:
        """Return EPG events whose title contains query (case-insensitive)."""

        def _fetch():
            endpoint = (
                f"{self.base_url}/api/epg/events/grid"
                f"?limit={limit}&title={quote(query, safe='')}"
            )
            log.debug("fetching EPG events from %s", endpoint)
            req = urllib.request.Request(
                endpoint, headers={"Authorization": self._auth}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return data.get("entries", [])

        entries = await asyncio.to_thread(_fetch)
        # TVheadend may return inexact matches; filter client-side too
        q = query.lower()
        return [e for e in entries if q in e.get("title", "").lower()]

    async def get_now_playing(self) -> dict[str, str]:
        """
        Return {channel_uuid: programme_title} for all currently airing events.

        Fetches up to 10 000 EPG events (TVheadend returns them sorted by start
        time ascending) and keeps only the ones whose window contains the current
        moment.  For typical personal setups (≤600 channels with a 1-day past-EPG
        window) this single request is sufficient.  If the EPG is unavailable or
        returns no matches the dict is simply empty — callers degrade gracefully.

        Results are cached for 60 seconds so repeated !channels calls don't hammer
        the EPG endpoint.
        """
        cached_ts, cached_data = self._now_playing_cache
        if time.time() - cached_ts < 60:
            log.debug("now-playing: returning cached data (%d entries)", len(cached_data))
            return cached_data

        def _fetch() -> dict[str, str]:
            now = time.time()
            endpoint = f"{self.base_url}/api/epg/events/grid?limit=10000"
            log.debug("fetching now-playing EPG from %s", endpoint)
            req = urllib.request.Request(
                endpoint, headers={"Authorization": self._auth}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            result: dict[str, str] = {}
            for e in data.get("entries", []):
                if e.get("start", 0) <= now < e.get("stop", 0):
                    uuid = e.get("channelUuid", "")
                    if uuid and uuid not in result:
                        result[uuid] = e.get("title", "")
            log.debug(
                "now-playing: %d/%d EPG events matched current time",
                len(result),
                len(data.get("entries", [])),
            )
            return result

        result = await asyncio.to_thread(_fetch)
        self._now_playing_cache = (time.time(), result)
        return result

    def stream_url(self, uuid: str) -> str:
        parsed = urlparse(self.base_url)
        netloc = (
            f"{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{parsed.hostname}"
        )
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(
            (parsed.scheme, netloc, f"/stream/channel/{uuid}", "", "", "")
        )

    def safe_stream_url(self, uuid: str) -> str:
        """Stream URL with password redacted — safe to log."""
        parsed = urlparse(self.base_url)
        netloc = f"{self.user}:***@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(
            (parsed.scheme, netloc, f"/stream/channel/{uuid}", "", "", "")
        )


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


def _fmt_time(ts: float) -> str:
    """Format a Unix timestamp as a human-readable local time (e.g. '7:30 PM')."""
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


class TV(commands.Cog):
    def __init__(self, bot: commands.Bot, tvh: TVheadendClient):
        self.bot = cast("SlopSoil", bot)
        self.tvh = tvh
        self._schedule_tasks: dict[int, asyncio.Task] = {}

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
        await send(f"checking stream…")

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
            "IPTV probe: '%s' → codec=%s profile=%s %s %.3ffps b_frames=%s audio=%s(%s)",
            name, codec, profile, res, fps, info["has_b_frames"],
            has_audio, info.get("audio_codec") or "none",
        )

        if codec not in ("h264", "hevc", "mpeg2video", "mpeg4"):
            await send(
                f"unsupported video codec `{codec}` in **{name}** — cannot stream"
            )
            return

        if not has_audio:
            log.info("IPTV stream '%s' has no audio — injecting silence", name)

        await self._start_stream(send, guild, voice_channel, vc, name, stream_url, subtitle, live=True, audio=has_audio, probe_size=10_000_000)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command()
    async def channels(self, ctx: commands.Context):
        """List all enabled channels (TVheadend + IPTV) with what's currently airing."""
        log.info("fetching channel list for %s in guild '%s'", ctx.author, ctx.guild)

        sm = getattr(self.bot, "source_manager", None)
        tvh_enabled = sm.tvh_enabled if sm else True

        lines: list[str] = []

        if tvh_enabled:
            # Fetch TVheadend channel list and now-playing EPG in parallel.
            results = await asyncio.gather(
                self.tvh.get_channels(),
                self.tvh.get_now_playing(),
                return_exceptions=True,
            )

            if isinstance(results[0], BaseException):
                exc = results[0]
                if isinstance(exc, urllib.error.URLError):
                    log.exception(
                        "could not reach TVheadend at %s: %s", self.tvh.base_url, exc
                    )
                    await ctx.send(f"could not reach TVheadend: {exc}")
                else:
                    log.exception("unexpected error fetching channels: %s", exc)
                    await ctx.send(f"failed to fetch channels: {exc}")
                return

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
                            log.warning("failed to fetch EPG for '%s': %s", src_name, exc)
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
                f"more channels available — see next page? (yes/no)"
            )

            def is_reply(m: discord.Message) -> bool:
                return m.author == ctx.author and m.channel == ctx.channel

            try:
                reply = await self.bot.wait_for("message", check=is_reply, timeout=30)
            except asyncio.TimeoutError:
                await prompt_msg.edit(content="channels: timed out waiting for reply")
                return

            # Any non-yes reply (including another command) stops pagination.
            if reply.content.strip().lower() not in ("yes", "y"):
                await ctx.send("ok, stopping here")
                return

            await ctx.send(page)

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str):
        """
        Stream a TVheadend channel (audio + video) into your voice channel.
        Match by channel number (!play 1) or name (!play BBC One).
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
        log.info(
            "looking up channel for query %r (requested by %s in guild '%s')",
            query,
            ctx.author,
            guild,
        )

        sm = getattr(self.bot, "source_manager", None)
        tvh_enabled = sm.tvh_enabled if sm else True

        chs: list[dict] = []
        if tvh_enabled:
            try:
                chs = await self.tvh.get_channels()
            except urllib.error.URLError as exc:
                log.exception("could not reach TVheadend at %s: %s", self.tvh.base_url, exc)
                await ctx.send(f"could not reach TVheadend: {exc}")
                return
            except Exception as exc:
                log.exception("unexpected error fetching channels: %s", exc)
                await ctx.send(f"failed to fetch channels: {exc}")
                return

            channel = _find_channel(chs, query)
            if channel:
                name = channel.get("name", "?")
                number = channel.get("number", "?")
                uuid = channel["uuid"]
                log.info("matched TVH channel: '%s' (#%s, uuid: %s)", name, number, uuid)
                self._cancel_schedule(guild.id)
                url = self.tvh.stream_url(uuid)
                await self._start_stream(ctx.send, guild, voice_channel, vc, name, url, f"#{number}")
                return

        iptv_ch = _find_iptv_channel(sm.get_iptv_channels() if sm else [], query)
        if iptv_ch:
            name = iptv_ch.get("name", "?")
            source = iptv_ch.get("source", "IPTV")
            log.info("matched IPTV channel: '%s' (source: %s)", name, source)
            self._cancel_schedule(guild.id)
            await self._start_iptv_stream(ctx.send, guild, voice_channel, vc, name, iptv_ch["stream_url"], source)
            return

        log.info("no channel matched query %r (searched %d TVH + IPTV)", query, len(chs))
        await ctx.send(
            f"channel not found: `{query}`"
            " — use `!channels` to see what's available"
        )

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
        tvh_enabled = sm.tvh_enabled if sm else True

        events: list[dict] = []
        if tvh_enabled:
            try:
                events = await self.tvh.get_epg_events(query)
            except urllib.error.URLError as exc:
                log.exception("could not reach TVheadend EPG: %s", exc)
                await ctx.send(f"could not reach TVheadend: {exc}")
                return
            except Exception as exc:
                log.exception("unexpected error searching EPG: %s", exc)
                await ctx.send(f"EPG search failed: {exc}")
                return

        if not events:
            iptv_ch = _find_iptv_channel(sm.get_iptv_channels() if sm else [], query)
            if iptv_ch:
                name = iptv_ch.get("name", "?")
                source = iptv_ch.get("source", "IPTV")
                log.info("EPG: no results; matched IPTV channel '%s' (source: %s)", name, source)
                self._cancel_schedule(guild.id)
                await self._start_iptv_stream(ctx.send, guild, voice_channel, vc, name, iptv_ch["stream_url"], source)
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
            return (
                msg.author.id == ctx.author.id
                and msg.channel.id == ctx.channel.id
                and msg.content.strip().lower() in ("y", "yes", "n", "no")
            )

        try:
            reply = await self.bot.wait_for("message", check=_yn_check, timeout=60.0)
        except TimeoutError:
            await ctx.send("no response — schedule cancelled")
            return

        if reply.content.strip().lower() not in ("y", "yes"):
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
                    _send, g, vc_channel_now, vc_now, ch_name, stream_url, f"#{ch_number}"
                )
            except asyncio.CancelledError:
                log.info("scheduled play cancelled for guild %s", guild_id)

        task = asyncio.create_task(_scheduled_play())
        self._schedule_tasks[guild_id] = task


async def setup(bot: commands.Bot):
    tvh = TVheadendClient(
        url=os.environ["TVHEADEND_URL"],
        user=os.environ["TVHEADEND_USER"],
        password=os.environ["TVHEADEND_PASS"],
    )
    log.info("TVheadend client configured for %s", tvh.base_url)
    await bot.add_cog(TV(bot, tvh))
