# 🎶 Telegram Music Addon (Python)

A high-performance personal music streaming server that turns any Telegram channel into a fully-featured audio backend. Built as a ground-up Python rewrite of the original Node.js/GramJS project — with identical API shapes, endpoints, and streaming behaviour — using **Telethon**, **FastAPI**, and **Uvicorn**.

Compatible with **BitChord**, **Stremio**, and any client that speaks the addon manifest protocol.

---

## Table of Contents

- [Key Features](#-key-features)
- [Technology Stack](#-technology-stack)
- [Project Structure](#-project-structure)
- [Module-by-Module Explanation](#-module-by-module-explanation)
  - [config.py](#configpy)
  - [cache.py](#cachepy)
  - [login.py](#loginpy)
  - [telegram_client.py](#telegram_clientpy)
  - [botpool.py](#botpoolpy)
  - [search.py](#searchpy)
  - [main.py](#mainpy)
- [Setup Guide](#-setup-guide)
  - [Prerequisites](#1-prerequisites)
  - [Installation](#2-installation)
  - [Generate Session String](#3-generate-telegram-session-string)
  - [Configure Environment](#4-configure-environment-variables)
  - [Start the Server](#5-start-the-server)
- [Environment Variables Reference](#-environment-variables-reference)
- [API Endpoints](#-api-endpoints)
  - [GET /manifest.json](#get-manifestjson)
  - [GET /search](#get-search)
  - [GET /stream/{id}](#get-streamid)
  - [GET /audio/{id}](#get-audioid)
  - [GET /artwork/{id}](#get-artworkid)
  - [GET /isrc/{code}](#get-isrccode)
  - [GET /ping](#get-ping)
  - [GET /refresh](#get-refresh)
- [Bot Pool Setup](#-bot-pool-setup)
- [Cloudflare Tunnel](#-cloudflare-tunnel)
- [Audio Streaming Pipeline](#-audio-streaming-pipeline)
- [RAM & Memory Budget](#-ram--memory-budget)
- [Startup Sequence](#-startup-sequence)
- [Docker Deployment](#-docker-deployment)
- [Quality Tier Logic](#-quality-tier-logic)
- [Atmos Detection Logic](#-atmos-detection-logic)
- [Troubleshooting](#-troubleshooting)
- [What Is NOT Implemented](#-what-is-not-implemented)
- [License](#-license)

---

## 🌟 Key Features

| Feature | Description |
|---|---|
| **Full Format Support** | FLAC, ALAC, MP3, AAC, WAV, OGG, Opus, Dolby Atmos (E-AC-3 JOC / EC3) |
| **HTTP 206 Range Streaming** | Instant seek with block-aligned MTProto offsets |
| **Fast-Start Caching** | First 3 MB of each played track held in RAM for zero-buffering restarts |
| **Multi-Bot Load Balancing** | Up to 20 helper bots distribute download streams round-robin |
| **Flood-Wait Cooldowns** | Automatic `FLOOD_WAIT_X` detection and transparent user-client fallback |
| **FILE_REFERENCE Recovery** | Expired media references auto-refreshed on retry |
| **Stripped Artwork** | Instant `PhotoStrippedSize` JPEG expansion — zero network for thumbnails |
| **2-Phase Search** | Token pre-filter → multi-signal scoring (title, artist, duration proximity) |
| **ISRC Lookup** | Direct `GET /isrc/{code}` for master recording identification |
| **Cloudflare Tunnel** | Auto-launched `cloudflared tunnel` with URL discovery from stderr |
| **URL Secret Protection** | Optional `/{URL_SECRET}/` prefix guards all endpoints |
| **Atomic Cache** | `tracks_cache.json.gz` written via temp → rename to prevent corruption |
| **Strict RAM Caps** | LRU eviction on every cache; never loads full audio into memory |

---

## 🏗️ Technology Stack

| Layer | Library | Role |
|---|---|---|
| Language | Python 3.11+ | `asyncio` native concurrency |
| MTProto | **Telethon** | Telegram client (replaces GramJS from the Node.js version) |
| HTTP | **FastAPI** + **Uvicorn** | ASGI web server, single worker, async routes |
| Audio Tags | **Mutagen** | Parses FLAC/ID3/M4A/Vorbis headers from bytes (replaces music-metadata) |
| JSON | **orjson** | Serialization for track index and HTTP responses |
| Async I/O | **aiofiles** | Non-blocking disk operations |
| Config | **pydantic-settings** | Typed env-var loading from `.env` |

---

## 📁 Project Structure

```
telegram_addon/
├── config.py              # Environment variable loader (pydantic-settings)
├── cache.py               # SimpleLRU: O(1) LRU backed by OrderedDict + asyncio.Lock
├── login.py               # Standalone CLI: interactive Telethon login → prints session string
├── telegram_client.py     # User MTProto client, channel resolver, audio indexer
├── botpool.py             # BotPool: multi-bot manager with round-robin lease & cooldowns
├── search.py              # SearchEngine: 2-phase tokenized search & ISRC lookup
├── main.py                # FastAPI app, all HTTP routes, streaming generator, startup lifespan
├── requirements.txt       # pip dependencies
├── Dockerfile             # Production image (python:3.11-slim + cloudflared + uvicorn)
├── .env.example           # Template with all supported variables
├── .gitignore             # Ignores .env, *.zip, *.img, caches, __pycache__
├── icon.png               # Addon icon served at /icon.png
└── tracks_cache.json.gz   # Auto-generated runtime cache (not committed to git)
```

---

## 📖 Module-by-Module Explanation

### `config.py`

Loads all environment variables once at import time using `pydantic-settings.BaseSettings`. The `.env` file is automatically read. Every variable has a Python type and a default.

**Key exported object:** `config` (singleton `Settings` instance).

**Properties:**
- `config.bot_tokens` → splits `TELEGRAM_BOT_TOKENS` on commas, strips quotes, caps at 20.
- `config.clean_url_secret` → cleaned `URL_SECRET` without `/` or quotes.
- `config.clean_public_url` → cleaned `PUBLIC_URL` without trailing `/`.
- `config.validate_for_main()` → raises `ValueError` listing every missing required variable.

---

### `cache.py`

Implements `SimpleLRU`, a lightweight LRU cache backed by `collections.OrderedDict`.

**Concurrency safety:** All mutating methods (`get`, `set`, `delete`, `clear`) acquire an `asyncio.Lock`, making the cache safe under a single-threaded async event loop with interleaved coroutines.

**Sync variants:** `get_sync()`, `set_sync()`, `has_sync()`, etc. are provided for use in non-async contexts (like the search scoring hot path).

**Why not `functools.lru_cache`?** — We need key-value storage with explicit eviction, size introspection, and async-safe mutations. Python's built-in decorator caches don't support any of these.

---

### `login.py`

A standalone CLI script. Run it as:

```bash
python login.py
```

**Flow:**
1. Reads `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from the environment (or prompts interactively).
2. Creates a temporary `TelegramClient(StringSession())` with blank session.
3. Calls `await client.start()` which triggers Telethon's interactive auth: phone number → OTP code → optional 2FA password.
4. On success, calls `client.session.save()` to obtain the session string.
5. Prints the string to stdout. The user manually copies it to `.env`.
6. **Does not write to any file.** No web UI, no browser.

---

### `telegram_client.py`

The core Telegram integration layer. Exported singleton: `telegram_manager`.

**`TelegramManager.connect()`**
Connects using the `StringSession` from `config.TELEGRAM_SESSION_STRING` and verifies authorization.

**`TelegramManager.resolve_channel(ref)`**
Multi-strategy channel entity resolution (mirrors the Node.js version):

1. Direct `client.get_entity(ref)`.
2. Try `@username` and bare-username variants.
3. Try numeric ID variations (`id`, `-id`, `-100id`).
4. Iterate user dialogs (up to 500) matching by ID, username, or title.

**`TelegramManager.index_tracks()`**
Walks all channel messages using `client.iter_messages(entity, reverse=True)` (chronological order). For each message with an audio document:

- Extracts: `id`, `title`, `artist`, `album`, `duration`, `format`, `bitDepth`, `sampleRate`, `fileSize`, `mimeType`, `isAtmos`, `fileName`, `quality`, `hasArtwork`, `isrc`.
- Detects Atmos from filename/mime/ext matching the regex `\b(atmos|dolby\s*atmos|eac3[-\s]?joc|eac3|ec3|e-ac-3|spatial\s*audio)\b`.
- Reuses previously-parsed metadata for tracks already in the index (avoids re-parsing on refresh).
- Saves to `tracks_cache.json.gz` every 50 messages and on completion (atomic gzip write).

**`TelegramManager.get_media(track_id, force=False)`**
Fetches `msg.media` via `client.get_messages(channel, ids=[id])`, cached in a 500-entry LRU.

**Artist formatting:** `format_artist_for_client()` expands known composer duos (Vishal-Shekhar → Vishal & Shekhar, etc.) and replaces dash-delimited artists with commas.

---

### `botpool.py`

Manages a fleet of up to 20 Telegram bot clients for parallel audio downloads.

**`BotPool.init(channel_ref)`**
For each token:
1. Creates a `TelegramClient(StringSession(''))`.
2. Starts it with `bot_token=token`.
3. Resolves the channel using a bot-specific strategy (bots can't use user access hashes):
   - Username lookup.
   - Direct `get_entity`.
   - `updates.GetDifference(pts=1)` → search `diff.chats`.
   - `updates.GetState()` + `GetDifference(pts=state.pts-1000)`.
   - **Never** falls back to the user's entity object.
4. On failure: logs a warning, disconnects the bot, continues to the next token.

**`BotPool.acquire() → BotLease | None`**
Round-robin assignment:
- Starts at `_rr_index`, wraps around all bots.
- Skips bots where `cooldown_until > time.time()`.
- Picks the **next** bot in rotation (no `maxPerBot` limit).
- Advances `_rr_index = (picked + 1) % len(bots)`.
- Returns `BotLease` with an idempotent `release()` method.
- Returns `None` if ALL bots are on cooldown.

**`BotPool.get_media(bot, track_id, force=False)`**
Per-bot media cache (500 entries). On miss or force: `bot.client.get_messages(bot.channel, ids=[id])`.

**`BotPool.report_error(bot, err)`**
Parses `FLOOD_WAIT_X` from `err.seconds` or error message regex. Sets `bot.cooldown_until = time.time() + seconds`.

---

### `search.py`

Implements `SearchEngine` for the `/search` and `/isrc` endpoints.

**Lazy indexing:** On first search call, `_ensure_search_words()` builds a `frozenset` of lowercase tokens from `title + artist + album + isrc` for every track and stores it as `track["_search_words"]`. This avoids re-tokenizing on every query.

**`search(query, atmos_preferred, duration)`**

**Phase 1 — Token Filter:**
Keep only tracks where at least one query token matches any track token. If zero candidates, fall back to the entire index.

**Phase 2 — Scoring:**

| Signal | Score |
|---|---|
| Exact title match (lowercased) | +10 |
| Title contains query core (brackets/noise stripped) | +7 |
| All query tokens found in title tokens | +5 |
| Any query token in artist tokens | +3 |
| Duration within 3 seconds of target | +2 |
| Per unmatched query token | −1 |

Results sorted by score descending. If `atmos_preferred`, Atmos tracks sort higher at equal scores. Top 20 returned.

**`isrc_lookup(code)`**
Exact upper-case match on `track.isrc`.

---

### `main.py`

The FastAPI application. Contains all HTTP route handlers, the audio streaming generator, pre-warm logic, Cloudflare tunnel management, and the startup/shutdown lifespan.

**Key globals:**
- `fast_start_cache` — `SimpleLRU(50)` — first 3 MB per track (150 MB max RAM).
- `tag_slice_cache` — `SimpleLRU(200)` — first 256 KB per track (50 MB max RAM).
- `bot_pool` — shared `BotPool` instance.
- `search_engine` — `SearchEngine` wrapping `telegram_manager.track_index`.
- `TUNNEL_URL` — discovered Cloudflare tunnel URL.

**`get_base_url(request)`**
Priority: `PUBLIC_URL` → `TUNNEL_URL` → `{proto}://{host}`, with optional `/{URL_SECRET}` suffix.

**`format_track_for_client(track, base_url)`**
Maps internal track dict to the response shape expected by BitChord/Stremio:
```
id, title, artist, album, duration, format, audioQuality, quality, tier,
bitDepth, sampleRate, audioModes, atmos, artworkURL, albumArtworkURL,
stream, streamURL, streamUrl, audio, audioUrl, isrc
```

**Audio streaming pipeline** — see [dedicated section below](#-audio-streaming-pipeline).

---

## 🚀 Setup Guide

### 1. Prerequisites

- **Python 3.11+**
- **Telegram API credentials** from [my.telegram.org](https://my.telegram.org) → *API development tools*
- **A Telegram channel** with audio files uploaded

### 2. Installation

```bash
cd telegram_addon

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate    # Linux/macOS
# venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

### 3. Generate Telegram Session String

```bash
python login.py
```

You will be prompted for:
1. **Phone number** (with country code, e.g. `+919876543210`)
2. **OTP code** (sent to your Telegram app)
3. **2FA password** (if enabled; press Enter to skip if not)

On success the session string is printed. Copy it.

### 4. Configure Environment Variables

```bash
cp .env.example .env
```

Fill in the values:

```env
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
TELEGRAM_SESSION_STRING=1BJWap8...        # from step 3
TELEGRAM_CHANNEL=@my_music_channel        # or -1001234567890
TELEGRAM_BOT_TOKENS=                      # optional, see Bot Pool section
PORT=3000
URL_SECRET=                               # optional
PUBLIC_URL=                               # optional
ENABLE_CLOUDFLARE_TUNNEL=True
```

### 5. Start the Server

```bash
# Option A: via uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 3000 --workers 1

# Option B: via main.py entry point
python main.py
```

> **Important:** Always use `--workers 1`. Multiple workers would duplicate in-memory caches and break bot lease state sharing.

---

## 📋 Environment Variables Reference

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `TELEGRAM_API_ID` | `int` | **Yes** | — | Telegram API application ID |
| `TELEGRAM_API_HASH` | `str` | **Yes** | — | Telegram API application hash |
| `TELEGRAM_SESSION_STRING` | `str` | **Yes** | — | Telethon `StringSession` (generated by `login.py`) |
| `TELEGRAM_CHANNEL` | `str` | **Yes** | — | Channel username (`@name`), numeric ID (`-100...`), or title |
| `TELEGRAM_BOT_TOKENS` | `str` | No | `""` | Comma-separated bot tokens (max 20) |
| `PORT` | `int` | No | `3000` | HTTP server listen port |
| `URL_SECRET` | `str` | No | `None` | If set, all endpoints served under `/{URL_SECRET}/` |
| `PUBLIC_URL` | `str` | No | `None` | Override base URL used in responses (e.g. `https://music.example.com`) |
| `ENABLE_CLOUDFLARE_TUNNEL` | `bool` | No | `True` | Launch `cloudflared tunnel` on startup |

---

## 📡 API Endpoints

All routes are mounted under `/{URL_SECRET}/` if `URL_SECRET` is set, otherwise at `/`.

### `GET /manifest.json`

Returns the addon manifest for BitChord / Stremio.

**Response:**
```json
{
  "id": "com.personal.telegrammusic",
  "name": "Telegram Music Addon",
  "version": "2.0.0 • 1234 songs",
  "description": "Personal hi-res, lossless, and high-quality music library streamed directly from Telegram",
  "icon": "https://example.com/icon.png",
  "canServeLossless": true,
  "canServeDolbyAtmos": true,
  "resources": ["search", "stream", "isrc"],
  "types": ["track"],
  "contentType": "music",
  "settings": [
    {
      "key": "quality",
      "type": "select",
      "default": "lossless",
      "options": [
        { "label": "Lossless (FLAC/ALAC)", "value": "lossless" },
        { "label": "High (320kbps MP3)", "value": "high" },
        { "label": "Low (Data Saver)", "value": "low" }
      ]
    },
    {
      "key": "atmos",
      "type": "select",
      "default": "auto",
      "options": [
        { "label": "Dolby Atmos (Spatial Audio)", "value": "auto" },
        { "label": "Stereo Only", "value": "off" }
      ]
    }
  ],
  "endpoints": {
    "search": "https://example.com/search?q={query}",
    "isrc": "https://example.com/isrc/{isrc}",
    "stream": "https://example.com/stream/{id}",
    "audio": "https://example.com/audio/{id}"
  }
}
```

---

### `GET /search`

**Query Parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | `""` | Search keywords |
| `atmos` | string | `"auto"` | `"auto"` or `"true"` prefers Atmos; `"off"` does not |
| `duration` | int | `null` | Target duration in seconds for proximity scoring (+2 if within 3s) |

**Response:**
```json
{
  "tracks": [
    {
      "id": "456",
      "title": "Bohemian Rhapsody",
      "artist": "Queen",
      "album": "A Night at the Opera",
      "duration": 355,
      "format": "flac",
      "audioQuality": "LOSSLESS",
      "quality": "LOSSLESS",
      "tier": "LOSSLESS",
      "bitDepth": 16,
      "sampleRate": 44100,
      "audioModes": ["STEREO"],
      "artworkURL": "https://example.com/artwork/456",
      "albumArtworkURL": "https://example.com/artwork/456",
      "stream": "https://example.com/stream/456",
      "streamURL": "https://example.com/audio/456",
      "streamUrl": "https://example.com/audio/456",
      "audio": "https://example.com/audio/456",
      "audioUrl": "https://example.com/audio/456",
      "isrc": "GBAYE7500100"
    }
  ]
}
```

---

### `GET /stream/{id}`

Returns stream metadata for a track and triggers background pre-warm into `fastStartCache`.

**Response:**
```json
{
  "url": "https://example.com/audio/456",
  "format": "flac",
  "codec": "flac",
  "container": "flac",
  "manifest": "none",
  "encrypted": false,
  "audioMode": "STEREO",
  "sampleRate": 44100,
  "bitDepth": 16,
  "quality": "LOSSLESS",
  "audioQuality": "LOSSLESS",
  "tier": "LOSSLESS",
  "streamQuality": "16-bit / 44.1kHz FLAC Lossless",
  "description": "16-bit / 44.1kHz FLAC Lossless"
}
```

For Atmos tracks `audioMode` is `"DOLBY_ATMOS"`, `format`/`codec` is `"eac3-joc"`, and `container` is `"mp4"`.

---

### `GET /audio/{id}`

Streams the raw audio file from Telegram MTProto. This is the main streaming endpoint.

**Supports:**
- `Range: bytes=0-` — full file
- `Range: bytes=1048576-2097151` — partial range (seek)
- `Range: bytes=-131072` — suffix range (last N bytes)

**Response headers:**
```
HTTP/1.1 206 Partial Content
Content-Type: audio/flac
Content-Length: 1048576
Content-Range: bytes 1048576-2097151/52428800
Accept-Ranges: bytes
Cache-Control: public, max-age=86400
```

Returns `200 OK` for non-range requests, `206 Partial Content` for range, `416` for invalid range.

---

### `GET /artwork/{id}`

Returns the embedded album art thumbnail as `image/jpeg`.

1. Checks for `PhotoStrippedSize` in document thumbs → expands instantly via `stripped_photo_to_jpg()` (zero network).
2. Otherwise downloads thumb bytes via bot pool or user client fallback.

**Response headers:**
```
Content-Type: image/jpeg
Cache-Control: public, max-age=86400
```

---

### `GET /isrc/{code}`

Same response shape as `/search`, filtered by exact ISRC code match.

---

### `GET /ping`

```json
{ "status": "ok" }
```

---

### `GET /refresh`

Triggers background re-indexing of the Telegram channel. Returns immediately:

```json
{ "status": "indexing" }
```

---

## 🤖 Bot Pool Setup

Telegram applies per-account download rate limits. A single user account streaming to multiple listeners will eventually hit `FLOOD_WAIT`. The bot pool solves this by distributing downloads across helper bots.

### Step-by-step

1. Message **@BotFather** on Telegram → `/newbot` → follow prompts. Repeat up to 20 times.
2. Add each bot as an **Administrator** to your music channel (read-only permissions are enough; bots need access to fetch messages).
3. Set `TELEGRAM_BOT_TOKENS` in `.env`:
   ```env
   TELEGRAM_BOT_TOKENS=123456:AAHdq...,789012:BBXyz...
   ```
4. On startup `BotPool.init()` connects each bot and resolves the channel using bot-specific strategies (username lookup → `GetDifference` → `GetState+GetDifference`).

### How round-robin works

```
Request 1 → bot1
Request 2 → bot2
Request 3 → bot3
Request 4 → bot1  (wraps around)
...
If bot2 is on FLOOD_WAIT cooldown → skipped, next available bot used
If ALL bots on cooldown → falls back to user client
```

---

## 🌐 Cloudflare Tunnel

When `ENABLE_CLOUDFLARE_TUNNEL=True` (default), the server automatically launches:

```
cloudflared tunnel --url http://localhost:{PORT}
```

It reads stdout and stderr line by line, scanning for a `https://....trycloudflare.com` URL. Once found, this URL is stored as `TUNNEL_URL` and used by `get_base_url()` to generate public endpoint URLs in responses.

**Requirements:** `cloudflared` binary must be in `PATH`. The Dockerfile auto-installs it. On local machines, install from [cloudflare/cloudflared releases](https://github.com/cloudflare/cloudflared/releases).

Set `ENABLE_CLOUDFLARE_TUNNEL=False` to disable.

---

## 🔊 Audio Streaming Pipeline

This is the core of `/audio/{id}` — here's exactly what happens:

```
Client sends:  GET /audio/456  Range: bytes=1048576-2097151
                                 ↓
1. Look up track in trackIndex by id
2. Determine total file size (from track metadata or media.document.size)
3. Parse Range header → start=1048576, end=2097151, bytesNeeded=1048576
4. Acquire bot lease (round-robin) or use user client
5. Resolve media object (bot.get_media or user.get_media, LRU cached)
6. If media is None → 404
7. Set response headers: Content-Range, Content-Length, Accept-Ranges, Content-Type
8. Status 206 (range) or 200 (full)
9. Stream via async generator:
   a. Check fastStartCache — if cached preamble covers [start, start+bytesNeeded), yield from RAM
   b. Align offset to 64KB boundary: alignedStart = (currentPos // 64KB) * 64KB
   c. iter_download(file=media, offset=alignedStart, request_size=64KB)
   d. Skip alignment gap bytes
   e. Yield chunks until bytesNeeded reached
   f. On first 3MB: populate fastStartCache[id]
   g. On first 256KB: populate tagSliceCache[id]
10. On FILE_REFERENCE_EXPIRED → refetch media (force=True), retry once
11. On FLOOD_WAIT → report_error(bot), fall back to user client, retry
12. finally: lease.release()
```

---

## 🧮 RAM & Memory Budget

| Cache | Max Entries | Max Size Per Entry | Max Total RAM |
|---|---|---|---|
| `fastStartCache` | 50 | 3 MB | ~150 MB |
| `tagSliceCache` | 200 | 256 KB | ~50 MB |
| `mediaCache` (user) | 500 | ~200 bytes (reference) | negligible |
| Per-bot `mediaCache` | 500 each | ~200 bytes (reference) | negligible |
| `trackIndex` | unlimited | plain dicts, no raw TG objects | proportional to library |
| Search `_search_words` | per track | `frozenset` of strings | lazy, lightweight |

**Full audio files are never loaded into memory.** The streaming generator yields chunks directly from `iter_download` to the HTTP response.

---

## 🔄 Startup Sequence

`main.py` uses FastAPI's `lifespan` context manager. On server start:

```
1. config.validate_for_main()              # Fail fast on missing env vars
2. telegram_manager.connect()              # Connect user MTProto client
3. telegram_manager.resolve_channel(...)   # Resolve channel entity
4. telegram_manager.load_cache()           # Load tracks_cache.json.gz → trackIndex
5. bot_pool.init(channel_entity)           # Init bot clients (if TELEGRAM_BOT_TOKENS set)
6. start_cloudflare_tunnel(PORT)           # Launch cloudflared subprocess (if enabled)
7. background_indexer()                    # Async task: re-index channel, update search engine
8. Uvicorn begins serving requests
```

On shutdown:
```
1. stop_cloudflare_tunnel()                # Kill cloudflared process
2. bot_pool.close()                        # Disconnect all bot clients
3. telegram_manager.client.disconnect()    # Disconnect user client
```

---

## 🐳 Docker Deployment

```bash
# Build
docker build -t telegram-music-addon .

# Run
docker run -d \
  --name telegram-music \
  --env-file .env \
  -p 3000:3000 \
  --restart unless-stopped \
  telegram-music-addon
```

The Dockerfile:
- Uses `python:3.11-slim` base image.
- Auto-downloads `cloudflared` for the container's architecture (amd64/arm64/armhf).
- Installs Python dependencies via `pip install -r requirements.txt`.
- Runs `uvicorn main:app --host 0.0.0.0 --port ${PORT:-3000} --workers 1`.

---

## 🎵 Quality Tier Logic

Each track is classified into one of four tiers based on its format and audio properties:

| Tier | Condition |
|---|---|
| `DOLBY_ATMOS` | `isAtmos == True` or `format == "eac3-joc"` |
| `HI_RES` | Lossless format **and** (`bitDepth > 16` **or** `sampleRate > 44100`) |
| `LOSSLESS` | Format is `flac`, `alac`, or `wav` at standard 16-bit / 44.1kHz |
| `HIGH` | Everything else (MP3, AAC, OGG, Opus, etc.) |

The `audioQuality`, `quality`, and `tier` fields in every response carry this value.

---

## 🔉 Atmos Detection Logic

A track is flagged `isAtmos = True` if **any** of these match:

- Filename matches regex: `\b(atmos|dolby\s*atmos|eac3[-\s]?joc|eac3|ec3|e-ac-3|spatial\s*audio)\b`
- Message text matches the same regex
- Title matches the same regex
- File extension is `ec3` or `eac3`
- MIME type matches the regex
- *(Original Node.js version also checks MP4 atom headers and bitrate heuristics for container-level detection)*

Atmos tracks get `format = "eac3-joc"`, `mimeType = "audio/mp4"`, `sampleRate = 48000`, `bitDepth = 16`.

---

## 🔧 Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `TELEGRAM_SESSION_STRING is required` | Missing or empty session string | Run `python login.py` and copy the output to `.env` |
| `Could not resolve Telegram channel` | Wrong channel ref, bot not added, or private channel without membership | Verify channel username/ID; ensure the user account has joined the channel |
| `[BotPool] Failed to resolve channel for bot1` | Bot not added as admin to channel | Add the bot as a channel administrator |
| `FLOOD_WAIT_X` errors | Too many requests to Telegram API | Add more bot tokens; the pool auto-handles cooldowns |
| `FILE_REFERENCE_EXPIRED` | Telegram invalidated cached media reference | Handled automatically — media is re-fetched on retry |
| Tunnel not starting | `cloudflared` not installed or not in PATH | Install cloudflared, or set `ENABLE_CLOUDFLARE_TUNNEL=False` |
| Audio plays but no seek | Client not sending `Range` header | Ensure the media player supports HTTP range requests |
| Empty search results | Index not yet built | Wait for background indexing to complete, or call `GET /refresh` |
| `workers > 1` breaks caching | LRU caches are in-process memory | Always use `--workers 1` |

---

## 🚫 What Is NOT Implemented

The following features from the original Node.js codebase are intentionally excluded per the rewrite specification:

- Any HTML, CSS, or JavaScript (no web UI)
- Setup wizard or web-based login
- `install.bat` / `start.bat` scripts
- Duplicate detection or channel cleaning
- Bot notifications or channel posting
- TeleDrive integration
- WebDAV protocol endpoints
- Debug endpoints (except `/ping`)

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).
# Telegram-Music-Stream
