from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

import openai
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.models.settings import Settings

_BACKOFF_BASE: float = 1.0
_BACKOFF_JITTER: float = 1.0

_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
)
_PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    BadRequestError,
    AuthenticationError,
)

_cached_clients: dict[tuple[str | None, str | None], openai.OpenAI] = {}


class _RateLimiter:
    def __init__(self, rate_per_minute: int) -> None:
        self._interval = 60.0 / max(rate_per_minute, 1)
        self._lock = asyncio.Lock()
        self._next_allowed: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait > 0.0:
            await asyncio.sleep(wait)


@dataclass
class _ClientState:
    rate_limiter: _RateLimiter
    retry_attempts: int


_client_states: WeakKeyDictionary[openai.OpenAI, _ClientState] = WeakKeyDictionary()


def build_openai_client(settings: Settings) -> openai.OpenAI:
    key = (settings.openai_base_url, settings.openai_api_key)
    if key not in _cached_clients:
        Log(
            INFO_LOG_LEVEL,
            "OpenAI client instantiated",
            {"base_url": settings.openai_base_url or ""},
        )
        client = openai.OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key or "dummy",
            timeout=float(settings.timeout_seconds),
        )
        _cached_clients[key] = client
        _client_states[client] = _ClientState(
            rate_limiter=_RateLimiter(settings.rate_limit_per_minute),
            retry_attempts=settings.retry_attempts,
        )
    return _cached_clients[key]


async def chat_completion_with_retry(
    client: openai.OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.1,
    max_tokens: int,
    request_id: str,
    stage: str,
    page: int,
) -> str:
    state = _client_states.get(client)
    max_attempts = (state.retry_attempts + 1) if state is not None else 4
    rate_limiter = state.rate_limiter if state is not None else None

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        if rate_limiter is not None:
            await rate_limiter.acquire()
        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from model")
            Log(
                INFO_LOG_LEVEL,
                "chat_completion success",
                {
                    "request_id": request_id,
                    "stage": stage,
                    "page": page,
                    "model": model,
                    "attempt": attempt,
                    "outcome": "success",
                },
            )
            return content
        except _PERMANENT_ERRORS as exc:
            Log(
                ERROR_LOG_LEVEL,
                "chat_completion permanent error",
                {
                    "request_id": request_id,
                    "stage": stage,
                    "page": page,
                    "model": model,
                    "attempt": attempt,
                    "outcome": "permanent_error",
                    "error": repr(exc),
                },
            )
            raise
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            Log(
                WARNING_LOG_LEVEL,
                "chat_completion transient error",
                {
                    "request_id": request_id,
                    "stage": stage,
                    "page": page,
                    "model": model,
                    "attempt": attempt,
                    "outcome": "transient_error",
                    "error": repr(exc),
                },
            )
            if attempt < max_attempts - 1:
                backoff = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, _BACKOFF_JITTER)
                await asyncio.sleep(backoff)

    assert last_exc is not None
    raise last_exc
