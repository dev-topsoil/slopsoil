"""yt-dlp download and live-stream extraction helpers.

Used by the TV cog to serve `!play <URL>`: ``_yt_extract_live_url`` returns a
direct stream URL for live broadcasts (metadata only, no download), while
``_yt_download`` downloads VODs into a temp dir for streaming, with cleanup
helpers for the temp directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Default yt-dlp format selector for downloaded VODs — best available v+a.
_DEFAULT_YT_FORMAT = "bestvideo+bestaudio/best"


def _yt_format() -> str:
    """yt-dlp format selector used to download `!play <URL>` VODs.

    Defaults to `_DEFAULT_YT_FORMAT` (best available video+audio).  Override via
    the ``YTDLP_FORMAT`` env var to pin a codec/resolution — useful on hardware
    that can't decode AV1/4K in real time, or to avoid downloading 4K only to
    downscale it (e.g. ``bv*[vcodec^=avc1][height<=1080]+ba/b[height<=1080]``).
    An unset or blank value falls back to the default.
    """
    return os.environ.get("YTDLP_FORMAT", "").strip() or _DEFAULT_YT_FORMAT


async def _yt_download(url: str, out_dir: str) -> tuple[str, str]:
    """Download url into out_dir via yt-dlp. Returns (file_path, title)."""
    import yt_dlp

    def _run() -> tuple[str, str]:
        opts = {
            "format": _yt_format(),
            "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title: str = (info or {}).get("title", "video")
        downloads = (info or {}).get("requested_downloads", [])
        if downloads:
            candidate = downloads[0].get("filepath", "")
            if candidate and os.path.isfile(candidate):
                return candidate, title

        files = [f for f in Path(out_dir).iterdir() if f.is_file()]
        if not files:
            raise FileNotFoundError("yt-dlp produced no output file")
        return str(max(files, key=lambda f: f.stat().st_size)), title

    return await asyncio.to_thread(_run)


def _yt_remove_dir(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
        log.info("removed yt-dlp temp dir: %s", path)
    except Exception as exc:
        log.warning("failed to remove %s: %s", path, exc)


async def _yt_cleanup_after_stream(task: asyncio.Task, tmp_dir: str) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        _yt_remove_dir(tmp_dir)


async def _yt_extract_live_url(url: str) -> tuple[str, str] | None:
    """Return (stream_url, title) if url is a live broadcast, else None.

    Uses yt-dlp metadata only — no download — so it's fast.  If the stream is
    upcoming (not yet started) or the URL is not a live broadcast, returns None.
    """
    import yt_dlp

    def _run() -> tuple[str, str] | None:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info or info.get("live_status") != "is_live":
            return None

        title: str = info.get("title") or "YouTube Live"

        # Prefer an HLS variant with both video and audio tracks.
        formats: list[dict] = info.get("formats") or []
        hls = [
            f for f in formats
            if f.get("protocol", "").startswith("m3u8")
            and f.get("url")
            and f.get("vcodec") not in (None, "none")
        ]
        if hls:
            best = max(hls, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
            return best["url"], title

        if info.get("url"):
            return info["url"], title

        return None

    return await asyncio.to_thread(_run)
