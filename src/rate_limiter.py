"""Small thread-safe pacing primitive for provider calls."""
import threading
import time
from typing import Callable


class RateLimiter:
    """Enforce a minimum gap between starts of calls made through one instance."""

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._monotonic()
            delay = max(0.0, self._next_start - now)
            self._next_start = max(now, self._next_start) + self.min_interval_seconds
        if delay:
            self._sleep(delay)

    def defer(self, seconds: float) -> None:
        """Pause all callers after a provider throttle response."""
        with self._lock:
            self._next_start = max(
                self._next_start,
                self._monotonic() + max(0.0, float(seconds)),
            )
