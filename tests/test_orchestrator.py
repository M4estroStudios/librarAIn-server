from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result
from src.models.request import PageRange, ReicatMetadata, UsefulPagesEnumeration
from src.ingestion.orchestrator import (
    PAGE_STATUS_COMPLETED,
    IngestJobEvent,
    run_pipeline,
)

SHA = "deadbeef" * 8
REQUEST_ID = "req-orchestrator-001"
PAGE_COUNT = 8

_P_RENDER = "src.ingestion.orchestrator.render_aligned_pdf_pages"
_P_RESOLVE = "src.ingestion.orchestrator.resolve_aligned_pdf_path_for_stage1"
_P_STAGE1 = "src.ingestion.orchestrator.run_stage1_ingest_step"
_P_STAGE2 = "src.ingestion.orchestrator.run_stage2_vision"
_P_STAGE3 = "src.ingestion.orchestrator.run_stage3_editor"
_P_OUTPUT = "src.ingestion.orchestrator.materialize_book_pages"
_P_CLIENT = "src.ingestion.orchestrator.build_openai_client"
_P_SWAP = "src.ingestion.orchestrator.swap_lmstudio_vision_to_editor"


class InMemoryRegistry:
    def __init__(self) -> None:
        self.events: list[IngestJobEvent] = []

    def append_event(self, request_id: str, event: IngestJobEvent) -> None:
        self.events.append(event)


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


def _settings(data_root: str, max_parallel: int = 3) -> MagicMock:
    settings = MagicMock()
    settings.data_root = data_root
    settings.processed_pdf_input_dir = str(Path(data_root) / "input" / "processed")
    settings.page_range_per_thread = 10
    settings.max_parallel_request = max_parallel
    settings.retry_attempts = 0
    settings.ocr_languages = ["it", "en"]
    settings.ocr_use_gpu = False
    settings.ocr_gpu_device = "all"
    settings.vision_model = "vision-model"
    settings.editor_model = "editor-model"
    return settings


def _enriched() -> MagicMock:
    enriched = MagicMock()
    enriched.source_sha256 = SHA
    enriched.request.reicat = ReicatMetadata.model_validate(
        {"titolo": "Test Book", "autore": ["Author One"]}
    )
    enriched.request.schema_version = "1.0"
    return enriched


def _stage1_result(page_count: int) -> Stage1Result:
    pages = [
        Stage1PageResult(
            aligned_page=i,
            original_page=i,
            txt_path=f"/tmp/p.{i:04d}.txt",
            char_count=10,
        )
        for i in range(1, page_count + 1)
    ]
    return Stage1Result(pages=pages, skipped_existing=0, missing=[])


