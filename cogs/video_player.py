"""
H.264 video player for Discord voice channels.

Reads a live stream URL via FFmpeg (outputting raw H.264 Annex B on stdout),
parses NAL units, packetizes them per RFC 6184 using FU-A fragmentation for
large NAL units, encrypts each RTP packet with the voice connection's secret
key, and sends them via the existing voice UDP socket.

The video SSRC is audio_ssrc + 1 (matching video_compat.VIDEO_SSRC_OFFSET).
A separate nonce counter starting at 0x80000000 is used for all encrypted
modes to prevent overlap with the audio player's own nonce counter.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import struct
import subprocess
import threading
import time

import discord
import nacl.secret
import nacl.utils

log = logging.getLogger(__name__)

# RTP constants for H.264 video
_H264_PT: int = 101  # payload type (matches video_compat.H264_PAYLOAD_TYPE)
_CLOCK: int = 90_000  # 90 kHz RTP clock rate for video
_MTU: int = 1_200  # safe MTU for Discord voice UDP

# H.264 NAL unit type IDs (low 5 bits of NAL header byte)
_NAL_NON_IDR: int = 1
_NAL_IDR: int = 5
_NAL_AUD: int = 9  # Access Unit Delimiter — marks frame boundaries

# Nonce base for video — high half of 32-bit space, avoids overlap with audio
_VIDEO_NONCE_BASE: int = 0x8000_0000

# Regex matching both 3-byte (\x00\x00\x01) and 4-byte (\x00\x00\x00\x01) start codes
_START_RE = re.compile(rb"\x00\x00\x00?\x01")

# NAL types that carry encoded picture data (slice headers) — each starts a new frame
_SLICE_NAL_TYPES = frozenset({1, 2, 3, 4, 5})
# NAL types that carry stream parameters — buffered and prepended to the next slice
_PARAM_NAL_TYPES = frozenset({6, 7, 8})  # SEI, SPS, PPS

# H.264 profile_idc values that use the extended SPS syntax (chroma, scaling, etc.)
_HIGH_PROFILES = frozenset({100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 144})


# ── H.264 SPS VUI rewriter ────────────────────────────────────────────────────
# Discord requires bitstream_restriction_flag=1 and max_num_reorder_frames=0
# in every SPS NAL unit or it rejects the video stream with Error 2015.
# Reference: https://github.com/Discord-RE/Discord-video-stream (SPSVUIRewriter.ts)


def _ep_remove(data: bytes) -> bytes:
    """Strip emulation prevention bytes (0x00 0x00 0x03 → 0x00 0x00)."""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out.append(0)
            out.append(0)
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _ep_add(data: bytes) -> bytes:
    """Re-insert emulation prevention bytes so 0x00 0x00 0x{00-03} never appears raw."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _BR:
    """H.264 RBSP bit reader (operates on emulation-prevention-stripped bytes)."""

    __slots__ = ("_d", "_p")

    def __init__(self, data: bytes) -> None:
        self._d = data
        self._p = 0  # bit position

    def remaining(self) -> int:
        return len(self._d) * 8 - self._p

    def u(self, n: int) -> int:
        val = 0
        for _ in range(n):
            bi = self._p >> 3
            if bi >= len(self._d):
                raise IndexError(f"SPS read past end at bit {self._p}")
            val = (val << 1) | ((self._d[bi] >> (7 - (self._p & 7))) & 1)
            self._p += 1
        return val

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("Exp-Golomb: too many leading zeros")
        return 0 if z == 0 else (1 << z) - 1 + self.u(z)

    def se(self) -> int:
        c = self.ue()
        if c == 0:
            return 0
        return ((c + 1) >> 1) if c & 1 else -(c >> 1)


