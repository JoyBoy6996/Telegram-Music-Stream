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
                self._release_fn()

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
        # Support a maximum of 20 configured bots
        self.tokens = [
            t.strip().strip('"').strip("'")
            for t in tokens
            if t.strip().strip('"').strip("'")
        ][:self.MAX_BOTS]
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

    async def _connect_bot(self, bot_id: str, token: str) -> Optional[dict[str, Any]]:
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
                "client": client,
                "channel": channel,
                "cooldown_until": 0.0,
                "media_cache": SimpleLRU(500),
                "active": True,
                "active_streams": 0,
            }
        except Exception as exc:
            print(f"[BotPool] Warning: Failed to initialize {bot_id}: {exc}")
            return None

    def _cleanup_expired_sessions(self, timeout_seconds: float = 30.0) -> None:
        """Auto-release any sessions with no request for timeout_seconds."""
        now = time.time()
        expired = [
            sk for sk, last_active in self._session_activity.items()
            if now - last_active > timeout_seconds
        ]
        for sk in expired:
            bot_id = self._sessions.pop(sk, None)
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
            if b.get("active_streams", 0) > 0:
                print(f"[BotPool] Bot {b['id']} has active stream; deferring disconnect.")
                continue
            try:
                if b.get("client") and b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass

        self.bots = [b for b in self.bots if b.get("active_streams", 0) > 0]
        self._rr_index = 0
        self._sessions.clear()
        self._session_activity.clear()

        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()

        if not self.tokens or not self.api_id or not self.api_hash:
            return

        # Connect up to 10 bots during application startup
        startup_count = min(self.STARTUP_BOTS, len(self.tokens))
        print(f"[BotPool] Connecting {startup_count} bot(s) at startup (configured: {len(self.tokens)}, max: {self.MAX_BOTS})...")

        for i in range(startup_count):
            bot_id = f"bot{i + 1}"
            token = self.tokens[i]
            bot = await self._connect_bot(bot_id, token)
            if bot:
                self.bots.append(bot)

        available_for_dyn = len(self.tokens) - len(self.bots)
        print(f"[BotPool] Startup complete: {len(self.bots)} active bot(s). {max(0, available_for_dyn)} bot(s) available for dynamic activation.")
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _activate_next_bot(self) -> Optional[dict[str, Any]]:
        """Dynamically activate another configured bot when all active bots are busy."""
        # Never activate more than 20 bots
        if len(self.bots) >= self.MAX_BOTS or len(self.bots) >= len(self.tokens):
            return None

        next_idx = len(self.bots)
        bot_id = f"bot{next_idx + 1}"
        token = self.tokens[next_idx]

        print(f"[BotPool] All active bots busy. Dynamically activating {bot_id}...")
        bot = await self._connect_bot(bot_id, token)
        if bot:
            self.bots.append(bot)
            print(f"[BotPool] Successfully activated {bot_id} dynamically (total active: {len(self.bots)}/{len(self.tokens)}).")
            return bot
        return None

    def _find_idle_bot(self, active_bot_ids: set[str], now: float) -> Optional[dict[str, Any]]:
        """Find the next idle, healthy bot (round-robin) among currently connected bots."""
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
                    # Bot was removed or is on FloodWait cooldown: release session to switch to healthy bot
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
        """Round-robin acquisition across available bots not on cooldown, with dynamic activation."""
        async with self._lock:
            now = time.time()
            if not self.bots and len(self.tokens) > 0:
                await self._activate_next_bot()
            if not self.bots:
                return None

            active_bot_ids = set(self._sessions.values())
            candidate = self._find_idle_bot(active_bot_ids, now)
            if candidate:
                return BotLease(candidate)

            # If all are in sessions, try activating another bot if under limit
            if len(self.bots) < min(self.MAX_BOTS, len(self.tokens)):
                new_bot = await self._activate_next_bot()
                if new_bot and new_bot.get("cooldown_until", 0.0) <= now:
                    return BotLease(new_bot)

            # Fallback: choose the healthy bot with fewest active streams
            healthy = [b for b in self.bots if b.get("cooldown_until", 0.0) <= now]
            if healthy:
                healthy.sort(key=lambda b: b.get("active_streams", 0))
                return BotLease(healthy[0])

            return None

    def mark_stream_start(self, bot_id: str) -> None:
        """Increment active stream counter for a bot to protect it from disconnection."""
        for b in self.bots:
            if b["id"] == bot_id:
                b["active_streams"] = b.get("active_streams", 0) + 1
                break

    def mark_stream_end(self, bot_id: str) -> None:
        """Decrement active stream counter for a bot."""
        for b in self.bots:
            if b["id"] == bot_id:
                b["active_streams"] = max(0, b.get("active_streams", 0) - 1)
                break

    def is_stream_active(self, bot_id: str) -> bool:
        """Check if bot is actively serving a stream."""
        for b in self.bots:
            if b["id"] == bot_id:
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
        """Parse FLOOD_WAIT from error, place bot in cooldown, and release affected sessions."""
        if not bot or not err:
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
            bot["cooldown_until"] = time.time() + seconds
            print(f"[BotPool] {bot['id']} flood-wait {seconds}s, placed on cooldown until {bot['cooldown_until']}")
            # Release any session using this bot so user can be reassigned to a healthy bot
            to_remove = [sk for sk, bid in self._sessions.items() if bid == bot["id"]]
            for sk in to_remove:
                self.release_session(sk)

    async def close(self) -> None:
        """Disconnect idle bot clients while strictly protecting bots with active streams."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        self._sessions.clear()
        self._session_activity.clear()

        remaining_bots: list[dict[str, Any]] = []
        for b in self.bots:
            # Never disconnect a bot that is currently serving an active stream
            if b.get("active_streams", 0) > 0:
                print(f"[BotPool] Bot {b['id']} has {b['active_streams']} active stream(s); preserving connection.")
                remaining_bots.append(b)
                continue
            try:
                if b.get("client") and b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass
        self.bots = remaining_bots
