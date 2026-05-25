"""Tests for video_compat.py constants and patch_video()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import video_compat
from video_compat import (
    H264_PAYLOAD_TYPE,
    RTX_SSRC_OFFSET,
    VIDEO_SSRC_OFFSET,
    patch_video,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_h264_payload_type():
    assert H264_PAYLOAD_TYPE == 101


def test_ssrc_offsets_are_sequential():
    assert VIDEO_SSRC_OFFSET == 1
    assert RTX_SSRC_OFFSET == 2
    assert RTX_SSRC_OFFSET == VIDEO_SSRC_OFFSET + 1


# ---------------------------------------------------------------------------
# patch_video() monkey-patching
# ---------------------------------------------------------------------------


def test_patch_video_replaces_methods():
    """patch_video should swap in the three patched methods on the class."""
    MockWS = MagicMock()
    mock_gateway = MagicMock()
    mock_gateway.DiscordVoiceWebSocket = MockWS

    patch_video(mock_gateway)

    assert MockWS.identify is video_compat._patched_identify
    assert MockWS.select_protocol is video_compat._patched_select_protocol
    assert MockWS.client_connect is video_compat._patched_client_connect


# ---------------------------------------------------------------------------
# _patched_identify payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patched_identify_sends_video_flag():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.IDENTIFY = 0
    ws._connection.server_id = 1
    ws._connection.user.id = 2
    ws._connection.session_id = "sess"
    ws._connection.token = "tok"
    ws._connection.max_dave_protocol_version = 1

    await video_compat._patched_identify(ws)

    payload = ws.send_as_json.call_args[0][0]
    assert payload["d"]["video"] is True
    assert payload["d"]["streams"] == [{"type": "video", "rid": "100", "quality": 100}]


@pytest.mark.asyncio
async def test_patched_identify_op_code():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.IDENTIFY = 7
    ws._connection.server_id = 1
    ws._connection.user.id = 2
    ws._connection.session_id = "s"
    ws._connection.token = "t"
    ws._connection.max_dave_protocol_version = 0

    await video_compat._patched_identify(ws)

    assert ws.send_as_json.call_args[0][0]["op"] == 7


# ---------------------------------------------------------------------------
# _patched_select_protocol payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patched_select_protocol_includes_h264_codec():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.SELECT_PROTOCOL = 1

    await video_compat._patched_select_protocol(ws, "1.2.3.4", 50001, "xsalsa20_poly1305")

    payload = ws.send_as_json.call_args[0][0]
    codecs = payload["d"]["codecs"]
    codec_names = [c["name"] for c in codecs]
    assert "opus" in codec_names
    assert "H264" in codec_names


@pytest.mark.asyncio
async def test_patched_select_protocol_address():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.SELECT_PROTOCOL = 1

    await video_compat._patched_select_protocol(ws, "10.0.0.1", 1234, "some_mode")

    data = ws.send_as_json.call_args[0][0]["d"]["data"]
    assert data["address"] == "10.0.0.1"
    assert data["port"] == 1234
    assert data["mode"] == "some_mode"


@pytest.mark.asyncio
async def test_patched_select_protocol_h264_payload_type():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.SELECT_PROTOCOL = 1

    await video_compat._patched_select_protocol(ws, "127.0.0.1", 9000, "m")

    codecs = ws.send_as_json.call_args[0][0]["d"]["codecs"]
    h264 = next(c for c in codecs if c["name"] == "H264")
    assert h264["payload_type"] == H264_PAYLOAD_TYPE


# ---------------------------------------------------------------------------
# _patched_client_connect payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patched_client_connect_ssrc_offsets():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.VIDEO = 12
    ws._connection.ssrc = 100

    await video_compat._patched_client_connect(ws)

    d = ws.send_as_json.call_args[0][0]["d"]
    assert d["audio_ssrc"] == 100
    assert d["video_ssrc"] == 101
    assert d["rtx_ssrc"] == 102


@pytest.mark.asyncio
async def test_patched_client_connect_stream_fields():
    ws = MagicMock()
    ws.send_as_json = AsyncMock()
    ws.VIDEO = 12
    ws._connection.ssrc = 50

    await video_compat._patched_client_connect(ws)

    stream = ws.send_as_json.call_args[0][0]["d"]["streams"][0]
    assert stream["active"] is True
    assert stream["max_framerate"] == 60
    assert stream["max_resolution"]["width"] == 1920
    assert stream["max_resolution"]["height"] == 1080
