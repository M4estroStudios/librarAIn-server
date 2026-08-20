from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.openai_client import _cached_clients, build_openai_client, use_compute_mode
from src.core.openai_client_sync import chat_completion_with_retry_sync
from src.persistence.book_sqlite import init_books_schema
from src.persistence.llm_metrics import (
    aggregate_llm_call_metrics,
    list_llm_call_metrics,
    record_llm_call_metric,
)


class TestLlmMetrics(unittest.TestCase):
    def tearDown(self) -> None:
        _cached_clients.clear()

    def test_aggregate_groups_retries_by_logical_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            common = {
                "sqlite_path": sqlite_path,
                "request_id": "request-1",
                "source_sha256": "a" * 64,
                "stage": "stage3_editor",
                "operation": "chat",
                "compute_mode": "local",
                "requested_compute_mode": "local",
                "model": "editor-local",
                "unit_index": 1,
                "started_at": "2026-08-13T10:00:00+00:00",
                "finished_at": "2026-08-13T10:00:01+00:00",
            }
            self.assertTrue(
                record_llm_call_metric(
                    **common,
                    call_id="call-1",
                    attempt=0,
                    status="error",
                    latency_ms=100,
                    input_chars=1000,
                    error_type="TransientError",
                )
            )
            self.assertTrue(
                record_llm_call_metric(
                    **common,
                    call_id="call-1",
                    attempt=1,
                    status="success",
                    latency_ms=200,
                    input_chars=1000,
                    output_chars=500,
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                )
            )
            self.assertTrue(
                record_llm_call_metric(
                    **{**common, "unit_index": 2},
                    call_id="call-2",
                    attempt=0,
                    status="success",
                    latency_ms=200,
                    output_chars=400,
                    prompt_tokens=80,
                    completion_tokens=40,
                    total_tokens=120,
                )
            )
            rows = list_llm_call_metrics(sqlite_path)
            self.assertEqual(len(rows), 3)
            groups = aggregate_llm_call_metrics(sqlite_path)
            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertEqual(group["logical_calls"], 2)
            self.assertEqual(group["attempts"], 3)
            self.assertEqual(group["retry_calls"], 1)
            self.assertEqual(group["successful_calls"], 2)
            self.assertEqual(group["success_rate"], 1.0)
            self.assertEqual(group["completion_tokens"], 90)
            self.assertEqual(group["latency_ms_p50"], 200.0)
            self.assertEqual(group["pages_processed"], 2)
            self.assertEqual(group["pages_per_second"], 2.0)

    def test_chat_wrapper_persists_mode_model_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_path = str(Path(tmp_dir) / "biblioteca.db")
            init_books_schema(sqlite_path)
            settings = MagicMock()
            settings.openai_base_url = "http://local.test/v1"
            settings.openai_api_key = "test"
            settings.rate_limit_per_minute = 60000
            settings.retry_attempts = 0
            settings.timeout_seconds = 10
            settings.research_timeout_seconds = 10
            settings.max_parallel_request = 1
            settings.sqlite_path = sqlite_path
            client = build_openai_client(settings)
            response = MagicMock()
            response.id = "response-1"
            response.choices[0].message.content = "output"
            response.usage.prompt_tokens = 12
            response.usage.completion_tokens = 7
            response.usage.total_tokens = 19
            client.chat.completions.create = MagicMock(return_value=response)  # type: ignore[attr-defined]
            with use_compute_mode("local", settings):
                result = chat_completion_with_retry_sync(
                    client,
                    model="editor-local",
                    messages=[{"role": "user", "content": "input"}],
                    max_tokens=100,
                    request_id="request-2",
                    stage="stage3_editor",
                    page=4,
                )
            self.assertEqual(result, "output")
            rows = list_llm_call_metrics(sqlite_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["compute_mode"], "local")
            self.assertEqual(rows[0]["model"], "editor-local")
            self.assertEqual(rows[0]["total_tokens"], 19)
            self.assertEqual(rows[0]["output_chars"], 6)


if __name__ == "__main__":
    unittest.main()
