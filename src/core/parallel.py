from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(func: Callable[[T], R], items: Sequence[T]) -> list[R]:
    if not items:
        return []
    if len(items) == 1:
        return [func(items[0])]
    workers = max(1, min(len(items), (os.cpu_count() or 4) * 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(func, items))
