import asyncio
from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Optional
import uuid

# Safety-critical configuration constants
GRACE_PERIOD_SECONDS = 300  # 5 minutes
PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_deletions.json")
KEPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kept_tracks.json")

dedupe_lock = asyncio.Lock()
_active_deletion_tasks: set[str] = set()

ARTIST_STOP_WORDS = {
    "the", "and", "feat", "ft", "featuring", "various", "artist", "artists",
    "unknown", "music", "official", "prod", "by", "of", "a", "an", "in", "on", "with"
}

ARTIST_DELIMITERS = re.compile(
    r"\s*(?:,|&|\+|\/|\b(?:x|vs\.?|feat\.?|ft\.?|featuring|and)\b)\s*",
    re.IGNORECASE,
)

ATMOS_REGEX = re.compile(
    r"\b(atmos|dolby\s*atmos|spatial\s*audio|eac3[-\s]?joc|eac3|ec3)\b|\[atmos\]|\(atmos\)",
    re.IGNORECASE,
)

VERSION_REGEX = re.compile(
    r"[\(\[\{](?:version|remix|acoustic|instrumental|lofi|lo-fi|jhankar|slowed|reverb|live|female|male|unplugged|stripped|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost|demo|cover|piano version|orchestral).*?[\)\]\}]|[-\u2013\u2014]\s*(?:version|remix|acoustic|instrumental|lofi|lo-fi|jhankar|slowed|reverb|live|female|male|unplugged|stripped|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost|demo|cover|piano version|orchestral)$",
    re.IGNORECASE,
)


