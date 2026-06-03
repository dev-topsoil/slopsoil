"""Stream probing via ffprobe.

Runs ffprobe against a stream URL and returns a summary of its first video
stream (codec, fps, profile, resolution, B-frames) plus whether any audio
stream is present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess

log = logging.getLogger(__name__)


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
