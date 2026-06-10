from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.rate_limit import AsyncTokenBucket
from src.core.openai_client import _ClientState, _client_states
from src.ingestion.pipeline.stage1 import Stage1PageResult, Stage1Result
from src.ingestion.pipeline.stage2 import Stage2PageResult, Stage2Result
from src.ingestion.pipeline.stage3 import Stage3PageResult, Stage3Result
from src.ingestion.progress import (
    PHASE_STAGE1_OCR, PHASE_STAGE2_VISION, PHASE_STAGE3_EDITOR,
    STATUS_COMPLETED, STATUS_DONE, STATUS_ERROR, STATUS_STARTED,
    make_event,
)

from src.api.ingest_pipeline_runner import run_full_pipeline
from src.ingestion.orchestrator import OrchestratorResult, OrchestratorStageError

SHA = "aabbccdd" * 8
REQUEST_ID = "req-test-001"

_P_VALIDATE = "src.api.ingest_pipeline_runner.validate_and_enrich_request"
_P_GATE = "src.api.ingest_pipeline_runner.run_ingest_gate_phase"
_P_ALIGN = "src.api.ingest_pipeline_runner.maybe_run_pdf_alignment"
_P_ENUM = "src.api.ingest_pipeline_runner.build_useful_pages_enumeration"
_P_ORCH = "src.api.ingest_pipeline_runner.run_pipeline"
_P_STAGE1_ORCH = "src.ingestion.orchestrator.run_stage1_ingest_step"
_P_STAGE3 = "src.ingestion.orchestrator.run_stage3_editor"
_P_CLIENT = "src.ingestion.orchestrator.build_openai_client"


def _make_enriched(sha: str = SHA) -> MagicMock:
    from src.models.request import ReicatMetadata

    m = MagicMock()
    m.source_sha256 = sha
    m.request.request_id = REQUEST_ID
    m.request.schema_version = "1.0"
    m.request.reicat = ReicatMetadata.model_validate(
        {"titolo": "Book", "autore": ["Author One"]}
    )
    m.model_dump.return_value = {"source_sha256": sha}
    return m


def _make_gate(pipeline_skipped: bool = False) -> MagicMock:
    m = MagicMock()
    m.pipeline_skipped = pipeline_skipped
    m.gate.status.value = "new_hash"
    m.model_dump.return_value = {"pipeline_skipped": pipeline_skipped}
    return m


def _make_pages_enum(n: int = 2):
    from src.models.request import PageRange, UsefulPagesEnumeration

    original_pages = list(range(1, n + 1))
    mapping = {i: i for i in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=n,
        aligned_page_count=n,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page=dict(mapping),
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=n, end=n),
    )


def _make_stage1_result(data_root: str, sha: str = SHA, n: int = 2) -> Stage1Result:
    ocr_dir = Path(data_root) / "tmp" / sha / "stage1OCR"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for i in range(1, n + 1):
        p = ocr_dir / f"p.{i:04d}.book.txt"
        p.write_text(f"ocr text page {i}", encoding="utf-8")
        pages.append(Stage1PageResult(
            aligned_page=i,
            original_page=i,
            txt_path=str(p),
            char_count=len(f"ocr text page {i}"),
        ))
    return Stage1Result(pages=pages, skipped_existing=0, missing=[])


def _make_stage2_result(n: int = 2, skipped: int = 0) -> Stage2Result:
    pages = [
        Stage2PageResult(
            aligned_page=i,
            original_page=i,
            md_path=f"/tmp/fake/p.{i:04d}.book.md",
            char_count=100,
        )
        for i in range(1, n + 1)
    ]
    return Stage2Result(pages=pages, skipped_existing=skipped, missing=[])


def _fake_client(content: str = "# REFINED") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = content
    client.chat.completions.create.return_value = resp
    _client_states[client] = _ClientState(
        token_bucket=AsyncTokenBucket(60),
        retry_attempts=0,
    )
    return client


def _make_stage3_result(n: int = 2, skipped: int = 0) -> Stage3Result:
    pages = [
        Stage3PageResult(
            aligned_page=i, original_page=i,
            md_path=f"/tmp/fake/p.{i:04d}.book.md",
            char_count=80, stage2_char_count=100, char_delta=-20,
        )
        for i in range(1, n + 1)
    ]
    return Stage3Result(pages=pages, skipped_existing=skipped, missing=[])


def _make_settings(data_root: str, vision_model: str = "test-model", editor_model: str = "test-editor-model") -> MagicMock:
    s = MagicMock()
    s.data_root = data_root
    s.sqlite_path = str(Path(data_root) / "db" / "biblioteca.db")
    s.vision_model = vision_model
    s.editor_model = editor_model
    s.max_parallel_request = 2
    return s


