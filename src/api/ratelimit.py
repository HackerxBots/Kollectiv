"""In-process rate limiting for write requests.

This is a per-process sliding window keyed by client address. It stops a single
client from flooding the write routes. It is not shared between replicas, so a
multi-instance deployment needs the limit at the proxy as well.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Dict

WINDOW_SECONDS = 60.0


class WriteRateLimiter:
    """Allow at most ``per_minute`` requests per key in any 60-second window."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = int(per_minute)
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record a request for ``key`` and return whether it is within the limit."""
        if self.per_minute <= 0:
            return True
        now = self._clock()
        window = self._hits.setdefault(key, deque())
        while window and now - window[0] >= WINDOW_SECONDS:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True
