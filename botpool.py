import asyncio
import re
import time
from typing import Any, Callable, Optional
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.updates import GetDifferenceRequest, GetStateRequest

from cache import SimpleLRU


class BotLease:
    """Represents a leased bot from the pool. Idempotent release."""

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
    """Manages a pool of Telegram bot clients for parallelized media streaming."""

    def __init__(self, api_id: int, api_hash: str, tokens: list[str]) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.tokens = tokens
        self._rr_index = 0
        self.bots: list[dict[str, Any]] = []

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

    async def init(self, channel_ref: Any) -> None:
        """Initialize up to 20 bot clients from tokens and resolve the target channel for each."""
        # Clean up any previously active bots
        for b in self.bots:
            try:
                if b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass
        self.bots = []
        self._rr_index = 0

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

    def acquire(self) -> Optional[BotLease]:
        """Round-robin assignment across all available bots not currently on cooldown."""
        if not self.bots:
            return None

        num_bots = len(self.bots)
        now = time.time()
        start = self._rr_index

        for step in range(num_bots):
            idx = (start + step) % num_bots
            candidate = self.bots[idx]
            if candidate.get("cooldown_until", 0.0) > now:
                continue

            self._rr_index = (idx + 1) % num_bots
            return BotLease(candidate)

        # If ALL bots are on cooldown
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

    async def close(self) -> None:
        """Disconnect all bot clients."""
        for b in self.bots:
            try:
                if b["client"].is_connected():
                    await b["client"].disconnect()
            except Exception:
                pass
        self.bots.clear()
