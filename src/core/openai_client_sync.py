from __future__ import annotations

from typing import Any

import openai

from src.core.errors import TransientError, classify_openai_exception
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log, WARNING_LOG_LEVEL
from src.core.openai_client import (
    _PERMANENT_ERRORS,
    _TRANSIENT_ERRORS,
    _chat_completion_create,
    _embedding_create,
    _log_chat_attempt,
    _log_chat_outcome,
    _resolve_client_state,
    build_chat_completion_extra_body,
)
from src.core.retry import retry_sync


def _log_embedding_attempt(
    *,
    attempt: int,
    max_attempts: int,
    stage: str,
    model: str,
    request_id: str,
) -> None:
    Log(
        INFO_LOG_LEVEL,
        "embedding retry loop iteration",
        {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "stage": stage,
            "model": model,
            "request_id": request_id,
        },
    )


def chat_completion_with_retry_sync(
    client: openai.OpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.1,
    max_tokens: int,
    request_id: str,
    stage: str,
    page: int = 0,
    reasoning_effort: str | None = None,
    reasoning_enable_thinking: bool | None = None,
) -> str:
    max_attempts, token_bucket = _resolve_client_state(client)
    extra_body = build_chat_completion_extra_body(
        reasoning_effort=reasoning_effort,
        reasoning_enable_thinking=reasoning_enable_thinking,
    )
    attempt_counter = 0

    def _attempt() -> str:
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
            token_bucket.acquire_blocking()
            Log(INFO_LOG_LEVEL, "chat_completion rate limiter wait done", {"attempt": attempt})
        try:
            Log(
                INFO_LOG_LEVEL,
                "chat_completion API invoke begin",
                {"attempt": attempt, "stage": stage, "page": page},
            )
            content = _chat_completion_create(
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
            Log(
                INFO_LOG_LEVEL,
                "chat_completion API invoke done",
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
        return retry_sync(
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


def embedding_with_retry_sync(
    client: openai.OpenAI,
    *,
    model: str,
    text: str,
    request_id: str,
    stage: str,
) -> list[float]:
    max_attempts, token_bucket = _resolve_client_state(client)
    attempt_counter = 0

    def _attempt() -> list[float]:
        nonlocal attempt_counter
        attempt = attempt_counter
        attempt_counter += 1
        _log_embedding_attempt(
            attempt=attempt,
            max_attempts=max_attempts,
            stage=stage,
            model=model,
            request_id=request_id,
        )
        if token_bucket is not None:
            Log(INFO_LOG_LEVEL, "embedding rate limiter wait begin", {"attempt": attempt})
            token_bucket.acquire_blocking()
            Log(INFO_LOG_LEVEL, "embedding rate limiter wait done", {"attempt": attempt})
        try:
            vector = _embedding_create(client, model=model, text=text)
            Log(
                INFO_LOG_LEVEL,
                "embedding success",
                {
                    "request_id": request_id,
                    "stage": stage,
                    "model": model,
                    "attempt": attempt,
                    "outcome": "success",
                },
            )
            return vector
        except _PERMANENT_ERRORS as exc:
            Log(
                ERROR_LOG_LEVEL,
                "embedding permanent error",
                {
                    "request_id": request_id,
                    "stage": stage,
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
                "embedding transient error",
                {
                    "request_id": request_id,
                    "stage": stage,
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

    return retry_sync(
        _attempt,
        max_attempts=max_attempts,
        base_delay=1.0,
        retry_on=_TRANSIENT_ERRORS,
        giveup_on=_PERMANENT_ERRORS,
    )
