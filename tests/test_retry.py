from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.errors import PermanentError, TransientError
from src.core.retry import retry_async


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


class TestRetryAsync(unittest.TestCase):
    def test_transient_error_causes_retry(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise TransientError("temporary")
            return "ok"

        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(retry_async(factory, max_attempts=3))
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)

    def test_permanent_error_no_retry(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise PermanentError("fatal")

        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(PermanentError):
                _run(retry_async(factory, max_attempts=3))
        self.assertEqual(calls, 1)

    def test_max_attempts_respected(self) -> None:
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise TransientError("still failing")

        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TransientError):
                _run(retry_async(factory, max_attempts=3))
        self.assertEqual(calls, 3)

    def test_backoff_grows(self) -> None:
        delays: list[float] = []
        calls = 0

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        async def factory() -> str:
            nonlocal calls
            calls += 1
            raise TransientError("retry")

        with patch("asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(TransientError):
                _run(retry_async(factory, max_attempts=3, base_delay=0.5, jitter=False))
        self.assertEqual(calls, 3)
        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0], delays[1])


if __name__ == "__main__":
    unittest.main()
