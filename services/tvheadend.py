"""TVheadend HTTP API client.

Wraps the TVheadend REST API: channel grid, EPG event search, now-playing
lookup, and stream-URL construction. ``from_env`` builds a client from the
TVHEADEND_URL/USER/PASS environment variables, or returns None when unset.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.request
from urllib.parse import quote, urlparse, urlunparse

log = logging.getLogger(__name__)


class TVheadendClient:
    def __init__(self, url: str, user: str, password: str):
        self.base_url = url.rstrip("/")
        self.user = user
        self.password = password
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {creds}"
        self._now_playing_cache: tuple[float, dict[str, str]] = (0.0, {})

    @classmethod
    def from_env(cls) -> TVheadendClient | None:
        """Build a client from TVHEADEND_URL/USER/PASS, or None if any are unset.

        Keeping this here means the "is TVheadend configured?" decision lives in
        one place; the cog and bot just check for None.
        """
        url = os.environ.get("TVHEADEND_URL")
        user = os.environ.get("TVHEADEND_USER")
        password = os.environ.get("TVHEADEND_PASS")
        if url and user and password:
            return cls(url, user, password)
        return None

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
            log.debug(
                "now-playing: returning cached data (%d entries)", len(cached_data)
            )
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
