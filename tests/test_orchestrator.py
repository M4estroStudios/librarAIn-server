from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.output_writer import BookOutput
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
_P_BUILD_BOOK = "src.ingestion.orchestrator.build_book_md"
_P_BUILD_TOC = "src.ingestion.orchestrator.build_toc_md"
_P_BUILD_INDEX = "src.ingestion.orchestrator.build_index_md"
_P_SYNC_POLYINDEX_TOC = "src.ingestion.orchestrator.sync_polyindex_toc_from_book"
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

    @patch(_P_SYNC_POLYINDEX_TOC)
    @patch(_P_BUILD_INDEX)
    @patch(_P_BUILD_TOC)
    @patch(_P_BUILD_BOOK)
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
        mock_build_book: MagicMock,
        mock_build_toc: MagicMock,
        mock_build_index: MagicMock,
        mock_sync_polyindex_toc: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path(self.tmp / "aligned.pdf")
        mock_render.side_effect = self._fake_render
        mock_stage1.return_value = _stage1_result(PAGE_COUNT)
        stage3_pages = [MagicMock(aligned_page=i) for i in range(1, PAGE_COUNT + 1)]
        mock_stage2.return_value = MagicMock(pages=[])
        mock_stage3.return_value = MagicMock(pages=stage3_pages)
        mock_output.return_value = MagicMock(pages=[MagicMock()] * PAGE_COUNT, manifest_path=Path("/tmp/manifest.json"))
        mock_client.return_value = MagicMock()
        mock_build_book.return_value = self.tmp / "book.md"
        mock_build_toc.return_value = self.tmp / "TOC.md"
        mock_build_index.return_value = self.tmp / "INDEX.md"
        mock_sync_polyindex_toc.return_value = Path(self.data_root) / "polyindex" / "TOC.json"

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

        mock_build_book.assert_called_once()
        mock_build_toc.assert_called_once()
        mock_build_index.assert_called_once()
        mock_sync_polyindex_toc.assert_called_once()
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

    @patch(_P_SYNC_POLYINDEX_TOC)
    @patch(_P_BUILD_INDEX)
    @patch(_P_BUILD_TOC)
    @patch(_P_BUILD_BOOK)
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
        mock_build_book: MagicMock,
        mock_build_toc: MagicMock,
        mock_build_index: MagicMock,
        mock_sync_polyindex_toc: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path(self.tmp / "aligned.pdf")
        mock_render.side_effect = self._fake_render
        mock_stage1.side_effect = self._stage1_side_effect
        mock_stage2.side_effect = self._stage2
        mock_stage3.side_effect = self._stage3
        mock_output.return_value = MagicMock(pages=[MagicMock()], manifest_path=Path("/tmp/manifest.json"))
        mock_client.return_value = MagicMock()
        mock_build_book.return_value = self.tmp / "book.md"
        mock_build_toc.return_value = self.tmp / "TOC.md"
        mock_build_index.return_value = self.tmp / "INDEX.md"
        mock_sync_polyindex_toc.return_value = Path(self.data_root) / "polyindex" / "TOC.json"

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


