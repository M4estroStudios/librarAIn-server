from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from src.persistence.book_page_preview import (
    PagePreviewError,
    clear_page_pending_review,
    confirm_page_transcript,
    ensure_page_render_png,
    list_pending_review_pages,
    load_page_transcript,
    mark_page_pending_review,
    save_page_transcript,
)


def _minimal_pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestBookPagePreview(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_existing_render_png(self) -> None:
        sha = "c" * 64
        png_path = self.data_root / "tmp" / sha / "render" / "p.0002.png"
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(2))
        result = ensure_page_render_png(self.data_root, sha, 2)
        self.assertEqual(result, png_path)

    def test_renders_png_on_demand(self) -> None:
        sha = "d" * 64
        processed = self.data_root / "input" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{sha}.pdf").write_bytes(_minimal_pdf_bytes(2))
        result = ensure_page_render_png(self.data_root, sha, 1, dpi=72)
        self.assertTrue(result.is_file())
        self.assertEqual(result.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(
            result,
            self.data_root / "tmp" / sha / "render" / "p.0001.png",
        )

    def test_missing_aligned_pdf_raises(self) -> None:
        sha = "e" * 64
        with self.assertRaises(PagePreviewError):
            ensure_page_render_png(self.data_root, sha, 1)

    def test_load_page_transcript_prefers_stage3(self) -> None:
        sha = "f" * 64
        slug = "libro-test"
        stage1 = self.data_root / "tmp" / sha / "stage1OCR" / f"p.0003.{slug}.txt"
        stage3 = self.data_root / "tmp" / sha / "stage3Editor" / f"p.0003.{slug}.md"
        stage1.parent.mkdir(parents=True, exist_ok=True)
        stage3.parent.mkdir(parents=True, exist_ok=True)
        stage1.write_text("ocr text", encoding="utf-8")
        stage3.write_text("<!-- librarain:model=gemma-test -->\neditor text", encoding="utf-8")
        manifest_dir = self.data_root / "output" / sha
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(
            '{"source_sha256":"' + sha + '","slug":"' + slug + '"}',
            encoding="utf-8",
        )
        text, stage_key, model = load_page_transcript(self.data_root, sha, 3)
        self.assertEqual(text, "editor text")
        self.assertEqual(stage_key, "stage3Editor")
        self.assertEqual(model, "gemma-test")

    def test_pending_review_and_confirm_writes_output(self) -> None:
        sha = "b" * 64
        slug = "libro-test"
        manifest_dir = self.data_root / "output" / sha
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": sha,
                    "slug": slug,
                    "pages": [{"aligned": 2, "original": 2, "file": "pages/p.0002." + slug + ".md"}],
                }
            ),
            encoding="utf-8",
        )
        mark_page_pending_review(self.data_root, sha, 2)
        self.assertEqual(list_pending_review_pages(self.data_root, sha), [2])
        result = confirm_page_transcript(self.data_root, sha, 2, "final text")
        self.assertEqual(result["aligned_page"], 2)
        self.assertEqual(result["producer_model"], "manual-review")
        stage3 = self.data_root / "tmp" / sha / "stage3Editor" / f"p.0002.{slug}.md"
        output = self.data_root / "output" / sha / "pages" / f"p.0002.{slug}.md"
        self.assertEqual(stage3.read_text(encoding="utf-8"), "<!-- librarain:model=manual-review -->\nfinal text")
        self.assertEqual(output.read_text(encoding="utf-8"), "<!-- librarain:model=manual-review -->\nfinal text")
        self.assertEqual(list_pending_review_pages(self.data_root, sha), [])
        clear_page_pending_review(self.data_root, sha, 99)

    def test_save_page_transcript_updates_existing_file(self) -> None:
        sha = "a" * 64
        slug = "libro-test"
        stage1 = self.data_root / "tmp" / sha / "stage1OCR" / f"p.0001.{slug}.txt"
        stage1.parent.mkdir(parents=True, exist_ok=True)
        stage1.write_text("before", encoding="utf-8")
        manifest_dir = self.data_root / "output" / sha
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "manifest.json").write_text(
            '{"source_sha256":"' + sha + '","slug":"' + slug + '"}',
            encoding="utf-8",
        )
        result = save_page_transcript(self.data_root, sha, 1, "after")
        self.assertEqual(result["stage"], "stage1OCR")
        self.assertEqual(stage1.read_text(encoding="utf-8"), "after\n")
