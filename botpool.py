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


class BotPool:
    """Manages a pool of Telegram bot clients for session-based and load-balanced media streaming."""

    def __init__(self, api_id: int, api_hash: str, tokens: list[str]) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.tokens = tokens
        self._rr_index = 0
        self.bots: list[dict[str, Any]] = []
        self._sessions: dict[str, str] = {}  # session_key -> bot_id
        self._session_activity: dict[str, float] = {}  # session_key -> last_active_timestamp
        self._reaper_task: Optional[asyncio.Task] = None

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

        # 5. DO NOT fall back to user's channel entity object (access_hash is account-specific)
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
                self._cleanup_expired_sessions(30.0)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def init(self, channel_ref: Any) -> None:
        """Initialize up to 20 bot clients from tokens and resolve target channel for each."""
        for b in self.bots:
            try:
                if b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass
        self.bots = []
        self._rr_index = 0
        self._sessions.clear()
        self._session_activity.clear()

        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()

        tokens_to_use = self.tokens[:20]
        if not tokens_to_use or not self.api_id or not self.api_hash:
            return

        print(f"[BotPool] Initializing pool with {len(tokens_to_use)} bot token(s)...")

        for i, token in enumerate(tokens_to_use):
            bot_id = f"bot{i + 1}"
            try:
                client = TelegramClient(
                    StringSession(""),
                    self.api_id,
                    self.api_hash,
                    connection_retries=5,
                    auto_reconnect=True,
                )
                await client.start(bot_token=token)

                channel = await self._resolve_channel_for_bot(client, channel_ref)
                if not channel:
                    print(f"[BotPool] Failed to resolve channel for {bot_id}. Skipping.")
                    await client.disconnect()
                    continue

                self.bots.append({
                    "id": bot_id,
                    "client": client,
                    "channel": channel,
                    "cooldown_until": 0.0,
                    "media_cache": SimpleLRU(500),
                    "active": True,
                })
                print(f"[BotPool] {bot_id} ready and channel resolved.")
            except Exception as exc:
                print(f"[BotPool] Warning: Failed to initialize {bot_id}: {exc}")

        print(f"[BotPool] Ready with {len(self.bots)}/{len(tokens_to_use)} bots")
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    def acquire_for_session(self, session_key: str) -> Optional[BotLease]:
        """Assign or retrieve dedicated bot for a user session.

        session_key = user IP address from request.
        If session_key already has an active bot: return same bot.
        Else: assign next available bot (round-robin).
        If all bots occupied: return None (log waiting).
        """
        if not self.bots:
            return None

        now = time.time()
        self._cleanup_expired_sessions(30.0)

        # If session_key already has an active bot: return same bot
        if session_key in self._sessions:
            assigned_bot_id = self._sessions[session_key]
            bot = next((b for b in self.bots if b["id"] == assigned_bot_id), None)
            if bot:
                self._session_activity[session_key] = now
                return BotLease(bot)
            else:
                self._sessions.pop(session_key, None)
                self._session_activity.pop(session_key, None)

        # Assign next available bot (round-robin)
        active_bot_ids = set(self._sessions.values())
        num_bots = len(self.bots)
        start = self._rr_index

        for step in range(num_bots):
            idx = (start + step) % num_bots
            candidate = self.bots[idx]

            # Bot is considered "in use" while session is active
            if candidate["id"] in active_bot_ids:
                continue

            if candidate.get("cooldown_until", 0.0) > now:
                continue

            self._rr_index = (idx + 1) % num_bots
            self._sessions[session_key] = candidate["id"]
            self._session_activity[session_key] = now
            return BotLease(candidate)

        # If all bots occupied: return None
        print(f"[BotPool] All bots busy, user {session_key} waiting")
        return None

    def release_session(self, session_key: str) -> None:
        """Remove session mapping and decrement bot usage."""
        bot_id = self._sessions.pop(session_key, None)
        self._session_activity.pop(session_key, None)
        if bot_id:
            print(f"[BotPool] Released session {session_key} (bot: {bot_id})")

    def acquire(self) -> Optional[BotLease]:
        """Round-robin assignment across all available bots not currently on cooldown or in use."""
        if not self.bots:
            return None

        now = time.time()
        active_bot_ids = set(self._sessions.values())
        num_bots = len(self.bots)
        start = self._rr_index

        for step in range(num_bots):
            idx = (start + step) % num_bots
            candidate = self.bots[idx]
            if candidate.get("cooldown_until", 0.0) > now:
                continue
            if candidate["id"] in active_bot_ids:
                continue

            self._rr_index = (idx + 1) % num_bots
            return BotLease(candidate)

        return None

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
        """Parse FLOOD_WAIT from error and place bot on cooldown."""
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
            print(f"[BotPool] {bot['id']} flood-wait {seconds}s")
            # Release any session using this bot so user can be reassigned
            to_remove = [sk for sk, bid in self._sessions.items() if bid == bot["id"]]
            for sk in to_remove:
                self.release_session(sk)

    async def close(self) -> None:
        """Disconnect all bot clients and clean up sessions."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        self._sessions.clear()
        self._session_activity.clear()

        for b in self.bots:
            try:
                if b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass
        self.bots.clear()
