import asyncio
from datetime import datetime
import re
from typing import Any, Optional


def _log(type_: str, msg: str) -> None:
    """Format and print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {type_:^8} | {msg}")


def normalize_title(t: str) -> str:
    if not t: return ""
    import re
    t = t.lower()
    t = re.sub(r'\((?:from|official|audio|video|lyrics|full song|remastered|hd|hq|club mix|feat\.?|ft\.?|radio edit|clean|explicit|atmos|dolby\s*atmos).*?\)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\[(?:from|official|audio|video|lyrics|full song|remastered|hd|hq|club mix|feat\.?|ft\.?|radio edit|clean|explicit|atmos|dolby\s*atmos).*?\]', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*[-–—]\s*(?:radio edit|original mix|single|clean|explicit|atmos|dolby\s*atmos|new version|version|lofi|remix|acoustic|live|slowed|reverb|edit|revisited|unplugged|from\s+.*?)$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def get_core_title(s: str) -> str:
    if not s: return ""
    if ' - ' in s or ' – ' in s or ' — ' in s:
        parts = re.split(r'\s+[-–—]+\s+', s)
        return normalize_title(parts[-1])
    return normalize_title(s)


def normalize_artist(a: str) -> str:
    if not a or a.lower() == 'unknown artist': return ""
    a = a.lower()
    a = re.sub(r'[^\w\s]', '', a)
    a = re.sub(r'\s+', ' ', a)
    return a.strip()


normalizeArtist = normalize_artist


def extract_version_tag(title: str) -> str:
    if not title: return ""
    match = re.search(
        r'[\(\[\{](?:version|remix|acoustic|instrumental|lofi|jhankar|slowed|reverb|live|female|male|unplugged|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost).*?[\)\]\}]|[-–—]\s*(?:version|remix|acoustic|instrumental|lofi|jhankar|slowed|reverb|live|female|male|unplugged|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost)$',
        title, re.IGNORECASE
    )
    return re.sub(r'[^\w]', '', match.group(0).lower()) if match else ""


def is_duplicate(a: dict, b: dict) -> bool:
    if a["id"] == b["id"]: return False

    is_atmos_a = bool(a.get("isAtmos") or a.get("format") == "eac3-joc")
    is_atmos_b = bool(b.get("isAtmos") or b.get("format") == "eac3-joc")
    if is_atmos_a != is_atmos_b: return False

    if a.get("isrc") and b.get("isrc"):
        if str(a["isrc"]).strip().upper() == str(b["isrc"]).strip().upper():
            return True

    tag_a = extract_version_tag(a.get("title", ""))
    tag_b = extract_version_tag(b.get("title", ""))
    if tag_a != tag_b: return False

    dur_a = a.get("duration")
    dur_b = b.get("duration")
    if dur_a and dur_b and abs(int(dur_a) - int(dur_b)) > 15:
        return False

    title_a = normalize_title(a.get("title", ""))
    title_b = normalize_title(b.get("title", ""))
    core_a = get_core_title(a.get("title", ""))
    core_b = get_core_title(b.get("title", ""))

    if not title_a or not title_b: return False

    titles_match = (title_a == title_b or core_a == core_b
                    or title_a == core_b or core_a == title_b)

    if titles_match:
        artist_a = normalize_artist(a.get("artist", ""))
        artist_b = normalize_artist(b.get("artist", ""))
        if artist_a and artist_b:
            words_a = [w for w in artist_a.split() if len(w) >= 2]
            words_b = [w for w in artist_b.split() if len(w) >= 2]
            has_common = (
                any(w in artist_b for w in words_a) or
                any(w in artist_a for w in words_b)
            )
            if has_common: return True
            if dur_a and dur_b and abs(int(dur_a) - int(dur_b)) <= 6:
                return True
            return False
        return True

    return False


isDuplicate = is_duplicate


def get_quality_score(track: dict[str, Any]) -> int:
    """Calculate quality score for comparison:
    eac3/atmos = 100
    flac/alac/wav 24bit+ or 48kHz+ = 90
    flac/alac/wav = 80
    mp3 >= 320kbps = 60
    mp3 >= 256kbps = 50
    mp3 >= 128kbps = 40
    else = 30
    """
    is_atmos = bool(
        track.get("isAtmos")
        or (track.get("format") or "").lower() in {"eac3-joc", "eac3", "ec3"}
        or track.get("quality") == "Dolby Atmos"
    )
    if is_atmos:
        return 100

    fmt = (track.get("format") or "").lower()
    if fmt in {"flac", "alac", "wav"}:
        bd = track.get("bitDepth") or 16
        sr = track.get("sampleRate") or 44100
        if bd >= 24 or sr >= 48000:
            return 90
        return 80

    raw_kbps: Optional[int] = track.get("bitrate")
    if not raw_kbps:
        file_size = track.get("fileSize") or 0
        duration = track.get("duration") or 0
        if file_size and duration and duration > 0:
            raw_kbps = round((file_size * 8) / (duration * 1000))
            if fmt == "mp3" and raw_kbps > 320:
                raw_kbps = 320
        else:
            q = track.get("quality") or ""
            m = re.search(r"\((\d+)kbps\)", q, re.IGNORECASE)
            if m:
                raw_kbps = int(m.group(1))
            elif fmt == "mp3":
                raw_kbps = 320
            else:
                raw_kbps = 0

    if fmt == "mp3":
        if raw_kbps >= 320:
            return 60
        elif raw_kbps >= 256:
            return 50
        elif raw_kbps >= 128:
            return 40
        else:
            return 30

    return 30


async def _delete_after_delay(msg_id: str | int, delay_seconds: int, client: Any, channel: Any) -> None:
    """Wait for delay_seconds then delete message from Telegram channel."""
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(channel, [int(msg_id)], revoke=True)
        _log("DUPLICATE", f"Deleted message ID: {msg_id} from channel")
    except Exception as e:
        _log("DUPLICATE", f"Failed to delete ID: {msg_id}: {e}")


async def deduplicate_library(
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> dict[str, int]:
    """Scan and deduplicate entire track index, scheduling duplicates for deletion."""
    def sort_key(t: dict[str, Any]) -> tuple[int, int, int]:
        score = get_quality_score(t)
        file_size = int(t.get("fileSize") or 0)
        try:
            tid = int(t.get("id") or 0)
        except ValueError:
            tid = 0
        return (-score, -file_size, tid)

    sorted_tracks = sorted(track_index, key=sort_key)
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for track in sorted_tracks:
        if track.get("keep"):
            kept.append(track)
            continue

        dup = next((existing for existing in kept if is_duplicate(existing, track)), None)
        if not dup:
            kept.append(track)
        else:
            _log("DUPLICATE", f"{track['title']} (ID: {track['id']}) -> duplicate of ID: {dup['id']}, deleting in 30s")
            if tg_client and channel_entity:
                asyncio.create_task(_delete_after_delay(track["id"], 30, tg_client, channel_entity))
            removed.append(track)

    track_index.clear()
    track_index.extend(kept)
    return {"kept": len(kept), "removed": len(removed)}


async def check_new_track(
    new_track: dict[str, Any],
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> Optional[dict[str, Any]]:
    """Real-time duplicate detection when a new message arrives in channel."""
    if new_track.get("keep"):
        track_index.append(new_track)
        return new_track

    existing = next((t for t in track_index if is_duplicate(t, new_track)), None)
    if not existing:
        track_index.append(new_track)
        return new_track

    # Duplicate found: determine which to delete
    if get_quality_score(new_track) > get_quality_score(existing):
        _log("DUPLICATE", f"New upload '{new_track.get('title')}' is higher quality, replacing existing ID: {existing.get('id')}")
        if tg_client and channel_entity:
            asyncio.create_task(_delete_after_delay(existing["id"], 30, tg_client, channel_entity))
        try:
            idx = track_index.index(existing)
            track_index[idx] = new_track
        except ValueError:
            track_index.append(new_track)
        return new_track
    else:
        _log("DUPLICATE", f"New upload '{new_track.get('title')}' is duplicate, deleting in 30s")
        if tg_client and channel_entity:
            asyncio.create_task(_delete_after_delay(new_track["id"], 30, tg_client, channel_entity))
        return None
