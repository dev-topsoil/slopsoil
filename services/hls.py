"""HLS master-playlist resolution.

Given a URL, detects whether it points at an HLS master playlist and, if so,
returns the highest-bandwidth variant stream URL — bypassing separate audio
rendition groups that some providers serve unreliably.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.request

log = logging.getLogger(__name__)


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
