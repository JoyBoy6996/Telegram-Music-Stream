import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import io
import os
import re
import time
from typing import Any, AsyncGenerator, Optional
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse, StreamingResponse
from mutagen import File as MutagenFile
from telethon.errors import FileReferenceExpiredError, FloodWaitError
from telethon.tl.types import PhotoStrippedSize
from telethon.utils import stripped_photo_to_jpg

from botpool import BotLease, BotPool
from cache import SimpleLRU
from config import config
from search import SearchEngine
from telegram_client import format_artist_for_client, telegram_manager


def _fmt_size(b: int) -> str:
    return f"{b / 1024 / 1024:.1f} MB"


def _fmt_duration(seconds) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}m"


def _fmt_quality(track: dict) -> str:
    fmt = (track.get("format") or "").upper()
    bd = track.get("bitDepth") or 16
    sr = (track.get("sampleRate") or 44100) / 1000
    if track.get("isAtmos"):
        return "Dolby Atmos"
    return f"{bd}-bit / {sr:.1f}kHz {fmt}"


def _log(type_: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {type_:^8} | {msg}")


def get_session_key(request: Request) -> str:
    """Extract client IP session key from request."""
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return "127.0.0.1"


# RAM limits configuration
FAST_START_BYTES = 512 * 1024       # 512 KB
TAG_SLICE_BYTES = 256 * 1024        # 256 KB
REQUEST_CHUNK_SIZE = 512 * 1024     # 512 KB (MAX_CHUNK_SIZE for high throughput)
CHUNK_ALIGNMENT = 4096              # 4 KB Telegram MTProto alignment (minimizes discarded seek bytes)
RANGE_REGEX = re.compile(r"^bytes=(\d*)-(\d*)$")

# Bounded for 1 GB RAM VPS: entry limits + explicit byte caps (~24 MB total RAM max)
fast_start_cache = SimpleLRU(25, max_bytes=14 * 1024 * 1024)   # ~14 MB max
tag_slice_cache = SimpleLRU(40, max_bytes=10 * 1024 * 1024)    # ~10 MB max

bot_pool = BotPool(
    api_id=config.TELEGRAM_API_ID,
    api_hash=config.TELEGRAM_API_HASH,
    tokens=config.bot_tokens,
)

search_engine = SearchEngine(telegram_manager.track_index)
TUNNEL_URL: Optional[str] = None
tunnel_process: Optional[asyncio.subprocess.Process] = None


def get_base_url(request: Request) -> str:
    """Resolve public base URL: PUBLIC_URL -> TUNNEL_URL -> host header with optional URL_SECRET."""
    secret_prefix = f"/{config.clean_url_secret}" if config.clean_url_secret else ""
    if config.clean_public_url:
        return f"{config.clean_public_url}{secret_prefix}"
    if TUNNEL_URL:
        return f"{TUNNEL_URL.rstrip('/')}{secret_prefix}"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or f"localhost:{config.PORT}"
    )
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    return f"{proto}://{host}{secret_prefix}"


def determine_quality_tier(track: dict[str, Any]) -> tuple[str, bool, bool]:
    """Calculate audioQuality tier, isLossless, and isHiRes flags."""
    is_atmos = bool(track.get("isAtmos") or track.get("format") == "eac3-joc")
    fmt = (track.get("format") or "").lower()
    is_lossless = fmt in {"flac", "alac", "wav"}
    bit_depth = track.get("bitDepth") or 16
    sample_rate = track.get("sampleRate") or 44100
    is_hires = is_lossless and (bit_depth > 16 or sample_rate > 44100)

    if is_atmos:
        tier = "DOLBY_ATMOS"
    elif is_hires:
        tier = "HI_RES"
    elif is_lossless:
        tier = "LOSSLESS"
    else:
        tier = "HIGH"

    return tier, is_lossless, is_hires


