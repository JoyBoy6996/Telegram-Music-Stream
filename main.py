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


# RAM limits configuration
FAST_START_BYTES = 512 * 1024       # 512 KB
TAG_SLICE_BYTES = 256 * 1024        # 256 KB
REQUEST_CHUNK_SIZE = 256 * 1024     # 256 KB

fast_start_cache = SimpleLRU(50)    # 50 x 512KB = ~25MB max
tag_slice_cache = SimpleLRU(200)    # 200 x 256KB = 50MB max

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


async def prewarm_track(track_id: str) -> None:
    """Pre-warm fastStartCache (512KB) and tagSliceCache (256KB) in background."""
    if await fast_start_cache.has(track_id):
        return

    lease = bot_pool.acquire()
    dl_client = lease.bot["client"] if lease else telegram_manager.client
    bot_ref = lease.bot if lease else None

    try:
        if lease:
            media = await bot_pool.get_media(lease.bot, track_id)
        else:
            media = await telegram_manager.get_media(track_id)

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
            await fast_start_cache.set(track_id, full_preamble)
            tag_slice = full_preamble[:TAG_SLICE_BYTES]
            await tag_slice_cache.set(track_id, tag_slice)
    except Exception as exc:
        if bot_ref:
            bot_pool.report_error(bot_ref, exc)
    finally:
        if lease:
            lease.release()


async def start_cloudflare_tunnel(port: int) -> None:
    """Launch cloudflared tunnel subprocess and extract the *.trycloudflare.com URL."""
    global TUNNEL_URL, tunnel_process
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
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore")
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
                if match and not TUNNEL_URL:
                    TUNNEL_URL = match.group(0)
                    print(f"\n[Tunnel] Cloudflare Tunnel active: {TUNNEL_URL}\n")

        if tunnel_process.stdout and tunnel_process.stderr:
            asyncio.create_task(read_stream(tunnel_process.stdout))
            asyncio.create_task(read_stream(tunnel_process.stderr))
    except Exception as exc:
        print(f"[Tunnel] Notice: cloudflared not started ({exc})")


async def stop_cloudflare_tunnel() -> None:
    """Terminate cloudflared tunnel process."""
    global tunnel_process, TUNNEL_URL
    if tunnel_process:
        try:
            tunnel_process.terminate()
            await tunnel_process.wait()
        except Exception:
            pass
        tunnel_process = None
        TUNNEL_URL = None


