"""Small in-memory LRU with optional TTL."""
from __future__ import annotations

import time
from collections import OrderedDict


class LRU:
    def __init__(self, maxsize: int = 256, ttl: float | None = None):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        item = self._d.get(key)
        if item is None:
            return default
        value, ts = item
        if self.ttl is not None and time.monotonic() - ts > self.ttl:
            del self._d[key]
            return default
        self._d.move_to_end(key)
        return value

    def put(self, key, value):
        self._d[key] = (value, time.monotonic())
        self._d.move_to_end(key)
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)

    def __contains__(self, key):
        return self.get(key) is not None
