"""
yt-dlp integration — !yt <url> downloads a video and streams it to voice.

Downloads to a per-invocation temp directory, streams via the existing
start_live_stream() go-live path, then deletes the temp dir when the
stream ends (whether stopped by the user or by the video finishing).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from discord.ext import commands

from cogs.stream import start_live_stream
from cogs.utils import resolve_voice

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


async def _download(url: str, out_dir: str) -> tuple[str, str]:
    """
    Download url into out_dir via yt-dlp.
    Returns (file_path, title).
    Raises yt_dlp.DownloadError or FileNotFoundError on failure.
    """
    import yt_dlp  # deferred — not in base env on older installs

    def _run() -> tuple[str, str]:
        opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title: str = (info or {}).get("title", "video")

        # yt-dlp reports the final merged path in requested_downloads.
        downloads = (info or {}).get("requested_downloads", [])
        if downloads:
            candidate = downloads[0].get("filepath", "")
            if candidate and os.path.isfile(candidate):
                return candidate, title

        # Fallback: pick the largest file in the temp dir (avoids thumbnails /
        # subtitle files that yt-dlp may write alongside the video).
        files = [f for f in Path(out_dir).iterdir() if f.is_file()]
        if not files:
            raise FileNotFoundError("yt-dlp produced no output file")
        return str(max(files, key=lambda f: f.stat().st_size)), title

    return await asyncio.to_thread(_run)


def _remove_dir(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
        log.info("removed yt-dlp temp dir: %s", path)
    except Exception as exc:
        log.warning("failed to remove %s: %s", path, exc)


async def _cleanup_after_stream(task: asyncio.Task, tmp_dir: str) -> None:
    """Wait for stream task to finish (or be cancelled), then delete tmp_dir."""
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        _remove_dir(tmp_dir)


class YtDlp(commands.Cog, name="YtDlp"):
    def __init__(self, bot: commands.Bot):
        self.bot = cast("SlopSoil", bot)

    @commands.command(name="yt")
    async def yt(self, ctx: commands.Context, *, url: str):
        """Download a video with yt-dlp and stream it to your voice channel."""
        guild, voice_channel, vc = await resolve_voice(ctx)

        if not voice_channel:
            await ctx.send("you're not in a voice channel")
            return

        assert guild is not None

        status = await ctx.send("downloading…")
        tmp_dir = tempfile.mkdtemp(prefix="slopsoil_yt_")

        try:
            try:
                file_path, title = await _download(url, tmp_dir)
            except Exception as exc:
                log.exception("yt-dlp download failed for %r: %s", url, exc)
                await status.edit(content=f"download failed: {exc}")
                _remove_dir(tmp_dir)
                return

            log.info("yt-dlp downloaded '%s' → %s", title, file_path)
            await status.edit(content=f"starting **{title}**…")

            await start_live_stream(
                self.bot,
                ctx.send,
                guild,
                voice_channel,
                vc,
                title=title,
                url=file_path,
                live=False,
                audio=True,
                probe_size=2_000_000,
            )

            # start_live_stream() returns as soon as the stream task is created.
            # Attach a cleanup task that removes the temp dir when streaming ends.
            stream_task = self.bot.stream_tasks.get(guild.id)
            if stream_task:
                asyncio.create_task(
                    _cleanup_after_stream(stream_task, tmp_dir),
                    name=f"yt-cleanup-{guild.id}",
                )
            else:
                # Stream didn't start (e.g. no H.264 encoder) — clean up now.
                _remove_dir(tmp_dir)

        except Exception:
            _remove_dir(tmp_dir)
            raise


async def setup(bot: commands.Bot):
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        log.error(
            "yt-dlp is not installed — !yt will not work. "
            "Add 'yt-dlp' to requirements.txt and rebuild."
        )
    await bot.add_cog(YtDlp(bot))
