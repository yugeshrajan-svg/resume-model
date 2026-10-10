"""In-process TTL cache for retrieval (query embeddings, validated answers).

Never persisted. Keys always start with the company id; callers add every version that makes a value valid
(document and index versions, embedding space, model, prompt and retrieval configuration, permission scope).
A hit is a candidate value only: answer callers re-check eligibility before returning it, so a consent change,
erasure, consolidation or newer resume version made in another process bypasses a stale entry.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class TTLCache:
    def __init__(self, ttl_s: float, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0 and self.max_entries > 0

    def get(self, key: Hashable) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] < self._clock():
                if item is not None:
                    del self._items[key]
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item[1]

    def put(self, key: Hashable, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._items[key] = (self._clock() + self.ttl_s, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def discard(self, key: Hashable) -> None:
        with self._lock:
            self._items.pop(key, None)

    def invalidate_company(self, company_id: Any) -> int:
        """Drop every entry of one company (keys are tuples whose first element is the company id)."""
        with self._lock:
            stale = [k for k in self._items if isinstance(k, tuple) and k and k[0] == company_id]
            for key in stale:
                del self._items[key]
            return len(stale)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