def _log(type_: str, msg: str) -> None:
    """Format and print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {type_:^8} | {msg}")


def load_kept_ids() -> set[str]:
    """Load persistently kept track IDs from disk."""
    if not os.path.exists(KEPT_FILE):
        return set()
    try:
        with open(KEPT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {str(x) for x in data}
    except Exception as exc:
        _log("DEDUPE", f"Warning: Failed to load kept track IDs from {KEPT_FILE}: {exc}")
    return set()


def save_kept_ids(kept_ids: set[str]) -> bool:
    """Persist kept track IDs atomically to disk."""
    tmp_path = f"{KEPT_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(kept_ids)), f, indent=2)
        os.replace(tmp_path, KEPT_FILE)
        return True
    except Exception as exc:
        _log("DEDUPE", f"CRITICAL: Failed to save kept track IDs to {KEPT_FILE}: {exc}")
        return False


def normalize_title(t: str) -> str:
    """Clean and normalize title string without destroying distinguishing version tags."""
    if not t:
        return ""
    t = t.lower()
    t = re.sub(
        r"\((?:from|official|audio|video|lyrics|full song|remastered|hd|hq|club mix|feat\.?|ft\.?|radio edit|clean|explicit|atmos|dolby\s*atmos).*?\)",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\[(?:from|official|audio|video|lyrics|full song|remastered|hd|hq|club mix|feat\.?|ft\.?|radio edit|clean|explicit|atmos|dolby\s*atmos).*?\]",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\s*[-\u2013\u2014]\s*(?:radio edit|original mix|single|clean|explicit|atmos|dolby\s*atmos|new version|version|lofi|remix|acoustic|live|slowed|reverb|edit|revisited|unplugged|from\s+.*?)$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


normalizeTitle = normalize_title


def get_core_title(s: str) -> str:
    """Extract stripped core title removing prefix before hyphen."""
    if not s:
        return ""
    if " - " in s or " \u2013 " in s or " \u2014 " in s:
        parts = re.split(r"\s+[-\u2013\u2014]+\s+", s)
        return normalize_title(parts[-1])
    return normalize_title(s)


getCoreTitle = get_core_title


def normalize_artist(a: str) -> str:
    """Normalize artist string."""
    if not a or a.strip().lower() == "unknown artist":
        return ""
    a = a.lower()
    a = re.sub(r"[^\w\s]", "", a)
    a = re.sub(r"\s+", " ", a)
    return a.strip()


normalizeArtist = normalize_artist


def get_artist_tokens(artist_str: str) -> set[str]:
    """Tokenize artist into distinct non-stopword tokens (replaces dangerous substring matching)."""
    norm = normalize_artist(artist_str)
    if not norm:
        return set()

    tokens: set[str] = set()
    # Split across artist delimiters (comma, &, feat, ft, etc.)
    segments = ARTIST_DELIMITERS.split(norm)
    for seg in segments:
        words = seg.split()
        for w in words:
            w_clean = re.sub(r"[^\w]", "", w)
            if len(w_clean) >= 2 and w_clean not in ARTIST_STOP_WORDS:
                tokens.add(w_clean)

    return tokens


def extract_version_tag(title: str) -> str:
    """Extract version tag from brackets or suffix."""
    if not title:
        return ""
    match = VERSION_REGEX.search(title)
    if match:
        return re.sub(r"[^\w]", "", match.group(0).lower())
    return ""


extractVersionTag = extract_version_tag


def is_atmos_track(track: dict[str, Any]) -> bool:
    """Determine whether a track is a Dolby Atmos or spatial audio mix."""
    if not track:
        return False
    if track.get("isAtmos"):
        return True
    fmt = (track.get("format") or "").lower()
    if fmt in {"eac3-joc", "eac3", "ec3", "e-ac-3"}:
        return True
    quality = (track.get("quality") or "").lower()
    if "atmos" in quality or "spatial" in quality:
        return True
    title = str(track.get("title") or "").lower()
    file_name = str(track.get("fileName") or "").lower()
    if ATMOS_REGEX.search(title) or ATMOS_REGEX.search(file_name):
        return True
    return False


def is_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Determine whether track a and track b are genuine duplicates.

    Safety principles enforced:
    - Never delete across different IDs or when either is marked keep.
    - Never delete across Atmos vs Stereo mixes.
    - Never delete across different versions (Remix, Live, Acoustic, etc.).
    - Exact token-based artist matching (NO substring matching).
    - Unknown artists NEVER match unless verified by identical ISRC.
    """
    id_a = str(a.get("id"))
    id_b = str(b.get("id"))
    if id_a == id_b:
        return False

    # Persistent /keep check
    kept_ids = load_kept_ids()
    if a.get("keep") or id_a in kept_ids or b.get("keep") or id_b in kept_ids:
        return False

    # Atmos vs Stereo protection
    is_atmos_a = is_atmos_track(a)
    is_atmos_b = is_atmos_track(b)
    if is_atmos_a != is_atmos_b:
        return False

    # Version protection: distinct musical arrangements must never be flagged as duplicates
    tag_a = extract_version_tag(str(a.get("title") or ""))
    tag_b = extract_version_tag(str(b.get("title") or ""))
    if tag_a != tag_b:
        return False

    # Exact ISRC match: identical master recording
    isrc_a = str(a.get("isrc") or "").strip().upper()
    isrc_b = str(b.get("isrc") or "").strip().upper()
    if isrc_a and isrc_b and isrc_a == isrc_b:
        return True

    # Duration check: reject if duration difference exceeds 15 seconds
    dur_a = a.get("duration")
    dur_b = b.get("duration")
    if dur_a is not None and dur_b is not None:
        try:
            if abs(float(dur_a) - float(dur_b)) > 15:
                return False
        except (ValueError, TypeError):
            pass

    # Title check
    title_a = normalize_title(str(a.get("title") or ""))
    title_b = normalize_title(str(b.get("title") or ""))
    core_a = get_core_title(str(a.get("title") or ""))
    core_b = get_core_title(str(b.get("title") or ""))

    if not title_a or not title_b:
        return False

    titles_match = (
        title_a == title_b
        or core_a == core_b
        or title_a == core_b
        or core_a == title_b
    )

    if not titles_match:
        return False

    # Artist check: Token-based match (NO substring matching)
    tokens_a = get_artist_tokens(str(a.get("artist") or ""))
    tokens_b = get_artist_tokens(str(b.get("artist") or ""))

    # Unknown artist safety: NEVER classify unknown artists as duplicates without independent ISRC
    if not tokens_a or not tokens_b:
        return False

    # Both artists known: check exact token intersection
    common_tokens = tokens_a & tokens_b
    if common_tokens:
        return True

    # If artists differ, close duration alone is insufficient
    return False


