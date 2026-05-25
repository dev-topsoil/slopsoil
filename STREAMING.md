# How slopsoil Streams Video to Discord

This document covers the internals of slopsoil's streaming pipeline, the runtime patches applied to `discord.py-self`, and the protocol-level discoveries that were required to make H.264 video work reliably in Discord voice channels.

---

## Table of Contents

- [Overview](#overview)
- [The Two Streaming Modes](#the-two-streaming-modes)
- [The FFmpeg Pipeline](#the-ffmpeg-pipeline)
- [H.264 Packetization (RFC 6184)](#h264-packetization-rfc-6184)
- [SPS/VUI Rewriting — The Critical Discovery](#spsvui-rewriting--the-critical-discovery)
- [RTP Encryption](#rtp-encryption)
- [DAVE E2EE — Discord's End-to-End Encryption](#dave-e2ee--discords-end-to-end-encryption)
- [The davey_compat Shim](#the-davey_compat-shim)
- [Patches to discord.py-self](#patches-to-discordpy-self)
- [The Go-Live Connection](#the-go-live-connection)
- [IPTV and HLS Stream Handling](#iptv-and-hls-stream-handling)
- [The libx264 Problem](#the-libx264-problem)
- [Audio/Video Sync](#audiovideo-sync)
- [Encoder Detection and Fallback](#encoder-detection-and-fallback)

---

## Overview

Discord's voice system uses a custom RTP-over-UDP protocol for audio. For video (screenshare / go-live), it extends this same protocol with H.264 video packets, a separate SSRC, and additional WebSocket negotiation opcodes.

`discord.py-self` exposes enough of the voice internals to send raw UDP packets, but it has no built-in concept of video. slopsoil adds video support by:

1. Patching the voice WebSocket handshake to declare video capability
2. Encoding video with FFmpeg into raw H.264 Annex B bytestream
3. Parsing and rewriting SPS NAL units to meet Discord's requirements
4. Packetizing NAL units into RTP packets (RFC 6184)
5. Encrypting those packets (same modes as audio)
6. Sending them over the existing voice UDP socket (or a separate go-live socket)

---

## The Two Streaming Modes

### Camera / Self-Video Mode

The voice connection sends video via opcode 12 (`VIDEO`) on the voice WebSocket, declaring a `video_ssrc = audio_ssrc + 1`. Video packets share the same UDP socket as audio and are differentiated by SSRC. This appears to other users as the bot "using their camera."

### Go-Live / Screenshare Mode

Discord's screenshare (go-live) uses a *separate* WebSocket connection to a dedicated go-live server and a *separate* UDP socket. The flow is:

1. Send op 18 (`STREAM_CREATE`) on the main voice WebSocket, specifying the guild and channel
2. Discord responds with op 21 (`STREAM_SERVER_UPDATE`) containing the go-live server URL and stream key
3. Open a new WebSocket to that server and complete a second full IDENTIFY / SELECT_PROTOCOL / SESSION_DESCRIPTION handshake — this time as a streaming sender, not just a voice participant
4. Send video and audio over the go-live UDP socket
5. When done, send op 19 (`STREAM_DELETE`) to tear down the go-live session

The go-live connection is implemented in `cogs/golive.py::GoLiveConnection`. It mimics enough of `discord.py-self`'s `VoiceConnectionState` interface that the same patched WebSocket methods (from `video_compat.py`) work against both the regular voice connection and the go-live connection.

Screenshare is the default mode because it looks better: viewers see a proper "go-live" stream UI with the preview thumbnail and the ability to fullscreen.

---

## The FFmpeg Pipeline

A single FFmpeg subprocess handles both audio and video. Using one process for both streams is critical: two separate processes reading from the same network source would desync immediately as each independently buffers data.

```
Stream URL (HTTP/HLS/RTSP/file)
         │
         ▼
    [FFmpeg process]
         │
         ├──► stdout (pipe:1)  ──► H.264 Annex B bytestream ──► H264VideoPlayer thread
         │
         └──► /tmp/slopsoil_{ssrc}_audio.fifo  ──► raw PCM S16LE 48kHz stereo
                                                        │
                                                        ▼
                                               _AudioPipeSource
                                                        │
                                                        ▼
                                              VoiceClient.play()
                                           (discord.py's Opus encoder)
```

The audio FIFO is a named pipe (`mkfifo`). FFmpeg writes PCM to it; the audio player reads 20ms frames (3840 bytes = 48000 Hz × 2 channels × 2 bytes × 0.02s) and feeds them to discord.py's built-in Opus encoder and audio sender.

Key FFmpeg flags:

- `-max_error_rate 1` — tolerates decode errors during stream startup without aborting
- `-fflags +discardcorrupt` — discards corrupt packets instead of failing
- `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` — for HTTP streams: auto-reconnect on drop
- `-r {fps} -g {fps}` — force output framerate and GOP size (one I-frame per second, eliminating B-frames)
- `-f h264 pipe:1` — output raw Annex B bytestream to stdout (no container overhead)

For live streams (IPTV, TVheadend), the encoder is always told `live=True`, which ensures B-frames are disabled at the encoder level — necessary because of the SPS `max_num_reorder_frames=0` constraint described below.

---

## H.264 Packetization (RFC 6184)

H.264 Annex B uses a bytestream format with start codes (`\x00\x00\x00\x01` or `\x00\x00\x01`) between NAL units. Discord's RTP protocol follows RFC 6184, which defines how to carry H.264 in RTP packets.

The packetizer in `cogs/video_player.py`:

1. **Reads the stdout stream** in 64 KiB chunks
2. **Splits on H.264 start codes** (`\x00\x00\x00?\x01`) using a regex
3. **Groups NAL units into frames** using Access Unit Delimiters (NAL type 9, `AUD`). FFmpeg emits an AUD before each frame when encoding to Annex B, making frame boundaries explicit.
4. **Rewrites the SPS NAL** (NAL type 7) before packetization — see next section
5. **Packetizes each NAL** into RTP:
   - NAL ≤ 1188 bytes → Single NAL Unit packet (NAL header byte is the RTP payload type)
   - NAL > 1188 bytes → FU-A fragmentation:
     - First fragment: FU indicator (`NRI | 28`) + FU header (`0x80 | original_type`) + data
     - Middle fragments: FU indicator + FU header (`original_type`) + data
     - Last fragment: FU indicator + FU header (`0x40 | original_type`) + data
6. **Prepends a 12-byte RTP header** to each packet:
   - Version=2, no padding, no extension, CC=0
   - Marker bit set on the last packet of each frame (signals frame end to the decoder)
   - Payload type 101 (H.264, declared during SELECT_PROTOCOL)
   - 16-bit sequence number, incrementing per packet
   - 32-bit timestamp in 90kHz clock units (frame index × 90000 / fps)
   - SSRC = audio_ssrc + 1

The MTU is 1200 bytes (RTP payload). Subtracting 12 bytes for the RTP header and 2 bytes for FU indicator + FU header leaves 1186 bytes per fragment.

---

## SPS/VUI Rewriting — The Critical Discovery

This is the single most important implementation detail in the entire project.

### The problem

When you send an H.264 stream to Discord's voice server and Discord drops it immediately with error 2015, it's because the SPS (Sequence Parameter Set) NAL unit is missing required VUI (Video Usability Information) parameters.

Discord requires:
- `bitstream_restriction_flag = 1`
- `max_num_reorder_frames = 0`

Without both of these, Discord's video decoder or media server rejects the stream. The error manifests as the video connection appearing to succeed (WebSocket handshake completes, SESSION_DESCRIPTION is received) but no video appearing to viewers, or the go-live stream dropping within one frame.

### Why max_num_reorder_frames=0 matters

`max_num_reorder_frames` tells the decoder how many frames it must buffer before it can display the first frame. A value of 0 means frames must be displayed in decode order — there is no reordering buffer. This is only valid if the stream contains **no B-frames**, since B-frames are bidirectionally predicted and require future frames to be decoded before past frames can be displayed.

Setting `max_num_reorder_frames=0` and then sending a stream with B-frames will produce corrupted video. This is why all IPTV and live streams use `-g {fps}` (disabling B-frames by keeping GOP size equal to framerate) and why the `live=True` parameter forces B-frame-free encoder settings.

### The implementation

`rewrite_sps_vui()` in `cogs/video_player.py` implements a complete H.264 bitstream parser and rewriter:

1. **Strip emulation prevention bytes** — H.264 inserts `0x03` bytes into the RBSP to prevent false start code matches (whenever `0x00 0x00` would appear in the data). The parser must remove these before interpreting the bits, and reinsert them afterward.

2. **Parse the SPS RBSP** — The SPS contains dozens of fields encoded with Exp-Golomb (variable-length) coding. The parser must read past all of them to reach the optional `vui_parameters_present_flag`.

3. **Handle high profiles** — For profile IDCs 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, and 135 (High and higher profiles), the SPS contains additional fields: chroma format, bit depth, scaling lists, and potentially HRD (Hypothetical Reference Decoder) parameters inside the VUI. All of these must be parsed correctly.

4. **Rewrite the VUI** — If `vui_parameters_present_flag=0`, a minimal VUI is appended. If it's already present:
   - Parse the existing VUI fields until `bitstream_restriction_flag`
   - Force `bitstream_restriction_flag=1`
   - Force `max_num_reorder_frames=0` (encoded as Exp-Golomb `0b1`, a single bit)
   - Copy all remaining VUI bits unchanged

5. **Reinsert emulation prevention bytes** — Scan the rewritten RBSP and insert `0x03` wherever three consecutive bytes would otherwise form a false start code.

6. **Reconstruct the NAL** — Prepend the NAL header byte (0x67 for SPS, nal_ref_idc=3, nal_unit_type=7).

If parsing fails at any step, the original unmodified NAL is passed through. Corrupting the SPS is worse than sending one without the required flags — better to let the decoder try than to send garbage.

### Why not just set encoder flags?

You might think `-x264opts vui-timing-info=1` or similar FFmpeg flags would solve this. The problem is:

- Not all encoders expose VUI parameters as configurable options
- Hardware encoders (`h264_nvenc`, `h264_vaapi`) often ignore or override these settings
- Even when set, the `bitstream_restriction_flag` and `max_num_reorder_frames` specifically may not be emitted
- For IPTV/HLS streams where the source is already H.264 (passthrough), there's no encoder — the incoming SPS must be patched on the fly

Rewriting the SPS in software, regardless of encoder, is the only approach that works consistently.

---

## RTP Encryption

Discord's voice protocol encrypts all RTP packets (audio and video) using the secret key established during the WebSocket SESSION_DESCRIPTION handshake. The encryption mode is negotiated during SELECT_PROTOCOL.

slopsoil supports all modes that `discord.py-self` negotiates:

| Mode | Algorithm | Nonce |
|---|---|---|
| `aead_xchacha20_poly1305_rtpsize` | XChaCha20-Poly1305 | 24-byte, derived from packet + RTP size prefix |
| `xsalsa20_poly1305` | XSalsa20-Poly1305 | 24 bytes, first 12 from RTP header, rest zeros |
| `xsalsa20_poly1305_suffix` | XSalsa20-Poly1305 | 24 bytes random, appended to packet |
| `xsalsa20_poly1305_lite` | XSalsa20-Poly1305 | 4-byte counter, appended to packet |

The active mode and key come from the voice connection state. Video packets are encrypted the same way as audio packets — the SSRC in the RTP header is what differentiates them, not the encryption.

### DAVE-layer encryption

When DAVE (Discord's E2EE protocol, see below) is active, the RTP *payload* is encrypted with the DAVE session key *before* the RTP-level encryption is applied. The result is double-encrypted: outer layer is the standard RTP encryption (same key for all participants on the server), inner layer is the DAVE E2EE layer (only decryptable by session members).

For video, `encrypt_h264(video_ssrc, annex_b_frame)` encrypts the full Annex B frame. The encrypted bytes replace the plaintext payload before packetization. This means DAVE encryption happens at the frame level, not the RTP packet level — the packetizer runs on encrypted data.

---

## DAVE E2EE — Discord's End-to-End Encryption

DAVE (Discord Audio/Video Encryption) is Discord's E2EE protocol for voice channels, built on the MLS (Message Layer Security) protocol. It was introduced in 2024 and is required for all voice connections in supported servers.

MLS is a group key agreement protocol. Each participant generates a keypair; proposals to add or remove members are committed by the group, and a shared symmetric key is derived. The DAVE session manages this lifecycle.

Key facts about DAVE in this context:

- The `dave.py` package wraps DisnakeDev's `libdave` C library, which is the official Discord implementation
- Each voice connection (main voice + go-live) has a separate DAVE session
- Video SSRCs must be explicitly registered with the DAVE session (`register_video_ssrc()`) so the library knows to use video-specific nonce counters
- Video uses nonce base `0x80000000` to avoid collision with audio's nonce counter (which starts at 0)
- The DAVE session status transitions: inactive (0) → pending proposal (1) → active (3)
- Encryption is only applied when the session status is active

The go-live connection has its own DAVE session. This is handled by `GoLiveConnection` in `cogs/golive.py`, which parses the DAVE opcodes from the go-live WebSocket and manages the session lifecycle independently of the main voice connection.

---

## The davey_compat Shim

`discord.py-self` 2.1.0 imports a package called `davey` — a Rust extension that was meant to implement DAVE. It doesn't work. The package is either broken or the API changed and was never updated.

`davey_compat.py` is a pure-Python drop-in replacement that wraps the working `dave.py` library and exposes `davey`'s API. It is injected at import time, before any voice code runs:

```python
# bot.py
import discord
import davey_compat

discord.voice_state.davey = davey_compat
discord.gateway.davey = davey_compat
davey_compat.patch_reinit(discord.voice_state)
```

The shim implements:

**`DaveSession`** — wraps `dave.Session`:
- `encrypt_opus(frame)` → encrypts 20ms Opus frame with audio SSRC nonce counter
- `encrypt_h264(video_ssrc, frame)` → encrypts Annex B frame with video nonce counter (base 0x80000000)
- `register_video_ssrc(video_ssrc)` → tells libdave about the video SSRC
- `process_proposals(data)` → handles MLS Add/Remove proposals; reads recognized channel members from `_voice_state` to determine which proposals to accept
- `process_commit(data)` → processes committed epoch transitions; returns `CommitWelcome` bytes
- `process_welcome(data)` → processes Welcome messages for new joiners

**`ProposalsOperationType`** — enum: `Append = 0`, `Revoke = 1`

**`CommitWelcome`** — wraps commit + welcome bytes (libdave concatenates them into one blob)

**`SessionStatus`** — constants: `INACTIVE = 0`, `PENDING = 1`, `ACTIVE = 3`

**`patch_reinit()`** — monkey-patches `VoiceConnectionState.reinit_dave_session()` to inject a back-reference to the voice state, which `process_proposals()` needs to look up the list of recognized channel members.

The `_voice_state` back-reference is the only way to know which user IDs are currently in the voice channel, which is needed to decide whether to accept or reject MLS proposals. Without it, proposals would always be rejected and DAVE sessions would never become active.

---

## Patches to discord.py-self

`video_compat.py` patches three methods on `discord.gateway.DiscordVoiceWebSocket`. All patches are applied at startup via `video_compat.patch_video(discord.gateway)`.

### 1. `identify()` — declare video capability

The IDENTIFY payload (op 0) is the first message sent to Discord's voice server. Without the `"video": true` field and a `"streams"` descriptor, Discord's voice server ignores any subsequent VIDEO opcode (op 12). It silently accepts the connection but never establishes a video channel.

Patched additions to the IDENTIFY payload:
```json
{
  "video": true,
  "streams": [
    {
      "type": "video",
      "rid": "100",
      "quality": 100
    }
  ]
}
```

### 2. `select_protocol()` — declare codecs

The SELECT_PROTOCOL payload (op 1) tells the voice server what audio/video codecs the client supports. Without a `"codecs"` array, the server defaults to audio-only mode.

Patched additions:
```json
{
  "codecs": [
    {
      "name": "opus",
      "type": "audio",
      "priority": 1000,
      "payload_type": 120
    },
    {
      "name": "H264",
      "type": "video",
      "priority": 1000,
      "payload_type": 101,
      "rtx_payload_type": 102,
      "parameters": {}
    }
  ]
}
```

Payload type 101 is the value used in the RTP header's PT field for all H.264 packets.

### 3. `client_connect()` — send VIDEO opcode with stream descriptor

The VIDEO opcode (op 12) is sent after SESSION_DESCRIPTION is received, signaling that this client is a video sender. Without the correct `streams` array, the server doesn't know the video capabilities and won't relay packets.

Patched payload additions:
```json
{
  "video_ssrc": "<audio_ssrc + 1>",
  "rtx_ssrc": "<audio_ssrc + 2>",
  "streams": [
    {
      "type": "video",
      "rid": "100",
      "ssrc": "<audio_ssrc + 1>",
      "rtx_ssrc": "<audio_ssrc + 2>",
      "quality": 100,
      "active": true,
      "max_bitrate": 10000000,
      "max_framerate": 60,
      "max_resolution": {
        "type": "fixed",
        "width": 1920,
        "height": 1080
      }
    }
  ]
}
```

The `rtx_ssrc` is declared but not actually used (no RTX/retransmission is implemented). Discord's server expects it to be declared regardless.

---

## The Go-Live Connection

`GoLiveConnection` in `cogs/golive.py` establishes the screenshare stream. It handles:

- WebSocket connection to the go-live server URL (received from Discord in op 21)
- A complete second IDENTIFY → HELLO → SELECT_PROTOCOL → SESSION_DESCRIPTION handshake
- Its own Opus encoder for audio (separate from the main voice connection's audio)
- Its own UDP socket for sending video and audio RTP packets
- Its own DAVE session for E2EE

The go-live audio sender (`GoLiveAudioSender`) runs in a daemon thread. It reads raw PCM frames from the same FIFO that the main voice connection's audio would use, encodes them to Opus, wraps them in RTP, and sends them through the go-live UDP socket. This means audio goes through the go-live connection when streaming via screenshare — the main voice connection's audio player is not used in go-live mode.

The reason both audio and video go through the go-live connection is that Discord's go-live system routes audio+video together. If audio came from the main voice connection and video from the go-live connection, viewers watching the screenshare would hear silence.

---

## IPTV and HLS Stream Handling

### M3U parsing

IPTV sources are M3U playlists. The parser extracts per-channel metadata from `EXTINF` tags:
- `tvg-id` — channel identifier for EPG matching
- `tvg-name` — display name
- `group-title` — category
- Stream URL (the line after each `EXTINF`)

EPG URLs are extracted from the M3U header (`url-tvg` or `x-tvg-url` attributes on the `#EXTM3U` line).

### HLS variant resolution

Many IPTV providers serve an HLS master playlist rather than a direct stream URL. A master playlist lists multiple variant streams at different bitrates. slopsoil picks the highest-bandwidth variant.

One discovered edge case: some providers (e.g., thetvapp.to-style streams) use HLS audio renditions — a separate `.m3u8` URL for audio. These renditions often return HTTP 500 when requested. The workaround is to ignore the audio rendition URL and use only the video variant URL, which contains embedded audio in the TS segments. `extract_hls_variant_url()` in `cogs/iptv.py` implements this.

### Stream probing

Before starting a stream, `probe_stream()` runs `ffprobe` against the URL to validate reachability and detect codec, framerate, resolution, and whether B-frames are present (`has_b_frames`). For HLS, the probe window is set to 10MB/10s to give FFmpeg enough time to resolve the master playlist, download segment manifests, and inspect actual TS segments.

If probing succeeds, the detected framerate is used for the encoder's `-r` and `-g` flags. If it fails, a safe default (25fps) is used.

---

## The libx264 Problem

`libx264` output causes Discord's video server to drop the stream after exactly one frame. This was discovered empirically: streams encoded with `libx264` would successfully complete the WebSocket handshake and even show the first frame to viewers, then immediately stop.

The root cause is not fully understood, but the observed behavior is consistent. It may relate to how `libx264` constructs the SEI (Supplemental Enhancement Information) NAL units, the specific values in the HRD parameters, or something else in the bitstream that Discord's server rejects.

Encoders that work correctly:
- `h264_nvenc` (NVIDIA hardware)
- `h264_vaapi` (VA-API)
- `libopenh264` (Cisco, baseline profile only)

This is why `ffmpeg-free` (which includes `libopenh264` but not `libx264`) is the required FFmpeg package, and why the Dockerfile explicitly documents this constraint. Using RPM Fusion's `ffmpeg` package on Fedora would introduce `libx264` as the preferred software encoder and break streaming.

---

## Audio/Video Sync

A/V sync is maintained at two levels:

### FFmpeg-level sync

Using a single FFmpeg process for both audio and video ensures the streams are read from the same demuxed packet sequence. If two separate FFmpeg processes were used, each would independently buffer and read from the network, causing them to drift apart — especially on HLS streams where segment downloads introduce variable latency.

### Playback-level sync

The `H264VideoPlayer` thread uses wall-clock timing to pace video frames:

```python
deadline = stream_start_time + (frame_count / fps)
sleep_duration = deadline - time.monotonic()
if sleep_duration > 0:
    time.sleep(sleep_duration)
```

If the video thread falls behind (encoding or network took too long), it skips the sleep and sends the next frame immediately. This keeps video from drifting relative to wall clock, which keeps it in sync with audio (which Discord's Opus sender also paces to wall clock via its own 20ms timing).

### First-frame synchronization

When a stream starts, Discord's jitter buffer needs to fill before audio playback begins. If video were sent immediately but audio were delayed, the stream would appear out of sync from the start.

The `first_frame_sent` event solves this: the audio player blocks until the video player sends its first frame. This ensures audio and video start together from Discord's perspective, even though the jitter buffer adds some audio latency on the viewer side.

---

## Encoder Detection and Fallback

`_detect_encoder()` in `cogs/video_player.py` runs at startup and caches the result. It tries each encoder by running a short test encode (1 second of a test signal) and checking for success.

Priority order:

1. **`h264_nvenc`** — lowest latency of all options; preset p1 + tune ll (low-latency)
2. **`h264_vaapi`** — good quality, minimal CPU; requires `/dev/dri`
3. **`libopenh264`** — Cisco's software encoder, constrained baseline profile, reliable
4. **`libx264`** — fastest software option but **broken with Discord** (see above); listed as fallback only for non-Docker environments where ffmpeg-free isn't used
5. **`None`** — audio-only mode; video stream is skipped, only audio is sent

All encoders are configured for low-latency output:
- CBR (constant bitrate), 6 Mbps
- High profile, Level 4.2 (except libopenh264 which only supports constrained baseline)
- No B-frames (via encoder options + `-g {fps}` GOP setting)
- Ultrafast/lowest-latency preset where available
