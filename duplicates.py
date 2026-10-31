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
dedupe_lock = asyncio.Lock()


def _log(type_: str, msg: str) -> None:
    """Format and print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {type_:^8} | {msg}")


def normalize_title(t: str) -> str:
    """Translate of normalizeTitle() from index.js."""
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
    """Translate of getCoreTitle() from index.js."""
    if not s:
        return ""
    if " - " in s or " \u2013 " in s or " \u2014 " in s:
        parts = re.split(r"\s+[-\u2013\u2014]+\s+", s)
        return normalize_title(parts[-1])
    return normalize_title(s)


getCoreTitle = get_core_title


def normalize_artist(a: str) -> str:
    """Translate of normalizeArtist() from index.js."""
    if not a or a.lower() == "unknown artist":
        return ""
    a = a.lower()
    a = re.sub(r"[^\w\s]", "", a)
    a = re.sub(r"\s+", " ", a)
    return a.strip()


normalizeArtist = normalize_artist


def extract_version_tag(title: str) -> str:
    """Translate of extractVersionTag() from index.js."""
    if not title:
        return ""
    match = re.search(
        r"[\(\[\{](?:version|remix|acoustic|instrumental|lofi|jhankar|slowed|reverb|live|female|male|unplugged|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost).*?[\)\]\}]|[-\u2013\u2014]\s*(?:version|remix|acoustic|instrumental|lofi|jhankar|slowed|reverb|live|female|male|unplugged|radio edit|original mix|extended mix|club mix|revisited|soundtrack|ost)$",
        title,
        re.IGNORECASE,
    )
    if match:
        return re.sub(r"[^\w]", "", match.group(0).lower())
    return ""


extractVersionTag = extract_version_tag


def is_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Translate of isDuplicate() from index.js."""
    if str(a.get("id")) == str(b.get("id")):
        return False

    # Preserve both Dolby Atmos and stereo mixes
    is_atmos_a = bool(a.get("isAtmos") or a.get("format") == "eac3-joc" or a.get("quality") == "Dolby Atmos")
    is_atmos_b = bool(b.get("isAtmos") or b.get("format") == "eac3-joc" or b.get("quality") == "Dolby Atmos")
    if is_atmos_a != is_atmos_b:
        return False

    # Exact ISRC match: identical master recording
    if a.get("isrc") and b.get("isrc") and str(a["isrc"]).strip().upper() == str(b["isrc"]).strip().upper():
        return True

    # Preserve distinct musical arrangements (Remixes, Acoustic, Instrumental, Live, etc.)
    tag_a = extract_version_tag(str(a.get("title") or ""))
    tag_b = extract_version_tag(str(b.get("title") or ""))
    if tag_a != tag_b:
        return False

    dur_a = a.get("duration")
    dur_b = b.get("duration")
    if dur_a is not None and dur_b is not None and abs(float(dur_a) - float(dur_b)) > 15:
        return False

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

    if titles_match:
        artist_a = normalize_artist(str(a.get("artist") or ""))
        artist_b = normalize_artist(str(b.get("artist") or ""))
        if artist_a and artist_b:
            words_a = [w for w in artist_a.split(" ") if len(w) >= 2]
            words_b = [w for w in artist_b.split(" ") if len(w) >= 2]
            has_common_artist = any(w in artist_b for w in words_a) or any(w in artist_a for w in words_b)
            if has_common_artist:
                return True

            if dur_a is not None and dur_b is not None and abs(float(dur_a) - float(dur_b)) <= 6:
                return True
            return False
        return True

    return False


isDuplicate = is_duplicate


