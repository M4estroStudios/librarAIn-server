from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)


class TransientError(Exception):
    pass


class PermanentError(Exception):
    pass


class ShutdownRequested(Exception):
    pass


_shutdown_event = threading.Event()
_current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)
_cancelled_jobs: set[str] = set()
_cancel_lock = threading.Lock()

_TRANSIENT_LOCAL_MODEL_MARKERS = (
    "model reloaded",
    "model has crashed",
    "model is loading",
    "model unloaded",
)


def request_shutdown() -> None:
    _shutdown_event.set()
    try:
        from src.core.openai_client import abandon_openai_client_pools

        abandon_openai_client_pools()
    except Exception:
        pass


def reset_shutdown_for_tests() -> None:
    _shutdown_event.clear()
    with _cancel_lock:
        _cancelled_jobs.clear()


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def request_job_cancel(job_id: str) -> None:
    cleaned = str(job_id or "").strip()
    if not cleaned:
        return
    with _cancel_lock:
        _cancelled_jobs.add(cleaned)


def clear_job_cancel(job_id: str) -> None:
    cleaned = str(job_id or "").strip()
    if not cleaned:
        return
    with _cancel_lock:
        _cancelled_jobs.discard(cleaned)


def is_job_cancel_requested(job_id: str | None = None) -> bool:
    target = str(job_id or "").strip() or _current_job_id.get()
    if not target:
        return False
    with _cancel_lock:
        return target in _cancelled_jobs


@contextmanager
def job_cancel_scope(job_id: str) -> Iterator[None]:
    cleaned = str(job_id or "").strip()
    token = _current_job_id.set(cleaned or None)
    try:
        yield
    finally:
        _current_job_id.reset(token)
        if cleaned:
            clear_job_cancel(cleaned)


def raise_if_shutdown() -> None:
    if _shutdown_event.is_set():
        raise ShutdownRequested("server shutdown requested")
    if is_job_cancel_requested():
        raise ShutdownRequested("job cancelled by user")


def is_transient_local_model_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _TRANSIENT_LOCAL_MODEL_MARKERS)


def classify_openai_exception(exc: Exception) -> type[Exception]:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return TransientError
    if isinstance(exc, BadRequestError) and is_transient_local_model_error(exc):
        return TransientError
    if isinstance(exc, (BadRequestError, AuthenticationError)):
        return PermanentError
    raise exc
