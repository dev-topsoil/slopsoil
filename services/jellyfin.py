"""Jellyfin HTTP API client.

Wraps the Jellyfin REST API: item search, episode lookup, and HLS stream-URL
construction via PlaybackInfo. Authentication uses an API key; ``get_stream_url``
returns a server-generated transcoding URL (H.264/AAC) with the key appended so
FFmpeg can fetch HLS segments directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

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
