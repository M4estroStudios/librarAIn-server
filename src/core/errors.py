from __future__ import annotations

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


_TRANSIENT_LOCAL_MODEL_MARKERS = (
    "model reloaded",
    "model has crashed",
    "model is loading",
    "model unloaded",
)


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
