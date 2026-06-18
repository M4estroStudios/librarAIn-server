from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from src.persistence.book_page_repair import (
    PageRepairError,
    build_enriched_from_manifest,
    filter_useful_pages_to_aligned,
    filter_useful_pages_to_single,
    infer_repair_entry_stage,
    infer_gaps_repair_entry_stage,
    merge_repaired_page_into_output,
    resolve_original_page,
    run_book_gaps_repair,
    run_book_page_repair,
)
from src.ingestion.page_enumeration import build_useful_pages_enumeration
from src.ingestion.pipeline.stage3 import Stage3PageResult, Stage3Result
from src.models.request import PageRange, UsefulPagesEnumeration


def _minimal_pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _write_manifest(data_root: Path, source_sha256: str, *, pages: list[dict[str, int]]) -> None:
    output_dir = data_root / "output" / source_sha256
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_sha256": source_sha256,
        "slug": "libro-test",
        "original_page_count": max(p["original"] for p in pages),
        "aligned_page_count": max(p["aligned"] for p in pages),
        "pages": [
            {
                "aligned": entry["aligned"],
                "original": entry["original"],
                "file": f"pages/p.{entry['aligned']:04d}.libro-test.md",
            }
            for entry in pages
        ],
        "reicat": {"titolo": "Libro Test", "autore": ["Autore"]},
        "pipeline_version": "1.0",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