async def _fake_run_pipeline(
    enriched,
    alignment,
    useful_pages,
    settings,
    sqlite_path,
    registry,
    request_id,
    *,
    progress=None,
    skip_vision_editor=False,
):
    del alignment, sqlite_path, registry, request_id
    sha = enriched.source_sha256
    n = len(useful_pages.useful_original_pages)
    stage1 = _make_stage1_result(settings.data_root, sha=sha, n=n)
    if skip_vision_editor:
        if progress is not None:
            progress(make_event(PHASE_STAGE1_OCR, STATUS_STARTED))
            progress(make_event(PHASE_STAGE1_OCR, STATUS_COMPLETED))
        return OrchestratorResult(
            page_jobs=[],
            rendered_page_count=n,
            stage1_result=stage1,
            completed_count=n,
            failed_count=0,
        )
    stage2 = _make_stage2_result(n)
    stage3 = _make_stage3_result(n)
    if progress is not None:
        progress(make_event(PHASE_STAGE1_OCR, STATUS_STARTED))
        progress(make_event(PHASE_STAGE1_OCR, STATUS_COMPLETED))
        progress(make_event(PHASE_STAGE2_VISION, STATUS_STARTED))
        progress(make_event(PHASE_STAGE2_VISION, STATUS_COMPLETED))
        progress(make_event(PHASE_STAGE3_EDITOR, STATUS_STARTED))
    return OrchestratorResult(
        page_jobs=[],
        rendered_page_count=n,
        stage1_result=stage1,
        stage2_result=stage2,
        stage3_result=stage3,
        completed_count=n,
        failed_count=0,
    )