isDuplicate = is_duplicate


def get_quality_score(track: dict[str, Any]) -> int:
    """Calculate audio quality score with robust unknown-format and division-by-zero handling."""
    if not track:
        return 0

    size_bytes = int(track.get("fileSize") or track.get("sizeBytes") or 0)

    # Tier 1: Dolby Atmos / Spatial Audio
    if is_atmos_track(track):
        return 9000000 + max(0, size_bytes)

    fmt = (track.get("format") or "").lower()
    file_name = track.get("fileName") or ""
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""
    effective_fmt = fmt or ext

    # Tier 2: Lossless (FLAC, WAV, ALAC, AIFF)
    if effective_fmt in {"flac", "wav", "alac", "aif", "aiff"}:
        try:
            bits = int(track.get("bitDepth") or 16)
        except (ValueError, TypeError):
            bits = 16
        try:
            rate = int(track.get("sampleRate") or 44100)
        except (ValueError, TypeError):
            rate = 44100
        bits = max(16, min(bits, 32))
        rate = max(44100, min(rate, 384000))
        return 1000000 + (bits * rate)

    # Tier 3: Lossy formats
    raw_kbps = 0
    if track.get("bitrate"):
        try:
            raw_kbps = int(track["bitrate"])
        except (ValueError, TypeError):
            raw_kbps = 0

    if not raw_kbps:
        duration = 0.0
        try:
            duration = float(track.get("duration") or 0.0)
        except (ValueError, TypeError):
            duration = 0.0

        if size_bytes > 0 and duration > 0.0:
            raw_kbps = round((size_bytes * 8) / (duration * 1000))
            if effective_fmt == "mp3" and raw_kbps > 320:
                raw_kbps = 320
            elif effective_fmt in {"aac", "m4a", "opus", "ogg"} and raw_kbps > 500:
                raw_kbps = 500
        else:
            if effective_fmt == "mp3":
                raw_kbps = 256
            elif effective_fmt in {"aac", "m4a", "opus", "ogg"}:
                raw_kbps = 192
            else:
                raw_kbps = 128

    multiplier = 1.0
    if effective_fmt == "opus":
        multiplier = 1.5
    elif effective_fmt in {"aac", "m4a"}:
        multiplier = 1.25

    return round(max(10, min(raw_kbps, 600)) * multiplier)


getQualityScore = get_quality_score