class TestBookPageRepair(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_infer_repair_entry_stage(self) -> None:
        self.assertEqual(
            infer_repair_entry_stage(["stage2Vision", "stage3Editor", "output"]),
            "stage2Vision",
        )
        self.assertEqual(infer_repair_entry_stage(["output"]), "output")

    def test_infer_gaps_repair_entry_stage(self) -> None:
        gap_pages = [
            {"aligned": 1, "missing_in": ["stage2Vision", "output"]},
            {"aligned": 2, "missing_in": ["stage3Editor", "output"]},
        ]
        self.assertEqual(infer_gaps_repair_entry_stage(gap_pages), "stage2Vision")
        self.assertEqual(
            infer_gaps_repair_entry_stage([{"aligned": 3, "missing_in": ["output"]}]),
            "output",
        )

    def test_build_enriched_from_manifest_uses_processed_pdf(self) -> None:
        sha = "a" * 64
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(3))
        _write_manifest(
            self.data_root,
            sha,
            pages=[{"aligned": 1, "original": 1}, {"aligned": 2, "original": 2}],
        )
        manifest = json.loads(
            (self.data_root / "output" / sha / "manifest.json").read_text(encoding="utf-8")
        )
        enriched = build_enriched_from_manifest(self.data_root, manifest)
        self.assertEqual(enriched.source_sha256, sha)
        self.assertEqual(enriched.source_pdf_page_count, 2)

    def test_merge_repaired_page_preserves_other_pages(self) -> None:
        sha = "b" * 64
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(2))
        _write_manifest(
            self.data_root,
            sha,
            pages=[{"aligned": 1, "original": 1}, {"aligned": 2, "original": 2}],
        )
        manifest_path = self.data_root / "output" / sha / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        enriched = build_enriched_from_manifest(self.data_root, manifest)
        useful = build_useful_pages_enumeration(enriched, None)
        stage3_dir = self.data_root / "tmp" / sha / "stage3Editor"
        stage3_dir.mkdir(parents=True, exist_ok=True)
        md_path = stage3_dir / "p.0002.libro-test.md"
        md_path.write_text("# pagina 2\n", encoding="utf-8")
        stage3 = Stage3Result(
            pages=[
                Stage3PageResult(
                    aligned_page=2,
                    original_page=2,
                    md_path=str(md_path),
                    char_count=10,
                    stage2_char_count=8,
                    char_delta=2,
                )
            ],
            skipped_existing=0,
            missing=[],
        )
        merge_repaired_page_into_output(
            self.data_root, sha, enriched, useful, stage3, manifest
        )
        updated = json.loads(manifest_path.read_text(encoding="utf-8"))
        aligned_values = [entry["aligned"] for entry in updated["pages"]]
        self.assertEqual(aligned_values, [1, 2])
        self.assertTrue(
            (self.data_root / "output" / sha / "pages" / "p.0002.libro-test.md").is_file()
        )

    def test_run_book_page_repair_rejects_excluded_page(self) -> None:
        sha = "c" * 64
        _write_manifest(self.data_root, sha, pages=[{"aligned": 1, "original": 1}])
        manifest_path = self.data_root / "output" / sha / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["excluded_aligned_pages"] = [1]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        settings = type("S", (), {"data_root": str(self.data_root), "ocr_use_gpu": False})()
        with self.assertRaises(PageRepairError):
            run_book_page_repair(self.data_root, settings, sha, 1)  # type: ignore[arg-type]

    def test_filter_useful_pages_to_single(self) -> None:
        useful = UsefulPagesEnumeration(
            source_sha256="d" * 64,
            original_page_count=3,
            aligned_page_count=3,
            useful_original_pages=[1, 2, 3],
            original_page_to_aligned_page={1: 1, 2: 2, 3: 3},
            aligned_page_to_original_page={1: 1, 2: 2, 3: 3},
            toc_range_aligned=PageRange(start=1, end=1),
            index_range_aligned=PageRange(start=1, end=1),
        )
        filtered = filter_useful_pages_to_single(useful, 2)
        self.assertEqual(filtered.useful_original_pages, [2])

    def test_filter_useful_pages_to_aligned(self) -> None:
        useful = UsefulPagesEnumeration(
            source_sha256="d" * 64,
            original_page_count=5,
            aligned_page_count=5,
            useful_original_pages=[1, 2, 3, 4, 5],
            original_page_to_aligned_page={1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            aligned_page_to_original_page={1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            toc_range_aligned=PageRange(start=1, end=1),
            index_range_aligned=PageRange(start=1, end=1),
        )
        filtered = filter_useful_pages_to_aligned(useful, [5, 2, 2])
        self.assertEqual(filtered.useful_original_pages, [2, 5])

    def test_resolve_original_page_from_manifest(self) -> None:
        sha = "e" * 64
        _write_manifest(
            self.data_root,
            sha,
            pages=[{"aligned": 5, "original": 7}],
        )
        manifest = json.loads(
            (self.data_root / "output" / sha / "manifest.json").read_text(encoding="utf-8")
        )
        useful = UsefulPagesEnumeration(
            source_sha256=sha,
            original_page_count=10,
            aligned_page_count=8,
            useful_original_pages=[7],
            original_page_to_aligned_page={7: 5},
            aligned_page_to_original_page={5: 7},
            toc_range_aligned=PageRange(start=1, end=1),
            index_range_aligned=PageRange(start=1, end=1),
        )
        self.assertEqual(resolve_original_page(manifest, useful, 5), 7)

    @patch("src.persistence.book_page_repair._run_repair_async")
    @patch("src.persistence.book_page_repair.require_gpu_vram_at_pipeline_start")
    @patch("src.persistence.book_page_repair.mark_page_pending_review")
    def test_run_book_page_repair_delegates_to_async_runner(
        self,
        mock_pending,
        mock_gpu,
        run_async,
    ) -> None:
        sha = "f" * 64
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(2))
        _write_manifest(
            self.data_root,
            sha,
            pages=[{"aligned": 1, "original": 1}, {"aligned": 2, "original": 2}],
        )

        async def _fake_async(*args, **kwargs):
            return {"aligned_page": args[4][0], "entry_stage": "stage2Vision"}

        run_async.side_effect = _fake_async
        settings = type("S", (), {"data_root": str(self.data_root), "ocr_use_gpu": False})()
        result = run_book_page_repair(
            self.data_root,
            settings,  # type: ignore[arg-type]
            sha,
            2,
            missing_in=["stage2Vision", "output"],
        )
        self.assertEqual(result["entry_stage"], "stage2Vision")
        run_async.assert_called_once()
        mock_gpu.assert_called_once()
        self.assertTrue(mock_gpu.call_args.kwargs.get("single_page"))
        self.assertEqual(mock_gpu.call_args.kwargs.get("entry_stage"), "stage2Vision")
        mock_pending.assert_called_once_with(self.data_root, sha, 2)

    @patch("src.persistence.book_page_repair._run_repair_async")
    @patch("src.persistence.book_page_repair.require_gpu_vram_at_pipeline_start")
    @patch("src.persistence.book_page_repair.mark_page_pending_review")
    def test_run_book_gaps_repair_uses_full_parallelism(
        self,
        mock_pending,
        mock_gpu,
        run_async,
    ) -> None:
        sha = "g" * 64
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(3))
        _write_manifest(
            self.data_root,
            sha,
            pages=[{"aligned": 1, "original": 1}, {"aligned": 2, "original": 2}],
        )

        async def _fake_async(*args, **kwargs):
            return {"aligned_pages": args[4], "stage3_pages": 2}

        run_async.side_effect = _fake_async
        settings = type("S", (), {"data_root": str(self.data_root), "ocr_use_gpu": False})()
        gap_pages = [
            {"aligned": 1, "missing_in": ["stage2Vision"]},
            {"aligned": 2, "missing_in": ["output"]},
        ]
        result = run_book_gaps_repair(
            self.data_root,
            settings,  # type: ignore[arg-type]
            sha,
            gap_pages,
        )
        self.assertEqual(result["aligned_pages"], [1, 2])
        run_async.assert_called_once()
        self.assertFalse(run_async.call_args.kwargs.get("single_page"))
        mock_gpu.assert_called_once()
        self.assertFalse(mock_gpu.call_args.kwargs.get("single_page"))
        self.assertEqual(mock_gpu.call_args.kwargs.get("entry_stage"), "stage2Vision")
        self.assertEqual(mock_pending.call_count, 2)
