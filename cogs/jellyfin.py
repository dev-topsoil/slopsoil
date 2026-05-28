from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, cast

from discord.ext import commands

from cogs.iptv import extract_hls_variant_url as _extract_hls_variant_url
from cogs.iptv import probe_stream as _probe_stream
from cogs.stream import start_live_stream
from cogs.utils import resolve_voice
from permissions import Role, require_role

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)

_DEVICE_ID = "slopsoil-discord-bot"


class JellyfinClient:
    def __init__(self, url: str, api_key: str) -> None:
        self.base_url = url.rstrip("/")
        self._api_key = api_key
        self._headers = {
            "Authorization": f'MediaBrowser Token="{api_key}"',
            "Content-Type": "application/json",
        }
        self._user_id: str | None = None  # cached after first fetch

    async def _get_user_id(self) -> str | None:
        """Fetch and cache the first Jellyfin user ID.

        PlaybackInfo requires a UserId even with API key auth — without it
        Jellyfin returns 400.  We fetch the user list once and cache the result.
        """
        if self._user_id is not None:
            return self._user_id

        def _fetch() -> list:
            req = urllib.request.Request(
                f"{self.base_url}/Users",
                headers=self._headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        try:
            users = await asyncio.to_thread(_fetch)
            if users:
                self._user_id = users[0]["Id"]
                log.info("Jellyfin: resolved user ID %s", self._user_id)
                return self._user_id
        except Exception as exc:
            log.warning("Jellyfin: could not fetch user list: %s", exc)
        return None

    async def get_stream_url(self, item_id: str) -> str | None:
        """Start a Jellyfin playback session and return the HLS stream URL.

        Uses POST /Items/{id}/PlaybackInfo with a DeviceProfile that requests
        H.264/AAC HLS output.  Jellyfin validates the session, picks the best
        media source, and returns a server-generated transcoding URL with the
        correct PlaySessionId and MediaSourceId baked in.

        The API key is appended to the returned URL so FFmpeg can authenticate
        when fetching HLS segments directly (it does not forward our headers).
        """
        user_id = await self._get_user_id()

        def _fetch() -> dict | None:
            payload: dict = {
                # -1 tells Jellyfin not to select any subtitle stream.
                # Combined with an empty SubtitleProfiles list this prevents
                # both soft-subtitle tracks and burned-in subtitles.
                "SubtitleStreamIndex": -1,
                "DeviceProfile": {
                    "MaxStreamingBitrate": 8_000_000,
                    "TranscodingProfiles": [
                        {
                            "Container": "ts",
                            "Type": "Video",
                            "Protocol": "hls",
                            "AudioCodec": "aac",
                            "VideoCodec": "h264",
                            "MaxAudioChannels": 2,
                            "Context": "Streaming",
                        }
                    ],
                    "DirectPlayProfiles": [],
                    "CodecProfiles": [],
                    "SubtitleProfiles": [],
                },
            }
            if user_id:
                payload["UserId"] = user_id

            body = json.dumps(payload).encode()
            params: dict[str, str] = {"DeviceId": _DEVICE_ID}
            if user_id:
                params["userId"] = user_id
            req = urllib.request.Request(
                f"{self.base_url}/Items/{item_id}/PlaybackInfo?{urllib.parse.urlencode(params)}",
                data=body,
                headers=self._headers,
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())

        try:
            result = await asyncio.to_thread(_fetch)
        except Exception as exc:
            log.warning("PlaybackInfo failed for item %s: %s", item_id, exc)
            return None

        sources = result.get("MediaSources", [])
        if not sources:
            log.warning("PlaybackInfo returned no media sources for item %s", item_id)
            return None

        # Prefer the HLS transcoding URL — Jellyfin outputs H.264/AAC.
        # Fall back to direct stream if transcoding is unavailable.
        raw = sources[0].get("TranscodingUrl") or sources[0].get("DirectStreamUrl")
        if not raw:
            log.warning("No stream URL in PlaybackInfo for item %s", item_id)
            return None

        # PlaybackInfo returns a relative path; make it absolute.
        full = raw if raw.startswith(("http://", "https://")) else f"{self.base_url}{raw}"

        # Force SubtitleStreamIndex=-1 in the URL. PlaybackInfo can populate this
        # from the user's server-side profile, overriding the -1 we sent in the
        # request body.  Rewriting it here is the only reliable way to guarantee
        # Jellyfin does not burn subtitles into the transcoded video.
        parsed = urllib.parse.urlparse(full)
        qp = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        qp["SubtitleStreamIndex"] = "-1"
        full = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(qp)))

        # Append api_key so FFmpeg/ffprobe can authenticate for HLS segment fetches.
        return f"{full}&api_key={self._api_key}"

    async def search(self, query: str, limit: int = 25) -> list[dict]:
        """Search Jellyfin for movies, series, and episodes matching query."""
        def _fetch() -> list[dict]:
            params = urllib.parse.urlencode({
                "searchTerm": query,
                "Recursive": "true",
                "Limit": str(limit),
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": "ProductionYear,SeriesName,SeasonName,IndexNumber,ParentIndexNumber",
            })
            req = urllib.request.Request(
                f"{self.base_url}/Items?{params}",
                headers=self._headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return data.get("Items", [])

        return await asyncio.to_thread(_fetch)

    async def find_episode(self, show: str, season: int, episode: int) -> list[dict]:
        """Find a specific episode by series name, season number, and episode number.

        Searches for the series by name first, then walks seasons → episodes using
        the series ID.  This is necessary because Jellyfin's searchTerm matches
        episode titles, not series names, so a direct episode search with searchTerm
        always returns nothing for series-name queries.
        """
        series_results = await self.search(show)
        series_list = [r for r in series_results if r.get("Type") == "Series"]
        if not series_list:
            return []

        matches: list[dict] = []
        for series in series_list:
            seasons = await self.get_seasons(series["Id"])
            target = next((s for s in seasons if s.get("IndexNumber") == season), None)
            if target is None:
                continue
            episodes = await self.get_episodes(series["Id"], target["Id"])
            matches.extend(e for e in episodes if e.get("IndexNumber") == episode)
        return matches

    async def get_seasons(self, series_id: str) -> list[dict]:
        """Return all seasons for a series, ordered by IndexNumber."""
        def _fetch() -> list[dict]:
            params = urllib.parse.urlencode({"Fields": "Id,Name,IndexNumber"})
            req = urllib.request.Request(
                f"{self.base_url}/Shows/{series_id}/Seasons?{params}",
                headers=self._headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            seasons = data.get("Items", [])
            return sorted(seasons, key=lambda s: s.get("IndexNumber") or 0)

        return await asyncio.to_thread(_fetch)

    async def get_episodes(self, series_id: str, season_id: str) -> list[dict]:
        """Return all episodes for a season, ordered by IndexNumber."""
        def _fetch() -> list[dict]:
            params = urllib.parse.urlencode({
                "SeasonId": season_id,
                "Fields": "Id,Name,IndexNumber,ParentIndexNumber,SeriesName",
            })
            req = urllib.request.Request(
                f"{self.base_url}/Shows/{series_id}/Episodes?{params}",
                headers=self._headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            episodes = data.get("Items", [])
            return sorted(episodes, key=lambda e: e.get("IndexNumber") or 0)

        return await asyncio.to_thread(_fetch)


_EPISODE_RE = re.compile(r"\bs(\d+)e(\d+)\b", re.IGNORECASE)


def _parse_episode_query(query: str) -> tuple[str, int | None, int | None]:
    """Split 'show name s02e01' into ('show name', 2, 1). Returns (query, None, None) if no match."""
    m = _EPISODE_RE.search(query)
    if not m:
        return query, None, None
    show = query[: m.start()].strip()
    return show, int(m.group(1)), int(m.group(2))


def _fmt_item(item: dict) -> str:
    """Return a human-readable label for a Jellyfin item."""
    name = item.get("Name", "Unknown")
    kind = item.get("Type", "")
    year = item.get("ProductionYear")

    if kind == "Episode":
        series = item.get("SeriesName", "")
        season = item.get("ParentIndexNumber")
        ep = item.get("IndexNumber")
        ep_str = f"S{season:02d}E{ep:02d}" if season and ep else ""
        return f"{series} — {ep_str} — {name}" if ep_str else f"{series} — {name}"

    return f"{name} ({year})" if year else name


class Jellyfin(commands.Cog):
    def __init__(self, bot: commands.Bot, client: JellyfinClient | None) -> None:
        self.bot = cast("SlopSoil", bot)
        self.client = client

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _wait_for_number(self, ctx: commands.Context, max_val: int) -> int | None:
        """Wait up to 10 s for the user to reply with a number in [1, max_val].

        Returns None on timeout or if the user starts a new command.
        New commands are not swallowed — they still run through the normal pipeline.
        """
        def _check(msg) -> bool:
            return (
                msg.author.id == ctx.author.id
                and msg.channel.id == ctx.channel.id
                and (msg.content.strip().isdigit() or msg.content.strip().startswith("!"))
            )

        try:
            reply = await self.bot.wait_for("message", check=_check, timeout=10.0)
        except TimeoutError:
            await ctx.send("timed out — cancelled")
            return None

        if reply.content.strip().startswith("!"):
            await ctx.send("cancelled")
            return None

        choice = int(reply.content.strip())
        if not 1 <= choice <= max_val:
            await ctx.send(f"invalid selection — pick a number between 1 and {max_val}")
            return None
        return choice

    async def _play_item(self, ctx: commands.Context, item: dict) -> None:
        """Resolve voice, probe the Jellyfin HLS stream, and start streaming."""
        assert self.client is not None

        label = _fmt_item(item)
        item_id = item.get("Id", "")
        log.info("Jellyfin: streaming '%s' (id: %s)", label, item_id)

        guild, voice_channel, vc = await resolve_voice(ctx)
        if not voice_channel:
            await ctx.send("you're not in a voice channel")
            return
        assert guild is not None

        await ctx.send("checking stream…")

        # Ask Jellyfin to start a transcoding session and give us the HLS URL.
        stream_url = await self.client.get_stream_url(item_id)
        if stream_url is None:
            await ctx.send(
                f"could not start a Jellyfin playback session for **{label}** "
                "— check that the server is reachable and the API key is valid"
            )
            return

        safe_url = stream_url.replace(self.client._api_key, "***")
        log.info("Jellyfin: stream URL: %s", safe_url)

        # Resolve HLS master playlist → highest-bandwidth variant.
        resolved_url = await _extract_hls_variant_url(stream_url)
        safe_resolved = resolved_url.replace(self.client._api_key, "***")
        if resolved_url != stream_url:
            log.info("Jellyfin: resolved variant URL: %s", safe_resolved)
        else:
            log.info("Jellyfin: using URL as-is (no HLS variants): %s", safe_resolved)

        info = await _probe_stream(resolved_url)
        if info is None:
            await ctx.send(
                f"could not probe the Jellyfin stream for **{label}** "
                "— transcoding may have failed on the server"
            )
            return

        codec = info["codec"]
        has_audio = info.get("has_audio", True)
        log.info(
            "Jellyfin probe: '%s' → codec=%s fps=%.3f audio=%s",
            label, codec, info["fps"], has_audio,
        )

        if codec not in ("h264", "hevc", "mpeg2video", "mpeg4", "mjpeg"):
            await ctx.send(
                f"unsupported video codec `{codec}` from Jellyfin for **{label}**"
            )
            return

        if not has_audio:
            log.info("Jellyfin stream '%s' has no audio — injecting silence", label)

        await start_live_stream(
            self.bot,
            ctx.send,
            guild,
            voice_channel,
            vc,
            title=label,
            url=resolved_url,
            live=True,
            audio=has_audio,
            probe_size=10_000_000,
        )

    async def _pick_episode(
        self, ctx: commands.Context, series_id: str, series_name: str, season: dict
    ) -> None:
        """List episodes for a season and ask the user to pick one."""
        try:
            episodes = await self.client.get_episodes(series_id, season["Id"])
        except Exception as exc:
            log.exception("failed to fetch episodes: %s", exc)
            await ctx.send(f"failed to fetch episodes: {exc}")
            return

        if not episodes:
            await ctx.send(f"no episodes found for **{season.get('Name')}** of **{series_name}**")
            return

        season_name = season.get("Name", "Unknown")
        lines = [f"**{series_name} — {season_name}**. Pick an episode:\n"]
        for i, ep in enumerate(episodes, 1):
            ep_num = ep.get("IndexNumber")
            ep_name = ep.get("Name", "Unknown")
            ep_str = f"E{ep_num:02d} — {ep_name}" if ep_num else ep_name
            lines.append(f"  `{i}` {ep_str}")
        await ctx.send("\n".join(lines))

        choice = await self._wait_for_number(ctx, len(episodes))
        if choice is None:
            return
        await self._play_item(ctx, episodes[choice - 1])

    async def _pick_season_then_episode(
        self, ctx: commands.Context, series: dict
    ) -> None:
        """Fetch seasons for a series, prompt for one (or skip if only one), then pick an episode."""
        series_id = series["Id"]
        series_name = series.get("Name", "Unknown")

        try:
            seasons = await self.client.get_seasons(series_id)
        except Exception as exc:
            log.exception("failed to fetch seasons: %s", exc)
            await ctx.send(f"failed to fetch seasons: {exc}")
            return

        if not seasons:
            await ctx.send(f"no seasons found for **{series_name}**")
            return

        if len(seasons) == 1:
            await self._pick_episode(ctx, series_id, series_name, seasons[0])
            return

        lines = [f"**{series_name}** — {len(seasons)} seasons. Pick a season:\n"]
        for i, s in enumerate(seasons, 1):
            lines.append(f"  `{i}` {s.get('Name', f'Season {i}')}")
        await ctx.send("\n".join(lines))

        choice = await self._wait_for_number(ctx, len(seasons))
        if choice is None:
            return
        await self._pick_episode(ctx, series_id, series_name, seasons[choice - 1])

    # ── Command ───────────────────────────────────────────────────────────────

    @require_role(Role.FRIEND)
    @commands.command()
    async def media(self, ctx: commands.Context, *, query: str) -> None:
        """
        Search Jellyfin for a movie, series, or episode.
        Accepts an optional sXXeYY suffix to target a specific episode directly.
        """
        if self.client is None:
            await ctx.send(
                "Jellyfin is not configured. "
                "Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` in `.env` and restart the bot."
            )
            return

        show, season, episode = _parse_episode_query(query)
        log.info(
            "Jellyfin search for %r by %s (season=%s episode=%s)",
            show, ctx.author, season, episode,
        )

        try:
            if season is not None and episode is not None:
                results = await self.client.find_episode(show, season, episode)
            else:
                results = await self.client.search(show)
        except urllib.error.URLError as exc:
            log.exception("could not reach Jellyfin: %s", exc)
            await ctx.send(f"could not reach Jellyfin: {exc}")
            return
        except Exception as exc:
            log.exception("Jellyfin search failed: %s", exc)
            await ctx.send(f"Jellyfin search failed: {exc}")
            return

        if not results:
            if season is not None and episode is not None:
                await ctx.send(
                    f"S{season:02d}E{episode:02d} of `{show}` not found in Jellyfin"
                )
            else:
                await ctx.send(f"nothing found in Jellyfin for `{query}`")
            return

        # Single unambiguous match — act on it immediately.
        if len(results) == 1:
            item = results[0]
            if item.get("Type") == "Series":
                await self._pick_season_then_episode(ctx, item)
            else:
                await self._play_item(ctx, item)
            return

        # Multiple results — let the user pick, then handle based on type.
        lines = [f"Found {len(results)} result(s) for `{query}`. Pick a number:\n"]
        for i, item in enumerate(results, 1):
            lines.append(f"  `{i}` [{item.get('Type', '')}] {_fmt_item(item)}")
        await ctx.send("\n".join(lines))

        choice = await self._wait_for_number(ctx, len(results))
        if choice is None:
            return

        item = results[choice - 1]
        if item.get("Type") == "Series":
            await self._pick_season_then_episode(ctx, item)
        else:
            await self._play_item(ctx, item)


async def setup(bot: commands.Bot) -> None:
    url = os.environ.get("JELLYFIN_URL", "").strip()
    api_key = os.environ.get("JELLYFIN_API_KEY", "").strip()
    if url and api_key:
        client: JellyfinClient | None = JellyfinClient(url=url, api_key=api_key)
        log.info("Jellyfin client configured for %s", client.base_url)
    else:
        client = None
        log.warning("JELLYFIN_URL/API_KEY not set — !media will report unconfigured")
    await bot.add_cog(Jellyfin(bot, client))