def get_quality_score(track: dict[str, Any]) -> int:
    """Translate of getQualityScore() from index.js."""
    size_bytes = int(track.get("fileSize") or track.get("sizeBytes") or 0)
    if track.get("isAtmos") or track.get("format") == "eac3-joc" or track.get("quality") == "Dolby Atmos":
        return 9000000 + size_bytes

    fmt = (track.get("format") or "").lower()
    if fmt in ["flac", "wav", "alac"]:
        bits = int(track.get("bitDepth") or 16)
        rate = int(track.get("sampleRate") or 44100)
        return 1000000 + (bits * rate)

    raw_kbps = 320
    if track.get("bitrate"):
        raw_kbps = int(track["bitrate"])
    elif size_bytes and track.get("duration"):
        raw_kbps = round((size_bytes * 8) / (float(track["duration"]) * 1000))
        # Hard cap fallback calculation to prevent massive artwork from inflating lossy scores
        if fmt == "mp3" and raw_kbps > 320:
            raw_kbps = 320
        if fmt in ["aac", "m4a", "opus"] and raw_kbps > 500:
            raw_kbps = 500

    multiplier = 1.0
    if fmt == "opus":
        multiplier = 1.5
    elif fmt in ["aac", "m4a"]:
        multiplier = 1.25

    return round(raw_kbps * multiplier)


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
    """Compatibility wrapper returning a structured decision object based on is_duplicate."""
    if is_duplicate(a, b):
        return {
            "decision": "exact_duplicate",
            "confidence": 1.0,
            "matched_by": ["exact_match"],
            "reason": "Duplicate match according to library rules",
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
    """Compatibility profile helper."""
    raw_title = str(title.get("title") if isinstance(title, dict) else title or "")
    tag = extract_version_tag(raw_title)
    is_atmos = bool(
        isinstance(title, dict) and (title.get("isAtmos") or title.get("format") == "eac3-joc" or title.get("quality") == "Dolby Atmos")
        or re.search(r"\b(atmos|dolby\s*atmos)\b", raw_title, re.IGNORECASE)
    )
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


def save_pending(records: list[dict[str, Any]]) -> None:
    """Atomically write pending records: write to .tmp then rename."""
    tmp_path = f"{PENDING_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp_path, PENDING_FILE)
    except Exception as exc:
        _log("DEDUPE", f"Error atomically saving pending records: {exc}")


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
    """Translate of deduplicateEntireLibrary() from index.js with 5m grace period and pending records."""
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
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []

        pending_records = load_pending()
        existing_candidate_ids = {
            str(r.get("candidate_id")) for r in pending_records if r.get("status") == "pending"
        }

        queued_count = 0

        for track in sorted_tracks:
            # Never auto-delete tracks flagged with keep
            if track.get("keep"):
                kept.append(track)
                continue

            dup = next((k for k in kept if not k.get("keep") and is_duplicate(k, track)), None)
            if not dup:
                kept.append(track)
            else:
                _log("DUPLICATE", f"{track.get('title')} (ID: {track.get('id')}) -> Removed duplicate in favor of ID: {dup.get('id')}")
                removed.append({"deleted": track, "kept": dup})
                cand_id = str(track.get("id"))

                if cand_id not in existing_candidate_ids:
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

                    pending_records.append(record)
                    existing_candidate_ids.add(cand_id)
                    queued_count += 1
                    asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

        save_pending(pending_records)
        track_index.clear()
        track_index.extend(kept)

        return {
            "checked": len(track_index),
            "duplicatesRemoved": len(removed),
            "removed": len(removed),
            "kept": len(kept),
            "audited": len(removed),
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
    """Wait until delete_after grace period expires, re-verify safety, and delete message."""
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

    async with dedupe_lock:
        records = load_pending()
        curr_record = next((r for r in records if r.get("pending_id") == record.get("pending_id")), None)
        if not curr_record or curr_record.get("status") != "pending":
            _log("DEDUPE", f"Deletion cancelled or no longer pending for ID: {record.get('candidate_id')}")
            return

        if not tg_client or not channel_entity:
            _log("DEDUPE", f"Telegram client or channel entity unavailable for deleting ID: {record.get('candidate_id')}")
            return

        cand_id_int = int(record["candidate_id"])
        keeper_id_int = int(record["keeper_id"])

        # Fetch candidate message with fetch-error safety fix
        try:
            cand_msgs = await tg_client.get_messages(
                channel_entity, ids=[cand_id_int]
            )
        except Exception as fetch_err:
            # Network or API error — NOT confirmed deletion
            curr_record["status"] = "failed"
            curr_record["last_error"] = str(fetch_err)
            save_pending(records)
            _log("DEDUPE", f"Fetch error for ID {record['candidate_id']}: {fetch_err} — retaining track")
            return  # DO NOT remove from index, DO NOT mark completed

        cand_msg = cand_msgs[0] if cand_msgs else None
        if cand_msg is None or getattr(cand_msg, "id", None) is None:
            curr_record["status"] = "completed"
            save_pending(records)
            for i, t in enumerate(track_index):
                if str(t.get("id")) == str(record["candidate_id"]):
                    track_index.pop(i)
                    break
            _log("DEDUPE", f"ID {record['candidate_id']} already gone from Telegram")
            return

        # Fetch keeper message with fetch-error safety fix
        try:
            keeper_msgs = await tg_client.get_messages(
                channel_entity, ids=[keeper_id_int]
            )
        except Exception as fetch_err:
            curr_record["status"] = "failed"
            curr_record["last_error"] = str(fetch_err)
            save_pending(records)
            _log("DEDUPE", f"Keeper fetch error — cancelling deletion of {record['candidate_id']}: {fetch_err}")
            return

        keeper_msg = keeper_msgs[0] if keeper_msgs else None
        if keeper_msg is None or getattr(keeper_msg, "id", None) is None:
            curr_record["status"] = "failed"
            save_pending(records)
            _log("DEDUPE", f"Keeper ID {keeper_id_int} no longer found, cancelling deletion of ID: {record['candidate_id']}")
            return

        # Re-verify is_duplicate on fresh metadata
        from telegram_client import telegram_manager
        fresh_cand = telegram_manager._parse_track_message(cand_msg)
        fresh_keeper = telegram_manager._parse_track_message(keeper_msg)

        if not fresh_cand or not fresh_keeper:
            curr_record["status"] = "cancelled"
            save_pending(records)
            _log("DEDUPE", f"Failed to re-parse fresh track metadata for ID {record['candidate_id']}")
            return

        if not is_duplicate(fresh_cand, fresh_keeper):
            curr_record["status"] = "cancelled"
            save_pending(records)
            _log("DEDUPE", f"Fresh metadata indicated not duplicate for ID: {record['candidate_id']}")
            return

        candidate_title = fresh_cand.get("title", f"ID {record['candidate_id']}")

        # Attempt deletion
        try:
            del_result = await tg_client.delete_messages(channel_entity, [cand_id_int], revoke=True)
            success = bool(del_result)
        except Exception as exc:
            _log("DEDUPE", f"Telegram API exception deleting message ID {record['candidate_id']}: {exc}")
            success = False

        if success:
            for i, t in enumerate(track_index):
                if str(t.get("id")) == str(record["candidate_id"]):
                    track_index.pop(i)
                    break
            curr_record["status"] = "completed"
            _log("DEDUPE", f"Deleted: {candidate_title} (ID: {record['candidate_id']})")
        else:
            curr_record["status"] = "failed"
            _log("DEDUPE", f"Failed to delete ID: {record['candidate_id']}")

        save_pending(records)


async def keep_track(candidate_id: str) -> bool:
    """Cancel a pending deletion and mark record as cancelled."""
    async with dedupe_lock:
        records = load_pending()
        found = False
        for r in records:
            if str(r.get("candidate_id")) == str(candidate_id) and r.get("status") == "pending":
                r["status"] = "cancelled"
                found = True

        if found:
            save_pending(records)
            _log("DEDUPE", f"Kept track ID: {candidate_id} (cancelled deletion)")
            return True

        return False


async def recover_pending(
    tg_client: Any,
    channel_entity: Any,
    track_index: list[dict[str, Any]],
) -> None:
    """Recover and resume pending deletions on startup."""
    records = load_pending()
    pending = [r for r in records if r.get("status") == "pending"]
    if not pending:
        return

    now = datetime.now(timezone.utc)
    for record in pending:
        try:
            delete_after_dt = datetime.fromisoformat(record["delete_after"])
            if delete_after_dt.tzinfo is None:
                delete_after_dt = delete_after_dt.replace(tzinfo=timezone.utc)
        except Exception:
            delete_after_dt = now

        if delete_after_dt > now:
            remaining = (delete_after_dt - now).total_seconds()
            _log("DEDUPE", f"Recovered pending deletion ID: {record['candidate_id']}, {remaining:.0f}s remaining")
        else:
            _log("DEDUPE", f"Recovered expired pending deletion ID: {record['candidate_id']}, executing now")

        asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))