class _BW:
    """H.264 RBSP bit writer."""

    __slots__ = ("_bits",)

    def __init__(self) -> None:
        self._bits: list[int] = []

    def u(self, n: int, val: int) -> None:
        for i in range(n - 1, -1, -1):
            self._bits.append((val >> i) & 1)

    def ue(self, val: int) -> None:
        n = val + 1
        bl = n.bit_length()
        for _ in range(bl - 1):
            self._bits.append(0)
        for i in range(bl - 1, -1, -1):
            self._bits.append((n >> i) & 1)

    def se(self, val: int) -> None:
        self.ue(2 * val - 1 if val > 0 else -2 * val)

    def to_bytes(self) -> bytes:
        # Append RBSP stop bit then zero-pad to byte boundary
        bits = list(self._bits) + [1] + [0] * ((-len(self._bits) - 1) % 8)
        out = bytearray()
        for i in range(0, len(bits), 8):
            b = 0
            for j in range(8):
                b = (b << 1) | bits[i + j]
            out.append(b)
        return bytes(out)


def _copy_scaling_list(r: _BR, w: _BW, size: int) -> None:
    last = 8
    nxt = 8
    for _ in range(size):
        if nxt != 0:
            delta = r.se()
            w.se(delta)
            nxt = (last + delta + 256) % 256
        last = nxt if nxt != 0 else last


def _copy_hrd(r: _BR, w: _BW) -> None:
    cpb = r.ue()
    w.ue(cpb)
    w.u(4, r.u(4))
    w.u(4, r.u(4))  # bit_rate_scale, cpb_size_scale
    for _ in range(cpb + 1):
        w.ue(r.ue())
        w.ue(r.ue())
        w.u(1, r.u(1))  # bit_rate, cpb_size, cbr_flag
    w.u(5, r.u(5))
    w.u(5, r.u(5))
    w.u(5, r.u(5))
    w.u(5, r.u(5))  # delay lengths