class TestOrchestratorBuildsTocMd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _settings(self.data_root)
        self.registry = InMemoryRegistry()
        self.builder_call_order: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_render(self, aligned_path: Path, target_dir: Path, dpi: int) -> list[tuple[int, Path]]:
        del aligned_path, dpi
        render_dir = target_dir / SHA / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        png_path = render_dir / "p.0001.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [(1, png_path)]

    def _book_md_side_effect(self, *args, **kwargs) -> Path:
        del args, kwargs
        self.builder_call_order.append("book_md")
        return self.tmp / "book.md"

    def _toc_md_side_effect(self, *args, **kwargs) -> Path:
        del args, kwargs
        self.builder_call_order.append("toc_md")
        return self.tmp / "TOC.md"

    def _index_md_side_effect(self, *args, **kwargs) -> Path:
        del args, kwargs
        self.builder_call_order.append("index_md")
        return self.tmp / "INDEX.md"

    def _polyindex_toc_side_effect(self, *args, **kwargs) -> Path:
        del args, kwargs
        self.builder_call_order.append("polyindex_toc")
        return Path(self.data_root) / "polyindex" / "TOC.json"

    @patch(_P_SYNC_POLYINDEX_TOC)
    @patch(_P_BUILD_INDEX)
    @patch(_P_BUILD_TOC)
    @patch(_P_BUILD_BOOK)
    @patch(_P_CLIENT)
    @patch(_P_SWAP)
    @patch(_P_OUTPUT)
    @patch(_P_STAGE3, new_callable=AsyncMock)
    @patch(_P_STAGE2, new_callable=AsyncMock)
    @patch(_P_STAGE1, new_callable=AsyncMock)
    @patch(_P_RENDER)
    @patch(_P_RESOLVE)
    def test_run_pipeline_calls_build_toc_md_after_book_md(
        self,
        mock_resolve: MagicMock,
        mock_render: MagicMock,
        mock_stage1: AsyncMock,
        mock_stage2: AsyncMock,
        mock_stage3: AsyncMock,
        mock_output: MagicMock,
        mock_swap: MagicMock,
        mock_client: MagicMock,
        mock_build_book: MagicMock,
        mock_build_toc: MagicMock,
        mock_build_index: MagicMock,
        mock_sync_polyindex_toc: MagicMock,
    ) -> None:
        mock_resolve.return_value = Path(self.tmp / "aligned.pdf")
        mock_render.side_effect = self._fake_render
        mock_stage1.return_value = _stage1_result(1)
        mock_stage2.return_value = MagicMock(pages=[])
        mock_stage3.return_value = MagicMock(pages=[MagicMock(aligned_page=1)])
        book_output = MagicMock(
            pages=[MagicMock()],
            manifest_path=Path(self.tmp / "manifest.json"),
        )
        mock_output.return_value = book_output
        mock_client.return_value = MagicMock()
        mock_build_book.side_effect = self._book_md_side_effect
        mock_build_toc.side_effect = self._toc_md_side_effect
        mock_build_index.side_effect = self._index_md_side_effect
        mock_sync_polyindex_toc.side_effect = self._polyindex_toc_side_effect

        useful_pages = _enumeration(page_count=1)

        asyncio.run(
            run_pipeline(
                _enriched(),
                None,
                useful_pages,
                self.settings,
                Path(self.data_root) / "db" / "biblioteca.db",
                self.registry,
                REQUEST_ID,
            )
        )

        mock_build_book.assert_called_once_with(book_output, useful_pages)
        mock_build_toc.assert_called_once_with(book_output, useful_pages)
        mock_build_index.assert_called_once_with(book_output, useful_pages)
        mock_sync_polyindex_toc.assert_called_once_with(
            Path(self.data_root) / "polyindex",
            SHA,
            book_output,
            self.tmp / "TOC.md",
            useful_pages,
        )
        self.assertEqual(
            self.builder_call_order,
            ["book_md", "toc_md", "index_md", "polyindex_toc"],
        )
        toc_events = [event for event in self.registry.events if event.stage == "toc_builder"]
        self.assertEqual(len(toc_events), 1)
        self.assertEqual(toc_events[0].payload, {"toc_md_path": str(self.tmp / "TOC.md")})
        index_events = [event for event in self.registry.events if event.stage == "index_builder"]
        self.assertEqual(len(index_events), 1)
        self.assertEqual(index_events[0].payload, {"index_md_path": str(self.tmp / "INDEX.md")})
        polyindex_events = [
            event for event in self.registry.events if event.stage == "polyindex_toc"
        ]
        self.assertEqual(len(polyindex_events), 1)
        self.assertEqual(
            polyindex_events[0].payload,
            {"toc_json_path": str(Path(self.data_root) / "polyindex" / "TOC.json")},
        )


