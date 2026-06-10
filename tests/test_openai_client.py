from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.errors import PermanentError, TransientError
from src.core.openai_client import (
    _client_states,
    _cached_clients,
    build_chat_completion_extra_body,
    build_openai_client,
    chat_completion_with_retry,
)


def _make_settings(
    base_url: str = "http://test.local/v1",
    api_key: str = "test-key",
    rate_limit: int = 60,
    retry: int = 2,
    timeout: int = 10,
) -> MagicMock:
    s = MagicMock()
    s.openai_base_url = base_url
    s.openai_api_key = api_key
    s.rate_limit_per_minute = rate_limit
    s.retry_attempts = retry
    s.timeout_seconds = timeout
    return s


def _make_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices[0].message.content = content
    return resp


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


class TestBuildOpenAIClient(unittest.TestCase):
    def setUp(self) -> None:
        _cached_clients.clear()

    def tearDown(self) -> None:
        _cached_clients.clear()

    def test_returns_openai_instance(self) -> None:
        import openai as _openai
        settings = _make_settings()
        client = build_openai_client(settings)
        self.assertIsInstance(client, _openai.OpenAI)

    def test_same_instance_on_repeated_call(self) -> None:
        settings = _make_settings()
        c1 = build_openai_client(settings)
        c2 = build_openai_client(settings)
        self.assertIs(c1, c2)

    def test_different_keys_yield_different_instances(self) -> None:
        c1 = build_openai_client(_make_settings(base_url="http://a.local/v1", api_key="key-a"))
        c2 = build_openai_client(_make_settings(base_url="http://b.local/v1", api_key="key-b"))
        self.assertIsNot(c1, c2)

    def test_state_registered_for_client(self) -> None:
        settings = _make_settings(retry=5)
        client = build_openai_client(settings)
        state = _client_states.get(client)
        self.assertIsNotNone(state)
        self.assertEqual(state.retry_attempts, 5)


class TestChatCompletionWithRetry(unittest.TestCase):
    def setUp(self) -> None:
        _cached_clients.clear()

    def tearDown(self) -> None:
        _cached_clients.clear()

    def _build_client(self, retry: int = 2) -> object:
        settings = _make_settings(retry=retry)
        return build_openai_client(settings)

    def _call(self, client: object, create_mock: object) -> str:
        client.chat.completions.create = create_mock  # type: ignore[attr-defined]
        return _run(
            chat_completion_with_retry(
                client,  # type: ignore[arg-type]
                model="gpt-4",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                request_id="req-001",
                stage="test",
                page=1,
            )
        )  # type: ignore[return-value]

    def test_success_first_attempt(self) -> None:
        client = self._build_client()
        mock_create = MagicMock(return_value=_make_response("hello"))
        with patch("asyncio.sleep", new=AsyncMock()):
            result = self._call(client, mock_create)
        self.assertEqual(result, "hello")
        mock_create.assert_called_once()

    def test_transient_then_success(self) -> None:
        client = self._build_client(retry=2)
        mock_create = MagicMock(
            side_effect=[TransientError("rate limit"), _make_response("ok")]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = self._call(client, mock_create)
        self.assertEqual(result, "ok")
        self.assertEqual(mock_create.call_count, 2)

    def test_permanent_error_no_retry(self) -> None:
        client = self._build_client(retry=3)
        mock_create = MagicMock(side_effect=PermanentError("auth failed"))
        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(PermanentError):
                self._call(client, mock_create)
        mock_create.assert_called_once()

    def test_exhausted_retries_reraise_last_transient(self) -> None:
        client = self._build_client(retry=2)
        mock_create = MagicMock(side_effect=TransientError("timeout"))
        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TransientError):
                self._call(client, mock_create)
        self.assertEqual(mock_create.call_count, 3)

    def test_rate_limiter_no_deadlock(self) -> None:
        client = self._build_client()
        mock_create = MagicMock(return_value=_make_response("ok"))
        with patch("asyncio.sleep", new=AsyncMock()):
            result = self._call(client, mock_create)
        self.assertEqual(result, "ok")

    def test_empty_content_retries_then_raises(self) -> None:
        client = self._build_client(retry=1)
        mock_create = MagicMock(return_value=_make_response(""))
        with patch("asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ValueError):
                self._call(client, mock_create)
        self.assertEqual(mock_create.call_count, 2)

    def test_empty_content_retries_until_success(self) -> None:
        client = self._build_client(retry=2)
        mock_create = MagicMock(
            side_effect=[_make_response(""), _make_response(""), _make_response("ok")]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = self._call(client, mock_create)
        self.assertEqual(result, "ok")
        self.assertEqual(mock_create.call_count, 3)


class TestBuildChatCompletionExtraBody(unittest.TestCase):
    def test_empty_when_reasoning_not_configured(self) -> None:
        self.assertIsNone(build_chat_completion_extra_body())

    def test_includes_reasoning_effort(self) -> None:
        extra = build_chat_completion_extra_body(reasoning_effort="low")
        self.assertEqual(extra, {"reasoning": {"effort": "low"}})

    def test_includes_enable_thinking(self) -> None:
        extra = build_chat_completion_extra_body(reasoning_enable_thinking=False)
        self.assertEqual(extra, {"enable_thinking": False})

    def test_includes_both_reasoning_controls(self) -> None:
        extra = build_chat_completion_extra_body(
            reasoning_effort="medium",
            reasoning_enable_thinking=True,
        )
        self.assertEqual(
            extra,
            {"reasoning": {"effort": "medium"}, "enable_thinking": True},
        )


class TestChatCompletionReasoningParams(unittest.TestCase):
    def setUp(self) -> None:
        _cached_clients.clear()

    def tearDown(self) -> None:
        _cached_clients.clear()

    def test_passes_extra_body_to_create(self) -> None:
        client = build_openai_client(_make_settings())
        mock_create = MagicMock(return_value=_make_response("ok"))
        client.chat.completions.create = mock_create  # type: ignore[attr-defined]
        _run(
            chat_completion_with_retry(
                client,  # type: ignore[arg-type]
                model="gpt-4",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                request_id="req-001",
                stage="test",
                page=1,
                reasoning_effort="low",
                reasoning_enable_thinking=False,
            )
        )
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(
            kwargs["extra_body"],
            {"reasoning": {"effort": "low"}, "enable_thinking": False},
        )


if __name__ == "__main__":
    unittest.main()
