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


def classify_openai_exception(exc: Exception) -> type[Exception]:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return TransientError
    if isinstance(exc, (BadRequestError, AuthenticationError)):
        return PermanentError
    raise exc