class TestOrchestratorUsesPipelineStages(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _settings(self.data_root)
        self.registry = InMemoryRegistry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_render(self, aligned_path: Path, target_dir: Path, dpi: int) -> list[tuple[int, Path]]:
        del aligned_path, dpi
        render_dir = target_dir / SHA / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[tuple[int, Path]] = []
        for page in range(1, PAGE_COUNT + 1):
            png_path = render_dir / f"p.{page:04d}.png"
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            rendered.append((page, png_path))
        return rendered

    @patch(_P_CLIENT)
    @patch(_P_SWAP)
    @patch(_P_OUTPUT)
    @patch(_P_STAGE3, new_callable=AsyncMock)
    @patch(_P_STAGE2, new_callable=AsyncMock)
    @patch(_P_STAGE1, new_callable=AsyncMock)
    @patch(_P_RENDER)
    @patch(_P_RESOLVE)
    def test_run_pipeline_calls_pipeline_stage1(
        self,
        mock_resolve: MagicMock,
        mock_render: MagicMock,
        mock_stage1: AsyncMock,
        mock_stage2: AsyncMock,
        mock_stage3: AsyncMock,
        mock_output: MagicMock,
        mock_swap: MagicMock,
        mock_client: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path(self.tmp / "aligned.pdf")
        mock_render.side_effect = self._fake_render
        mock_stage1.return_value = _stage1_result(PAGE_COUNT)
        stage3_pages = [MagicMock(aligned_page=i) for i in range(1, PAGE_COUNT + 1)]
        mock_stage2.return_value = MagicMock(pages=[])
        mock_stage3.return_value = MagicMock(pages=stage3_pages)
        mock_output.return_value = MagicMock(pages=[MagicMock()] * PAGE_COUNT, manifest_path=Path("/tmp/manifest.json"))
        mock_client.return_value = MagicMock()

        result = asyncio.run(
            run_pipeline(
                _enriched(),
                None,
                _enumeration(),
                self.settings,
                Path(self.data_root) / "db" / "biblioteca.db",
                self.registry,
                REQUEST_ID,
            )
        )

        mock_stage1.assert_awaited_once()
        mock_stage2.assert_awaited_once()
        mock_stage3.assert_awaited_once()
        mock_output.assert_called_once()
        self.assertEqual(result.completed_count, PAGE_COUNT)
        self.assertTrue(all(job.status == PAGE_STATUS_COMPLETED for job in result.page_jobs))
        self.assertTrue(any(event.stage == "render" for event in self.registry.events))
        self.assertTrue(any(event.stage == "stage1" for event in self.registry.events))


class TestOrchestratorStageOrdering(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _settings(self.data_root)
        self.registry = InMemoryRegistry()
        self.call_log: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_render(self, aligned_path: Path, target_dir: Path, dpi: int) -> list[tuple[int, Path]]:
        del aligned_path, dpi
        render_dir = target_dir / SHA / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        return [(1, render_dir / "p.0001.png")]

    def _stage1_side_effect(self, *args, **kwargs) -> Stage1Result:
        del args, kwargs
        self.call_log.append("stage1")
        return _stage1_result(1)

    async def _stage2(self, *args, **kwargs) -> MagicMock:
        del args, kwargs
        self.call_log.append("stage2")
        result = MagicMock()
        result.pages = [MagicMock(aligned_page=1, original_page=1, md_path="/tmp/x.md")]
        return result

    async def _stage3(self, *args, **kwargs) -> MagicMock:
        del args, kwargs
        self.call_log.append("stage3")
        result = MagicMock()
        result.pages = [MagicMock(aligned_page=1)]
        return result

    @patch(_P_CLIENT)
    @patch(_P_SWAP)
    @patch(_P_OUTPUT)
    @patch(_P_STAGE3, new_callable=AsyncMock)
    @patch(_P_STAGE2, new_callable=AsyncMock)
    @patch(_P_STAGE1, new_callable=AsyncMock)
    @patch(_P_RENDER)
    @patch(_P_RESOLVE)
    def test_stages_run_batch_order(
        self,
        mock_resolve: MagicMock,
        mock_render: MagicMock,
        mock_stage1: AsyncMock,
        mock_stage2: AsyncMock,
        mock_stage3: AsyncMock,
        mock_output: MagicMock,
        mock_swap: MagicMock,
        mock_client: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path(self.tmp / "aligned.pdf")
        mock_render.side_effect = self._fake_render
        mock_stage1.side_effect = self._stage1_side_effect
        mock_stage2.side_effect = self._stage2
        mock_stage3.side_effect = self._stage3
        mock_output.return_value = MagicMock(pages=[MagicMock()], manifest_path=Path("/tmp/manifest.json"))
        mock_client.return_value = MagicMock()

        asyncio.run(
            run_pipeline(
                _enriched(),
                None,
                _enumeration(page_count=1),
                self.settings,
                Path(self.data_root) / "db" / "biblioteca.db",
                self.registry,
                REQUEST_ID,
            )
        )

        self.assertEqual(self.call_log, ["stage1", "stage2", "stage3"])


class TestOrchestratorMaxParallelFromEnv(unittest.TestCase):
    def test_settings_max_parallel_request_from_env(self) -> None:
        from src.core.config import load_settings

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DATA_ROOT=data",
                        "OPENAI_PROVIDER=local",
                        "MAX_PARALLEL_REQUEST=3",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                settings = load_settings(str(env_path))

        self.assertEqual(settings.max_parallel_request, 3)
