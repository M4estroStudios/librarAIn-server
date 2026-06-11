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


def build_chat_completion_extra_body(
    *,
    reasoning_effort: str | None = None,
    reasoning_enable_thinking: bool | None = None,
) -> dict[str, Any] | None:
    extra: dict[str, Any] = {}
    if reasoning_effort:
        extra["reasoning"] = {"effort": reasoning_effort}
    if reasoning_enable_thinking is not None:
        extra["enable_thinking"] = reasoning_enable_thinking
    return extra or None


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


def _resolve_client_state(
    client: openai.OpenAI,
) -> tuple[int, AsyncTokenBucket | None]:
    state = _client_states.get(client)
    max_attempts = (state.retry_attempts + 1) if state is not None else 4
    token_bucket = state.token_bucket if state is not None else None
    return max_attempts, token_bucket


def _chat_completion_create(
    client: openai.OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    extra_body: dict[str, Any] | None,
) -> str:
    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body is not None:
        create_kwargs["extra_body"] = extra_body
    try:
        response = client.chat.completions.create(**create_kwargs)
    except openai.OpenAIError as exc:
        raise classify_openai_exception(exc)(str(exc)) from exc
    except Exception as exc:
        classify_openai_exception(exc)
        raise
    content = response.choices[0].message.content
    if not content or not str(content).strip():
        raise TransientError("Empty response from model")
    return str(content)


def _embedding_vector_from_response(data: Any) -> list[float]:
    if hasattr(data, "__iter__") and not isinstance(data, (str, bytes)):
        return [float(x) for x in data]
    raise ValueError("unexpected embedding payload")


def _embedding_create(client: openai.OpenAI, *, model: str, text: str) -> list[float]:
    try:
        response = client.embeddings.create(model=model, input=text)
    except openai.OpenAIError as exc:
        raise classify_openai_exception(exc)(str(exc)) from exc
    except Exception as exc:
        classify_openai_exception(exc)
        raise
    return _embedding_vector_from_response(response.data[0].embedding)


def _log_chat_attempt(
    *,
    attempt: int,
    max_attempts: int,
    stage: str,
    page: int,
    model: str,
    request_id: str,
    reasoning_effort: str | None = None,
    reasoning_enable_thinking: bool | None = None,
) -> None:
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
            "reasoning_effort": reasoning_effort or "",
            "reasoning_enable_thinking": reasoning_enable_thinking,
        },
    )


def _log_chat_outcome(
    *,
    level: int,
    message: str,
    request_id: str,
    stage: str,
    page: int,
    model: str,
    attempt: int,
    outcome: str,
    error: str = "",
) -> None:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "stage": stage,
        "page": page,
        "model": model,
        "attempt": attempt,
        "outcome": outcome,
    }
    if error:
        payload["error"] = error
    Log(level, message, payload)


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
    reasoning_effort: str | None = None,
    reasoning_enable_thinking: bool | None = None,
) -> str:
    max_attempts, token_bucket = _resolve_client_state(client)
    attempt_counter = 0
    extra_body = build_chat_completion_extra_body(
        reasoning_effort=reasoning_effort,
        reasoning_enable_thinking=reasoning_enable_thinking,
    )

    async def _attempt() -> str:
        nonlocal attempt_counter
        attempt = attempt_counter
        attempt_counter += 1
        _log_chat_attempt(
            attempt=attempt,
            max_attempts=max_attempts,
            stage=stage,
            page=page,
            model=model,
            request_id=request_id,
            reasoning_effort=reasoning_effort,
            reasoning_enable_thinking=reasoning_enable_thinking,
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
            content = await asyncio.to_thread(
                _chat_completion_create,
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
            Log(
                INFO_LOG_LEVEL,
                "chat_completion API thread invoke done",
                {"attempt": attempt, "stage": stage, "page": page},
            )
            _log_chat_outcome(
                level=INFO_LOG_LEVEL,
                message="chat_completion success",
                request_id=request_id,
                stage=stage,
                page=page,
                model=model,
                attempt=attempt,
                outcome="success",
            )
            return content
        except _PERMANENT_ERRORS as exc:
            _log_chat_outcome(
                level=ERROR_LOG_LEVEL,
                message="chat_completion permanent error",
                request_id=request_id,
                stage=stage,
                page=page,
                model=model,
                attempt=attempt,
                outcome="permanent_error",
                error=repr(exc),
            )
            raise
        except _TRANSIENT_ERRORS as exc:
            _log_chat_outcome(
                level=WARNING_LOG_LEVEL,
                message="chat_completion transient error",
                request_id=request_id,
                stage=stage,
                page=page,
                model=model,
                attempt=attempt,
                outcome="transient_error",
                error=repr(exc),
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


def build_system_prompt(base_prompt: str, notes: str | None) -> str:
    if not notes:
        return base_prompt
    stripped = notes.strip()
    if not stripped:
        return base_prompt
    return f"{base_prompt}\n\n## Operator notes\n\n{stripped}"
