import asyncio
import gzip
import io
import os
import re
from typing import Any, Optional
import orjson
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    MessageMediaDocument,
)

from cache import SimpleLRU
from config import config

AUDIO_EXTENSIONS = {"flac", "mp3", "m4a", "aac", "wav", "ogg", "opus", "alac", "ec3", "eac3"}
EXT_TO_FORMAT = {
    "flac": "flac",
    "mp3": "mp3",
    "m4a": "m4a",
    "aac": "aac",
    "wav": "wav",
    "ogg": "ogg",
    "opus": "opus",
    "alac": "alac",
    "ec3": "eac3",
    "eac3": "eac3",
}

ATMOS_REGEX = re.compile(
    r"\b(atmos|dolby\s*atmos|eac3[-\s]?joc|eac3|ec3|e-ac-3|spatial\s*audio)\b|\[atmos\]|\(atmos\)",
    re.IGNORECASE,
)

COMPOSER_DUOS = [
    (re.compile(r"vishal[-\s\u2013\u2014]+shekhar", re.IGNORECASE), "Vishal & Shekhar"),
    (re.compile(r"sachin[-\s\u2013\u2014]+jigar", re.IGNORECASE), "Sachin & Jigar"),
    (re.compile(r"salim[-\s\u2013\u2014]+sulaiman", re.IGNORECASE), "Salim & Sulaiman"),
    (re.compile(r"shankar[-\s\u2013\u2014]+ehsaan[-\s\u2013\u2014]+loy", re.IGNORECASE), "Shankar & Ehsaan & Loy"),
    (re.compile(r"ajay[-\s\u2013\u2014]+atul", re.IGNORECASE), "Ajay & Atul"),
    (re.compile(r"sajid[-\s\u2013\u2014]+wajid", re.IGNORECASE), "Sajid & Wajid"),
    (re.compile(r"nadeem[-\s\u2013\u2014]+shravan", re.IGNORECASE), "Nadeem & Shravan"),
    (re.compile(r"jatin[-\s\u2013\u2014]+lalit", re.IGNORECASE), "Jatin & Lalit"),
    (re.compile(r"anand[-\s\u2013\u2014]+milind", re.IGNORECASE), "Anand & Milind"),
    (re.compile(r"laxmikant[-\s\u2013\u2014]+pyarelal", re.IGNORECASE), "Laxmikant & Pyarelal"),
    (re.compile(r"kalyanji[-\s\u2013\u2014]+anandji", re.IGNORECASE), "Kalyanji & Anandji"),
    (re.compile(r"shiv[-\s\u2013\u2014]+hari", re.IGNORECASE), "Shiv & Hari"),
    (re.compile(r"raam[-\s\u2013\u2014]+laxman", re.IGNORECASE), "Raam & Laxman"),
]


def format_artist_for_client(artist_str: Optional[str]) -> str:
    """Format artist names, expanding known composer duos and cleaning delimiters."""
    if not artist_str or artist_str.strip().lower() == "unknown artist":
        return "Unknown Artist"
    formatted = artist_str
    for pattern, replacement in COMPOSER_DUOS:
        formatted = pattern.sub(replacement, formatted)
    formatted = re.sub(r"\s+[-\u2013\u2014]+\s+", ", ", formatted)
    return formatted.strip()


def is_audio_document(doc: Document) -> bool:
    """Determine whether a Telegram document is an audio track."""
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    file_name = ""
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename):
            file_name = attr.file_name or ""
            break

    ext = file_name.split(".")[-1].lower() if "." in file_name else ""

    # Reject video containers
    if mime.startswith("video/") or ext in {"mp4", "mkv", "avi", "mov", "webm", "flv"}:
        return False

    is_audio_mime = mime.startswith("audio/") or mime in {"application/ogg", "application/x-flac"}
    is_audio_ext = ext in AUDIO_EXTENSIONS
    is_audio_attr = any(isinstance(a, DocumentAttributeAudio) for a in (getattr(doc, "attributes", []) or []))

    return bool(is_audio_mime or is_audio_ext or is_audio_attr)


