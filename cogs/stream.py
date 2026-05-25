"""
Shared streaming infrastructure used by all media cogs (TV, Media, etc.).

start_stream() connects to voice, launches a single FFmpeg process that writes
H.264 to stdout and raw PCM to a named FIFO, and wires both to Discord via the
camera/self-video path.

start_live_stream() does the same but routes audio+video through a separate
go-live (screenshare) voice connection so viewers see it as a screen share.

cancel_stream() tears down any active camera stream for a guild.
cancel_live_stream() tears down any active go-live stream for a guild.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import discord

from cogs.golive import GoLiveAudioSender, GoLiveConnection, _GoLiveVCProxy
from cogs.video_player import _ENCODER as _VIDEO_ENCODER
from cogs.video_player import H264VideoPlayer, _AudioPipeSource

if TYPE_CHECKING:
    from bot import SlopSoil

log = logging.getLogger(__name__)


def _safe_url(url: str) -> str:
    """Strip credentials (user:pass and api_key= query param) for safe logging."""
    p = urlparse(url)
    netloc = (p.hostname or "") + (f":{p.port}" if p.port else "")
    qs = {
        k: v
        for k, v in parse_qs(p.query, keep_blank_values=True).items()
        if k.lower() not in ("api_key", "token", "password")
    }
    return urlunparse(p._replace(netloc=netloc, query=urlencode(qs, doseq=True)))


def cancel_stream(bot: SlopSoil, guild_id: int) -> None:
    """Cancel any active camera stream and video player for a guild."""
    task = bot.stream_tasks.pop(guild_id, None)
    if task and not task.done():
        log.info("cancelling active stream task for guild %s", guild_id)
        task.cancel()
    vp = bot.video_players.pop(guild_id, None)
    if vp is not None:
        log.info("stopping video player for guild %s", guild_id)
        vp.stop()


def cancel_live_stream(bot: SlopSoil, guild_id: int) -> None:
    """Cancel any active go-live stream for a guild.

    Cancels the stream task (whose finally block calls conn.disconnect()) and
    stops the video player.  The GoLiveConnection cleanup — including sending
    op 19 (STREAM_DELETE) on the main gateway — happens asynchronously inside
    the cancelled task's finally block.
    """
    cancel_stream(bot, guild_id)  # reuses task + video_player slots
    # live_connections is cleaned up by the task's finally block


async def start_stream(
    bot: SlopSoil,
    send: Callable,
    guild: discord.Guild,
    voice_channel: discord.VoiceChannel | discord.StageChannel,
    vc: discord.VoiceClient | None,
    title: str,
    url: str,
    subtitle: str = "",
    live: bool | None = True,
    audio: bool = True,
    probe_size: int = 2_000_000,
) -> None:
    """
    Connect to voice and begin streaming a URL.

    send     — async callable for status messages (ctx.send or channel.send)
    title    — primary display name shown to users
    url      — full stream URL passed directly to FFmpeg (may contain credentials)
    subtitle — optional secondary label (channel number, episode code, etc.)
    """
    # ── Voice connection ──────────────────────────────────────────────────────
    if vc:
        if vc.channel != voice_channel:
            log.info("moving to voice channel '%s'", voice_channel)
            await vc.move_to(voice_channel)
    else:
        log.info("connecting to voice channel '%s' in guild '%s'", voice_channel, guild)
        vc = await voice_channel.connect(self_deaf=True)

    if _VIDEO_ENCODER is not None:
        await guild.change_voice_state(
            channel=vc.channel, self_deaf=True, self_video=True
        )
        await vc.ws.client_connect()
    else:
        log.warning("no H.264 encoder available — streaming audio only")

    cancel_stream(bot, guild.id)
    if vc.is_playing():
        vc.stop()

    label = f"**{title}**" + (f" ({subtitle})" if subtitle else "")
    log.info("starting stream: %s → %s", label, _safe_url(url))
    await send(f"▶ {label}")

    # ── Video + audio (single FFmpeg process) ─────────────────────────────────
    video_player: H264VideoPlayer | None = None
    if _VIDEO_ENCODER is not None:
        video_player = H264VideoPlayer(
            url=url, voice_client=vc, fps=60.0,
            live=live, audio=audio, probe_size=probe_size,
        )
        bot.video_players[guild.id] = video_player
        video_player.start()
        log.info("video player started for '%s'", title)

    async def _run_audio() -> None:
        log.info("audio task started for '%s'", title)
        try:
            if video_player is not None:
                log.info("waiting for audio FIFO from FFmpeg (up to 15 s)...")
                try:
                    f = await asyncio.wait_for(
                        asyncio.to_thread(open, video_player.audio_fifo, "rb"),
                        timeout=15.0,
                    )
                except TimeoutError:
                    log.error("timed out waiting for audio FIFO")
                    await send("stream failed to start", delete_after=10)
                    return
                raw: discord.AudioSource = _AudioPipeSource(f)
            else:
                before_opts = (
                    "-probesize 5000000 -analyzeduration 5000000"
                    " -fflags +nobuffer"
                    " -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                )
                raw = discord.FFmpegPCMAudio(
                    url,
                    before_options=before_opts,
                    options="-vn -ar 48000 -ac 2",
                )

            src = discord.PCMVolumeTransformer(raw, volume=1.0)
            done: asyncio.Future = asyncio.get_event_loop().create_future()

            def _after(error: Exception | None) -> None:
                if done.done():
                    return
                if error:
                    done.set_exception(error)
                else:
                    done.set_result(None)

            vc.play(src, after=_after)
            await done
            log.info("stream finished for '%s'", title)
        except asyncio.CancelledError:
            log.info("stream cancelled for '%s'", title)
        except Exception as exc:
            log.exception("stream error for '%s': %s", title, exc)
            try:
                await send(f"stream error: {exc}", delete_after=10)
            except Exception:
                pass
        finally:
            bot.stream_tasks.pop(guild.id, None)
            vp = bot.video_players.pop(guild.id, None)
            if vp is not None:
                vp.stop()
            log.debug("stream cleanup done for guild %s", guild.id)

    audio_task = asyncio.create_task(_run_audio())
    bot.stream_tasks[guild.id] = audio_task


async def start_live_stream(
    bot: SlopSoil,
    send: Callable,
    guild: discord.Guild,
    voice_channel: discord.VoiceChannel | discord.StageChannel,
    vc: discord.VoiceClient | None,
    title: str,
    url: str,
    subtitle: str = "",
    live: bool | None = True,
    audio: bool = True,
    probe_size: int = 2_000_000,
) -> None:
    """
    Connect to voice and begin a go-live screenshare stream.

    Unlike start_stream() which uses the camera/self-video path, this function
    sends op 18 (STREAM_CREATE) on the main gateway to create a dedicated
    go-live stream connection.  Both audio and video are sent through that
    separate stream connection so viewers see the content as a screen share.

    send     — async callable for status messages
    title    — primary display name shown to users
    url      — full stream URL passed directly to FFmpeg
    subtitle — optional secondary label (channel number, etc.)
    live     — True for live/MPEG-2 sources, False for VOD/H.264 copy sources
    """
    if _VIDEO_ENCODER is None:
        log.warning("no H.264 encoder available — go-live requires video encoding")
        await send("go-live requires a working H.264 encoder; none found")
        return

    # ── Voice connection ──────────────────────────────────────────────────────
    if vc:
        if vc.channel != voice_channel:
            log.info("moving to voice channel '%s'", voice_channel)
            await vc.move_to(voice_channel)
    else:
        log.info("connecting to voice channel '%s' in guild '%s'", voice_channel, guild)
        vc = await voice_channel.connect(self_deaf=True)

    cancel_live_stream(bot, guild.id)
    if vc.is_playing():
        vc.stop()

    label = f"**{title}**" + (f" ({subtitle})" if subtitle else "")
    log.info("starting go-live stream: %s → %s", label, _safe_url(url))
    await send(f"📺 {label} (go-live)")

    # ── Go-live connection ────────────────────────────────────────────────────
    conn = GoLiveConnection(bot, guild.id, voice_channel.id, vc)
    try:
        await conn.connect(timeout=30.0)
    except Exception as exc:
        log.exception("failed to establish go-live connection: %s", exc)
        await send(f"go-live connection failed: {exc}", delete_after=10)
        return

    bot.live_connections[guild.id] = conn

    # ── Video (H264VideoPlayer via go-live connection) ────────────────────────
    proxy_vc = _GoLiveVCProxy(conn)
    video_player = H264VideoPlayer(
        url=url, voice_client=proxy_vc, fps=60.0,  # type: ignore[arg-type]
        live=live, audio=audio, probe_size=probe_size,
    )
    bot.video_players[guild.id] = video_player
    video_player.start()
    log.info("go-live video player started for '%s'", title)

    # ── Audio + lifecycle task ────────────────────────────────────────────────
    async def _run_live() -> None:
        log.info("go-live task started for '%s'", title)
        audio_sender: GoLiveAudioSender | None = None
        try:
            # Open audio FIFO — blocks in a thread until FFmpeg opens the write end
            log.info("waiting for audio FIFO from FFmpeg (up to 15 s)...")
            try:
                f = await asyncio.wait_for(
                    asyncio.to_thread(open, video_player.audio_fifo, "rb"),
                    timeout=15.0,
                )
            except TimeoutError:
                log.error("go-live: timed out waiting for audio FIFO")
                await send("go-live stream failed to start", delete_after=10)
                return
            audio_sender = GoLiveAudioSender(
                file_obj=f,
                conn=conn,
            )
            audio_sender.start()
            log.info("go-live audio sender started for '%s'", title)

            # Block until the video player thread exits (stream ends or is stopped)
            await asyncio.to_thread(video_player.join)
            log.info("go-live stream finished for '%s'", title)

        except asyncio.CancelledError:
            log.info("go-live stream cancelled for '%s'", title)
        except Exception as exc:
            log.exception("go-live stream error for '%s': %s", title, exc)
            try:
                await send(f"go-live error: {exc}", delete_after=10)
            except Exception:
                pass
        finally:
            bot.stream_tasks.pop(guild.id, None)
            vp = bot.video_players.pop(guild.id, None)
            if vp is not None:
                vp.stop()
            if audio_sender is not None and audio_sender.is_alive():
                audio_sender.stop()
            live_conn = bot.live_connections.pop(guild.id, None)
            if live_conn is not None:
                try:
                    await live_conn.disconnect()
                except Exception:
                    pass
            log.debug("go-live cleanup done for guild %s", guild.id)

    live_task = asyncio.create_task(_run_live())
    bot.stream_tasks[guild.id] = live_task
