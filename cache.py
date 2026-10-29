import asyncio
from collections import OrderedDict
from typing import Any, Optional


class SimpleLRU:
    """Lightweight, O(1) Least Recently Used (LRU) Cache backed by collections.OrderedDict.

    Thread-safe and coroutine-safe using asyncio.Lock.
    """

    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max(1, max_size)
        self._map: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: Any, default: Any = None) -> Any:
        """Retrieve an item from cache and mark it as most recently used."""
        k = str(key)
        async with self._lock:
            if k not in self._map:
                return default
            self._map.move_to_end(k)
            return self._map[k]

    async def set(self, key: Any, val: Any) -> None:
        """Store an item in cache, evicting the oldest item if capacity is reached."""
        k = str(key)
        async with self._lock:
            if k in self._map:
                self._map.move_to_end(k)
            elif len(self._map) >= self.max_size:
                self._map.popitem(last=False)
            self._map[k] = val

    async def has(self, key: Any) -> bool:
        """Check whether a key exists in cache without updating its access order."""
        k = str(key)
        async with self._lock:
            return k in self._map

    async def delete(self, key: Any) -> bool:
        """Remove a key from cache if present. Returns True if deleted, False otherwise."""
        k = str(key)
        async with self._lock:
            if k in self._map:
                del self._map[k]
                return True
            return False

    async def clear(self) -> None:
        """Purge all entries from cache."""
        async with self._lock:
            self._map.clear()

    @property
    def size(self) -> int:
        """Current number of items stored in cache."""
        return len(self._map)

    async def keys(self) -> list[str]:
        """Return a copy of all current keys in cache."""
        async with self._lock:
            return list(self._map.keys())

    # Synchronous helpers for non-async / fast lookup contexts
    def get_sync(self, key: Any, default: Any = None) -> Any:
        k = str(key)
        if k not in self._map:
            return default
        self._map.move_to_end(k)
        return self._map[k]

    def set_sync(self, key: Any, val: Any) -> None:
        k = str(key)
        if k in self._map:
            self._map.move_to_end(k)
        elif len(self._map) >= self.max_size:
            self._map.popitem(last=False)
        self._map[k] = val

    def has_sync(self, key: Any) -> bool:
        return str(key) in self._map

    def delete_sync(self, key: Any) -> bool:
        k = str(key)
        if k in self._map:
            del self._map[k]
            return True
        return False

    def clear_sync(self) -> None:
        self._map.clear()