class TelegramManager:
    """Manages the single user MTProto TelegramClient, channel resolution, track indexing, and media cache."""

    def __init__(self) -> None:
        self.client: Optional[TelegramClient] = None
        self.channel_entity: Any = None
        self.media_cache = SimpleLRU(500)
        self.track_index: list[dict[str, Any]] = []
        self.is_indexing = False
        self._cache_file_gz = os.path.join(os.path.dirname(__file__), "tracks_cache.json.gz")
        self._cache_file_tmp = self._cache_file_gz + ".tmp"

    async def connect(self) -> None:
        """Connect and verify authentication of the primary user client."""
        session_str = config.TELEGRAM_SESSION_STRING.strip()
        self.client = TelegramClient(
            StringSession(session_str),
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
            connection_retries=10,
            auto_reconnect=True,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram session string is not authorized. Please run python login.py again."
            )

    async def resolve_channel(self, ref: str | int) -> Any:
        """Resolve channel entity using multiple strategies: direct, @username, numeric ID, or dialog search."""
        if not self.client:
            raise RuntimeError("Telegram client is not connected.")

        raw_str = str(ref).strip().strip('"').strip("'")
        # Handle t.me link variations
        tme_match = re.search(r"t\.me/(?:c/)?([a-zA-Z0-9_+-]+)", raw_str, re.IGNORECASE)
        target = tme_match.group(1) if tme_match else raw_str

        # 1. Try direct entity retrieval
        try:
            entity = await self.client.get_entity(target)
            self.channel_entity = entity
            return entity
        except Exception:
            pass

        # 2. Try with @ prefix or without @ prefix
        if not target.startswith("@") and not target.replace("-", "").isdigit():
            try:
                entity = await self.client.get_entity(f"@{target}")
                self.channel_entity = entity
                return entity
            except Exception:
                pass
        elif target.startswith("@"):
            try:
                entity = await self.client.get_entity(target[1:])
                self.channel_entity = entity
                return entity
            except Exception:
                pass

        # 3. Try numeric ID variations
        numeric_id_str = target.replace("-100", "").replace("-", "")
        if numeric_id_str.isdigit():
            num = int(numeric_id_str)
            for variation in (num, -num, int(f"-100{num}")):
                try:
                    entity = await self.client.get_entity(variation)
                    self.channel_entity = entity
                    return entity
                except Exception:
                    pass

        # 4. Search user's dialogs
        try:
            clean_lower = target.lower()
            async for dialog in self.client.iter_dialogs(limit=500):
                ent = dialog.entity
                if not ent:
                    continue
                ent_id = str(getattr(ent, "id", ""))
                username = (getattr(ent, "username", "") or "").lower()
                title = (getattr(dialog, "name", "") or "").lower()

                if (
                    ent_id == target
                    or f"-100{ent_id}" == target
                    or ent_id == numeric_id_str
                    or (username and (username == clean_lower or f"@{username}" == clean_lower))
                    or title == clean_lower
                ):
                    self.channel_entity = ent
                    return ent
        except Exception:
            pass

        raise ValueError(f"Could not resolve Telegram channel: {ref}")

    def load_cache(self) -> bool:
        """Load track index from tracks_cache.json.gz if available."""
        if not os.path.exists(self._cache_file_gz):
            return False
        try:
            with gzip.open(self._cache_file_gz, "rb") as f:
                data = f.read()
            raw_tracks = orjson.loads(data)
            if isinstance(raw_tracks, list):
                self.track_index = raw_tracks
                return True
        except Exception as exc:
            print(f"[Cache] Failed to load {self._cache_file_gz}: {exc}")
        return False

    def save_cache(self) -> None:
        """Atomically persist track index to tracks_cache.json.gz."""
        try:
            compressed = gzip.compress(orjson.dumps(self.track_index))
            with open(self._cache_file_tmp, "wb") as f:
                f.write(compressed)
            os.replace(self._cache_file_tmp, self._cache_file_gz)
        except Exception as exc:
            print(f"[Cache] Failed to save {self._cache_file_gz}: {exc}")

    def _parse_track_message(self, msg: Any) -> Optional[dict[str, Any]]:
        """Extract audio metadata from a Telegram Message."""
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return None
        doc = getattr(media, "document", None)
        if not doc or not is_audio_document(doc):
            return None

        attrs = getattr(doc, "attributes", []) or []
        audio_attr: Optional[DocumentAttributeAudio] = next(
            (a for a in attrs if isinstance(a, DocumentAttributeAudio)), None
        )
        filename_attr: Optional[DocumentAttributeFilename] = next(
            (a for a in attrs if isinstance(a, DocumentAttributeFilename)), None
        )

        file_name = filename_attr.file_name if filename_attr and filename_attr.file_name else f"track_{msg.id}"
        ext = file_name.split(".")[-1].lower() if "." in file_name else ""
        fallback_title = re.sub(r"\.[^.]+$", "", file_name)

        title = audio_attr.title.strip() if audio_attr and audio_attr.title else fallback_title
        artist = audio_attr.performer.strip() if audio_attr and audio_attr.performer else "Unknown Artist"
        duration = round(audio_attr.duration) if audio_attr and audio_attr.duration else None

        msg_text = getattr(msg, "message", "") or ""
        mime = getattr(doc, "mime_type", "") or "audio/mpeg"
        file_size = int(getattr(doc, "size", 0) or 0)
        has_artwork = bool(getattr(doc, "thumbs", None))

        is_atmos = bool(
            ATMOS_REGEX.search(file_name)
            or ATMOS_REGEX.search(msg_text)
            or ATMOS_REGEX.search(title)
            or ext in {"ec3", "eac3"}
            or ATMOS_REGEX.search(mime)
        )

        format_name = EXT_TO_FORMAT.get(ext, ext or "mp3")
        sample_rate = 48000 if is_atmos else None
        bit_depth = 16 if is_atmos else None

        if is_atmos:
            format_name = "eac3-joc"

        # Quality text calculation
        if is_atmos:
            quality_text = "Dolby Atmos"
        elif format_name in {"flac", "alac", "wav"}:
            quality_text = f"16-bit / 44.1kHz {format_name.upper()} Lossless"
        else:
            raw_kbps = 320
            if file_size and duration and duration > 0:
                raw_kbps = round((file_size * 8) / (duration * 1000))
                if format_name == "mp3" and raw_kbps > 320:
                    raw_kbps = 320
            quality_text = f"{format_name.upper()} ({raw_kbps}kbps)"

        return {
            "id": str(msg.id),
            "title": title or fallback_title,
            "artist": format_artist_for_client(artist),
            "album": "",
            "duration": duration,
            "format": format_name,
            "bitDepth": bit_depth,
            "sampleRate": sample_rate,
            "fileSize": file_size,
            "mimeType": "audio/mp4" if is_atmos else mime,
            "isAtmos": is_atmos,
            "fileName": file_name,
            "quality": quality_text,
            "hasArtwork": has_artwork,
            "isrc": None,
        }

    async def index_tracks(self) -> list[dict[str, Any]]:
        """Scan channel messages in chronological order, extracting audio tracks and updating the cache."""
        if self.is_indexing:
            return self.track_index
        if not self.client or not self.channel_entity:
            return self.track_index

        self.is_indexing = True
        try:
            print("[Indexer] Starting channel audio indexing...")
            new_index: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            existing_map = {str(t["id"]): t for t in self.track_index}
            batch_count = 0

            async for msg in self.client.iter_messages(self.channel_entity, reverse=True):
                msg_id_str = str(msg.id)
                if msg_id_str in seen_ids:
                    continue
                seen_ids.add(msg_id_str)

                # Cache media in user media cache
                if getattr(msg, "media", None):
                    await self.media_cache.set(msg_id_str, msg.media)

                # Reuse existing parsed metadata when available
                if msg_id_str in existing_map:
                    new_index.append(existing_map[msg_id_str])
                else:
                    parsed = self._parse_track_message(msg)
                    if parsed:
                        new_index.append(parsed)

                batch_count += 1
                if batch_count % 50 == 0:
                    self.track_index = new_index
                    self.save_cache()

            self.track_index = new_index
            self.save_cache()
            print(f"[Indexer] Indexing complete: {len(self.track_index)} tracks loaded.")
            return self.track_index
        except Exception as exc:
            print(f"[Indexer] Error during track indexing: {exc}")
            return self.track_index
        finally:
            self.is_indexing = False

    async def get_media(self, track_id: str | int, force: bool = False) -> Any:
        """Retrieve the msg.media document for a track, utilizing the LRU cache (500 entries)."""
        key = str(track_id)
        if not force and await self.media_cache.has(key):
            return await self.media_cache.get(key)

        if not self.client or not self.channel_entity:
            return None

        try:
            msgs = await self.client.get_messages(self.channel_entity, ids=[int(key)])
            if msgs and msgs[0] and getattr(msgs[0], "media", None):
                media = msgs[0].media
                await self.media_cache.set(key, media)
                return media
        except Exception as exc:
            print(f"[TelegramClient] Error fetching media for track {track_id}: {exc}")

        return None


telegram_manager = TelegramManager()
