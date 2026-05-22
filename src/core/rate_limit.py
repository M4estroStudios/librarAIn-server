from __future__ import annotations

import asyncio
import threading
import time


class AsyncTokenBucket:
    def __init__(self, rate_per_minute: int, capacity: int | None = None) -> None:
        self._unlimited = rate_per_minute == 0 or capacity == 0
        if self._unlimited:
            return
        self._capacity = float(capacity if capacity is not None else rate_per_minute)
        self._refill_rate = rate_per_minute / 60.0
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

    async def acquire(self, n: int = 1) -> None:
        if self._unlimited:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait_time = deficit / self._refill_rate
            await asyncio.sleep(wait_time)


_buckets: dict[int | str, AsyncTokenBucket] = {}
_buckets_lock = threading.Lock()


def get_token_bucket(key: int | str, rate_per_minute: int) -> AsyncTokenBucket:
    with _buckets_lock:
        if key not in _buckets:
            _buckets[key] = AsyncTokenBucket(rate_per_minute)
        return _buckets[key]