def _do_rewrite_sps(nal: bytes) -> bytes:
    r = _BR(_ep_remove(nal[1:]))
    w = _BW()

    profile_idc = r.u(8)
    w.u(8, profile_idc)
    w.u(8, r.u(8))  # constraint_set_flags + reserved_zero_2bits
    w.u(8, r.u(8))  # level_idc
    w.ue(r.ue())  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = r.ue()
        w.ue(chroma_format_idc)
        if chroma_format_idc == 3:
            w.u(1, r.u(1))  # separate_colour_plane_flag
        w.ue(r.ue())  # bit_depth_luma_minus8
        w.ue(r.ue())  # bit_depth_chroma_minus8
        w.u(1, r.u(1))  # qpprime_y_zero_transform_bypass_flag
        ssmf = r.u(1)
        w.u(1, ssmf)
        if ssmf:
            n_lists = 12 if chroma_format_idc == 3 else 8
            for i in range(n_lists):
                flag = r.u(1)
                w.u(1, flag)
                if flag:
                    _copy_scaling_list(r, w, 16 if i < 6 else 64)

    w.ue(r.ue())  # log2_max_frame_num_minus4
    poc = r.ue()
    w.ue(poc)
    if poc == 0:
        w.ue(r.ue())  # log2_max_pic_order_cnt_lsb_minus4
    elif poc == 1:
        w.u(1, r.u(1))  # delta_pic_order_always_zero_flag
        w.se(r.se())  # offset_for_non_ref_pic
        w.se(r.se())  # offset_for_top_to_bottom_field
        n = r.ue()
        w.ue(n)
        for _ in range(n):
            w.se(r.se())  # offset_for_ref_frame[i]

    max_num_ref_frames = r.ue()
    w.ue(max_num_ref_frames)
    w.u(1, r.u(1))  # gaps_in_frame_num_value_allowed_flag
    w.ue(r.ue())  # pic_width_in_mbs_minus1
    w.ue(r.ue())  # pic_height_in_map_units_minus1
    fmof = r.u(1)
    w.u(1, fmof)
    if not fmof:
        w.u(1, r.u(1))  # mb_adaptive_frame_field_flag
    w.u(1, r.u(1))  # direct_8x8_inference_flag
    fcf = r.u(1)
    w.u(1, fcf)
    if fcf:
        w.ue(r.ue())
        w.ue(r.ue())
        w.ue(r.ue())
        w.ue(r.ue())  # crop offsets

    # Force vui_parameters_present_flag = 1
    vui_present = r.u(1) if r.remaining() > 0 else 0
    w.u(1, 1)

    def _write_restriction() -> None:
        w.u(1, 1)  # motion_vectors_over_pic_boundaries_flag (default 1)
        w.ue(2)  # max_bytes_per_pic_denom (default 2)
        w.ue(1)  # max_bits_per_mb_denom (default 1)
        w.ue(16)  # log2_max_mv_length_horizontal (default 16)
        w.ue(16)  # log2_max_mv_length_vertical (default 16)
        w.ue(0)  # max_num_reorder_frames = 0  ← CRITICAL for Discord
        w.ue(max_num_ref_frames)  # max_dec_frame_buffering

    if not vui_present:
        # No VUI at all — write a minimal one from scratch
        w.u(2, 0)  # aspect_ratio_info_present=0, overscan_info_present=0
        w.u(1, 0)  # video_signal_type_present=0
        w.u(5, 0)  # chroma_loc=0, timing=0, nal_hrd=0, vcl_hrd=0, pic_struct=0
        w.u(1, 1)  # bitstream_restriction_flag=1
        _write_restriction()
    else:
        arif = r.u(1)
        w.u(1, arif)
        if arif:
            ari = r.u(8)
            w.u(8, ari)
            if ari == 255:  # Extended_SAR
                w.u(16, r.u(16))
                w.u(16, r.u(16))

        oif = r.u(1)
        w.u(1, oif)
        if oif:
            w.u(1, r.u(1))  # overscan_appropriate_flag

        # Read video_signal_type but write 0 — strip it for compatibility
        vstf = r.u(1)
        w.u(1, 0)
        if vstf:
            r.u(3)
            r.u(1)  # video_format, video_full_range_flag (discard)
            cdpf = r.u(1)
            if cdpf:
                r.u(8)
                r.u(8)
                r.u(8)  # colour_{primaries,transfer,matrix} (discard)

        clif = r.u(1)
        w.u(1, clif)
        if clif:
            w.ue(r.ue())
            w.ue(r.ue())  # chroma_sample_loc_type top/bottom

        tif = r.u(1)
        w.u(1, tif)
        if tif:
            w.u(32, r.u(32))  # num_units_in_tick
            w.u(32, r.u(32))  # time_scale
            w.u(1, r.u(1))  # fixed_frame_rate_flag

        nhp = r.u(1)
        w.u(1, nhp)
        if nhp:
            _copy_hrd(r, w)

        vhp = r.u(1)
        w.u(1, vhp)
        if vhp:
            _copy_hrd(r, w)

        if nhp or vhp:
            w.u(1, r.u(1))  # low_delay_hrd_flag

        w.u(1, r.u(1))  # pic_struct_present_flag

        brf = r.u(1)
        w.u(1, 1)  # force bitstream_restriction_flag = 1
        if not brf:
            _write_restriction()
        else:
            w.u(1, r.u(1))  # motion_vectors_over_pic_boundaries_flag
            w.ue(r.ue())  # max_bytes_per_pic_denom
            w.ue(r.ue())  # max_bits_per_mb_denom
            w.ue(r.ue())  # log2_max_mv_length_horizontal
            w.ue(r.ue())  # log2_max_mv_length_vertical
            r.ue()  # max_num_reorder_frames — discard original
            w.ue(0)  # force 0  ← CRITICAL for Discord
            r.ue()  # max_dec_frame_buffering — discard original
            w.ue(max_num_ref_frames)

    return bytes([nal[0]]) + _ep_add(w.to_bytes())


def rewrite_sps_vui(nal: bytes) -> bytes:
    """
    Rewrite H.264 SPS NAL unit to force bitstream_restriction_flag=1 and
    max_num_reorder_frames=0.  Discord requires both or rejects with Error 2015.
    Returns the original NAL unchanged if parsing/rewriting fails.
    """
    if not nal or (nal[0] & 0x1F) != 7:
        return nal
    try:
        return _do_rewrite_sps(nal)
    except Exception:
        log.debug("SPS VUI rewrite failed; passing original through", exc_info=True)
        return nal


