from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from src.core.errors import PermanentError, TransientError

T = TypeVar("T")


def retry_sync(
    fn: Callable[[], T],
    *,
    max_attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    jitter: bool = True,
    retry_on: tuple[type[Exception], ...] = (TransientError,),
    giveup_on: tuple[type[Exception], ...] = (PermanentError,),
) -> T:
    """Synchronous twin of retry_async, for blocking call sites."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except giveup_on:
            raise
        except retry_on as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            if jitter:
                delay += random.uniform(0, base_delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    jitter: bool = True,
    retry_on: tuple[type[Exception], ...] = (TransientError,),
    giveup_on: tuple[type[Exception], ...] = (PermanentError,),
) -> T:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except giveup_on:
            raise
        except retry_on as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            if jitter:
                delay += random.uniform(0, base_delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
