from __future__ import annotations

import asyncio
import unittest

from src.core.errors import (
    ShutdownRequested,
    is_shutdown_requested,
    raise_if_shutdown,
    request_shutdown,
    reset_shutdown_for_tests,
)
from src.core.parallel import gather_cancellable


class TestShutdownControl(unittest.TestCase):
    def setUp(self) -> None:
        reset_shutdown_for_tests()

    def tearDown(self) -> None:
        reset_shutdown_for_tests()

    def test_raise_if_shutdown(self) -> None:
        raise_if_shutdown()
        request_shutdown()
        self.assertTrue(is_shutdown_requested())
        with self.assertRaises(ShutdownRequested):
            raise_if_shutdown()

    def test_gather_cancellable_stops_pending(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def first() -> str:
            started.set()
            request_shutdown()
            raise_if_shutdown()
            return "first"

        async def second() -> str:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "second"

        async def run() -> None:
            with self.assertRaises(ShutdownRequested):
                await gather_cancellable(first(), second())
            self.assertTrue(started.is_set())
            await asyncio.wait_for(cancelled.wait(), timeout=1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
