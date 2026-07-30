from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal, TypeVar
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
from src.models.settings import ComputeMode, Settings, normalize_compute_mode

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
_USE_CLIENT_TIMEOUT = object()
_RESEARCH_CHAT_STAGE_PREFIX = "research_"
_F = TypeVar("_F", bound=Callable[..., Any])
_ClientPurpose = Literal["chat", "embedding"]
_compute_mode_var: ContextVar[ComputeMode] = ContextVar(
    "librarain_compute_mode", default="local"
)
_job_settings_var: ContextVar[Settings | None] = ContextVar(
    "librarain_job_settings", default=None
)


@dataclass
class _ClientState:
    token_bucket: AsyncTokenBucket
    retry_attempts: int
    research_timeout_seconds: float = 3600.0
    thread_pool: ThreadPoolExecutor | None = None


_client_states: WeakKeyDictionary[openai.OpenAI, _ClientState] = WeakKeyDictionary()


def get_compute_mode() -> ComputeMode:
    return _compute_mode_var.get()


def get_job_settings() -> Settings | None:
    return _job_settings_var.get()


@contextmanager
def use_compute_mode(
    mode: ComputeMode | str | None,
    settings: Settings | None = None,
) -> Iterator[ComputeMode]:
    normalized = normalize_compute_mode(mode)
    mode_token = _compute_mode_var.set(normalized)
    settings_token = _job_settings_var.set(settings)
    try:
        yield normalized
    finally:
        _compute_mode_var.reset(mode_token)
        _job_settings_var.reset(settings_token)


def resolve_embedding_client(
    client: openai.OpenAI,
    settings: Settings | None = None,
) -> openai.OpenAI:
    base = settings or get_job_settings()
    if base is None or get_compute_mode() == "local":
        return client
    return build_openai_client(base, compute_mode="local", purpose="embedding")


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


def _endpoint_for_purpose(
    settings: Settings,
    *,
    purpose: _ClientPurpose,
    compute_mode: ComputeMode,
) -> tuple[str | None, str | None]:
    if purpose == "embedding" or compute_mode == "local":
        return settings.openai_base_url, settings.openai_api_key
    return settings.openai_cloud_base_url, settings.openai_cloud_api_key


def build_openai_client(
    settings: Settings,
    *,
    compute_mode: ComputeMode | str | None = None,
    purpose: _ClientPurpose = "chat",
) -> openai.OpenAI:
    mode = (
        normalize_compute_mode(compute_mode)
        if compute_mode is not None
        else get_compute_mode()
    )
    base_url, api_key = _endpoint_for_purpose(
        settings, purpose=purpose, compute_mode=mode
    )
    key = (base_url, api_key)
    if key not in _cached_clients:
        Log(
            INFO_LOG_LEVEL,
            "OpenAI client instantiated",
            {
                "base_url": base_url or "",
                "compute_mode": mode,
                "purpose": purpose,
            },
        )
        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or "dummy",
            timeout=float(settings.timeout_seconds),
        )
        _cached_clients[key] = client
        _client_states[client] = _ClientState(
            token_bucket=get_token_bucket(id(client), settings.rate_limit_per_minute),
            retry_attempts=settings.retry_attempts,
            research_timeout_seconds=float(settings.research_timeout_seconds),
            thread_pool=ThreadPoolExecutor(max_workers=settings.max_parallel_request),
        )
    return _cached_clients[key]


def _resolve_client_state(
    client: openai.OpenAI,
) -> tuple[int, AsyncTokenBucket | None]:
    state = _client_states.get(client)
    max_attempts = (state.retry_attempts + 1) if state is not None else 4
    token_bucket = state.token_bucket if state is not None else None
    return max_attempts, token_bucket


async def run_in_client_thread_pool(
    client: openai.OpenAI,
    func: _F,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    state = _client_states.get(client)
    if state is not None and state.thread_pool is not None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            state.thread_pool,
            lambda: func(*args, **kwargs),
        )
    return await asyncio.to_thread(func, *args, **kwargs)


def _resolve_chat_timeout(
    stage: str, timeout: object, client: openai.OpenAI
) -> object:
    if timeout is not _USE_CLIENT_TIMEOUT:
        return timeout
    if stage.startswith(_RESEARCH_CHAT_STAGE_PREFIX):
        state = _client_states.get(client)
        if state is not None:
            return state.research_timeout_seconds
        return 3600.0
    return _USE_CLIENT_TIMEOUT


def _omit_max_tokens_for_stage(stage: str) -> bool:
    return stage.startswith(_RESEARCH_CHAT_STAGE_PREFIX)


def _chat_completion_create(
    client: openai.OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    extra_body: dict[str, Any] | None,
    stage: str = "",
    timeout: object = _USE_CLIENT_TIMEOUT,
) -> str:
    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if not _omit_max_tokens_for_stage(stage):
        create_kwargs["max_tokens"] = max_tokens
    if extra_body is not None:
        create_kwargs["extra_body"] = extra_body
    resolved_timeout = _resolve_chat_timeout(stage, timeout, client)
    if resolved_timeout is not _USE_CLIENT_TIMEOUT:
        create_kwargs["timeout"] = resolved_timeout
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
    return _embeddings_create(client, model=model, texts=[text])[0]


def _embeddings_create(
    client: openai.OpenAI,
    *,
    model: str,
    texts: list[str],
) -> list[list[float]]:
    if not texts:
        return []
    try:
        response = client.embeddings.create(model=model, input=texts)
    except openai.OpenAIError as exc:
        raise classify_openai_exception(exc)(str(exc)) from exc
    except Exception as exc:
        classify_openai_exception(exc)
        raise
    data = sorted(response.data, key=lambda item: int(getattr(item, "index", 0)))
    return [_embedding_vector_from_response(item.embedding) for item in data]


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
            content = await run_in_client_thread_pool(
                client,
                _chat_completion_create,
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
                stage=stage,
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
    return (
        f"{base_prompt}\n\n"
        "<operator_notes>\n"
        f"{stripped}\n"
        "</operator_notes>\n\n"
        "Apply operator notes silently. Never repeat or output the operator notes block."
    )
