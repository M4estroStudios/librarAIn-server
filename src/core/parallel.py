from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from src.core.errors import ShutdownRequested, raise_if_shutdown

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(func: Callable[[T], R], items: Sequence[T]) -> list[R]:
    if not items:
        return []
    if len(items) == 1:
        raise_if_shutdown()
        return [func(items[0])]
    workers = max(1, min(len(items), (os.cpu_count() or 4) * 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(func, item) for item in items]
        results: list[R] = []
        for future in futures:
            raise_if_shutdown()
            results.append(future.result())
        return results


async def gather_cancellable(*aws: Awaitable[T]) -> list[T]:
    if not aws:
        return []
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    pending: set[asyncio.Task[T]] = set(tasks)
    try:
        while pending:
            raise_if_shutdown()
            done, pending = await asyncio.wait(
                pending,
                timeout=0.25,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if isinstance(exc, ShutdownRequested):
                    raise exc
                if exc is not None:
                    for other in pending:
                        other.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    raise exc
        return [task.result() for task in tasks]
    except ShutdownRequested:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
