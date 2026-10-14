"""Tiny in-memory sliding-window rate limiter."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict


class RateLimiter:
    def __init__(self, limit: int = 30, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if not hits:
            # drop idle keys so memory stays bounded
            self._hits.pop(key, None)
            hits = self._hits[key]
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
