"""Thread-safe token bucket used to respect enrichment API limits (GLEIF 60/min, Companies House 600/5min)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = rate_per_s
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self._lock = threading.Lock()

    @classmethod
    def per_window(cls, calls: int, window_s: float, **kw: object) -> TokenBucket:
        return cls(calls / window_s, burst=max(1, min(calls, 10)), **kw)  # type: ignore[arg-type]

    def acquire(self, n: float = 1.0) -> float:
        """Block until ``n`` tokens are available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                need = (n - self.tokens) / self.rate
            self.sleep(need)
            waited += need
