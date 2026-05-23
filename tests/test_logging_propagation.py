from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.log import INFO_LOG_LEVEL, Log, bind_log_context, logInit, reset_log_context
from src.ingestion.orchestrator import run_pipeline
from src.models.request import PageRange, ReicatMetadata, UsefulPagesEnumeration

SHA = "cafebabe" * 8
REQUEST_ID = "req-log-propagation-001"
PAGE_COUNT = 2

_P_RENDER = "src.ingestion.orchestrator.render_aligned_pdf_pages"
_P_RESOLVE = "src.ingestion.orchestrator.resolve_aligned_pdf_path_for_stage1"
_P_STAGE1 = "src.ingestion.orchestrator.run_stage1_ingest_step"


class InMemoryRegistry:
    def append_event(self, request_id: str, event: object) -> None:
        del request_id, event


def _enumeration(page_count: int = PAGE_COUNT) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=page_count, end=page_count),
    )


def _settings(data_root: str) -> MagicMock:
    settings = MagicMock()
    settings.data_root = data_root
    settings.processed_pdf_input_dir = str(Path(data_root) / "input" / "processed")
    settings.page_range_per_thread = 10
    settings.max_parallel_request = 1
    settings.retry_attempts = 0
    settings.ocr_languages = ["it", "en"]
    settings.ocr_use_gpu = False
    settings.ocr_gpu_device = "all"
    return settings


def _enriched() -> MagicMock:
    enriched = MagicMock()
    enriched.source_sha256 = SHA
    enriched.request.reicat = ReicatMetadata.model_validate(
        {"titolo": "Log Propagation Book", "autore": ["Author One"]}
    )
    enriched.request.schema_version = "1.0"
    return enriched


class TestLoggingPropagation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        logInit(INFO_LOG_LEVEL, log_dir=self.tmp / "logs")
        self.data_root = str(self.tmp / "data")
        self.sqlite_path = str(self.tmp / "biblioteca.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_pipeline_logs_include_request_id_and_source_sha256(self) -> None:
        from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result

        stage1_result = Stage1Result(
            pages=[
                Stage1PageResult(
                    aligned_page=1,
                    original_page=1,
                    txt_path="/tmp/p.0001.txt",
                    char_count=10,
                ),
                Stage1PageResult(
                    aligned_page=2,
                    original_page=2,
                    txt_path="/tmp/p.0002.txt",
                    char_count=10,
                ),
            ],
            skipped_existing=0,
            missing=[],
        )

        buffer = io.StringIO()
        with (
            patch(_P_RENDER, return_value=[(1, Path("/tmp/p.0001.png")), (2, Path("/tmp/p.0002.png"))]),
            patch(_P_RESOLVE, return_value=Path("/tmp/aligned.pdf")),
            patch(_P_STAGE1, return_value=stage1_result),
            patch("src.ingestion.orchestrator.create_pipeline_run"),
            patch("src.ingestion.orchestrator.mark_pipeline_run_finished"),
            redirect_stdout(buffer),
        ):
            asyncio.run(
                run_pipeline(
                    _enriched(),
                    None,
                    _enumeration(),
                    _settings(self.data_root),
                    self.sqlite_path,
                    InMemoryRegistry(),
                    REQUEST_ID,
                    skip_vision_editor=True,
                )
            )

        output = buffer.getvalue()
        self.assertIn(REQUEST_ID, output)
        self.assertIn(SHA, output)
        self.assertIn("'stage': 'pipeline'", output)
        self.assertIn("'event': 'start'", output)
        self.assertIn("'event': 'end'", output)
        self.assertIn("duration_ms", output)

    def test_log_inherits_bound_context(self) -> None:
        buffer = io.StringIO()
        request_token, sha_token = bind_log_context(
            request_id=REQUEST_ID,
            source_sha256=SHA,
        )
        try:
            with redirect_stdout(buffer):
                Log(INFO_LOG_LEVEL, "context bound")
        finally:
            reset_log_context(request_token, sha_token)

        output = buffer.getvalue()
        self.assertIn(REQUEST_ID, output)
        self.assertIn(SHA, output)
