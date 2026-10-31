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

ARTIST_STOP_WORDS = {
    "the", "and", "feat", "ft", "featuring", "various", "artist", "artists",
    "unknown", "music", "official", "of", "a", "an", "in", "on", "by", "for", "with"
}


def _log(type_: str, msg: str) -> None:
    """Format and print a timestamped log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {type_:^8} | {msg}")


def normalize_title(t: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace.

    Do NOT remove version terms before extracting them.
    """
    if not t:
        return ""
    t = t.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def get_core_title(s: str) -> str:
    """Extract stripped core title removing brackets, features, and punctuation."""
    if not s:
        return ""
    clean = s.lower()
    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", clean)
    clean = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\s+.*$", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+[-–—]+\s+.*$", " ", clean)
    clean = re.sub(r"[^\w\s]", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def normalize_artist(a: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace.

    Return "" if artist is "unknown artist" or empty.
    """
    if not a or a.strip().lower() == "unknown artist":
        return ""
    a = a.lower()
    a = re.sub(r"[^\w\s]", "", a)
    a = re.sub(r"\s+", " ", a)
    clean = a.strip()
    if clean == "unknown artist":
        return ""
    return clean


normalizeArtist = normalize_artist


def extract_version_profile(title: str | dict[str, Any]) -> dict[str, str]:
    """Extract mix type, version type, and version label from title or track dictionary.

    Returns:
    {
      "mix_type": "atmos" | "stereo" | "unknown",
      "version_type": "original" | "remix" | "live" | "acoustic" |
                      "instrumental" | "slowed" | "reverb" |
                      "jhankar" | "unplugged" | "cover" | "unknown",
      "version_label": "<original matched text or empty>"
    }
    """
    if isinstance(title, dict):
        raw_title = str(title.get("title") or "")
        is_atmos_meta = bool(
            title.get("isAtmos")
            or (title.get("format") or "").lower() in {"eac3-joc", "eac3", "ec3"}
            or title.get("quality") == "Dolby Atmos"
        )
    else:
        raw_title = str(title or "")
        is_atmos_meta = False

    clean = raw_title.lower()

    # 1. Mix type detection
    mix_type = "unknown"
    if is_atmos_meta or re.search(r"\b(atmos|dolby\s*atmos|spatial\s*audio|eac3[-\s]?joc|eac3|ec3)\b", clean):
        mix_type = "atmos"
    elif re.search(r"\bstereo\b", clean):
        mix_type = "stereo"

    # 2. Extract version candidates from brackets and suffixes
    version_type = "unknown"
    version_label = ""

    patterns = [
        r"[\(\[\{]([^\)\]\}]+)[\)\]\}]",
        r"[-–—]\s*(.+)$",
    ]
    candidates: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, raw_title):
            extracted = m.group(1).strip()
            if extracted:
                candidates.append(extracted)

    version_keywords: list[tuple[str, str]] = [
        ("jhankar", r"\bjhankar\b"),
        ("unplugged", r"\bunplugged\b"),
        ("acoustic", r"\bacoustic\b"),
        ("instrumental", r"\b(?:instrumental|karaoke)\b"),
        ("slowed", r"\bslowed\b"),
        ("reverb", r"\breverb\b"),
        ("live", r"\b(?:live|concert|in\s+concert)\b"),
        ("cover", r"\bcover\b"),
        ("remix", r"\b(?:remix|club\s*mix|extended\s*mix|radio\s*edit|edit|revisited)\b"),
        ("original", r"\b(?:original|original\s*mix)\b"),
    ]

    for cand in candidates:
        cand_lower = cand.lower()
        if mix_type == "unknown":
            if re.search(r"\b(atmos|dolby\s*atmos)\b", cand_lower):
                mix_type = "atmos"
            elif re.search(r"\bstereo\b", cand_lower):
                mix_type = "stereo"

        for vtype, regex in version_keywords:
            if re.search(regex, cand_lower):
                version_type = vtype
                version_label = cand
                break
        if version_type != "unknown":
            break

    return {
        "mix_type": mix_type,
        "version_type": version_type,
        "version_label": version_label,
    }


def artist_match(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Determine artist match status.

    Returns one of: "MATCH" | "CONFLICT" | "UNKNOWN"
    UNKNOWN: either artist is empty/unknown
    CONFLICT: both artists known but share NO common word >= 3 chars
    MATCH: both artists known and share at least one common word >= 3 chars
    """
    art_a = normalize_artist(a.get("artist", ""))
    art_b = normalize_artist(b.get("artist", ""))
    if not art_a or not art_b:
        return "UNKNOWN"

    words_a = [w for w in art_a.split() if len(w) >= 3 and w not in ARTIST_STOP_WORDS]
    words_b = [w for w in art_b.split() if len(w) >= 3 and w not in ARTIST_STOP_WORDS]

    if not words_a or not words_b:
        return "UNKNOWN"

    # Split into words and check if ANY word from artist_a appears in artist_b string
    has_common = any(w in art_b for w in words_a) or any(w in art_a for w in words_b)
    if has_common:
        return "MATCH"
    return "CONFLICT"


def compare_tracks(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compare two tracks and return a comprehensive safety decision object."""
    matched_by: list[str] = []
    confidence = 0.0

    # 1. Same message ID -> safe_to_auto_delete=False
    if str(a.get("id")) == str(b.get("id")):
        return {
            "decision": "same_message",
            "confidence": 1.0,
            "matched_by": ["id"],
            "reason": "Identical message ID",
            "safe_to_auto_delete": False,
        }

    # 2. Mix/version protection:
    prof_a = extract_version_profile(a)
    prof_b = extract_version_profile(b)

    is_atmos_a = bool(a.get("isAtmos") or a.get("format") == "eac3-joc" or prof_a["mix_type"] == "atmos")
    is_atmos_b = bool(b.get("isAtmos") or b.get("format") == "eac3-joc" or prof_b["mix_type"] == "atmos")

    if is_atmos_a != is_atmos_b or (
        prof_a["mix_type"] != "unknown"
        and prof_b["mix_type"] != "unknown"
        and prof_a["mix_type"] != prof_b["mix_type"]
    ):
        return {
            "decision": "different_mix",
            "confidence": 0.0,
            "matched_by": [],
            "reason": f"Different audio mixes (Atmos vs Stereo or {prof_a['mix_type']} vs {prof_b['mix_type']})",
            "safe_to_auto_delete": False,
        }

    if (
        prof_a["version_type"] != "unknown"
        and prof_b["version_type"] != "unknown"
        and prof_a["version_type"] != prof_b["version_type"]
    ):
        return {
            "decision": "different_version",
            "confidence": 0.0,
            "matched_by": [],
            "reason": f"Different versions ({prof_a['version_type']} vs {prof_b['version_type']})",
            "safe_to_auto_delete": False,
        }

    # 3. ISRC check: +0.40 bonus (not required)
    isrc_a = str(a.get("isrc") or "").strip().upper()
    isrc_b = str(b.get("isrc") or "").strip().upper()
    if isrc_a and isrc_b and isrc_a == isrc_b:
        # Compatible version profiles
        if (
            prof_a["version_type"] == prof_b["version_type"]
            or prof_a["version_type"] == "unknown"
            or prof_b["version_type"] == "unknown"
        ):
            confidence += 0.40
            matched_by.append("isrc")

    # 4. Artist check: Exact artist token match: +0.30
    result_art = artist_match(a, b)
    if result_art == "CONFLICT":
        return {
            "decision": "different_artist",
            "confidence": 0.0,
            "matched_by": matched_by,
            "reason": f"Artist conflict ({a.get('artist')} vs {b.get('artist')})",
            "safe_to_auto_delete": False,
        }
    elif result_art == "MATCH":
        confidence += 0.30
        matched_by.append("artist")

    # 5. Title check:
    # Exact title match: +0.35
    # Core title match: +0.20
    norm_a = normalize_title(a.get("title", ""))
    norm_b = normalize_title(b.get("title", ""))
    core_a = get_core_title(a.get("title", ""))
    core_b = get_core_title(b.get("title", ""))

    if norm_a and norm_b and norm_a == norm_b:
        confidence += 0.35
        matched_by.append("exact_title")
    elif core_a and core_b and core_a == core_b:
        confidence += 0.20
        matched_by.append("core_title")

    # 6. Duration check:
    # Duration <= 2s: +0.15
    # Duration 3-5s: +0.05
    # Duration > 5s: -0.30
    dur_a = a.get("duration")
    dur_b = b.get("duration")
    if dur_a is not None and dur_b is not None:
        try:
            diff = abs(float(dur_a) - float(dur_b))
            if diff <= 2:
                confidence += 0.15
                matched_by.append("duration")
            elif diff <= 5:
                confidence += 0.05
                matched_by.append("duration")
            else:
                confidence -= 0.30
        except (ValueError, TypeError):
            pass

    # Artist unknown constraint: confidence stays low, can never reach 0.95
    if result_art == "UNKNOWN":
        confidence = min(confidence, 0.80)

    confidence = round(max(0.0, min(1.0, confidence)), 2)

    # 7. Final decision:
    # Threshold for safe_to_auto_delete: 0.95
    if confidence >= 0.95:
        decision = "exact_duplicate"
        safe_to_auto_delete = True
        reason = f"High confidence duplicate matched by {', '.join(matched_by)}"
    elif confidence >= 0.80:
        decision = "probable_duplicate"
        safe_to_auto_delete = False
        reason = f"Probable duplicate matched by {', '.join(matched_by)} (audit only)"
    elif confidence >= 0.60:
        decision = "possible_duplicate"
        safe_to_auto_delete = False
        reason = f"Possible duplicate matched by {', '.join(matched_by)}"
    else:
        decision = "different_recording"
        safe_to_auto_delete = False
        reason = f"Different recordings (confidence: {confidence:.2f})"

    return {
        "decision": decision,
        "confidence": confidence,
        "matched_by": matched_by,
        "reason": reason,
        "safe_to_auto_delete": safe_to_auto_delete,
    }


def is_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compatibility wrapper checking if two tracks are considered duplicates."""
    res = compare_tracks(a, b)
    return bool(res.get("safe_to_auto_delete") or res.get("decision") in {"exact_duplicate", "probable_duplicate"})


isDuplicate = is_duplicate


def get_quality_score(track: dict[str, Any]) -> int:
    """Calculate audio quality score:
    eac3/atmos = 100
    flac/alac/wav + bitDepth > 16 or sampleRate > 44100 = 90
    flac/alac/wav = 80
    mp3 bitrate >= 320 = 60
    mp3 bitrate >= 256 = 50
    mp3 bitrate >= 128 = 40
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
        bd = int(track.get("bitDepth") or 16)
        sr = int(track.get("sampleRate") or 44100)
        if bd > 16 or sr > 44100:
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


def choose_keeper(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Determine keeper and deletion candidate between two duplicate tracks.

    Returns (keeper, candidate_to_delete)
    Comparison order:
    1. Higher quality score wins
    2. Tie -> larger file size wins
    3. Tie -> older message (lower id) wins — prefer existing copy
    Never delete based on quality score alone if difference < 10 points.
    """
    score_a = get_quality_score(a)
    score_b = get_quality_score(b)
    diff = score_a - score_b

    # 1. Higher quality score wins (only if diff >= 10 points)
    if diff >= 10:
        return a, b
    elif diff <= -10:
        return b, a

    # 2. Tie / diff < 10 -> larger file size wins
    size_a = int(a.get("fileSize") or 0)
    size_b = int(b.get("fileSize") or 0)
    if size_a > size_b:
        return a, b
    elif size_b > size_a:
        return b, a

    # 3. Tie -> older message (lower id) wins
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
    """Scan all tracks and find duplicate candidates using compare_tracks().

    NO deletion. NO scheduling. Report only.
    """
    candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Index tracks into buckets by exact title, core title, and ISRC for O(candidates) speed
    buckets: dict[str, list[dict[str, Any]]] = {}
    for track in track_index:
        norm = normalize_title(track.get("title", ""))
        core = get_core_title(track.get("title", ""))
        isrc = str(track.get("isrc") or "").strip().upper()

        if norm:
            buckets.setdefault(f"norm:{norm}", []).append(track)
        if core and core != norm:
            buckets.setdefault(f"core:{core}", []).append(track)
        if isrc:
            buckets.setdefault(f"isrc:{isrc}", []).append(track)

    for bucket_tracks in buckets.values():
        if len(bucket_tracks) < 2:
            continue
        n = len(bucket_tracks)
        for i in range(n):
            for j in range(i + 1, n):
                t1 = bucket_tracks[i]
                t2 = bucket_tracks[j]
                id1 = str(t1.get("id"))
                id2 = str(t2.get("id"))
                if id1 == id2:
                    continue

                pair_key = (min(id1, id2), max(id1, id2))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                decision = compare_tracks(t1, t2)
                if decision.get("decision") != "different_recording":
                    keeper, candidate = choose_keeper(t1, t2)
                    candidates.append({
                        "candidate": candidate,
                        "keeper": keeper,
                        "decision": decision,
                    })

    _log("DEDUPE", f"Audit complete: {len(candidates)} duplicate candidates found")
    return candidates


async def deduplicate_library(
    track_index: list[dict[str, Any]],
    tg_client: Any,
    channel_entity: Any,
) -> dict[str, int]:
    """Scan library, audit candidates, and schedule safe auto-deletions under dedupe_lock."""
    async with dedupe_lock:
        audit_results = await audit_library(track_index)
        pending_records = load_pending()
        existing_candidate_ids = {
            str(r.get("candidate_id")) for r in pending_records if r.get("status") == "pending"
        }

        queued_count = 0
        safe_auto_count = 0

        for item in audit_results:
            decision = item.get("decision", {})
            if decision.get("safe_to_auto_delete"):
                safe_auto_count += 1
                candidate = item["candidate"]
                keeper = item["keeper"]
                cand_id = str(candidate.get("id"))

                if cand_id in existing_candidate_ids:
                    continue

                now = datetime.now(timezone.utc)
                delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
                delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

                record: dict[str, Any] = {
                    "pending_id": str(uuid.uuid4()),
                    "candidate_id": cand_id,
                    "keeper_id": str(keeper.get("id")),
                    "confidence": decision.get("confidence", 0.0),
                    "reason": decision.get("reason", ""),
                    "matched_by": decision.get("matched_by", []),
                    "created_at": now.isoformat(),
                    "delete_after": delete_after_iso,
                    "status": "pending",
                    "notice_message_id": None,
                }

                pending_records.append(record)
                existing_candidate_ids.add(cand_id)
                queued_count += 1

                cand_title = candidate.get("title", f"ID {cand_id}")
                _log("DEDUPE", f"Queued: {cand_title} (ID: {cand_id}) -> duplicate of {keeper.get('id')}, deleting in 5m")
                asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

        save_pending(pending_records)
        return {
            "audited": len(audit_results),
            "queued": queued_count,
            "safe_auto": safe_auto_count,
        }


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

        # FIX 2: Re-fetch candidate message from Telegram with separate try/except
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

        # Only reach here if fetch succeeded
        cand_msg = cand_msgs[0] if cand_msgs else None
        if cand_msg is None or getattr(cand_msg, "id", None) is None:
            # Confirmed: message does not exist on Telegram
            curr_record["status"] = "completed"
            save_pending(records)
            for i, t in enumerate(track_index):
                if str(t.get("id")) == str(record["candidate_id"]):
                    track_index.pop(i)
                    break
            _log("DEDUPE", f"ID {record['candidate_id']} already gone from Telegram")
            return

        # FIX 2: Re-fetch keeper message with separate try/except
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

        # Re-run compare_tracks on fresh metadata
        from telegram_client import telegram_manager
        fresh_cand = telegram_manager._parse_track_message(cand_msg)
        fresh_keeper = telegram_manager._parse_track_message(keeper_msg)

        if not fresh_cand or not fresh_keeper:
            curr_record["status"] = "cancelled"
            save_pending(records)
            _log("DEDUPE", f"Failed to re-parse fresh track metadata for ID {record['candidate_id']}")
            return

        check_res = compare_tracks(fresh_cand, fresh_keeper)
        if not check_res.get("safe_to_auto_delete"):
            curr_record["status"] = "cancelled"
            save_pending(records)
            _log("DEDUPE", f"Fresh metadata indicated unsafe auto-deletion for ID: {record['candidate_id']} ({check_res.get('reason')})")
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
    """Real-time duplicate detection when a new message arrives in channel."""
    if new_track.get("keep"):
        track_index.append(new_track)
        return new_track

    async with dedupe_lock:
        for existing in list(track_index):
            result = compare_tracks(existing, new_track)
            if result.get("safe_to_auto_delete"):
                keeper, candidate = choose_keeper(existing, new_track)
                cand_id = str(candidate.get("id"))
                keeper_id = str(keeper.get("id"))

                now = datetime.now(timezone.utc)
                delete_after_dt = now.timestamp() + GRACE_PERIOD_SECONDS
                delete_after_iso = datetime.fromtimestamp(delete_after_dt, tz=timezone.utc).isoformat()

                record: dict[str, Any] = {
                    "pending_id": str(uuid.uuid4()),
                    "candidate_id": cand_id,
                    "keeper_id": keeper_id,
                    "confidence": result.get("confidence", 0.0),
                    "reason": result.get("reason", ""),
                    "matched_by": result.get("matched_by", []),
                    "created_at": now.isoformat(),
                    "delete_after": delete_after_iso,
                    "status": "pending",
                    "notice_message_id": None,
                }

                pending_records = load_pending()
                pending_records.append(record)
                save_pending(pending_records)

                cand_title = candidate.get("title", f"ID {cand_id}")
                _log("DEDUPE", f"Queued: {cand_title} (ID: {cand_id}) -> duplicate of {keeper_id}, deleting in 5m")
                asyncio.create_task(_execute_deletion(record, tg_client, channel_entity, track_index))

                if str(candidate.get("id")) == str(new_track.get("id")):
                    # Candidate to delete is the new track: do NOT add to index
                    return None
                else:
                    # Keeper is new track: replace existing in track_index
                    try:
                        idx = track_index.index(existing)
                        track_index[idx] = new_track
                    except ValueError:
                        track_index.append(new_track)
                    return new_track

        # No duplicate found
        track_index.append(new_track)
        return new_track
