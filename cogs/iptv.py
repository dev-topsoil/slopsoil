"""
IPTV playlist source management — !add-source and !sources commands.

Parses M3U playlists, persists sources as JSON, and exposes a SourceManager
that other cogs (tv.py) use to pull live channel lists from enabled sources.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from discord.ext import commands

from permissions import Role, require_role

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)

_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def _parse_attrs(line: str) -> dict[str, str]:
    return dict(_ATTR_RE.findall(line))


def parse_m3u(text: str) -> list[dict]:
    """Parse M3U playlist text into a list of channel dicts.

    Each channel dict has: name, tvg_id, group, stream_url.
    Raises ValueError if the text is not a valid M3U playlist.
    """
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        raise ValueError("not a valid M3U playlist (missing #EXTM3U header)")

    channels: list[dict] = []
    pending: dict | None = None

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = _parse_attrs(line)
            comma = line.find(",")
            display_name = line[comma + 1:].strip() if comma != -1 else ""
            pending = {
                "name": display_name or attrs.get("tvg-name", "unknown"),
                "tvg_id": attrs.get("tvg-id", ""),
                "group": attrs.get("group-title", ""),
            }
        elif not line.startswith("#") and pending is not None:
            pending["stream_url"] = line
            channels.append(pending)
            pending = None

    return channels


def _get_epg_url(m3u_text: str) -> str | None:
    """Return the url-tvg / x-tvg-url attribute from the #EXTM3U header, if present."""
    lines = m3u_text.splitlines()
    if not lines:
        return None
    header = lines[0].strip()
    if not header.startswith("#EXTM3U"):
        return None
    attrs = _parse_attrs(header)
    return attrs.get("url-tvg") or attrs.get("x-tvg-url") or None


async def fetch_and_parse(url: str) -> tuple[list[dict], str | None]:
    """Fetch an M3U URL, validate, and parse. Returns (channels, epg_url).
    Raises on network or parse errors."""
    def _fetch() -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "slopsoil/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")  # type: ignore[no-any-return]

    text = await asyncio.to_thread(_fetch)
    channels = parse_m3u(text)
    epg_url = _get_epg_url(text)
    log.info(
        "parsed %d channel(s) from %s%s",
        len(channels), url,
        f" (EPG: {epg_url})" if epg_url else "",
    )
    return channels, epg_url


def _parse_xmltv_dt(s: str) -> datetime:
    """Parse an XMLTV datetime string such as '20260519120000 +0000'."""
    s = s.strip()
    parts = s.split(maxsplit=1)
    dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
    if len(parts) > 1:
        off = parts[1]
        sign = -1 if off.startswith("-") else 1
        h, m = int(off[1:3]), int(off[3:5])
        tz = timezone(timedelta(hours=sign * h, minutes=sign * m))
    else:
        tz = UTC
    return dt.replace(tzinfo=tz)


