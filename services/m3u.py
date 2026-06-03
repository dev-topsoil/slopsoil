"""M3U playlist parsing and fetching.

Parses M3U/M3U8 playlist text into channel dicts and exposes ``fetch_and_parse``
for retrieving a playlist over HTTP. Also extracts the EPG (XMLTV) URL advertised
in the ``#EXTM3U`` header so callers can pair channels with now-playing data.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.request

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
