"""Tests for cogs/iptv.py — probe_stream and extract_hls_variant_url."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cogs.iptv import extract_hls_variant_url, probe_stream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_urlopen(text: str):
    """Return a context-manager mock that yields a response with the given text body."""
    resp = MagicMock()
    resp.read.return_value = text.encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_ffprobe_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _streams_json(video: dict | None = None, audio: dict | None = None) -> str:
    streams = []
    if video:
        streams.append({"codec_type": "video", **video})
    if audio:
        streams.append({"codec_type": "audio", **audio})
    return json.dumps({"streams": streams})


# ---------------------------------------------------------------------------
# probe_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_stream_returns_none_on_nonzero_exit():
    r = _make_ffprobe_result(returncode=1, stderr="HTTP Error 404")
    with patch("subprocess.run", return_value=r):
        assert await probe_stream("http://example.com/stream") is None


@pytest.mark.asyncio
async def test_probe_stream_returns_none_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
        assert await probe_stream("http://example.com/stream") is None


@pytest.mark.asyncio
async def test_probe_stream_returns_none_on_exception():
    with patch("subprocess.run", side_effect=OSError("no such file")):
        assert await probe_stream("http://example.com/stream") is None


@pytest.mark.asyncio
async def test_probe_stream_returns_none_when_no_video_streams():
    stdout = _streams_json(audio={"codec_name": "aac"})
    r = _make_ffprobe_result(returncode=0, stdout=stdout)
    with patch("subprocess.run", return_value=r):
        assert await probe_stream("http://example.com/stream") is None


@pytest.mark.asyncio
async def test_probe_stream_returns_dict_on_success():
    stdout = _streams_json(
        video={
            "codec_name": "h264",
            "r_frame_rate": "25/1",
            "profile": "High",
            "has_b_frames": 0,
            "width": 1920,
            "height": 1080,
        },
        audio={"codec_name": "aac"},
    )
    r = _make_ffprobe_result(returncode=0, stdout=stdout)
    with patch("subprocess.run", return_value=r):
        result = await probe_stream("http://example.com/stream")

    assert result is not None
    assert result["codec"] == "h264"
    assert result["fps"] == 25.0
    assert result["profile"] == "High"
    assert result["has_b_frames"] is False
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert result["has_audio"] is True
    assert result["audio_codec"] == "aac"


@pytest.mark.asyncio
async def test_probe_stream_video_only_has_no_audio():
    stdout = _streams_json(video={"codec_name": "h264", "r_frame_rate": "30/1"})
    r = _make_ffprobe_result(returncode=0, stdout=stdout)
    with patch("subprocess.run", return_value=r):
        result = await probe_stream("http://example.com/stream")

    assert result is not None
    assert result["has_audio"] is False
    assert result["audio_codec"] is None


@pytest.mark.asyncio
async def test_probe_stream_bad_fps_defaults_to_25():
    stdout = _streams_json(video={"codec_name": "h264", "r_frame_rate": "0/0"})
    r = _make_ffprobe_result(returncode=0, stdout=stdout)
    with patch("subprocess.run", return_value=r):
        result = await probe_stream("http://example.com/stream")

    assert result is not None
    assert result["fps"] == 25.0


@pytest.mark.asyncio
async def test_probe_stream_returns_none_on_invalid_json():
    r = _make_ffprobe_result(returncode=0, stdout="not json at all")
    with patch("subprocess.run", return_value=r):
        assert await probe_stream("http://example.com/stream") is None


# ---------------------------------------------------------------------------
# extract_hls_variant_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_hls_returns_original_for_non_hls():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("not a playlist")):
        result = await extract_hls_variant_url("http://example.com/stream.ts")
    assert result == "http://example.com/stream.ts"


@pytest.mark.asyncio
async def test_extract_hls_returns_original_for_variant_playlist():
    """A playlist with EXTM3U but no EXT-X-STREAM-INF is already a variant — return as-is."""
    variant = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nsegment0.ts\n"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(variant)):
        result = await extract_hls_variant_url("http://example.com/stream.m3u8")
    assert result == "http://example.com/stream.m3u8"


@pytest.mark.asyncio
async def test_extract_hls_picks_highest_bandwidth():
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
        "low/stream.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=8000000\n"
        "high/stream.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=4000000\n"
        "mid/stream.m3u8\n"
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(master)):
        result = await extract_hls_variant_url("http://example.com/master.m3u8")
    assert result == "http://example.com/high/stream.m3u8"


@pytest.mark.asyncio
async def test_extract_hls_resolves_relative_urls():
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000\n"
        "variant/stream.m3u8\n"
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(master)):
        result = await extract_hls_variant_url("http://server.local/hls/master.m3u8")
    assert result == "http://server.local/hls/variant/stream.m3u8"


@pytest.mark.asyncio
async def test_extract_hls_preserves_absolute_variant_urls():
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000\n"
        "http://cdn.example.com/stream.m3u8\n"
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(master)):
        result = await extract_hls_variant_url("http://server.local/master.m3u8")
    assert result == "http://cdn.example.com/stream.m3u8"


@pytest.mark.asyncio
async def test_extract_hls_returns_original_on_fetch_error():
    with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
        result = await extract_hls_variant_url("http://example.com/master.m3u8")
    assert result == "http://example.com/master.m3u8"
