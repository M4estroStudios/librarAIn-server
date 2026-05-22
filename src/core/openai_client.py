from __future__ import annotations

import asyncio
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

from src.core.errors import PermanentError, TransientError, classify_openai_exception
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.rate_limit import AsyncTokenBucket, get_token_bucket
from src.core.retry import retry_async
from src.models.settings import Settings

_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    TransientError,
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
)
_PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    PermanentError,
    BadRequestError,
    AuthenticationError,
)

_cached_clients: dict[tuple[str | None, str | None], openai.OpenAI] = {}


@dataclass
class _ClientState:
    token_bucket: AsyncTokenBucket
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
            token_bucket=get_token_bucket(id(client), settings.rate_limit_per_minute),
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
    token_bucket = state.token_bucket if state is not None else None
    attempt_counter = 0

    async def _attempt() -> str:
        nonlocal attempt_counter
        attempt = attempt_counter
        attempt_counter += 1
        Log(
            INFO_LOG_LEVEL,
            "chat_completion retry loop iteration",
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "stage": stage,
                "page": page,
                "model": model,
                "request_id": request_id,
            },
        )
        if token_bucket is not None:
            Log(INFO_LOG_LEVEL, "chat_completion rate limiter wait begin", {"attempt": attempt})
            await token_bucket.acquire()
            Log(INFO_LOG_LEVEL, "chat_completion rate limiter wait done", {"attempt": attempt})
        try:
            Log(
                INFO_LOG_LEVEL,
                "chat_completion API thread invoke begin",
                {"attempt": attempt, "stage": stage, "page": page},
            )
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            Log(
                INFO_LOG_LEVEL,
                "chat_completion API thread invoke done",
                {"attempt": attempt, "stage": stage, "page": page},
            )
            content = response.choices[0].message.content
            if not content or not str(content).strip():
                Log(
                    WARNING_LOG_LEVEL,
                    "chat_completion empty response",
                    {
                        "request_id": request_id,
                        "stage": stage,
                        "page": page,
                        "model": model,
                        "attempt": attempt,
                        "outcome": "empty_response",
                    },
                )
                raise TransientError("Empty response from model")
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
            raise
        except Exception as exc:
            classify_openai_exception(exc)
            raise

    try:
        return await retry_async(
            _attempt,
            max_attempts=max_attempts,
            base_delay=1.0,
            retry_on=_TRANSIENT_ERRORS,
            giveup_on=_PERMANENT_ERRORS,
        )
    except TransientError as exc:
        if "Empty response from model" in str(exc):
            raise ValueError(str(exc)) from exc
        raise