async def fetch_xmltv_now_playing(epg_url: str) -> dict[str, str]:
    """Fetch an XMLTV file and return {channel_id: title} for airing programmes.

    Uses iterparse so only one <programme> element lives in memory at a time,
    keeping RAM reasonable even for large EPG files.  Handles gzip-compressed
    responses transparently.
    """
    def _fetch_and_parse() -> dict[str, str]:
        req = urllib.request.Request(
            epg_url,
            headers={"User-Agent": "slopsoil/1.0", "Accept-Encoding": "gzip, deflate"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        # Decompress if gzip (check magic bytes — more reliable than Content-Encoding)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        now = datetime.now(tz=UTC)
        result: dict[str, str] = {}

        # iterparse with root.clear() pattern: hold a reference to the root
        # <tv> element, process each <programme> while its children are still
        # intact, then clear root to free memory.  Clearing root also frees
        # the <programme> and all its sub-elements (title, desc, etc.) so they
        # don't accumulate.  Eagerly clearing child elements (the naive pattern)
        # would null out <title>.text before <programme> fires its "end" event.
        context = ET.iterparse(io.BytesIO(raw), events=("start", "end"))
        root: ET.Element | None = None
        for event, elem in context:
            if event == "start" and root is None:
                root = elem  # first start event is always the root <tv>
                continue
            if event != "end" or elem.tag != "programme":
                continue
            ch = elem.get("channel", "")
            try:
                start = _parse_xmltv_dt(elem.get("start", ""))
                stop = _parse_xmltv_dt(elem.get("stop", ""))
            except Exception:
                if root is not None:
                    root.clear()
                continue
            if start <= now < stop and ch not in result:
                title_el = elem.find("title")
                title = (title_el.text or "").strip() if title_el is not None else ""
                if title:
                    result[ch] = title
            # Clear root *after* extracting what we need — this frees the
            # processed <programme> and all its children from memory.
            if root is not None:
                root.clear()
        return result

    return await asyncio.to_thread(_fetch_and_parse)


async def extract_hls_variant_url(master_url: str) -> str:
    """
    If master_url is an HLS master playlist, fetch it and return the URL of
    the highest-bandwidth variant stream. Otherwise return master_url unchanged.

    HLS master playlists declare EXT-X-MEDIA audio rendition groups; when FFmpeg
    opens the master, it follows those groups separately. For thetvapp.to-style
    streams the audio rendition (mono.m3u8) often returns 500 while the TS
    segments themselves carry embedded audio (tracks-v1a1 = video+audio in one
    MPEG-TS mux). Giving FFmpeg the variant URL directly bypasses the rendition
    groups and lets it use the embedded audio instead.
    """
    def _fetch() -> str:
        try:
            req = urllib.request.Request(
                master_url, headers={"User-Agent": "slopsoil/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                # Read only enough to detect HLS markers — avoids hanging on
                # MJPEG or other live streams whose bodies never end.
                text = resp.read(8192).decode("utf-8", errors="replace")
        except Exception:
            return master_url

        if "#EXTM3U" not in text or "#EXT-X-STREAM-INF" not in text:
            return master_url  # already a variant playlist or not HLS

        base = master_url.rsplit("/", 1)[0] + "/"
        best_bw = -1
        best: str | None = None

        lines = text.splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF:"):
                m = re.search(r"BANDWIDTH=(\d+)", line, re.IGNORECASE)
                bw = int(m.group(1)) if m else 0
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        is_abs = nxt.startswith(("http://", "https://"))
                        abs_url = nxt if is_abs else base + nxt
                        if bw > best_bw:
                            best_bw = bw
                            best = abs_url

        if best and best != master_url:
            log.info("HLS master → variant: %s", best)
            return best
        return master_url

    return await asyncio.to_thread(_fetch)


async def probe_stream(url: str) -> dict | None:
    """Run ffprobe on a stream URL and return video + audio stream info.

    Returns a dict with codec, fps, profile, has_b_frames, width, height, and
    has_audio on success, or None if the stream could not be reached or contains
    no video.  Probes ALL streams (not just video) so audio presence is reliably
    detected even for HLS streams that have separate audio rendition groups.
    """
    def _run() -> str | None:
        try:
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v", "warning",
                    "-show_streams",          # all streams — no -select_streams
                    "-print_format", "json",
                    # 10 MB / 10 s: HLS streams need time to follow the master
                    # playlist → audio rendition playlist → first audio segment.
                    # A 2 MB window frequently returns only video streams.
                    "-probesize", "10000000",
                    "-analyzeduration", "10000000",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                log.warning("ffprobe failed (exit %d) for %s", r.returncode, url)
                if r.stderr:
                    for line in r.stderr.splitlines():
                        log.warning("ffprobe stderr: %s", line)
                return None
            return r.stdout
        except subprocess.TimeoutExpired:
            log.warning("ffprobe timed out for %s", url)
            return None
        except Exception as exc:
            log.warning("ffprobe error for %s: %s", url, exc)
            return None

    raw = await asyncio.to_thread(_run)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        return None

    v = video_streams[0]
    fps_str = v.get("r_frame_rate", "25/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 25.0
    except (ValueError, ZeroDivisionError):
        fps = 25.0

    return {
        "codec": v.get("codec_name", "unknown"),
        "fps": round(fps, 3),
        "profile": v.get("profile", "unknown"),
        "has_b_frames": bool(v.get("has_b_frames", 0)),
        "width": v.get("width", 0),
        "height": v.get("height", 0),
        "has_audio": len(audio_streams) > 0,
        "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
    }


class SourceManager:
    """Manages IPTV playlist sources and global source toggles with JSON persistence."""

    def __init__(self, persist_path: str | Path):
        self._path = Path(persist_path)
        self._sources: list[dict] = []
        self._tvh_enabled: bool = True
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            # Support old format (plain list) and new format (dict with metadata).
            if isinstance(data, list):
                self._sources = data
            else:
                self._sources = data.get("sources", [])
                self._tvh_enabled = data.get("tvh_enabled", True)
            log.info(
                "loaded %d IPTV source(s) from %s", len(self._sources), self._path
            )
        except Exception as exc:
            log.warning("failed to load IPTV sources from %s: %s", self._path, exc)
            self._sources = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"tvh_enabled": self._tvh_enabled, "sources": self._sources}, indent=2
            )
        )

    @property
    def tvh_enabled(self) -> bool:
        return self._tvh_enabled

    def set_tvh_enabled(self, enabled: bool) -> None:
        self._tvh_enabled = enabled
        self._save()

    def get_sources(self) -> list[dict]:
        """Return a shallow copy of the sources list."""
        return list(self._sources)

    def add_source(
        self,
        name: str,
        url: str,
        channels: list[dict],
        epg_url: str | None = None,
    ) -> None:
        """Add a new source or replace an existing one with the same name."""
        entry: dict = {
            "name": name,
            "url": url,
            "channels": channels,
        }
        if epg_url:
            entry["epg_url"] = epg_url
        for i, src in enumerate(self._sources):
            if src["name"].lower() == name.lower():
                entry["enabled"] = src.get("enabled", False)
                self._sources[i] = entry
                self._save()
                return
        entry["enabled"] = True
        self._sources.append(entry)
        self._save()

    def get_epg_sources(self) -> list[tuple[str, str]]:
        """Return [(source_name, epg_url)] for enabled sources with an EPG URL."""
        return [
            (src["name"], src["epg_url"])
            for src in self._sources
            if src.get("enabled") and src.get("epg_url")
        ]

    async def backfill_epg_urls(self) -> int:
        """
        For any stored source that has no epg_url, fetch the first 1 KB of its
        M3U URL to read the #EXTM3U header and extract url-tvg / x-tvg-url.
        Saves and returns the number of sources updated.
        """
        def _peek(m3u_url: str) -> str:
            req = urllib.request.Request(
                m3u_url,
                headers={"User-Agent": "slopsoil/1.0", "Range": "bytes=0-1023"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read(1024).decode("utf-8", errors="replace")  # type: ignore[no-any-return]
            except Exception:
                # Server may not support Range; fall back to a plain GET and read 1 KB
                req2 = urllib.request.Request(
                    m3u_url, headers={"User-Agent": "slopsoil/1.0"}
                )
                with urllib.request.urlopen(req2, timeout=10) as resp:
                    return resp.read(1024).decode("utf-8", errors="replace")  # type: ignore[no-any-return]

        updated = 0
        for i, src in enumerate(self._sources):
            if src.get("epg_url"):
                continue
            m3u_url = src.get("url")
            if not m3u_url:
                continue
            try:
                header_text = await asyncio.to_thread(_peek, m3u_url)
                epg_url = _get_epg_url(header_text)
                if epg_url:
                    self._sources[i]["epg_url"] = epg_url
                    updated += 1
                    log.info(
                        "backfilled epg_url for source '%s': %s", src["name"], epg_url
                    )
            except Exception as exc:
                log.debug("could not backfill epg_url for '%s': %s", src["name"], exc)

        if updated:
            self._save()
        return updated

    def set_enabled(self, idx: int, enabled: bool) -> None:
        self._sources[idx]["enabled"] = enabled
        self._save()

    def remove_source(self, idx: int) -> str:
        """Remove a source by index. Returns the removed source's name."""
        name = str(self._sources[idx]["name"])
        del self._sources[idx]
        self._save()
        return name

    def get_iptv_channels(self) -> list[dict]:
        """Return all channels from enabled sources with a 'source' field added."""
        result: list[dict] = []
        for src in self._sources:
            if src.get("enabled"):
                for ch in src.get("channels", []):
                    result.append({**ch, "source": src["name"]})
        return result


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


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "slopsoil"


async def setup(bot: commands.Bot):
    sm = SourceManager(_data_dir() / "sources.json")
    bot.source_manager = sm  # type: ignore[attr-defined]
    log.info("IPTV SourceManager loaded (%d source(s))", len(sm.get_sources()))
    await bot.add_cog(IPTVCog(bot, sm))
    n = await sm.backfill_epg_urls()
    if n:
        log.info("backfilled EPG URL(s) for %d source(s)", n)