class TestHappyPath(unittest.TestCase):
    """Test 1: 2 pages, Stage 1 mocked, Stage 2 + Stage 3 mocked via AsyncMock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _make_settings(self.data_root)
        self.stage1 = _make_stage1_result(self.data_root, n=2)
        self.stage2 = _make_stage2_result(n=2)
        self.stage3 = _make_stage3_result(n=2)
        self.events: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reporter(self, ev: dict) -> None:
        self.events.append(ev)

    def _run(self, mock_validate, mock_gate, mock_align, mock_enum,
             mock_orch, set_total: MagicMock | None = None) -> dict:
        mock_validate.return_value = _make_enriched()
        mock_gate.return_value = _make_gate(pipeline_skipped=False)
        mock_align.return_value = None
        mock_enum.return_value = _make_pages_enum(n=2)
        mock_orch.side_effect = _fake_run_pipeline
        return run_full_pipeline(
            {}, Path(self.tmp / "fake.pdf"), self.settings,
            reporter=self._reporter,
            set_global_total=set_total,
        )

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_payload_contains_stage1_and_stage2(
        self, mv, mg, ma, me, morch
    ) -> None:
        result = self._run(mv, mg, ma, me, morch)
        self.assertIn("stage1", result)
        self.assertIn("stage2", result)
        self.assertIn("stage3", result)
        self.assertIsNotNone(result["stage2"])
        self.assertIsNotNone(result["stage3"])
        self.assertEqual(len(result["stage1"]["pages"]), 2)
        self.assertEqual(len(result["stage2"]["pages"]), 2)
        self.assertEqual(len(result["stage3"]["pages"]), 2)

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_set_global_total_alignment_plus_2x3(
        self, mv, mg, ma, me, morch
    ) -> None:
        set_total = MagicMock()
        self._run(mv, mg, ma, me, morch, set_total=set_total)
        set_total.assert_called_once_with(7)

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_event_sequence(
        self, mv, mg, ma, me, morch
    ) -> None:
        self._run(mv, mg, ma, me, morch)
        ps = [(e["phase"], e["status"]) for e in self.events]

        self.assertIn(("stage1_ocr", "started"), ps)
        self.assertIn(("stage1_ocr", "completed"), ps)
        self.assertNotIn(("stage1_ocr", "done"), ps)
        self.assertIn(("stage3_editor", "done"), ps)

        morch.assert_called_once()
        self.assertIsNotNone(morch.call_args.kwargs.get("progress"))

        def idx(phase: str, status: str) -> int:
            for i, (p, s) in enumerate(ps):
                if p == phase and s == status:
                    return i
            return -1

        self.assertLess(idx("stage1_ocr", "started"), idx("stage1_ocr", "completed"))
        self.assertLess(idx("stage1_ocr", "completed"), idx("stage3_editor", "done"))

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_stage3_editor_done_carries_full_payload(
        self, mv, mg, ma, me, morch
    ) -> None:
        self._run(mv, mg, ma, me, morch)
        done_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE3_EDITOR and e.get("status") == STATUS_DONE
        ]
        self.assertEqual(len(done_evs), 1)
        result_payload = done_evs[0]["result"]
        self.assertIn("stage1", result_payload)
        self.assertIn("stage2", result_payload)
        self.assertIn("stage3", result_payload)
        self.assertIsNotNone(result_payload["stage2"])
        self.assertIsNotNone(result_payload["stage3"])

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_run_pipeline_called_once(
        self, mv, mg, ma, me, morch
    ) -> None:
        self._run(mv, mg, ma, me, morch)
        morch.assert_called_once()
        self.assertTrue(morch.call_args.kwargs.get("skip_vision_editor") is False)


class TestSkipDuplicate(unittest.TestCase):
    """Test 2: pipeline_skipped=True → Stage 2 not invoked, stage2=None."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _make_settings(self.data_root)
        self.stage1 = _make_stage1_result(self.data_root, n=2)
        self.events: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reporter(self, ev: dict) -> None:
        self.events.append(ev)

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_stage2_not_called(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=True)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = _fake_run_pipeline

        result = run_full_pipeline(
            {}, Path(self.tmp / "fake.pdf"), self.settings,
            reporter=self._reporter, set_global_total=None,
        )

        morch.assert_called_once()
        self.assertTrue(morch.call_args.kwargs.get("skip_vision_editor"))
        self.assertIsNone(result["stage2"])
        self.assertIsNone(result["stage3"])

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_no_stage2_vision_events(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=True)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = _fake_run_pipeline

        run_full_pipeline(
            {}, Path(self.tmp / "fake.pdf"), self.settings,
            reporter=self._reporter, set_global_total=None,
        )

        stage2_evs = [e for e in self.events if e.get("phase") == PHASE_STAGE2_VISION]
        self.assertEqual(len(stage2_evs), 0)
        stage3_evs = [e for e in self.events if e.get("phase") == PHASE_STAGE3_EDITOR]
        self.assertEqual(len(stage3_evs), 0)

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_stage1_ocr_done_terminates_pipeline_when_skipped(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=True)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = _fake_run_pipeline

        run_full_pipeline(
            {}, Path(self.tmp / "fake.pdf"), self.settings,
            reporter=self._reporter, set_global_total=None,
        )

        done_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE1_OCR and e.get("status") == STATUS_DONE
        ]
        self.assertEqual(len(done_evs), 1)
        self.assertIsNone(done_evs[0]["result"]["stage2"])
        self.assertIsNone(done_evs[0]["result"]["stage3"])