async def background_indexer() -> None:
    """Background task to index tracks, deduplicate library, and refresh search engine index."""
    await telegram_manager.index_tracks()
    from duplicates import deduplicate_library
    result = await deduplicate_library(
        telegram_manager.track_index,
        telegram_manager.client,
        telegram_manager.channel_entity,
    )
    _log("DEDUPE", f"Complete: kept {result['kept']}, removed {result['removed']}")
    telegram_manager.save_cache()
    search_engine.track_index = telegram_manager.track_index
    search_engine._initialized = False


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

    # 4. Init botpool if bot tokens configured
    if config.bot_tokens:
        await bot_pool.init(telegram_manager.channel_entity)

    # 5. Start Cloudflare tunnel if enabled
    if config.ENABLE_CLOUDFLARE_TUNNEL:
        asyncio.create_task(start_cloudflare_tunnel(config.PORT))

    # 6. Start background index_tracks() task
    asyncio.create_task(background_indexer())

    yield

    # Teardown
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
    atmos_preferred = atmos in {"auto", "true"}
    matches = search_engine.search(query=q, atmos_preferred=atmos_preferred, duration=duration)
    ms = round((time.time() - t0) * 1000)
    _log("SEARCH", f'"{q}" ({len(matches)} hits, {ms}ms) -> ID: {matches[0]["id"] if matches else "none"}')
    if matches:
        asyncio.create_task(prewarm_track(str(matches[0]["id"])))
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
    track = next((t for t in telegram_manager.track_index if str(t["id"]) == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    _log("STREAM", f'{track["title"]} • {_fmt_quality(track)} • {_fmt_size(track.get("fileSize", 0))} ({_fmt_duration(track.get("duration"))}) -> 206 Partial')

    tier, _, _ = determine_quality_tier(track)
    is_atmos = bool(track.get("isAtmos") or tier == "DOLBY_ATMOS")

    # Trigger prewarm of fastStartCache in background
    asyncio.create_task(prewarm_track(track_id))

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
    """Serve album artwork thumbnail: PhotoStrippedSize or downloaded thumb bytes."""
    media = await telegram_manager.get_media(track_id)
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

    # Download thumbnail via bot lease or user client fallback
    thumb_buf: Optional[bytes] = None
    lease = bot_pool.acquire()

    if lease:
        try:
            bot_media = await bot_pool.get_media(lease.bot, track_id)
            if bot_media:
                try:
                    thumb_buf = await lease.bot["client"].download_media(bot_media, file=bytes, thumb=0)
                except FileReferenceExpiredError:
                    bot_media = await bot_pool.get_media(lease.bot, track_id, force=True)
                    if bot_media:
                        thumb_buf = await lease.bot["client"].download_media(bot_media, file=bytes, thumb=0)
        except Exception as exc:
            bot_pool.report_error(lease.bot, exc)
        finally:
            lease.release()

    if not thumb_buf:
        try:
            thumb_buf = await telegram_manager.client.download_media(media, file=bytes, thumb=0)
        except FileReferenceExpiredError:
            fresh_media = await telegram_manager.get_media(track_id, force=True)
            if fresh_media:
                thumb_buf = await telegram_manager.client.download_media(fresh_media, file=bytes, thumb=0)

    if not thumb_buf:
        raise HTTPException(status_code=404, detail="Artwork not available")

    return Response(
        content=thumb_buf,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/audio/{track_id}")
async def stream_audio(
    track_id: str,
    range: Optional[str] = Header(None),
) -> Response:
    """Stream audio document with HTTP 206 Range support, fast start caching, and bot load-balancing."""
    # 1. Check metadata and total size
    track = next((t for t in telegram_manager.track_index if str(t["id"]) == track_id), None)
    total_size = int(track.get("fileSize", 0)) if track else 0

    # 2. Acquire bot lease
    lease: Optional[BotLease] = bot_pool.acquire()
    dl_client = lease.bot["client"] if lease else telegram_manager.client
    bot_ref = lease.bot if lease else None

    # 4. Resolve media
    if lease:
        media = await bot_pool.get_media(lease.bot, track_id)
    else:
        media = await telegram_manager.get_media(track_id)

    if not media:
        if lease:
            lease.release()
        raise HTTPException(status_code=404, detail="Media not found")

    doc = getattr(media, "document", None)
    if not total_size and doc:
        total_size = int(getattr(doc, "size", 0) or 0)

    if not total_size:
        if lease:
            lease.release()
        raise HTTPException(status_code=500, detail="Unable to determine audio file size")

    # 1. Parse Range header -> start, end bytes
    start = 0
    end = total_size - 1
    is_range = False

    if range:
        match = re.match(r"bytes=(\d*)-(\d*)", range)
        if match:
            s_str, e_str = match.groups()
            if s_str == "" and e_str != "":
                suffix = int(e_str)
                start = max(0, total_size - suffix)
                end = total_size - 1
                is_range = True
            elif s_str != "":
                start = int(s_str)
                end = int(e_str) if e_str != "" else total_size - 1
                is_range = True

    if is_range and (start >= total_size or start > end):
        if lease:
            lease.release()
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
        else (getattr(doc, "mime_type", None) or "audio/mpeg")
    )

    headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Content-Length": str(bytes_needed),
        "Cache-Control": "public, max-age=86400",
    }
    if is_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

    if start == 0 and not await fast_start_cache.has(track_id):
        _log("PROBE", f'{track["title"]} (64 KB header read)')

    if is_range and start > 0:
        pct = round(start / total_size * 100) if total_size else 0
        _log("SEEK", f'{track["title"]} • Seeked to ~{_fmt_duration(start // (total_size // max(track.get("duration",1),1))*1 if total_size else 0)} ({pct}%) • {_fmt_size(start)}')
    elif not is_range:
        _log("PRECACHE", f'{track["title"]} • {_fmt_quality(track)} • {_fmt_size(total_size)} ({_fmt_duration(track.get("duration"))}) -> 200 OK')

    # 9. Async streaming generator
    async def audio_generator() -> AsyncGenerator[bytes, None]:
        nonlocal lease, dl_client, media, bot_ref
        bytes_sent = 0

        try:
            # Check fastStartCache for requested byte range
            cached_preamble = await fast_start_cache.get(track_id)
            if cached_preamble and start < len(cached_preamble):
                available = len(cached_preamble) - start
                take = min(available, bytes_needed)
                slice_data = cached_preamble[start : start + take]
                yield slice_data
                bytes_sent += len(slice_data)

                if bytes_sent >= bytes_needed:
                    return

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

                    if not refreshed and is_file_ref:
                        refreshed = True
                        if bot_ref:
                            media = await bot_pool.get_media(bot_ref, track_id, force=True)
                        else:
                            media = await telegram_manager.get_media(track_id, force=True)
                        if media:
                            current_pos = start + bytes_sent
                            aligned_start = (current_pos // REQUEST_CHUNK_SIZE) * REQUEST_CHUNK_SIZE
                            alignment_gap = current_pos - aligned_start
                            continue

                    # Fallback to user client if bot failed
                    if bot_ref:
                        if lease:
                            lease.release()
                            lease = None
                        bot_ref = None
                        dl_client = telegram_manager.client
                        media = await telegram_manager.get_media(track_id, force=True)
                        if media:
                            current_pos = start + bytes_sent
                            aligned_start = (current_pos // REQUEST_CHUNK_SIZE) * REQUEST_CHUNK_SIZE
                            alignment_gap = current_pos - aligned_start
                            continue

                    raise exc
        finally:
            # 12. Release bot lease
            if lease:
                lease.release()
                lease = None

    status_code = 206 if is_range else 200
    return StreamingResponse(
        audio_generator(),
        status_code=status_code,
        headers=headers,
    )


@router.get("/refresh")
async def refresh_index() -> dict[str, str]:
    """Trigger background track indexing."""
    asyncio.create_task(background_indexer())
    return {"status": "indexing"}


@router.get("/deduplicate")
async def trigger_deduplicate() -> dict[str, str]:
    """Trigger deduplicate_library() as background task, returns immediately."""
    async def run_dedupe() -> None:
        from duplicates import deduplicate_library
        result = await deduplicate_library(
            telegram_manager.track_index,
            telegram_manager.client,
            telegram_manager.channel_entity,
        )
        _log("DEDUPE", f"Complete: kept {result['kept']}, removed {result['removed']}")
        telegram_manager.save_cache()
        search_engine.track_index = telegram_manager.track_index
        search_engine._initialized = False

    asyncio.create_task(run_dedupe())
    return {"status": "deduplication started"}


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
