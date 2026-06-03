"""IPTV playlist source persistence.

``SourceManager`` stores the list of configured IPTV playlist sources (and the
global TVheadend toggle) as JSON, and exposes the merged channel list that the
TV cog pulls from. ``_data_dir`` resolves the on-disk location.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from pathlib import Path

from services.m3u import _get_epg_url

log = logging.getLogger(__name__)


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "slopsoil"


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
