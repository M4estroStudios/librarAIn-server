from __future__ import annotations

import threading

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


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def raise_if_shutdown() -> None:
    if _shutdown_event.is_set():
        raise ShutdownRequested("server shutdown requested")


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
