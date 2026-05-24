# slopsoil — Discord live TV streaming bot

Stream live TV into Discord voice channels from a [TVheadend](https://tvheadend.org) backend or any IPTV M3U playlist. slopsoil transcodes the stream to H.264 and broadcasts it as a go-live screen share so everyone in the voice channel can watch together.

**Key features:**
- **TVheadend integration** — browse live channels with now-playing EPG info and stream by name or channel number
- **IPTV support** — add any M3U playlist source; XMLTV EPG is picked up automatically
- **TV guide search** — find a show by title and start watching immediately or schedule it to start when it airs
- **yt-dlp** — download and stream any YouTube/web video directly with `!yt <url>`
- **H.264 video + Opus audio** — full video streaming into Discord voice, not just audio
- **Docker support** — single `docker compose up` deployment with persistent IPTV source storage

Built in Python using [discord.py-self](https://github.com/dolfies/discord.py-self).

> **Warning:** Self-bots violate Discord's [Terms of Service](https://discord.com/terms). Use at your own risk — your account may be suspended or banned.

![slopsoil streaming a live TV channel via Discord go-live screen share in a voice channel](images/slopsoil.png)

![slopsoil bot commands in Discord showing channel list with EPG now-playing info](images/slopsoil2.png)

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Getting your token](#getting-your-token)
- [Configuration](#configuration)
- [Running](#running)
- [Docker Compose](#docker-compose)
- [Commands](#commands)
  - [yt-dlp video streaming](#yt-dlp-video-streaming)
  - [TVheadend streaming](#tvheadend-streaming)
  - [TV guide search](#tv-guide-search)
  - [IPTV](#iptv)
- [Further reading](#further-reading)

## Requirements

- Python 3.11+
- `ffmpeg` with H.264 encoder support installed and on `$PATH`
- `yt-dlp` (installed automatically via `pip install -r requirements.txt`)
- A Discord user account token

The encoder selection priority is: `libx264` → `h264_nvenc` (NVIDIA) → `h264_vaapi` (VA-API) → `libopenh264`. On Fedora, `ffmpeg-free` (the standard package) does not include `libx264` due to patent restrictions, so the bot falls back to `libopenh264` — this is the tested and working configuration. Do **not** swap to RPM Fusion's `ffmpeg` build; it ships a different FFmpeg version whose `libx264` output causes Discord to drop the video stream after one frame.

```bash
# Fedora / RHEL
sudo dnf install ffmpeg-free

# Ubuntu / Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

## Installation

```bash
pip install -r requirements.txt
```

## Getting your token

1. Open Discord in a browser (not the desktop app)
2. Open DevTools (`F12` or `Ctrl+Shift+I`)
3. Go to the **Network** tab
4. Send any message in any channel
5. Find a request to `discord.com/api` and click it
6. Under **Request Headers**, copy the value of the `Authorization` header

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Your Discord user account token |
| `ALLOWED_USER_IDS` | No | Comma-separated user IDs allowed to send commands |
| `TVHEADEND_URL` | No | TVheadend base URL e.g. `http://192.168.1.100:9981` |
| `TVHEADEND_USER` | No | TVheadend username |
| `TVHEADEND_PASS` | No | TVheadend password |

The `!channels`, `!play`, and `!search` commands are only enabled when all three TVheadend variables are set. The bot logs a warning at startup if they are missing.

IPTV (`!add-source`, `!sources`, `!delete-source`) is always available — it does not require any environment variables.

## Running

```bash
python bot.py
```

## Docker Compose

### First run

```bash
cp .env.example .env   # fill in your values
docker compose up -d
```

The image is built automatically on first run. Logs are visible with:

```bash
docker compose logs -f
```

IPTV source data is stored in a named Docker volume (`slopsoil-data`) mounted at the XDG data directory inside the container, so sources survive container restarts and image rebuilds.

### Rebuilding after updates

After pulling new code or editing any source file, rebuild the image before restarting:

```bash
docker compose up -d --build
```

If you changed `requirements.txt` or the `Dockerfile` itself and want to guarantee a clean slate (no cached layers), do a full rebuild:

```bash
docker compose build --no-cache
docker compose up -d
```

### Stopping

```bash
docker compose down
```

### Hardware encoding (VA-API)

The compose file includes a `devices` block for `/dev/dri`. Comment it out if your host has no GPU or VA-API support:

```yaml
devices:
  - /dev/dri:/dev/dri
```

The bot detects available encoders at startup and falls back to `libopenh264` (software) automatically if the device is absent or unusable, so this is optional.

## Commands

| Command | Description |
|---|---|
| `!ping` | Check if the bot is alive |
| `!help` | Show all available commands |
| `!join` | Join your current voice channel |
| `!leave` | Disconnect from voice |
| `!stop` | Stop the current stream |
| `!yt <url>` | Download a video with yt-dlp and stream it to voice |
| `!channels` | List all channels (TVheadend + IPTV) with now-playing info (paginated) |
| `!play <name or #>` | Stream a TVheadend or IPTV channel into voice |
| `!search <title>` | Find a show in the TV guide; plays now or schedules |
| `!add-source <name> <url>` | Add an IPTV M3U playlist source |
| `!sources` | List all sources and their enabled/disabled state |
| `!sources enable/disable <name>` | Enable or disable a source by name |
| `!delete-source` | Remove an IPTV source |

### yt-dlp video streaming

```
!yt https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

`!yt` downloads the video using [yt-dlp](https://github.com/yt-dlp/yt-dlp) (which supports YouTube, Twitch VODs, and hundreds of other sites), then streams it into your voice channel as a go-live screen share. The downloaded file is stored in a temporary directory and deleted automatically once playback ends or is stopped with `!stop`.

Playlist URLs are intentionally blocked — only a single video is downloaded per command. Use `!stop` to end playback early.

### TVheadend streaming

```
!channels           ← lists TVheadend and IPTV channels with what's on now (paginated)
!play BBC One       ← match by name (case-insensitive substring)
!play 1             ← match by channel number
!stop
```

`!play` will join your current voice channel automatically if the bot isn't already there. It searches TVheadend channels first, then IPTV channels from enabled sources.

### TV guide search

```
!search Speed Racer
```

`!search` queries the TVheadend EPG (electronic programme guide) by show title.

**If the show is currently airing**, the bot switches to that channel immediately — same behaviour as `!play`, but found by programme name rather than channel name.

**If the show is coming up within the next 24 hours**, the bot replies with the channel and airtime and asks whether to schedule a viewing:

```
Speed Racer is on Cartoon Network (#59.3) at 7:00 PM. Schedule a viewing? (y/n)
```

Reply **y** (or **yes**) and the bot will automatically switch to that channel 30 seconds before the show starts, joining whichever voice channel you are in at that time. Any previously scheduled viewing is replaced. Running `!play` also cancels a pending schedule.

### IPTV

```
!add-source <name> <playlist.m3u url>
!sources
!sources enable <name>
!sources disable <name>
```

`!add-source` fetches the M3U playlist from `<url>`, parses all channels, and saves the source (enabled immediately). If the playlist's `#EXTM3U` header contains a `url-tvg` or `x-tvg-url` attribute pointing to an XMLTV EPG feed, it is stored automatically and used to populate now-playing info in `!channels`.

`!sources` lists all sources with their enabled/disabled state.

`!sources enable <name>` / `!sources disable <name>` enables or disables a source by name (case-insensitive substring match). Use `TVheadend` to toggle the TVheadend source, or any part of an IPTV source name.

Once a source is enabled its channels appear in `!channels` and are searchable by `!play`. IPTV channels are streamed by resolving the HLS master playlist to the highest-bandwidth variant and probing the stream before playback.

Source data is persisted to `$XDG_DATA_HOME/slopsoil/sources.json` (default: `~/.local/share/slopsoil/sources.json`).

## Further reading

- [STREAMING.md](STREAMING.md) — how the FFmpeg pipeline, H.264 SPS rewriting, DAVE E2EE encryption, and A/V sync work
- [ARCHITECTURE.md](ARCHITECTURE.md) — project structure and how to add new commands