# ── Encoder detection ─────────────────────────────────────────────────────────


@dataclasses.dataclass
class _EncoderConfig:
    name: str
    pre_input: list[str]  # args before -i
    post_codec: list[str]  # args after -c:v <name>
    vf: str  # -vf value


def _test_encoder(name: str, pre_input: list[str]) -> bool:
    """Return True if FFmpeg can actually use this encoder (device/driver check)."""
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                *pre_input,
                "-f",
                "lavfi",
                "-i",
                "nullsrc=size=64x64:rate=1",
                "-vframes",
                "1",
                "-c:v",
                name,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _detect_encoder() -> _EncoderConfig | None:
    """Pick the best available H.264 encoder. Returns None if nothing works."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
    )
    available = result.stdout

    vaapi_pre = ["-vaapi_device", "/dev/dri/renderD128"]

    if "libx264" in available and _test_encoder("libx264", []):
        log.info("video encoder: libx264 (software)")
        return _EncoderConfig(
            name="libx264",
            pre_input=[],
            post_codec=[
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "high",
                "-level:v",
                "4.2",
                "-x264-params",
                "aud=1",
                "-b:v",
                "6000k",
                "-maxrate",
                "6000k",
                "-bufsize",
                "12000k",
            ],
            vf="scale=1920:1080",
        )

    if "h264_nvenc" in available and _test_encoder("h264_nvenc", []):
        log.info("video encoder: h264_nvenc (NVIDIA)")
        return _EncoderConfig(
            name="h264_nvenc",
            pre_input=[],
            post_codec=[
                "-preset",
                "p1",
                "-tune",
                "ll",
                "-profile:v",
                "high",
                "-level:v",
                "4.2",
                "-aud",
                "1",
                "-b:v",
                "6000k",
                "-maxrate",
                "6000k",
                "-bufsize",
                "12000k",
            ],
            vf="scale=1920:1080",
        )

    if "h264_vaapi" in available and _test_encoder("h264_vaapi", vaapi_pre):
        log.info("video encoder: h264_vaapi (VA-API)")
        return _EncoderConfig(
            name="h264_vaapi",
            pre_input=vaapi_pre,
            post_codec=[
                "-rc_mode",
                "CBR",
                "-profile:v",
                "high",
                "-level",
                "42",
                "-aud",
                "1",
                "-b:v",
                "6000k",
            ],
            vf="format=nv12,hwupload,scale_vaapi=1920:1080",
        )

    if "libopenh264" in available and _test_encoder("libopenh264", []):
        log.info("video encoder: libopenh264 (software)")
        return _EncoderConfig(
            name="libopenh264",
            pre_input=[],
            post_codec=[
                "-profile:v",
                "constrained_baseline",
                "-level:v",
                "4.2",
                "-b:v",
                "6000k",
                "-maxrate",
                "6000k",
                "-bufsize",
                "12000k",
            ],
            vf="scale=1920:1080",
        )

    log.warning(
        "no working H.264 encoder found — video streaming disabled. "
        "Ensure ffmpeg-free (or equivalent) is installed with libopenh264, "
        "or that VA-API/NVENC drivers are functional."
    )
    return None


_ENCODER: _EncoderConfig | None = _detect_encoder()


# ── NAL unit utilities ────────────────────────────────────────────────────────


def _nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else 0


def _rtp_header(seq: int, ts: int, ssrc: int, marker: bool = False) -> bytearray:
    hdr = bytearray(12)
    hdr[0] = 0x80
    hdr[1] = (0x80 if marker else 0x00) | (_H264_PT & 0x7F)
    struct.pack_into(">H", hdr, 2, seq & 0xFFFF)
    struct.pack_into(">I", hdr, 4, ts & 0xFFFF_FFFF)
    struct.pack_into(">I", hdr, 8, ssrc & 0xFFFF_FFFF)
    return hdr


def _packetize_nal(nal: bytes) -> list[bytes]:
    """
    Return a list of RTP payloads (no header) for one NAL unit.

    Small NAL units (≤ _MTU - 12) become a single-NAL-unit packet.
    Large ones are fragmented using FU-A (RFC 6184 §5.8).
    The caller is responsible for building the RTP header and setting
    the marker bit on the last packet of the last NAL unit in a frame.
    """
    max_payload = _MTU - 12

    if not nal:
        return []

    if len(nal) <= max_payload:
        return [nal]

    # FU-A fragmentation
    nal_hdr = nal[0]
    nal_type = nal_hdr & 0x1F
    nal_ref_idc = nal_hdr & 0x60  # NRI bits
    fu_indicator = nal_ref_idc | 28  # FU-A NAL type = 28

    data = nal[1:]  # strip original NAL header
    payloads: list[bytes] = []
    is_first = True

    while data:
        chunk = data[: max_payload - 2]  # -2 for FU indicator + FU header
        data = data[len(chunk) :]
        is_last = not data

        fu_hdr = (0x80 if is_first else 0x00) | (0x40 if is_last else 0x00) | nal_type
        payloads.append(bytes([fu_indicator, fu_hdr]) + chunk)
        is_first = False

    return payloads


# ── Encryption (mirrors VoiceClient._encrypt_* for video packets) ─────────────


def _encrypt(
    header: bytes,
    payload: bytes,
    mode: str,
    secret_key: list[int],
    nonce_counter: list[int],  # mutable single-element list so we can increment
) -> bytes:
    key = bytes(secret_key)

    if mode == "aead_xchacha20_poly1305_rtpsize":
        aead_box = nacl.secret.Aead(key)
        nonce: bytes | bytearray = bytearray(24)
        struct.pack_into(">I", nonce, 0, nonce_counter[0])
        nonce_counter[0] = (nonce_counter[0] + 1) & 0xFFFF_FFFF
        ct = aead_box.encrypt(payload, bytes(header), bytes(nonce)).ciphertext
        return bytes(header) + ct + bytes(nonce[:4])

    if mode == "xsalsa20_poly1305":
        box = nacl.secret.SecretBox(key)
        nonce = bytearray(24)
        nonce[:12] = header
        return bytes(header) + box.encrypt(payload, bytes(nonce)).ciphertext

    if mode == "xsalsa20_poly1305_suffix":
        box = nacl.secret.SecretBox(key)
        nonce_bytes: bytes = nacl.utils.random(24)
        return (
            bytes(header) + box.encrypt(payload, nonce_bytes).ciphertext + nonce_bytes
        )

    if mode == "xsalsa20_poly1305_lite":
        box = nacl.secret.SecretBox(key)
        nonce = bytearray(24)
        struct.pack_into(">I", nonce, 0, nonce_counter[0])
        nonce_counter[0] = (nonce_counter[0] + 1) & 0xFFFF_FFFF
        ct = box.encrypt(payload, bytes(nonce)).ciphertext
        return bytes(header) + ct + bytes(nonce[:4])

    raise ValueError(f"Unknown voice encryption mode: {mode!r}")


# ── Audio FIFO source ─────────────────────────────────────────────────────────


class _AudioPipeSource(discord.AudioSource):
    """Reads raw PCM S16LE stereo 48 kHz from a FIFO into 20-ms frames."""

    FRAME_SIZE: int = 3840  # 48000 Hz × 2 ch × 2 bytes × 0.020 s

    def __init__(self, f) -> None:
        self._f = f

    def read(self) -> bytes:
        data = self._f.read(self.FRAME_SIZE)
        return data if len(data) == self.FRAME_SIZE else b""

    def cleanup(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass

    def is_opus(self) -> bool:
        return False


# ── H264VideoPlayer ───────────────────────────────────────────────────────────


class H264VideoPlayer(threading.Thread):
    """
    Reads H.264 Annex B from an FFmpeg subprocess and streams it to Discord.

    A single FFmpeg process outputs video to stdout (pipe:1) and raw PCM audio
    to a named FIFO.  The FIFO is consumed by _AudioPipeSource in tv.py so both
    streams are demuxed from the same packet sequence, eliminating A/V desync
    caused by two independent HTTP connections to the same live-stream URL.

    Frame grouping: Access Unit Delimiters (NAL type 9) mark frame boundaries.
    All NAL units between two AUDs share one RTP timestamp.  The marker bit is
    set on the last RTP packet of the last NAL unit of each frame.
    """

    def __init__(
        self,
        url: str,
        voice_client: discord.VoiceClient,
        fps: float = 25.0,
        live: bool | None = True,
        audio: bool = True,
        probe_size: int = 2_000_000,
    ) -> None:
        super().__init__(name="H264VideoPlayer", daemon=True)
        self._url = url
        self._vc = voice_client
        self._fps = fps
        self._end = threading.Event()
        self._proc: subprocess.Popen | None = None

        self._live = live
        self._audio = audio
        self._probe_size = probe_size
        self._seq: int = 0
        self._ts: int = 0
        self._ts_inc: int = round(_CLOCK / fps)
        self._ssrc: int = voice_client.ssrc + 1  # VIDEO_SSRC_OFFSET = 1
        self._packets_sent: int = 0
        self._nonce: list[int] = [_VIDEO_NONCE_BASE]  # mutable for _encrypt()

        # Set by _emit() the moment the first video frame is transmitted.
        # stream.py waits on this before calling vc.play() so audio doesn't
        # get ahead of video during Discord's video jitter-buffer fill phase.
        self.first_frame_sent: threading.Event = threading.Event()

        self._audio_fifo: str = f"/tmp/slopsoil_{self._ssrc}_audio.fifo"
        self._ensure_fifo()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def audio_fifo(self) -> str:
        return self._audio_fifo

    def stop(self) -> None:
        self._end.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        """SIGTERM then SIGKILL if needed, then reap."""
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("FFmpeg did not exit after SIGTERM — sending SIGKILL")
            proc.kill()
            proc.wait()

    def run(self) -> None:
        try:
            self._stream()
        except Exception:
            log.exception("H264VideoPlayer error")
        finally:
            if self._proc is not None:
                self._kill_proc(self._proc)
            self._cleanup_fifo()
        log.info("H264VideoPlayer stopped")

    # ── FIFO helpers ──────────────────────────────────────────────────────────

    def _ensure_fifo(self) -> None:
        try:
            os.remove(self._audio_fifo)
        except FileNotFoundError:
            pass
        os.mkfifo(self._audio_fifo)

    def _cleanup_fifo(self) -> None:
        try:
            os.remove(self._audio_fifo)
        except OSError:
            pass

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ffmpeg_cmd(self) -> list[str]:
        enc = _ENCODER
        assert enc is not None, "H264VideoPlayer started with no encoder available"

        probe_args = ["-probesize", str(self._probe_size), "-analyzeduration", str(self._probe_size)]
        pre_input = enc.pre_input
        rate_args: list[str] = []
        fflags = "+discardcorrupt"
        # -reconnect flags are HTTP-protocol-specific; FFmpeg rejects them for local files.
        reconnect_args = (
            ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if self._url.startswith(("http://", "https://", "rtmp://", "rtsp://"))
            else []
        )
        video_out_args = [
            "-map", "0:v:0",
            "-vf", enc.vf,
            "-c:v", enc.name,
            *enc.post_codec,
            "-r", str(int(self._fps)),
            "-g", str(int(self._fps)),
            "-f", "h264",
            "pipe:1",
        ]

        return [
            "ffmpeg",
            "-y",  # overwrite FIFO without interactive prompt
            "-loglevel",
            "warning",
            # Tolerate initial decode errors (mpeg2video frames before the first
            # sequence header have unknown dimensions; let FFmpeg keep going until
            # it finds a valid GOP rather than aborting the pipeline)
            "-max_error_rate",
            "1",
            *pre_input,
            *probe_args,
            "-fflags",
            fflags,
            *rate_args,
            *reconnect_args,
            "-i",
            self._url,
            *video_out_args,
            # Audio → FIFO consumed by _AudioPipeSource in stream.py.
            # If the source has no audio track (some IPTV streams are video-only,
            # or audio lives in a separate HLS rendition FFmpeg doesn't auto-select),
            # inject a lavfi silence source as input 1 so the FIFO writer always
            # exists and GoLiveAudioSender doesn't hang waiting for data.
            *(
                []
                if self._audio
                else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            ),
            "-map",
            "0:a:0" if self._audio else "1:a:0",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "s16le",
            self._audio_fifo,
        ]

    def _send_frame(self, nal_units: list[bytes]) -> None:
        """Packetize, encrypt, and send all NAL units for one video frame."""
        if not nal_units:
            return

        conn = self._vc._connection
        mode = self._vc.mode
        key = self._vc.secret_key

        # DAVE access-unit-level encryption: encrypt the complete Annex B frame
        # once, then split the encrypted output back using input NAL sizes.
        # Ciphertext is the same size as plaintext; the supplemental trailer
        # (tag + LEB128 nonce + unencrypted ranges + 0xFAFA marker) is appended
        # after the last NAL and must ride along on the last RTP payload.
        _dave = (
            conn.dave_session
            if (conn.dave_session and hasattr(conn.dave_session, "encrypt_h264"))
            else None
        )

        rtp_nals: list[bytes]
        if _dave is not None:
            annex_b = b"".join(b"\x00\x00\x00\x01" + nal for nal in nal_units)
            enc_frame = _dave.encrypt_h264(self._ssrc, annex_b)
            if enc_frame is not annex_b:
                # Encrypted: split output using input NAL sizes, append trailer to last
                rtp_nals = []
                offset = 0
                for nal in nal_units:
                    offset += 4  # skip 4-byte start code
                    rtp_nals.append(enc_frame[offset : offset + len(nal)])
                    offset += len(nal)
                if offset < len(enc_frame) and rtp_nals:
                    rtp_nals[-1] = rtp_nals[-1] + enc_frame[offset:]
            else:
                rtp_nals = list(nal_units)  # passthrough: DAVE key not yet ready
        else:
            rtp_nals = list(nal_units)

        # Collect all payloads so we know which is the very last one
        all_payloads: list[bytes] = []
        for nal in rtp_nals:
            all_payloads.extend(_packetize_nal(nal))

        if not all_payloads:
            return

        for i, payload in enumerate(all_payloads):
            marker = i == len(all_payloads) - 1
            hdr = _rtp_header(self._seq, self._ts, self._ssrc, marker=marker)
            try:
                packet = _encrypt(bytes(hdr), payload, mode, key, self._nonce)
                self._vc._connection.send_packet(packet)
                self._packets_sent += 1
            except OSError:
                log.debug("Video packet dropped (seq=%d)", self._seq)
            self._seq = (self._seq + 1) & 0xFFFF

        self._ts = (self._ts + self._ts_inc) & 0xFFFF_FFFF

    # Transient errors emitted by libopenh264 when it receives P-frames before
    # the first IDR at stream start.  They resolve within the first GOP and do
    # not affect the output stream, so we demote them to DEBUG.
    _STARTUP_NOISE = frozenset(
        [
            "DecodeFrame failed",
            "no exist Sequence Parameter Sets",
            "Error submitting packet to decoder",
        ]
    )

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        t_start = time.monotonic()
        for raw in self._proc.stderr:
            msg = raw.decode(errors="replace").rstrip()
            if (
                time.monotonic() - t_start < 10.0
                and any(n in msg for n in self._STARTUP_NOISE)
            ):
                log.debug("ffmpeg (startup): %s", msg)
            else:
                log.warning("ffmpeg video: %s", msg)

    def _stream(self) -> None:
        conn = self._vc._connection
        dave_ver = getattr(conn, "dave_protocol_version", 0)
        dave_ready = (
            getattr(conn.dave_session, "ready", False) if conn.dave_session else False
        )
        log.info(
            "DAVE state: protocol_version=%d, session=%s, ready=%s, mode=%s",
            dave_ver,
            "present" if conn.dave_session else "absent",
            dave_ready,
            conn.mode,
        )
        if conn.dave_session and hasattr(conn.dave_session, "register_video_ssrc"):
            try:
                conn.dave_session.register_video_ssrc(self._ssrc)
                log.info("DAVE: registered video SSRC %d with H264 codec", self._ssrc)
            except Exception:
                log.warning("DAVE: failed to register video SSRC", exc_info=True)
        cmd = self._ffmpeg_cmd()
        from urllib.parse import urlparse, urlunparse

        _p = urlparse(self._url)
        _safe = urlunparse(
            _p._replace(netloc=(_p.hostname or "") + (f":{_p.port}" if _p.port else ""))
        )
        log.info("Starting H.264 video stream from %s", _safe)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # If stop() was called before Popen completed, terminate immediately.
        if self._end.is_set():
            self._kill_proc(self._proc)
            return
        threading.Thread(
            target=self._drain_stderr, daemon=True, name="ffmpeg-stderr"
        ).start()
        assert self._proc.stdout is not None

        buf = bytearray()
        frame: list[bytes] = []  # NAL units for the current frame
        pending: list[bytes] = []  # SPS/PPS/SEI to prepend to the next slice

        # Wall-clock pacing state.  _t0 is set on the first frame so that the
        # pacing clock starts when real output begins (not when FFmpeg starts
        # buffering).  Every call to _emit() advances _n and sleeps until the
        # next frame deadline, keeping video at exactly self._fps on average.
        _t0: float | None = None
        _n = 0

        def _emit(f: list[bytes]) -> bool:
            """Send a frame and pace to wall clock. Returns True → stop."""
            nonlocal _t0, _n
            if not f:
                return False
            if _t0 is None:
                _t0 = time.monotonic()
            self._send_frame(f)
            if not self.first_frame_sent.is_set():
                self.first_frame_sent.set()
            _n += 1
            due = _t0 + _n / self._fps
            slack = due - time.monotonic()
            if slack > 0.001:
                return self._end.wait(timeout=slack)
            return self._end.is_set()

        while not self._end.is_set():
            chunk = self._proc.stdout.read(65_536)
            if not chunk:
                break

            buf += chunk

            # Split on start codes.  All parts except the last are complete.
            # The last part may be cut off mid-NAL — keep it for next iteration.
            parts = _START_RE.split(bytes(buf))
            _stop = False
            for raw in parts[:-1]:
                nal = raw.rstrip(b"\x00")
                if not nal:
                    continue

                nt = _nal_type(nal)
                if nt == _NAL_AUD:
                    # AUD: explicit frame boundary
                    if frame:
                        if _emit(frame):
                            _stop = True
                            break
                        frame = []
                elif nt in _SLICE_NAL_TYPES:
                    # New slice = new frame; flush whatever was accumulating
                    if frame:
                        if _emit(frame):
                            _stop = True
                            break
                    frame = pending + [nal]
                    pending = []
                elif nt in _PARAM_NAL_TYPES:
                    if nt == 7:  # SPS: rewrite VUI so Discord accepts the stream
                        nal = rewrite_sps_vui(nal)
                    pending.append(nal)
                # else: filler, end-of-stream, etc. — discard

            if _stop:
                break
            buf = bytearray(parts[-1])  # incomplete tail, carry forward

        # Flush any remaining NAL units
        if buf:
            nal = bytes(buf).rstrip(b"\x00")
            if nal:
                if _nal_type(nal) == 7:
                    nal = rewrite_sps_vui(nal)
                frame.append(nal)
        _emit(frame)

        # Reap the process; SIGKILL if it's still alive after a short grace period.
        if self._end.is_set() and self._proc.poll() is None:
            self._kill_proc(self._proc)
        try:
            rc = self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("FFmpeg still running after stream end — killing")
            self._proc.kill()
            rc = self._proc.wait()
        log.info("H.264 video stream ended (exit code %d)", rc)
