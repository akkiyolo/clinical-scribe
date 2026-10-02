"""Rate limiting: slowapi for per-IP limits plus a per-email limiter for login attempts."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


class EmailRateLimiter:
    """Sliding-window limiter keyed by e-mail address (login: 5 attempts per minute)."""

    def __init__(
        self, limit: int, window_seconds: int, clock: Callable[[], float] = time.monotonic
    ):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self.enabled = True
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt for `key`; False when it exceeds the limit."""
        if not self.enabled:
            return True
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory under a spray of random addresses
                for stale in [
                    k for k, v in self._hits.items() if not v or now - v[-1] >= self.window
                ]:
                    del self._hits[stale]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_email_limiter = EmailRateLimiter(limit=5, window_seconds=60)
