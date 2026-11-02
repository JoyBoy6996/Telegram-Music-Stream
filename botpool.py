import asyncio
import re
import time
from typing import Any, Callable, Optional
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.updates import GetDifferenceRequest, GetStateRequest

from cache import SimpleLRU


class BotLease:
    """Represents a leased bot from the pool."""

    def __init__(self, bot: dict[str, Any], release_fn: Optional[Callable[[], None]] = None) -> None:
        self.bot = bot
        self._release_fn = release_fn
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            if self._release_fn:
                try:
                    self._release_fn()
                except Exception:
                    pass

    def __await__(self):
        async def _identity():
            return self
        return _identity().__await__()


class BotPool:
    """Manages a pool of up to 20 Telegram bot clients with startup pre-connection,

    dynamic activation, FloodWait cooldown handling, and active stream protection.
    """

    MAX_BOTS = 20
    STARTUP_BOTS = 10

    def __init__(self, api_id: int, api_hash: str, tokens: list[str]) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        # Support a maximum of 20 configured unique bots
        seen_tokens: set[str] = set()
        cleaned_tokens: list[str] = []
        for t in tokens:
            cleaned = t.strip().strip('"').strip("'")
            if cleaned and cleaned not in seen_tokens:
                seen_tokens.add(cleaned)
                cleaned_tokens.append(cleaned)
        self.tokens = cleaned_tokens[:self.MAX_BOTS]

        self._channel_ref: Any = None
        self._rr_index = 0
        self.bots: list[dict[str, Any]] = []
        self._sessions: dict[str, str] = {}  # session_key -> bot_id
        self._session_activity: dict[str, float] = {}  # session_key -> last_active_timestamp
        self._reaper_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def _resolve_channel_for_bot(self, client: TelegramClient, channel_ref: Any) -> Any:
        """Resolve channel entity specifically for a bot client using multi-strategy fallback.

        Bots cannot use the user's access_hash.
        """
        raw_ref = getattr(channel_ref, "id", channel_ref)
        raw_str = str(raw_ref).strip()
        normalized_id = raw_str.replace("-100", "").replace("-", "")

        username = None
        if isinstance(channel_ref, str):
            clean_str = channel_ref.strip()
            if clean_str.startswith("@"):
                username = clean_str[1:]
            elif re.match(r"^[a-zA-Z][a-zA-Z0-9_]{3,}$", clean_str):
                username = clean_str
        elif hasattr(channel_ref, "username") and channel_ref.username:
            username = channel_ref.username

        # 1. If channel_ref has username: client.get_entity(username)
        if username:
            try:
                entity = await client.get_entity(username)
                if entity:
                    return entity
            except Exception:
                pass

        # 2. client.get_entity(channel_ref)
        try:
            entity = await client.get_entity(channel_ref)
            if entity:
                return entity
        except Exception:
            pass

        # 3. client.invoke(GetDifference(pts=1, date=0, qts=0)) -> search diff.chats
        try:
            diff = await client(GetDifferenceRequest(pts=1, date=0, qts=0))
            chats = getattr(diff, "chats", []) or []
            for c in chats:
                cid = str(getattr(c, "id", ""))
                if cid == normalized_id or f"-100{cid}" == raw_str or cid == raw_str:
                    return c
        except Exception:
            pass

        # 4. state = client.invoke(GetState()) -> GetDifference with pts offset
        try:
            state = await client(GetStateRequest())
            pts = max(1, getattr(state, "pts", 1) - 1000)
            diff = await client(GetDifferenceRequest(pts=pts, date=0, qts=0))
            chats = getattr(diff, "chats", []) or []
            for c in chats:
                cid = str(getattr(c, "id", ""))
                if cid == normalized_id or f"-100{cid}" == raw_str or cid == raw_str:
                    return c
        except Exception:
            pass

        return None

    async def _connect_bot(self, bot_id: str, token: str, token_idx: int) -> Optional[dict[str, Any]]:
        """Connect a single bot client and resolve the target channel without crashing."""
        try:
            client = TelegramClient(
                StringSession(""),
                self.api_id,
                self.api_hash,
                connection_retries=5,
                auto_reconnect=True,
            )
            await client.start(bot_token=token)

            channel = await self._resolve_channel_for_bot(client, self._channel_ref)
            if not channel:
                print(f"[BotPool] Failed to resolve channel for {bot_id}. Skipping.")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None

            print(f"[BotPool] {bot_id} ready and channel resolved.")
            return {
                "id": bot_id,
                "token": token,
                "token_idx": token_idx,
                "client": client,
                "channel": channel,
                "cooldown_until": 0.0,
                "media_cache": SimpleLRU(100),
                "active": True,
                "active_streams": 0,
                "active_stream_ids": set(),
            }
        except Exception as exc:
            print(f"[BotPool] Warning: Failed to initialize {bot_id}: {exc}")
            return None

    def _cleanup_expired_sessions(self, timeout_seconds: float = 30.0) -> None:
        """Auto-release any sessions with no request for timeout_seconds,

        unless the assigned bot is currently serving an active stream.
        """
        now = time.time()
        for sk, last_active in list(self._session_activity.items()):
            if now - last_active > timeout_seconds:
                bot_id = self._sessions.get(sk)
                # Prevent reaper from expiring bots with active streams
                if bot_id and self.is_stream_active(bot_id):
                    self._session_activity[sk] = now  # Keep alive while streaming
                    continue
                self._sessions.pop(sk, None)
                self._session_activity.pop(sk, None)
                if bot_id:
                    print(f"[BotPool] Session {sk} timed out ({timeout_seconds}s), released {bot_id}")

    async def _reaper_loop(self) -> None:
        """Background loop to auto-release idle sessions every 5 seconds."""
        while True:
            try:
                await asyncio.sleep(5)
                async with self._lock:
                    self._cleanup_expired_sessions(30.0)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def init(self, channel_ref: Any) -> None:
        """Initialize up to 10 bot clients at startup and keep remainder available for dynamic activation."""
        self._channel_ref = channel_ref

        # Disconnect any previously active bots that do not have active streams
        for b in self.bots:
            if self.is_stream_active(b["id"]):
                print(f"[BotPool] Bot {b['id']} has active stream; deferring disconnect.")
                continue
            try:
                client = b.get("client")
                if client and client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        self.bots = [b for b in self.bots if self.is_stream_active(b["id"])]
        self._rr_index = 0
        self._sessions.clear()
        self._session_activity.clear()

        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()

        if not self.tokens or not self.api_id or not self.api_hash:
            return

        # Connect up to 10 bots during application startup with unique 1:1 token allocation
        startup_count = min(self.STARTUP_BOTS, len(self.tokens))
        print(f"[BotPool] Connecting {startup_count} bot(s) at startup (configured: {len(self.tokens)}, max: {self.MAX_BOTS})...")

        for i in range(startup_count):
            bot_id = f"bot{i + 1}"
            token = self.tokens[i]
            bot = await self._connect_bot(bot_id, token, i)
            if bot:
                self.bots.append(bot)

        available_for_dyn = len(self.tokens) - len(self.bots)
        print(f"[BotPool] Startup complete: {len(self.bots)} active bot(s). {max(0, available_for_dyn)} bot(s) available for dynamic activation.")
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _activate_next_bot(self) -> Optional[dict[str, Any]]:
        """Dynamically activate another configured bot when all active bots are busy.

        Guarantees unique bot IDs and strict 1:1 token allocation.
        """
        if len(self.bots) >= self.MAX_BOTS or len(self.bots) >= len(self.tokens):
            return None

        # Find the lowest token index not currently allocated to any connected bot
        allocated_indices = {b.get("token_idx") for b in self.bots if "token_idx" in b}
        next_idx = None
        for i in range(len(self.tokens)):
            if i not in allocated_indices:
                next_idx = i
                break

        if next_idx is None:
            return None

        bot_id = f"bot{next_idx + 1}"
        token = self.tokens[next_idx]

        print(f"[BotPool] All active bots busy. Dynamically activating {bot_id}...")
        bot = await self._connect_bot(bot_id, token, next_idx)
        if bot:
            self.bots.append(bot)
            print(f"[BotPool] Successfully activated {bot_id} dynamically (total active: {len(self.bots)}/{len(self.tokens)}).")
            return bot
        return None

    def _find_idle_bot(self, active_bot_ids: set[str], now: float) -> Optional[dict[str, Any]]:
        """Find the next idle, healthy bot (round-robin) among currently connected bots.

        Never selects bots on cooldown, assigned to sessions, or actively streaming.
        """
        num_bots = len(self.bots)
        if num_bots == 0:
            return None
        start = self._rr_index
        for step in range(num_bots):
            idx = (start + step) % num_bots
            candidate = self.bots[idx]
            if candidate["id"] in active_bot_ids:
                continue
            if candidate.get("cooldown_until", 0.0) > now:
                continue
            if self.is_stream_active(candidate["id"]):
                continue
            self._rr_index = (idx + 1) % num_bots
            return candidate
        return None

    async def acquire_for_session(self, session_key: str) -> Optional[BotLease]:
        """Assign or retrieve dedicated bot for a user session with concurrency lock and dynamic activation."""
        async with self._lock:
            now = time.time()
            self._cleanup_expired_sessions(30.0)

            # 1. If session_key already has an active bot: return same bot if healthy
            if session_key in self._sessions:
                assigned_bot_id = self._sessions[session_key]
                bot = next((b for b in self.bots if b["id"] == assigned_bot_id), None)
                if bot and bot.get("cooldown_until", 0.0) <= now:
                    self._session_activity[session_key] = now
                    return BotLease(bot)
                else:
                    # Bot was removed or is on FloodWait cooldown: release session mapping safely
                    if bot:
                        print(f"[BotPool] Assigned bot {assigned_bot_id} for session {session_key} is on cooldown; reassigning")
                    self._sessions.pop(session_key, None)
                    self._session_activity.pop(session_key, None)

            # 2. Check for an idle healthy bot from already connected bots (round-robin)
            active_bot_ids = set(self._sessions.values())
            candidate = self._find_idle_bot(active_bot_ids, now)
            if candidate:
                self._sessions[session_key] = candidate["id"]
                self._session_activity[session_key] = now
                return BotLease(candidate)

            # 3. All connected bots are busy or on cooldown: activate another bot if under limit
            if len(self.bots) < min(self.MAX_BOTS, len(self.tokens)):
                new_bot = await self._activate_next_bot()
                if new_bot and new_bot.get("cooldown_until", 0.0) <= now:
                    self._sessions[session_key] = new_bot["id"]
                    self._session_activity[session_key] = now
                    return BotLease(new_bot)

        # 4. If all bots are busy, wait safely briefly (up to 1.5s) to see if a session finishes
        for _ in range(3):
            await asyncio.sleep(0.5)
            async with self._lock:
                now = time.time()
                self._cleanup_expired_sessions(30.0)
                active_bot_ids = set(self._sessions.values())
                candidate = self._find_idle_bot(active_bot_ids, now)
                if candidate:
                    self._sessions[session_key] = candidate["id"]
                    self._session_activity[session_key] = now
                    return BotLease(candidate)
                # Try activating if capacity opened
                if len(self.bots) < min(self.MAX_BOTS, len(self.tokens)):
                    new_bot = await self._activate_next_bot()
                    if new_bot and new_bot.get("cooldown_until", 0.0) <= now:
                        self._sessions[session_key] = new_bot["id"]
                        self._session_activity[session_key] = now
                        return BotLease(new_bot)

        # Still no bot available: log and return None safely without crashing
        print(f"[BotPool] All bots busy, user {session_key} waiting")
        return None

    def release_session(self, session_key: str) -> None:
        """Remove session mapping and decrement bot usage."""
        bot_id = self._sessions.pop(session_key, None)
        self._session_activity.pop(session_key, None)
        if bot_id:
            print(f"[BotPool] Released session {session_key} (bot: {bot_id})")

    async def acquire(self) -> Optional[BotLease]:
        """Round-robin acquisition across available bots not on cooldown, with dynamic activation.

        Guarantees that artwork and background requests NEVER use stream-reserved bots.
        """
        async with self._lock:
            now = time.time()
            if not self.bots and len(self.tokens) > 0:
                await self._activate_next_bot()
            if not self.bots:
                return None

            active_bot_ids = set(self._sessions.values())
            # 1. Look for an idle, unreserved bot (not in sessions, not streaming, not on cooldown)
            candidate = self._find_idle_bot(active_bot_ids, now)
            if candidate:
                return BotLease(candidate)

            # 2. If all active bots are reserved/busy, activate another bot if under limit
            if len(self.bots) < min(self.MAX_BOTS, len(self.tokens)):
                new_bot = await self._activate_next_bot()
                if (
                    new_bot
                    and new_bot.get("cooldown_until", 0.0) <= now
                    and not self.is_stream_active(new_bot["id"])
                ):
                    return BotLease(new_bot)

            # 3. Do NOT borrow stream-reserved or actively streaming bots.
            return None

    def mark_stream_start(self, bot_id: str, stream_id: Optional[str] = None) -> str:
        """Register active stream for a bot idempotently to protect it from disconnection."""
        sid = stream_id or f"stream_{time.time_ns()}"
        for b in self.bots:
            if b["id"] == bot_id:
                if "active_stream_ids" not in b or not isinstance(b["active_stream_ids"], set):
                    b["active_stream_ids"] = set()
                b["active_stream_ids"].add(sid)
                b["active_streams"] = len(b["active_stream_ids"])
                break
        return sid

    def mark_stream_end(self, bot_id: str, stream_id: Optional[str] = None) -> None:
        """Unregister active stream for a bot idempotently."""
        for b in self.bots:
            if b["id"] == bot_id:
                if "active_stream_ids" in b and isinstance(b["active_stream_ids"], set):
                    if stream_id:
                        b["active_stream_ids"].discard(stream_id)
                    elif b["active_stream_ids"]:
                        b["active_stream_ids"].pop()
                    b["active_streams"] = len(b["active_stream_ids"])
                else:
                    b["active_streams"] = max(0, b.get("active_streams", 0) - 1)
                break

    def is_stream_active(self, bot_id: str) -> bool:
        """Check if bot is actively serving a stream."""
        for b in self.bots:
            if b["id"] == bot_id:
                if "active_stream_ids" in b and isinstance(b["active_stream_ids"], set):
                    return len(b["active_stream_ids"]) > 0
                return b.get("active_streams", 0) > 0
        return False

    async def get_media(self, bot: dict[str, Any], track_id: str | int, force: bool = False) -> Any:
        """Fetch media document for track using bot client, cached in bot's private LRU."""
        if not bot or not bot.get("client") or not track_id:
            return None

        key = str(track_id)
        cache: SimpleLRU = bot["media_cache"]

        if not force and await cache.has(key):
            return await cache.get(key)

        try:
            msgs = await bot["client"].get_messages(bot["channel"], ids=[int(key)])
            if msgs and msgs[0] and getattr(msgs[0], "media", None):
                media = msgs[0].media
                await cache.set(key, media)
                return media
        except Exception as exc:
            print(f"[BotPool] {bot['id']} error getting media for track {track_id}: {exc}")
            self.report_error(bot, exc)

        return None

    def report_error(self, bot: dict[str, Any], err: Any) -> None:
        """Parse FLOOD_WAIT from error, place bot in cooldown safely, and release affected sessions."""
        if not bot or not err:
            return

        bot_id = bot.get("id") if isinstance(bot, dict) else getattr(bot, "id", None)
        if not bot_id:
            return

        seconds = 0
        if hasattr(err, "seconds") and isinstance(err.seconds, (int, float)) and err.seconds > 0:
            seconds = int(err.seconds)
        else:
            msg = str(getattr(err, "message", "") or err)
            match = re.search(r"FLOOD_WAIT_(\d+)", msg, re.IGNORECASE) or re.search(
                r"wait of (\d+)\s*seconds", msg, re.IGNORECASE
            )
            if match:
                seconds = int(match.group(1))

        if seconds > 0:
            target_cooldown = time.time() + seconds
            if isinstance(bot, dict):
                bot["cooldown_until"] = max(bot.get("cooldown_until", 0.0), target_cooldown)
            for b in self.bots:
                if b["id"] == bot_id:
                    b["cooldown_until"] = max(b.get("cooldown_until", 0.0), target_cooldown)
                    break

            print(f"[BotPool] {bot_id} flood-wait {seconds}s, placed on cooldown")
            # Release any session using this bot safely
            to_remove = [sk for sk, bid in list(self._sessions.items()) if bid == bot_id]
            for sk in to_remove:
                self.release_session(sk)

    async def close(self) -> None:
        """Disconnect idle bot clients while strictly protecting bots with active streams.

        Race-safe and idempotent.
        """
        async with self._lock:
            if self._reaper_task and not self._reaper_task.done():
                self._reaper_task.cancel()
            self._sessions.clear()
            self._session_activity.clear()

            remaining_bots: list[dict[str, Any]] = []
            for b in self.bots:
                # Never disconnect a bot that is currently serving an active stream
                if self.is_stream_active(b["id"]):
                    print(f"[BotPool] Bot {b['id']} has active stream(s); preserving connection.")
                    remaining_bots.append(b)
                    continue
                try:
                    client = b.get("client")
                    if client and client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
            self.bots = remaining_bots