def format_track_for_client(track: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Format internal track dictionary into client schema matching BitChord / Stremio."""
    tier, _, _ = determine_quality_tier(track)
    is_atmos = bool(track.get("isAtmos") or tier == "DOLBY_ATMOS")
    track_id = str(track["id"])

    result: dict[str, Any] = {
        "id": track_id,
        "title": track.get("title", ""),
        "artist": format_artist_for_client(track.get("artist")),
        "album": track.get("album") or "",
        "duration": round(track["duration"]) if track.get("duration") else None,
        "format": "eac3-joc" if is_atmos else track.get("format", "mp3"),
        "audioQuality": tier,
        "quality": tier,
        "tier": tier,
        "bitDepth": track.get("bitDepth") or 16,
        "sampleRate": track.get("sampleRate") or 44100,
        "audioModes": ["DOLBY_ATMOS"] if is_atmos else ["STEREO"],
        "stream": f"{base_url}/stream/{track['id']}",
        "streamURL": f"{base_url}/audio/{track['id']}",
        "streamUrl": f"{base_url}/audio/{track['id']}",
        "audio": f"{base_url}/audio/{track['id']}",
        "audioUrl": f"{base_url}/audio/{track['id']}",
        "artworkURL": f"{base_url}/artwork/{track['id']}" if track.get("hasArtwork") else None,
        "albumArtworkURL": f"{base_url}/artwork/{track['id']}" if track.get("hasArtwork") else None,
        "isrc": track.get("isrc"),
    }

    if is_atmos:
        result["atmos"] = True

    return result


_prewarming_ids: set[str] = set()
_prewarm_semaphore = asyncio.Semaphore(2)  # Limit concurrent background prewarms to save RAM/bandwidth on 1 GB VPS
_background_indexer_task: Optional[asyncio.Task] = None
_background_indexer_lock = asyncio.Lock()
_dedupe_task: Optional[asyncio.Task] = None
_dedupe_lock = asyncio.Lock()
_tunnel_tasks: list[asyncio.Task] = []


async def prewarm_track(track_id: str, session_key: str = "127.0.0.1") -> None:
    """Pre-warm fastStartCache (512KB) and tagSliceCache (256KB) in background using user session's bot."""
    try:
        if await fast_start_cache.has(track_id):
            return

        async with _prewarm_semaphore:
            lease = await bot_pool.acquire_for_session(session_key)
            if not lease:
                return

            _log("BOTPOOL", f"{lease.bot['id']} -> prewarm {track_id}")
            dl_client = lease.bot["client"]
            bot_ref = lease.bot

            try:
                media = await bot_pool.get_media(lease.bot, track_id)
                if not media or not dl_client:
                    return

                chunks: list[bytes] = []
                bytes_collected = 0

                async for chunk in dl_client.iter_download(
                    file=media,
                    offset=0,
                    request_size=REQUEST_CHUNK_SIZE,
                ):
                    chunks.append(chunk)
                    bytes_collected += len(chunk)
                    if bytes_collected >= FAST_START_BYTES:
                        break

                if chunks:
                    full_preamble = b"".join(chunks)
                    chunks.clear()  # Free chunk buffers immediately from RAM
                    await fast_start_cache.set(track_id, full_preamble)
                    tag_slice = full_preamble[:TAG_SLICE_BYTES]
                    await tag_slice_cache.set(track_id, tag_slice)
            except Exception as exc:
                if bot_ref:
                    bot_pool.report_error(bot_ref, exc)
            finally:
                bot_pool.release_session(session_key)
    finally:
        _prewarming_ids.discard(track_id)


async def start_cloudflare_tunnel(port: int) -> None:
    """Launch cloudflared tunnel subprocess and extract the *.trycloudflare.com URL."""
    global TUNNEL_URL, tunnel_process, _tunnel_tasks
    try:
        tunnel_process = await asyncio.create_subprocess_exec(
            "cloudflared",
            "tunnel",
            "--url",
            f"http://localhost:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_stream(stream: asyncio.StreamReader) -> None:
            global TUNNEL_URL
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="ignore")
                    match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
                    if match and not TUNNEL_URL:
                        TUNNEL_URL = match.group(0)
                        print(f"\n[Tunnel] Cloudflare Tunnel active: {TUNNEL_URL}\n")
            except asyncio.CancelledError:
                pass

        if tunnel_process.stdout and tunnel_process.stderr:
            t1 = asyncio.create_task(read_stream(tunnel_process.stdout))
            t2 = asyncio.create_task(read_stream(tunnel_process.stderr))
            _tunnel_tasks.extend([t1, t2])
    except Exception as exc:
        print(f"[Tunnel] Notice: cloudflared not started ({exc})")


async def stop_cloudflare_tunnel() -> None:
    """Terminate cloudflared tunnel process and reader tasks."""
    global tunnel_process, TUNNEL_URL, _tunnel_tasks
    for t in _tunnel_tasks:
        if not t.done():
            t.cancel()
    if _tunnel_tasks:
        await asyncio.gather(*_tunnel_tasks, return_exceptions=True)
    _tunnel_tasks.clear()

    if tunnel_process:
        try:
            tunnel_process.terminate()
            await tunnel_process.wait()
        except Exception:
            pass
        tunnel_process = None
        TUNNEL_URL = None


async def trigger_background_indexer() -> asyncio.Task:
    """Trigger background_indexer idempotently without duplicate tasks."""
    global _background_indexer_task
    async with _background_indexer_lock:
        if _background_indexer_task is None or _background_indexer_task.done():
            _background_indexer_task = asyncio.create_task(background_indexer())
        return _background_indexer_task


async def background_indexer() -> None:
    """Background task to index tracks, deduplicate library, and refresh search engine index.

    Yields cooperatively to avoid starving playback or search requests on the event loop.
    """
    try:
        await telegram_manager.index_tracks()
        await asyncio.sleep(0)  # Cooperative yield for active HTTP traffic

        from duplicates import deduplicate_library, recover_pending
        await recover_pending(
            telegram_manager.client,
            telegram_manager.channel_entity,
            telegram_manager.track_index,
        )
        await asyncio.sleep(0)

        result = await deduplicate_library(
            telegram_manager.track_index,
            telegram_manager.client,
            telegram_manager.channel_entity,
        )
        _log("DEDUPE", f"Queued {result['queued']} deletions from {result['audited']} candidates")
        await telegram_manager.save_cache()
        await asyncio.sleep(0)

        # Refresh and precompute search structures off the event loop to avoid user search stalls
        search_engine.track_index = telegram_manager.track_index
        search_engine._initialized = False
        await asyncio.to_thread(search_engine._ensure_search_words)

        # Reclaim temporary objects created during indexing on 1 GB VPS
        import gc
        gc.collect()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[Indexer] Background indexing error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager controlling application startup and teardown sequence."""
    config.validate_for_main()

    # 1. Connect user Telegram client
    await telegram_manager.connect()

    # 2. Resolve channel entity
    await telegram_manager.resolve_channel(config.TELEGRAM_CHANNEL)

    # 3. Load tracks_cache.json.gz if exists
    if telegram_manager.load_cache():
        print(f"[Cache] Loaded {len(telegram_manager.track_index)} tracks from cache.")
        search_engine.track_index = telegram_manager.track_index
        await asyncio.to_thread(search_engine._ensure_search_words)

    # 4. Init botpool if bot tokens configured
    if config.bot_tokens:
        await bot_pool.init(telegram_manager.channel_entity)

    # 5. Start Cloudflare tunnel if enabled
    _tunnel_runner_task: Optional[asyncio.Task] = None
    if config.ENABLE_CLOUDFLARE_TUNNEL:
        _tunnel_runner_task = asyncio.create_task(start_cloudflare_tunnel(config.PORT))

    # 6. Start background index_tracks() task idempotently
    await trigger_background_indexer()

    yield

    # Teardown
    global _background_indexer_task, _dedupe_task
    if _background_indexer_task and not _background_indexer_task.done():
        _background_indexer_task.cancel()
        try:
            await _background_indexer_task
        except asyncio.CancelledError:
            pass

    if _dedupe_task and not _dedupe_task.done():
        _dedupe_task.cancel()
        try:
            await _dedupe_task
        except asyncio.CancelledError:
            pass

    if _tunnel_runner_task and not _tunnel_runner_task.done():
        _tunnel_runner_task.cancel()
        try:
            await _tunnel_runner_task
        except asyncio.CancelledError:
            pass

    await stop_cloudflare_tunnel()
    await bot_pool.close()
    if telegram_manager.client and telegram_manager.client.is_connected():
        await telegram_manager.client.disconnect()


app = FastAPI(
    title="Telegram Music Addon",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/icon.png")
async def get_icon() -> Response:
    icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(icon_path):
        return FileResponse(
            icon_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    raise HTTPException(status_code=404, detail="Icon not found")


@router.get("/manifest.json")
async def get_manifest(request: Request) -> dict[str, Any]:
    base = get_base_url(request)
    track_count = len(telegram_manager.track_index)

    return {
        "id": "com.personal.telegrammusic",
        "name": "Telegram Music Addon",
        "version": f"2.0.0 • {track_count} songs",
        "description": "Personal hi-res, lossless, and high-quality music library streamed directly from Telegram",
        "icon": f"{base}/icon.png",
        "canServeLossless": True,
        "canServeDolbyAtmos": True,
        "resources": ["search", "stream", "isrc"],
        "types": ["track"],
        "contentType": "music",
        "settings": [
            {
                "key": "quality",
                "type": "select",
                "default": "lossless",
                "options": [
                    {"label": "Lossless (FLAC/ALAC)", "value": "lossless"},
                    {"label": "High (320kbps MP3)", "value": "high"},
                    {"label": "Low (Data Saver)", "value": "low"},
                ],
            },
            {
                "key": "atmos",
                "type": "select",
                "default": "auto",
                "options": [
                    {"label": "Dolby Atmos (Spatial Audio)", "value": "auto"},
                    {"label": "Stereo Only", "value": "off"},
                ],
            },
        ],
        "endpoints": {
            "search": f"{base}/search?q={{query}}",
            "isrc": f"{base}/isrc/{{isrc}}",
            "stream": f"{base}/stream/{{id}}",
            "audio": f"{base}/audio/{{id}}",
        },
    }


@router.get("/search")
async def search_tracks(
    request: Request,
    q: str = Query("", description="Search query"),
    atmos: str = Query("auto", description="Atmos preference: auto | off"),
    duration: Optional[int] = Query(None, description="Target duration in seconds"),
) -> dict[str, Any]:
    t0 = time.time()
    base = get_base_url(request)
    session_key = get_session_key(request)
    if not search_engine._initialized:
        await asyncio.to_thread(search_engine._ensure_search_words)
    matches = await asyncio.to_thread(
        search_engine.search,
        query=q,
        atmos_preferred=atmos_preferred,
        duration=duration,
    )
    ms = round((time.time() - t0) * 1000)
    _log("SEARCH", f'"{q}" ({len(matches)} hits, {ms}ms) -> ID: {matches[0]["id"] if matches else "none"}')
    if matches:
        top_id = str(matches[0]["id"])
        if top_id not in _prewarming_ids:
            existing = await fast_start_cache.get(top_id)
            if not existing or len(existing) < FAST_START_BYTES:
                _prewarming_ids.add(top_id)
                asyncio.create_task(prewarm_track(top_id, session_key))
    return {
        "tracks": [format_track_for_client(t, base) for t in matches],
    }


@router.get("/isrc/{code}")
async def get_isrc(request: Request, code: str) -> dict[str, Any]:
    base = get_base_url(request)
    matches = search_engine.isrc_lookup(code)
    return {
        "tracks": [format_track_for_client(t, base) for t in matches],
    }


@router.get("/stream/{track_id}")
async def get_stream_info(track_id: str, request: Request) -> dict[str, Any]:
    base = get_base_url(request)
    track = telegram_manager.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    _log("STREAM", f'{track["title"]} • {_fmt_quality(track)} • {_fmt_size(track.get("fileSize", 0))} ({_fmt_duration(track.get("duration"))}) -> 206 Partial')

    tier, _, _ = determine_quality_tier(track)
    is_atmos = bool(track.get("isAtmos") or tier == "DOLBY_ATMOS")

    return {
        "url": f"{base}/audio/{track_id}",
        "format": "eac3-joc" if is_atmos else (track.get("format") or "flac"),
        "codec": "eac3-joc" if is_atmos else (track.get("format") or "flac"),
        "container": "mp4" if is_atmos else (track.get("format") or "flac"),
        "manifest": "none",
        "encrypted": False,
        "audioMode": "DOLBY_ATMOS" if is_atmos else "STEREO",
        "sampleRate": track.get("sampleRate") or 44100,
        "bitDepth": track.get("bitDepth") or 16,
        "quality": tier,
        "audioQuality": tier,
        "tier": tier,
        "streamQuality": "Dolby Atmos" if is_atmos else (track.get("quality") or tier),
        "description": track.get("quality", tier),
    }


@router.get("/artwork/{track_id}")
async def get_artwork(track_id: str) -> Response:
    """Serve album artwork thumbnail: PhotoStrippedSize or downloaded thumb bytes via bot."""
    lease = await bot_pool.acquire()
    if not lease:
        raise HTTPException(status_code=503, detail="Bots currently unavailable")

    try:
        media = await bot_pool.get_media(lease.bot, track_id)
        if not media:
            raise HTTPException(status_code=404, detail="Media not found")

        doc = getattr(media, "document", None)
        thumbs = getattr(doc, "thumbs", []) or []
        if not thumbs:
            raise HTTPException(status_code=404, detail="No artwork thumbnail")

        # Check for PhotoStrippedSize (instant zero-download return)
        stripped = next((t for t in thumbs if isinstance(t, PhotoStrippedSize)), None)
        if stripped:
            jpg_bytes = stripped_photo_to_jpg(stripped.bytes)
            return Response(
                content=jpg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        # Download thumbnail via bot lease
        thumb_buf: Optional[bytes] = None
        try:
            thumb_buf = await lease.bot["client"].download_media(media, file=bytes, thumb=0)
        except FileReferenceExpiredError:
            bot_media = await bot_pool.get_media(lease.bot, track_id, force=True)
            if bot_media:
                thumb_buf = await lease.bot["client"].download_media(bot_media, file=bytes, thumb=0)

        if not thumb_buf:
            raise HTTPException(status_code=404, detail="Artwork not available")

        return Response(
            content=thumb_buf,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    finally:
        lease.release()


@router.get("/audio/{track_id}")
async def stream_audio(
    track_id: str,
    request: Request,
    range: Optional[str] = Header(None),
) -> Response:
    """Stream audio document with HTTP 206 Range support, fast start caching, and session-based bot leasing."""
    session_key = get_session_key(request)

    # 1. O(1) track lookup
    track = telegram_manager.get_track(track_id)
    total_size = int(track.get("fileSize", 0)) if track else 0

    lease: Optional[BotLease] = None
    media_resolved: Any = None
    doc: Any = None

    # 2. If total_size is missing/0, resolve media from Telegram before Range parsing
    if not total_size:
        lease = await bot_pool.acquire_for_session(session_key)
        if not lease:
            raise HTTPException(
                status_code=503,
                detail="All bots are currently busy. Please wait and try again.",
            )
        media_resolved = await bot_pool.get_media(lease.bot, track_id)
        if not media_resolved:
            bot_pool.release_session(session_key)
            raise HTTPException(status_code=404, detail="Media not found")
        doc = getattr(media_resolved, "document", None)
        total_size = int(getattr(doc, "size", 0) or 0)
        if not total_size:
            bot_pool.release_session(session_key)
            raise HTTPException(status_code=500, detail="Unable to determine audio file size")

    # 3. Parse and validate Range header
    start = 0
    end = total_size - 1
    is_range = False

    if range:
        range_str = range.strip()
        match = RANGE_REGEX.match(range_str)
        if not match:
            # Malformed range syntax (not matching bytes=\d*-\d*)
            if lease:
                bot_pool.release_session(session_key)
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{total_size}"},
            )

        s_str, e_str = match.groups()
        if s_str == "" and e_str == "":
            # Malformed: "bytes=-" with no offsets
            if lease:
                bot_pool.release_session(session_key)
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{total_size}"},
            )

        if s_str == "":
            # Suffix range: bytes=-suffix (e.g. bytes=-500)
            suffix = int(e_str)
            if suffix <= 0:
                if lease:
                    bot_pool.release_session(session_key)
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{total_size}"},
                )
            start = max(0, total_size - suffix)
            end = total_size - 1
            is_range = True
        else:
            # Explicit start: bytes=start- or bytes=start-end
            start = int(s_str)
            if e_str != "":
                req_end = int(e_str)
                if start > req_end:
                    # Invalid range: start > end (e.g. bytes=500-200)
                    if lease:
                        bot_pool.release_session(session_key)
                    return Response(
                        status_code=416,
                        headers={"Content-Range": f"bytes */{total_size}"},
                    )
                end = min(req_end, total_size - 1)
            else:
                end = total_size - 1
            is_range = True

        # Out-of-bounds check (e.g. start >= total_size)
        if start >= total_size or start > end:
            if lease:
                bot_pool.release_session(session_key)
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{total_size}"},
            )

    start = max(0, min(start, total_size - 1))
    end = max(start, min(end, total_size - 1))
    bytes_needed = end - start + 1

    content_type = (
        "audio/mp4"
        if (track and track.get("isAtmos"))
        else (track.get("mimeType") if track else None)
        or (getattr(doc, "mime_type", None) if doc else None)
        or "audio/mpeg"
    )

    headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Content-Length": str(bytes_needed),
        "Cache-Control": "public, max-age=86400",
    }
    if is_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

    # 4. Check fastStartCache: if entire range is in cache, serve directly from RAM
    cached_preamble = await fast_start_cache.get(track_id)
    if is_range and cached_preamble and end < len(cached_preamble):
        if lease:
            bot_pool.release_session(session_key)
        slice_data = cached_preamble[start : end + 1]
        return Response(
            content=slice_data,
            status_code=206,
            headers=headers,
        )

    # 5. Acquire bot lease for Telegram streaming if not already acquired
    if not lease:
        lease = await bot_pool.acquire_for_session(session_key)
        if not lease:
            raise HTTPException(
                status_code=503,
                detail="All bots are currently busy. Please wait and try again.",
            )

    dl_client = lease.bot["client"]
    bot_ref = lease.bot
    _log("BOTPOOL", f"{lease.bot['id']} -> track {track_id}")

    # If no cached preamble exists for initial chunk, verify media resolution before sending 200/206 headers
    if not cached_preamble or start >= len(cached_preamble):
        if not media_resolved:
            media_resolved = await bot_pool.get_media(bot_ref, track_id)
            if not media_resolved:
                bot_pool.release_session(session_key)
                raise HTTPException(status_code=502, detail="Media not available from Telegram")

    if start == 0 and not cached_preamble:
        _log("PROBE", f'{track["title"] if track else track_id} (64 KB header read)')

    if is_range and start > 0 and track:
        pct = round(start / total_size * 100) if total_size else 0
        _log("SEEK", f'{track["title"]} • Seeked to ~{_fmt_duration(start // (total_size // max(track.get("duration",1),1))*1 if total_size else 0)} ({pct}%) • {_fmt_size(start)}')
    elif not is_range and track:
        _log("PRECACHE", f'{track["title"]} • {_fmt_quality(track)} • {_fmt_size(total_size)} ({_fmt_duration(track.get("duration"))}) -> 200 OK')

    # 6. Async streaming generator
    async def audio_generator() -> AsyncGenerator[bytes, None]:
        nonlocal dl_client, bot_ref, session_key, media_resolved
        stream_id = f"{session_key}:{track_id}:{time.time_ns()}"
        bot_pool.mark_stream_start(bot_ref["id"], stream_id)

        try:
            bytes_sent = 0

            # Yield available cached preamble immediately for minimal TTFB
            if cached_preamble and start < len(cached_preamble):
                available = len(cached_preamble) - start
                take = min(available, bytes_needed)
                slice_data = cached_preamble[start : start + take]
                yield slice_data
                bytes_sent += len(slice_data)

                if bytes_sent >= bytes_needed:
                    return

            # Resolve media; raise exception if fetch fails so stream is not silently incomplete
            media = media_resolved or await bot_pool.get_media(bot_ref, track_id)
            if not media:
                _log("STREAM", f"Media fetch failed for track {track_id}")
                raise RuntimeError(f"Failed to fetch media for track {track_id}")

            # Alignment logic: offset must align with REQUEST_CHUNK_SIZE
            current_pos = start + bytes_sent
            aligned_start = (current_pos // REQUEST_CHUNK_SIZE) * REQUEST_CHUNK_SIZE
            alignment_gap = current_pos - aligned_start

            refreshed = False
            preamble_collector: list[bytes] = []
            collector_size = 0

            while bytes_sent < bytes_needed:
                try:
                    async for chunk in dl_client.iter_download(
                        file=media,
                        offset=aligned_start,
                        request_size=REQUEST_CHUNK_SIZE,
                    ):
                        # Collect first 512KB for fastStartCache and first 256KB for tagSliceCache
                        if start == 0 and collector_size < FAST_START_BYTES:
                            needed_cache = FAST_START_BYTES - collector_size
                            c_slice = chunk[:needed_cache]
                            preamble_collector.append(c_slice)
                            collector_size += len(c_slice)
                            if collector_size >= FAST_START_BYTES or collector_size >= total_size:
                                full_p = b"".join(preamble_collector)
                                preamble_collector.clear()  # Free chunk buffers immediately
                                await fast_start_cache.set(track_id, full_p)
                                await tag_slice_cache.set(track_id, full_p[:TAG_SLICE_BYTES])
                                _log("BUFFER", f'Prewarmed {_fmt_size(collector_size)} -> ID: {track_id}')

                        # Skip initial alignment gap
                        usable_chunk = chunk
                        if alignment_gap > 0:
                            if len(usable_chunk) <= alignment_gap:
                                alignment_gap -= len(usable_chunk)
                                continue
                            usable_chunk = usable_chunk[alignment_gap:]
                            alignment_gap = 0

                        # Truncate to remaining requested bytes
                        remaining = bytes_needed - bytes_sent
                        if len(usable_chunk) > remaining:
                            usable_chunk = usable_chunk[:remaining]

                        yield usable_chunk
                        bytes_sent += len(usable_chunk)

                        if bytes_sent >= bytes_needed:
                            break
                    break
                except (FileReferenceExpiredError, Exception) as exc:
                    is_file_ref = (
                        isinstance(exc, FileReferenceExpiredError)
                        or "FILE_REFERENCE" in str(exc)
                        or "FILEREF" in str(exc)
                    )
                    is_flood = isinstance(exc, FloodWaitError) or "FLOOD_WAIT" in str(exc)

                    if bot_ref and (is_file_ref or is_flood):
                        bot_pool.report_error(bot_ref, exc)

                    if not refreshed and is_file_ref and bot_ref:
                        refreshed = True
                        media = await bot_pool.get_media(bot_ref, track_id, force=True)
                        if media:
                            current_pos = start + bytes_sent
                            aligned_start = (current_pos // REQUEST_CHUNK_SIZE) * REQUEST_CHUNK_SIZE
                            alignment_gap = current_pos - aligned_start
                            continue

                    raise exc

            # Verify complete stream was delivered
            if bytes_sent < bytes_needed:
                raise RuntimeError(
                    f"Incomplete audio stream for track {track_id}: sent {bytes_sent}/{bytes_needed} bytes"
                )
        finally:
            bot_pool.mark_stream_end(bot_ref["id"], stream_id)
            bot_pool.release_session(session_key)

    status_code = 206 if is_range else 200
    return StreamingResponse(
        audio_generator(),
        status_code=status_code,
        headers=headers,
    )


@router.get("/refresh")
async def refresh_index() -> dict[str, str]:
    """Trigger background track indexing idempotently."""
    task = await trigger_background_indexer()
    status = "indexing" if not task.done() else "completed"
    return {"status": status}


@router.get("/deduplicate/preview")
async def deduplicate_preview() -> dict[str, Any]:
    """Run audit_library(), return candidates list, NO deletion."""
    from duplicates import audit_library
    candidates = await audit_library(telegram_manager.track_index)
    return {"candidates": candidates}


@router.post("/deduplicate/run")
async def deduplicate_run() -> dict[str, str]:
    """Trigger deduplicate_library() as background task idempotently."""
    global _dedupe_task
    async with _dedupe_lock:
        if _dedupe_task and not _dedupe_task.done():
            return {"status": "already_running"}

        async def run_dedupe() -> None:
            try:
                from duplicates import deduplicate_library
                result = await deduplicate_library(
                    telegram_manager.track_index,
                    telegram_manager.client,
                    telegram_manager.channel_entity,
                )
                _log("DEDUPE", f"Queued {result['queued']} deletions from {result['audited']} candidates")
                await telegram_manager.save_cache()
                search_engine.track_index = telegram_manager.track_index
                search_engine._initialized = False
                await asyncio.to_thread(search_engine._ensure_search_words)
                import gc
                gc.collect()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"[Dedupe] Background deduplication error: {exc}")

        _dedupe_task = asyncio.create_task(run_dedupe())
        return {"status": "started"}


@router.get("/deduplicate/pending")
async def deduplicate_pending() -> dict[str, Any]:
    """Load and return pending_deletions.json."""
    from duplicates import load_pending
    return {"pending": load_pending()}


@router.post("/deduplicate/keep/{candidate_id}")
async def deduplicate_keep(candidate_id: str) -> dict[str, str]:
    """Call keep_track(candidate_id)."""
    from duplicates import keep_track
    status = await keep_track(candidate_id)
    return {"status": "cancelled" if status else "not_found"}


# Mount routes: under /{URL_SECRET}/ if set, else under /
if config.clean_url_secret:
    app.include_router(router, prefix=f"/{config.clean_url_secret}")
    # Also expose /ping and /icon.png at root level for external uptime monitors
    app.add_api_route("/ping", ping, methods=["GET"])
    app.add_api_route("/icon.png", get_icon, methods=["GET"])
else:
    app.include_router(router)


if __name__ == "__main__":
    import logging
    import uvicorn

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.PORT,
        workers=1,
        access_log=False,
        log_level="warning",
    )
