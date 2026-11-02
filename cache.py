import asyncio
from collections import OrderedDict
import threading
from typing import Any, Optional


class SimpleLRU:
    """Lightweight, O(1) Least Recently Used (LRU) Cache backed by collections.OrderedDict.

    Features:
    - Thread-safe and coroutine-safe using reentrant threading lock.
    - Dual eviction: bounds by entry count (max_size) and optional byte size (max_bytes).
    - Memory-optimized using __slots__ to eliminate per-instance __dict__ overhead.
    - Hit and miss tracking for monitoring cache effectiveness.
    """

    __slots__ = (
        "max_size",
        "max_bytes",
        "_current_bytes",
        "_map",
        "_lock",
        "hits",
        "misses",
    )

    def __init__(self, max_size: int = 500, max_bytes: Optional[int] = None) -> None:
        self.max_size = max(1, max_size)
        self.max_bytes = max_bytes if (max_bytes and max_bytes > 0) else None
        self._current_bytes = 0
        self._map: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _val_bytes(val: Any) -> int:
        """Calculate memory size in bytes for buffer-like values."""
        if isinstance(val, (bytes, bytearray, memoryview)):
            return len(val)
        return 0

    def _evict_if_needed(self) -> None:
        """Evict oldest entries until within max_size and max_bytes boundaries."""
        # Evict by count
        while len(self._map) > self.max_size:
            _, evicted = self._map.popitem(last=False)
            self._current_bytes -= self._val_bytes(evicted)

        # Evict by byte limit if configured
        if self.max_bytes:
            while self._map and self._current_bytes > self.max_bytes:
                _, evicted = self._map.popitem(last=False)
                self._current_bytes -= self._val_bytes(evicted)

    async def get(self, key: Any, default: Any = None) -> Any:
        """Retrieve an item from cache and mark it as most recently used."""
        return self.get_sync(key, default)

    async def set(self, key: Any, val: Any) -> None:
        """Store an item in cache, evicting the oldest item if capacity is reached."""
        self.set_sync(key, val)

    async def peek(self, key: Any, default: Any = None) -> Any:
        """Retrieve an item without updating its access order."""
        return self.peek_sync(key, default)

    async def has(self, key: Any) -> bool:
        """Check whether a key exists in cache without updating its access order."""
        return self.has_sync(key)

    async def delete(self, key: Any) -> bool:
        """Remove a key from cache if present. Returns True if deleted, False otherwise."""
        return self.delete_sync(key)

    async def clear(self) -> None:
        """Purge all entries from cache."""
        self.clear_sync()

    @property
    def size(self) -> int:
        """Current number of items stored in cache."""
        with self._lock:
            return len(self._map)

    @property
    def current_bytes(self) -> int:
        """Current tracked byte size of cached buffers."""
        with self._lock:
            return self._current_bytes

    @property
    def hit_rate(self) -> float:
        """Ratio of cache hits to total lookups (0.0 to 1.0)."""
        with self._lock:
            total = self.hits + self.misses
            return (self.hits / total) if total > 0 else 0.0

    async def keys(self) -> list[str]:
        """Return a copy of all current keys in cache."""
        with self._lock:
            return list(self._map.keys())

    # Synchronous helpers for non-async / fast lookup contexts (thread-safe)
    def get_sync(self, key: Any, default: Any = None) -> Any:
        k = str(key)
        with self._lock:
            if k not in self._map:
                self.misses += 1
                return default
            self.hits += 1
            self._map.move_to_end(k)
            return self._map[k]

    def set_sync(self, key: Any, val: Any) -> None:
        k = str(key)
        val_size = self._val_bytes(val)
        with self._lock:
            if k in self._map:
                old_val = self._map[k]
                self._current_bytes -= self._val_bytes(old_val)
                self._map.move_to_end(k)
            self._map[k] = val
            self._current_bytes += val_size
            self._evict_if_needed()

    def peek_sync(self, key: Any, default: Any = None) -> Any:
        k = str(key)
        with self._lock:
            if k not in self._map:
                return default
            return self._map[k]

    def has_sync(self, key: Any) -> bool:
        k = str(key)
        with self._lock:
            return k in self._map

    def delete_sync(self, key: Any) -> bool:
        k = str(key)
        with self._lock:
            if k in self._map:
                val = self._map.pop(k)
                self._current_bytes -= self._val_bytes(val)
                return True
            return False

    def clear_sync(self) -> None:
        with self._lock:
            self._map.clear()
            self._current_bytes = 0