def choose_keeper(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Determine keeper and deletion candidate between two duplicate tracks.

    Returns (keeper, candidate_to_delete)
    """
    score_a = get_quality_score(a)
    score_b = get_quality_score(b)
    if score_a > score_b:
        return a, b
    elif score_b > score_a:
        return b, a

    size_a = int(a.get("fileSize") or a.get("sizeBytes") or 0)
    size_b = int(b.get("fileSize") or b.get("sizeBytes") or 0)
    if size_a > size_b:
        return a, b
    elif size_b > size_a:
        return b, a

    try:
        id_a = int(a.get("id") or 0)
    except (ValueError, TypeError):
        id_a = 0
    try:
        id_b = int(b.get("id") or 0)
    except (ValueError, TypeError):
        id_b = 0

    if id_a <= id_b:
        return a, b
    else:
        return b, a


def compare_tracks(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compatibility decision object wrapper."""
    if is_duplicate(a, b):
        return {
            "decision": "exact_duplicate",
            "confidence": 1.0,
            "matched_by": ["exact_match"],
            "reason": "Duplicate match according to strict library rules",
            "safe_to_auto_delete": True,
        }
    return {
        "decision": "different_recording",
        "confidence": 0.0,
        "matched_by": [],
        "reason": "Not a duplicate",
        "safe_to_auto_delete": False,
    }


def extract_version_profile(title: str | dict[str, Any]) -> dict[str, str]:
    """Compatibility version profile helper."""
    raw_title = str(title.get("title") if isinstance(title, dict) else title or "")
    tag = extract_version_tag(raw_title)
    is_atmos = is_atmos_track(title) if isinstance(title, dict) else bool(ATMOS_REGEX.search(raw_title))
    return {
        "mix_type": "atmos" if is_atmos else "stereo",
        "version_type": tag or "unknown",
        "version_label": tag,
    }


def load_pending() -> list[dict[str, Any]]:
    """Read PENDING_FILE if exists and return list of pending records."""
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as exc:
        _log("DEDUPE", f"Error loading pending records from {PENDING_FILE}: {exc}")
    return []


def save_pending(records: list[dict[str, Any]]) -> bool:
    """Atomically write pending records: write to .tmp then rename. Returns True on success, False on error."""
    tmp_path = f"{PENDING_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp_path, PENDING_FILE)
        return True
    except Exception as exc:
        _log("DEDUPE", f"CRITICAL: Failed to save pending records to {PENDING_FILE}: {exc}")
        return False


async def audit_library(track_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan all tracks and find duplicate candidates using is_duplicate.

    NO deletion. NO scheduling. Report only.
    """
    def sort_key(t: dict[str, Any]) -> tuple[int, int, int]:
        score = get_quality_score(t)
        size = int(t.get("fileSize") or t.get("sizeBytes") or 0)
        try:
            tid = int(t.get("id") or 0)
        except (ValueError, TypeError):
            tid = 0
        return (-score, -size, tid)

    sorted_tracks = sorted(track_index, key=sort_key)
    kept: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for track in sorted_tracks:
        if track.get("keep"):
            kept.append(track)
            continue

        dup = next((k for k in kept if not k.get("keep") and is_duplicate(k, track)), None)
        if not dup:
            kept.append(track)
        else:
            keeper, candidate = choose_keeper(dup, track)
            candidates.append({
                "candidate": candidate,
                "keeper": keeper,
                "decision": {
                    "decision": "exact_duplicate",
                    "confidence": 1.0,
                    "safe_to_auto_delete": True,
                    "reason": "Duplicate match",
                    "matched_by": ["exact_match"],
                },
            })

    _log("DEDUPE", f"Audit complete: {len(candidates)} duplicate candidates found")
    return candidates


async def deduplicate_library(
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> dict[str, Any]:
    """Scan library, audit candidates, and schedule safe deletions with 5m grace period.

    CRITICAL SAFETY RULES ENFORCED:
    - Never remove candidate from track_index until Telegram confirms deletion.
    - Persistence failure aborts scheduling.
    - Prevent duplicate pending deletion tasks.
    """
    async with dedupe_lock:
        def sort_key(t: dict[str, Any]) -> tuple[int, int, int]:
            score = get_quality_score(t)
            size = int(t.get("fileSize") or t.get("sizeBytes") or 0)
            try:
                tid = int(t.get("id") or 0)
            except (ValueError, TypeError):
                tid = 0
            return (-score, -size, tid)

        sorted_tracks = sorted(track_index, key=sort_key)
        kept_candidates: list[dict[str, Any]] = []
        duplicate_pairs: list[dict[str, Any]] = []

        pending_records = load_pending()
        existing_candidate_ids = {
            str(r.get("candidate_id")) for r in pending_records if r.get("status") == "pending"
        }

        for track in sorted_tracks:
            if track.get("keep"):
                kept_candidates.append(track)
                continue

            dup = next((k for k in kept_candidates if not k.get("keep") and is_duplicate(k, track)), None)
            if not dup:
                kept_candidates.append(track)
            else:
                duplicate_pairs.append({"deleted": track, "kept": dup})

        queued_count = 0
        new_pending_records = list(pending_records)
        tasks_to_spawn: list[dict[str, Any]] = []

        for pair in duplicate_pairs:
            track = pair["deleted"]
            dup = pair["kept"]
            cand_id = str(track.get("id"))

            if cand_id in existing_candidate_ids or cand_id in _active_deletion_tasks:
                continue

            now = datetime.now(timezone.utc)
            delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
            delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

            record: dict[str, Any] = {
                "pending_id": str(uuid.uuid4()),
                "candidate_id": cand_id,
                "keeper_id": str(dup.get("id")),
                "confidence": 1.0,
                "reason": "duplicate",
                "matched_by": ["exact_match"],
                "created_at": now.isoformat(),
                "delete_after": delete_after_iso,
                "status": "pending",
                "notice_message_id": None,
            }

            new_pending_records.append(record)
            existing_candidate_ids.add(cand_id)
            tasks_to_spawn.append(record)

        # Make persistence failure abort task scheduling
        if tasks_to_spawn:
            if not save_pending(new_pending_records):
                _log("DEDUPE", "Persistence failure — ABORTED scheduling of deletion tasks")
                tasks_to_spawn = []
            else:
                for rec in tasks_to_spawn:
                    cid = rec["candidate_id"]
                    _active_deletion_tasks.add(cid)
                    queued_count += 1
                    cand_title = rec.get("candidate_id")
                    _log("DEDUPE", f"Queued duplicate ID: {cid} (Keeper: {rec['keeper_id']}), deleting in 5m")
                    asyncio.create_task(_execute_deletion(rec, tg_client, channel_entity, track_index))

        # NOTE: Do NOT remove candidates from track_index here!
        # Tracks remain in track_index until Telegram confirms deletion.
        return {
            "checked": len(track_index),
            "duplicatesRemoved": queued_count,
            "removed": queued_count,
            "kept": len(track_index) - queued_count,
            "audited": len(duplicate_pairs),
            "queued": queued_count,
            "safe_auto": queued_count,
        }


deduplicateEntireLibrary = deduplicate_library


async def _execute_deletion(
    record: dict[str, Any],
    tg_client: Any,
    channel_entity: Any,
    track_index: list[dict[str, Any]],
) -> None:
    """Wait for grace period, revalidate both Telegram messages, and delete message safely."""
    cand_id_str = str(record["candidate_id"])
    cand_id_int = int(record["candidate_id"])
    keeper_id_int = int(record["keeper_id"])

    try:
        delete_after_dt = datetime.fromisoformat(record["delete_after"])
        if delete_after_dt.tzinfo is None:
            delete_after_dt = delete_after_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delay_seconds = max(0.0, (delete_after_dt - now).total_seconds())
    except Exception:
        delay_seconds = 0.0

    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    try:
        # Check current pending status under lock
        async with dedupe_lock:
            records = load_pending()
            curr_record = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
            if not curr_record or curr_record.get("status") != "pending":
                _log("DEDUPE", f"Deletion cancelled or no longer pending for ID: {cand_id_str}")
                return

            kept_ids = load_kept_ids()
            if cand_id_str in kept_ids:
                curr_record["status"] = "cancelled"
                save_pending(records)
                _log("DEDUPE", f"Track ID {cand_id_str} is persistently kept — cancelled deletion")
                return

        if not tg_client or not channel_entity:
            _log("DEDUPE", f"Telegram client or channel entity unavailable for deleting ID: {cand_id_str}")
            return

        # NETWORK OPERATION 1: Fetch candidate message WITHOUT holding dedupe_lock
        try:
            cand_msgs = await tg_client.get_messages(channel_entity, ids=[cand_id_int])
        except Exception as fetch_err:
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "failed"
                    cr["last_error"] = str(fetch_err)
                    save_pending(records)
            _log("DEDUPE", f"Fetch error for candidate ID {cand_id_str}: {fetch_err} — retaining track")
            return

        cand_msg = cand_msgs[0] if cand_msgs else None
        if cand_msg is None or getattr(cand_msg, "id", None) is None:
            # Confirmed gone from Telegram: update index and status under lock
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "completed"
                    save_pending(records)
                for i, t in enumerate(track_index):
                    if str(t.get("id")) == cand_id_str:
                        track_index.pop(i)
                        break
            _log("DEDUPE", f"ID {cand_id_str} already gone from Telegram")
            return

        # NETWORK OPERATION 2: Fetch keeper message WITHOUT holding dedupe_lock
        try:
            keeper_msgs = await tg_client.get_messages(channel_entity, ids=[keeper_id_int])
        except Exception as fetch_err:
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "failed"
                    cr["last_error"] = str(fetch_err)
                    save_pending(records)
            _log("DEDUPE", f"Fetch error for keeper ID {keeper_id_int}: {fetch_err} — cancelling deletion of {cand_id_str}")
            return

        keeper_msg = keeper_msgs[0] if keeper_msgs else None
        if keeper_msg is None or getattr(keeper_msg, "id", None) is None:
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "failed"
                    cr["last_error"] = f"Keeper message {keeper_id_int} missing"
                    save_pending(records)
            _log("DEDUPE", f"Keeper ID {keeper_id_int} no longer exists, cancelling deletion of ID: {cand_id_str}")
            return

        # REVALIDATION: Re-parse and compare both fresh Telegram messages immediately before deletion
        from telegram_client import telegram_manager
        fresh_cand = telegram_manager._parse_track_message(cand_msg)
        fresh_keeper = telegram_manager._parse_track_message(keeper_msg)

        if not fresh_cand or not fresh_keeper:
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "cancelled"
                    cr["last_error"] = "Failed to parse fresh metadata"
                    save_pending(records)
            _log("DEDUPE", f"Failed to re-parse fresh track metadata for ID {cand_id_str}")
            return

        if not is_duplicate(fresh_cand, fresh_keeper):
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "cancelled"
                    cr["last_error"] = "Fresh comparison shows not duplicate"
                    save_pending(records)
            _log("DEDUPE", f"Revalidation failed: fresh metadata shows ID {cand_id_str} is NOT a duplicate")
            return

        # Verify keeper quality is still superior or equal to candidate
        if get_quality_score(fresh_keeper) < get_quality_score(fresh_cand):
            async with dedupe_lock:
                records = load_pending()
                cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
                if cr:
                    cr["status"] = "cancelled"
                    cr["last_error"] = "Keeper quality is lower than candidate"
                    save_pending(records)
            _log("DEDUPE", f"Revalidation failed: keeper ID {keeper_id_int} is lower quality than ID {cand_id_str}")
            return

        candidate_title = fresh_cand.get("title", f"ID {cand_id_str}")

        # NETWORK OPERATION 3: Delete message WITHOUT holding dedupe_lock
        try:
            del_result = await tg_client.delete_messages(channel_entity, [cand_id_int], revoke=True)
            success = bool(del_result)
        except Exception as exc:
            _log("DEDUPE", f"Telegram API exception deleting message ID {cand_id_str}: {exc}")
            success = False

        # Update state and remove from track_index ONLY after confirmed Telegram deletion
        async with dedupe_lock:
            records = load_pending()
            cr = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
            if success:
                for i, t in enumerate(track_index):
                    if str(t.get("id")) == cand_id_str:
                        track_index.pop(i)
                        break
                if cr:
                    cr["status"] = "completed"
                _log("DEDUPE", f"Confirmed Telegram deletion: {candidate_title} (ID: {cand_id_str})")
            else:
                if cr:
                    cr["status"] = "failed"
                _log("DEDUPE", f"Failed to delete ID: {cand_id_str}")

            save_pending(records)

    finally:
        _active_deletion_tasks.discard(cand_id_str)


async def keep_track(candidate_id: str) -> bool:
    """Persistently protect track from deletion and cancel pending deletion task."""
    cand_id_str = str(candidate_id)
    async with dedupe_lock:
        kept_ids = load_kept_ids()
        kept_ids.add(cand_id_str)
        save_kept_ids(kept_ids)

        records = load_pending()
        found = False
        for r in records:
            if str(r.get("candidate_id")) == cand_id_str and r.get("status") == "pending":
                r["status"] = "cancelled"
                found = True

        save_pending(records)
        _log("DEDUPE", f"Kept track ID: {cand_id_str} (persistently protected)")
        return found or True


async def recover_pending(
    tg_client: Any,
    channel_entity: Any,
    track_index: list[dict[str, Any]],
) -> None:
    """Recover and resume pending deletions on startup with duplicate task prevention."""
    records = load_pending()
    pending = [r for r in records if r.get("status") == "pending"]
    if not pending:
        return

    now = datetime.now(timezone.utc)
    for record in pending:
        cid = str(record["candidate_id"])
        if cid in _active_deletion_tasks:
            continue

        try:
            delete_after_dt = datetime.fromisoformat(record["delete_after"])
            if delete_after_dt.tzinfo is None:
                delete_after_dt = delete_after_dt.replace(tzinfo=timezone.utc)
        except Exception:
            delete_after_dt = now

        if delete_after_dt > now:
            remaining = (delete_after_dt - now).total_seconds()
            _log("DEDUPE", f"Recovered pending deletion ID: {cid}, {remaining:.0f}s remaining")
        else:
            _log("DEDUPE", f"Recovered expired pending deletion ID: {cid}, executing now")

        _active_deletion_tasks.add(cid)
        asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))


async def check_new_track(
    new_track: dict[str, Any],
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> Optional[dict[str, Any]]:
    """Real-time duplicate detection on new Telegram message upload."""
    if not new_track or not new_track.get("id"):
        return None

    cand_id = str(new_track.get("id"))
    kept_ids = load_kept_ids()
    if new_track.get("keep") or cand_id in kept_ids:
        track_index.insert(0, new_track)
        return new_track

    async with dedupe_lock:
        existing_dup = next((t for t in track_index if not t.get("keep") and is_duplicate(t, new_track)), None)
        if not existing_dup:
            track_index.insert(0, new_track)
            return new_track

        score_new = get_quality_score(new_track)
        score_old = get_quality_score(existing_dup)

        if score_new > score_old:
            # New track is superior quality: schedule existing for deletion, insert new track into index
            old_id = str(existing_dup.get("id"))
            _log("DUPLICATE", f"{new_track.get('title')} -> Upgraded quality (Replacing ID: {old_id})")

            if old_id not in _active_deletion_tasks:
                now = datetime.now(timezone.utc)
                delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
                delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

                record: dict[str, Any] = {
                    "pending_id": str(uuid.uuid4()),
                    "candidate_id": old_id,
                    "keeper_id": cand_id,
                    "confidence": 1.0,
                    "reason": "upgrade",
                    "matched_by": ["exact_match"],
                    "created_at": now.isoformat(),
                    "delete_after": delete_after_iso,
                    "status": "pending",
                    "notice_message_id": None,
                }

                pending_records = load_pending()
                pending_records.append(record)
                if save_pending(pending_records):
                    _active_deletion_tasks.add(old_id)
                    asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

            try:
                idx = track_index.index(existing_dup)
                track_index[idx] = new_track
            except ValueError:
                track_index.insert(0, new_track)
            return new_track

        else:
            # New track is lower/equal quality: schedule new track for deletion, DO NOT add to track_index
            is_lower = score_new < score_old
            reason = "lower_quality" if is_lower else "identical"
            keeper_id = str(existing_dup.get("id"))

            _log("DUPLICATE", f"{new_track.get('title')} (ID: {cand_id}) -> {reason} duplicate of ID: {keeper_id}, deleting in 5m")

            if cand_id not in _active_deletion_tasks:
                now = datetime.now(timezone.utc)
                delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
                delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

                record = {
                    "pending_id": str(uuid.uuid4()),
                    "candidate_id": cand_id,
                    "keeper_id": keeper_id,
                    "confidence": 1.0,
                    "reason": reason,
                    "matched_by": ["exact_match"],
                    "created_at": now.isoformat(),
                    "delete_after": delete_after_iso,
                    "status": "pending",
                    "notice_message_id": None,
                }

                pending_records = load_pending()
                pending_records.append(record)
                if save_pending(pending_records):
                    _active_deletion_tasks.add(cand_id)
                    asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

            return None