async def check_new_track(
    new_track: dict[str, Any],
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> Optional[dict[str, Any]]:
    """Translate of processTrackUpload() from index.js with 5m grace period."""
    if not new_track or not new_track.get("id"):
        return None

    if new_track.get("keep"):
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
            _log("DUPLICATE", f"{new_track.get('title')} -> Upgraded quality (Replacing ID: {existing_dup.get('id')})")
            cand_id = str(existing_dup.get("id"))
            keeper_id = str(new_track.get("id"))

            now = datetime.now(timezone.utc)
            delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
            delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

            record: dict[str, Any] = {
                "pending_id": str(uuid.uuid4()),
                "candidate_id": cand_id,
                "keeper_id": keeper_id,
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
            save_pending(pending_records)
            asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

            try:
                idx = track_index.index(existing_dup)
                track_index[idx] = new_track
            except ValueError:
                track_index.insert(0, new_track)
            return new_track
        else:
            is_lower = score_new < score_old
            reason = "lower_quality" if is_lower else "identical"
            cand_id = str(new_track.get("id"))
            keeper_id = str(existing_dup.get("id"))

            _log("DUPLICATE", f"{new_track.get('title')} (ID: {cand_id}) -> {reason} duplicate of ID: {keeper_id}, deleting in 5m")

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
            save_pending(pending_records)
            asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))
            return None
