"""Tests for stream-profile env knobs in streaming/video_player.py.

Profile is chosen by the STREAM_QUALITY preset (720p/1080p/4k, default 1080p),
with STREAM_RESOLUTION / STREAM_FPS / STREAM_VIDEO_BITRATE overriding any axis.
"""
from __future__ import annotations

import pytest

from streaming.video_player import (
    _packet_pace_fraction,
    _stream_bitrate,
    _stream_fps,
    _stream_preset,
    _stream_resolution,
)


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch):
    """Start each test from a clean profile env so the 1080p default applies."""
    for var in (
        "STREAM_QUALITY",
        "STREAM_RESOLUTION",
        "STREAM_FPS",
        "STREAM_VIDEO_BITRATE",
        "STREAM_PACKET_PACE",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# _stream_preset (STREAM_QUALITY)
# ---------------------------------------------------------------------------


def test_preset_default_is_1080p():
    assert _stream_preset() == ("1920:1080", 60.0, "12000k")


def test_preset_720p(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "720p")
    assert _stream_preset() == ("1280:720", 30.0, "10000k")


def test_preset_4k_case_insensitive(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "4K")
    assert _stream_preset() == ("3840:2160", 60.0, "24000k")


def test_preset_unknown_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "potato")
    assert _stream_preset() == ("1920:1080", 60.0, "12000k")


# ---------------------------------------------------------------------------
# _stream_resolution
# ---------------------------------------------------------------------------


def test_resolution_follows_preset():
    assert _stream_resolution() == "1920:1080"


def test_resolution_follows_720p_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "720p")
    assert _stream_resolution() == "1280:720"


def test_resolution_override_beats_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "720p")
    monkeypatch.setenv("STREAM_RESOLUTION", "1600:900")
    assert _stream_resolution() == "1600:900"


def test_resolution_x_form_normalized(monkeypatch):
    monkeypatch.setenv("STREAM_RESOLUTION", "1280x720")
    assert _stream_resolution() == "1280:720"


def test_resolution_invalid_falls_back_to_preset(monkeypatch):
    monkeypatch.setenv("STREAM_RESOLUTION", "huge")
    assert _stream_resolution() == "1920:1080"


# ---------------------------------------------------------------------------
# _stream_bitrate
# ---------------------------------------------------------------------------


def test_bitrate_follows_preset():
    assert _stream_bitrate() == "12000k"


def test_bitrate_follows_720p_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "720p")
    assert _stream_bitrate() == "10000k"


def test_bitrate_override_beats_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "1080p")
    monkeypatch.setenv("STREAM_VIDEO_BITRATE", "4000k")
    assert _stream_bitrate() == "4000k"


def test_bitrate_blank_falls_back_to_preset(monkeypatch):
    monkeypatch.setenv("STREAM_VIDEO_BITRATE", "   ")
    assert _stream_bitrate() == "12000k"


# ---------------------------------------------------------------------------
# _stream_fps
# ---------------------------------------------------------------------------


def test_fps_follows_preset():
    assert _stream_fps() == 60.0


def test_fps_follows_720p_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "720p")
    assert _stream_fps() == 30.0


def test_fps_override_beats_preset(monkeypatch):
    monkeypatch.setenv("STREAM_QUALITY", "1080p")
    monkeypatch.setenv("STREAM_FPS", "24")
    assert _stream_fps() == 24.0


def test_fps_out_of_range_falls_back_to_preset(monkeypatch):
    monkeypatch.setenv("STREAM_FPS", "240")
    assert _stream_fps() == 60.0


def test_fps_non_numeric_falls_back_to_preset(monkeypatch):
    monkeypatch.setenv("STREAM_FPS", "fast")
    assert _stream_fps() == 60.0


# ---------------------------------------------------------------------------
# _packet_pace_fraction (STREAM_PACKET_PACE)
# ---------------------------------------------------------------------------


def test_pace_default_is_disabled():
    assert _packet_pace_fraction() == 0.0


def test_pace_override(monkeypatch):
    monkeypatch.setenv("STREAM_PACKET_PACE", "0.75")
    assert _packet_pace_fraction() == 0.75


def test_pace_clamps_to_max(monkeypatch):
    monkeypatch.setenv("STREAM_PACKET_PACE", "2")
    assert _packet_pace_fraction() == 0.95


def test_pace_clamps_negative_to_zero(monkeypatch):
    monkeypatch.setenv("STREAM_PACKET_PACE", "-1")
    assert _packet_pace_fraction() == 0.0


def test_pace_non_numeric_falls_back_to_disabled(monkeypatch):
    monkeypatch.setenv("STREAM_PACKET_PACE", "smooth")
    assert _packet_pace_fraction() == 0.0
