from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.core.rate_limit import AsyncTokenBucket, get_token_bucket


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


class TestAsyncTokenBucket(unittest.TestCase):
    def test_first_sixty_acquires_immediate(self) -> None:
        current_time = [0.0]
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def run() -> None:
            with patch("src.core.rate_limit.time.monotonic", side_effect=lambda: current_time[0]):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    bucket = AsyncTokenBucket(60)
                    for _ in range(60):
                        await bucket.acquire()

        _run(run())
        self.assertEqual(sleeps, [])

    def test_sixty_first_acquire_waits(self) -> None:
        current_time = [0.0]
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            current_time[0] += delay

        async def run() -> None:
            with patch("src.core.rate_limit.time.monotonic", side_effect=lambda: current_time[0]):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    bucket = AsyncTokenBucket(60)
                    for _ in range(60):
                        await bucket.acquire()
                    await bucket.acquire()

        _run(run())
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.0, places=5)

    def test_zero_capacity_unlimited(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        async def run() -> None:
            with patch("asyncio.sleep", side_effect=fake_sleep):
                bucket = AsyncTokenBucket(60, capacity=0)
                for _ in range(100):
                    await bucket.acquire()

        _run(run())
        self.assertEqual(sleeps, [])

    def test_get_token_bucket_singleton(self) -> None:
        b1 = get_token_bucket("test-key", 60)
        b2 = get_token_bucket("test-key", 60)
        self.assertIs(b1, b2)


if __name__ == "__main__":
    unittest.main()
