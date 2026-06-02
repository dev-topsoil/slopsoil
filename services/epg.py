"""XMLTV electronic programme guide (EPG) fetching.

Downloads an XMLTV file (optionally gzip-compressed) and extracts the title of
the programme currently airing on each channel, keyed by channel id. Uses
iterparse so only one ``<programme>`` element lives in memory at a time.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone

log = logging.getLogger(__name__)


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