class TestCacheHit(unittest.TestCase):
    """Test 3: second run with same sha hits Stage 2 cache (skipped_existing == 2)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _make_settings(self.data_root)
        self.settings.processed_pdf_input_dir = str(Path(self.data_root) / "input" / "processed")
        self.settings.page_range_per_thread = 10
        self.settings.retry_attempts = 0
        self.settings.ocr_languages = ["it", "en"]
        self.settings.ocr_use_gpu = False
        self.settings.ocr_gpu_device = "all"
        _make_stage1_result(self.data_root, n=2)
        render_dir = Path(self.data_root) / "tmp" / SHA / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 3):
            (render_dir / f"p.{i:04d}.png").write_bytes(b"\x89PNG")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_pipeline(self, fake_openai_client: MagicMock) -> dict:
        enriched = _make_enriched()
        enriched.request.request_id = f"{REQUEST_ID}-{uuid.uuid4().hex[:8]}"
        with (
            patch(_P_VALIDATE, return_value=enriched),
            patch(_P_GATE, return_value=_make_gate(pipeline_skipped=False)),
            patch(_P_ALIGN, return_value=None),
            patch(_P_ENUM, return_value=_make_pages_enum(n=2)),
            patch(
                _P_STAGE1_ORCH,
                new_callable=AsyncMock,
                return_value=_make_stage1_result(self.data_root, n=2),
            ),
            patch(_P_CLIENT, return_value=fake_openai_client),
            patch("src.ingestion.orchestrator.swap_lmstudio_vision_to_editor"),
        ):
            return run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=lambda ev: None, set_global_total=None,
            )

    def test_second_run_all_pages_cached(self) -> None:
        self._run_pipeline(_fake_client())
        result2 = self._run_pipeline(_fake_client())
        stage2 = result2["stage2"]
        self.assertIsNotNone(stage2)
        self.assertEqual(stage2["skipped_existing"], 2)
        self.assertEqual(len(stage2["pages"]), 2)
        self.assertIsNotNone(result2["stage3"])
        self.assertEqual(len(result2["stage3"]["pages"]), 2)

    def test_second_run_no_openai_calls(self) -> None:
        self._run_pipeline(_fake_client())
        client2 = _fake_client()
        self._run_pipeline(client2)
        client2.chat.completions.create.assert_not_called()

    def test_md_files_present_after_first_run(self) -> None:
        self._run_pipeline(_fake_client())
        stage2_dir = Path(self.data_root) / "tmp" / SHA / "stage2Vision"
        md_files = list(stage2_dir.glob("*.md"))
        self.assertEqual(len(md_files), 2)
        self.assertEqual(len(list(stage2_dir.glob("*.json"))), 0)


class TestStage2Error(unittest.TestCase):
    """Test 4: Stage 2 raises → STATUS_ERROR emitted on PHASE_STAGE2_VISION, exception re-raised."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _make_settings(self.data_root)
        self.events: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reporter(self, ev: dict) -> None:
        self.events.append(ev)

    async def _orch_stage2_fail(self, *args, **kwargs) -> OrchestratorResult:
        del args, kwargs
        raise OrchestratorStageError("stage2_vision", RuntimeError("vision API unreachable"))

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_status_error_emitted(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage2_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )

        error_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE2_VISION and e.get("status") == STATUS_ERROR
        ]
        self.assertEqual(len(error_evs), 1)
        self.assertIn("vision API unreachable", error_evs[0]["message"])

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_exception_propagates(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage2_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_no_stage2_done_event_on_error(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage2_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )

        done_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE2_VISION and e.get("status") == STATUS_DONE
        ]
        self.assertEqual(len(done_evs), 0)


class TestStage3Error(unittest.TestCase):
    """Test 5: Stage 3 raises → STATUS_ERROR on PHASE_STAGE3_EDITOR, exception re-raised."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_root = str(self.tmp / "data")
        self.settings = _make_settings(self.data_root)
        self.events: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reporter(self, ev: dict) -> None:
        self.events.append(ev)

    async def _orch_stage3_fail(self, *args, **kwargs) -> OrchestratorResult:
        del args, kwargs
        raise OrchestratorStageError("stage3_editor", RuntimeError("editor API unreachable"))

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_status_error_emitted_on_stage3(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage3_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )

        error_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE3_EDITOR and e.get("status") == STATUS_ERROR
        ]
        self.assertEqual(len(error_evs), 1)
        self.assertIn("editor API unreachable", error_evs[0]["message"])

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_no_stage3_done_event_on_error(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage3_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )

        done_evs = [
            e for e in self.events
            if e.get("phase") == PHASE_STAGE3_EDITOR and e.get("status") == STATUS_DONE
        ]
        self.assertEqual(len(done_evs), 0)

    @patch(_P_ORCH, new_callable=AsyncMock)
    @patch(_P_ENUM)
    @patch(_P_ALIGN)
    @patch(_P_GATE)
    @patch(_P_VALIDATE)
    def test_stage3_exception_propagates(
        self, mv, mg, ma, me, morch
    ) -> None:
        mv.return_value = _make_enriched()
        mg.return_value = _make_gate(pipeline_skipped=False)
        ma.return_value = None
        me.return_value = _make_pages_enum(n=2)
        morch.side_effect = self._orch_stage3_fail

        with self.assertRaises(RuntimeError):
            run_full_pipeline(
                {}, Path(self.tmp / "fake.pdf"), self.settings,
                reporter=self._reporter, set_global_total=None,
            )


class TestPipelineTiming(unittest.TestCase):
    def test_enrich_adds_elapsed_and_phase_duration(self) -> None:
        from src.ingestion.progress import PHASE_VALIDATION, PipelineTiming, STATUS_COMPLETED, STATUS_STARTED

        timing = PipelineTiming()
        events = [
            timing.enrich({"phase": PHASE_VALIDATION, "status": STATUS_STARTED}),
            timing.enrich({"phase": PHASE_VALIDATION, "status": STATUS_COMPLETED}),
        ]
        self.assertIn("elapsed_seconds", events[0])
        self.assertIn("phase_duration_seconds", events[1])
        summary = timing.summary()
        self.assertIn("total_seconds", summary)
        self.assertIn(PHASE_VALIDATION, summary["phases"])


if __name__ == "__main__":
    unittest.main()