class TestOrchestratorWritesPolyindexTocJson(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = self.tmp / "data"
        self.settings = _settings(str(self.data_root))
        self.registry = InMemoryRegistry()
        self.output_dir = self.data_root / "output" / SHA
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_dir / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "slug": "test-book",
                    "reicat": {"titolo": "Test Book", "autore": ["Author One"]},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.toc_md_path = self.output_dir / "TOC.md"
        self.toc_md_path.write_text(
            "\n".join(
                [
                    "# TOC — Test Book",
                    "",
                    "Introduzione generale 3",
                    "Capitolo I 14",
                ]
            ),
            encoding="utf-8",
        )
        self.book_output = BookOutput(
            output_dir=self.output_dir,
            manifest_path=self.manifest_path,
            slug="test-book",
            pages=[],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_render(self, aligned_path: Path, target_dir: Path, dpi: int) -> list[tuple[int, Path]]:
        del aligned_path, dpi
        render_dir = target_dir / SHA / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        png_path = render_dir / "p.0001.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [(1, png_path)]

    def _toc_md_side_effect(self, book_output: BookOutput, useful_pages: object) -> Path:
        del book_output, useful_pages
        return self.toc_md_path

    @patch(_P_BUILD_INDEX)
    @patch(_P_BUILD_TOC)
    @patch(_P_BUILD_BOOK)
    @patch(_P_CLIENT)
    @patch(_P_SWAP)
    @patch(_P_OUTPUT)
    @patch(_P_STAGE3, new_callable=AsyncMock)
    @patch(_P_STAGE2, new_callable=AsyncMock)
    @patch(_P_STAGE1, new_callable=AsyncMock)
    @patch(_P_RENDER)
    @patch(_P_RESOLVE)
    def test_run_pipeline_writes_polyindex_toc_json_on_disk(
        self,
        mock_resolve: MagicMock,
        mock_render: MagicMock,
        mock_stage1: AsyncMock,
        mock_stage2: AsyncMock,
        mock_stage3: AsyncMock,
        mock_output: MagicMock,
        mock_swap: MagicMock,
        mock_client: MagicMock,
        mock_build_book: MagicMock,
        mock_build_toc: MagicMock,
        mock_build_index: MagicMock,
    ) -> None:
        mock_resolve.return_value = self.tmp / "aligned.pdf"
        mock_render.side_effect = self._fake_render
        mock_stage1.return_value = _stage1_result(1)
        mock_stage2.return_value = MagicMock(pages=[])
        mock_stage3.return_value = MagicMock(pages=[MagicMock(aligned_page=1)])
        mock_output.return_value = self.book_output
        mock_client.return_value = MagicMock()
        mock_build_book.return_value = self.output_dir / "test-book.md"
        mock_build_toc.side_effect = self._toc_md_side_effect
        mock_build_index.return_value = self.output_dir / "INDEX.md"

        useful_pages = _enumeration(page_count=100)

        asyncio.run(
            run_pipeline(
                _enriched(),
                None,
                useful_pages,
                self.settings,
                self.data_root / "db" / "biblioteca.db",
                self.registry,
                REQUEST_ID,
            )
        )

        toc_json_path = self.data_root / "polyindex" / "TOC.json"
        self.assertTrue(toc_json_path.is_file())

        data = json.loads(toc_json_path.read_text(encoding="utf-8"))
        book = data["books"][SHA]
        self.assertEqual(book["title"], "Test Book")
        self.assertEqual(book["slug"], "test-book")
        self.assertEqual(len(book["chapters"]), 2)
        self.assertEqual(book["chapters"][0]["label"], "Introduzione generale")
        self.assertEqual(book["chapters"][1]["label"], "Capitolo I")

        polyindex_events = [
            event for event in self.registry.events if event.stage == "polyindex_toc"
        ]
        self.assertEqual(len(polyindex_events), 1)
        self.assertEqual(polyindex_events[0].payload, {"toc_json_path": str(toc_json_path)})


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
