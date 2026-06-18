from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.persistence.book_pages_audit import audit_all_books, audit_book


def _write_manifest(
    data_root: Path,
    source_sha256: str,
    *,
    slug: str,
    title: str,
    aligned_pages: list[int],
) -> None:
    output_dir = data_root / "output" / source_sha256
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_pages: list[dict[str, object]] = []
    for aligned in aligned_pages:
        filename = f"p.{aligned:04d}.{slug}.md"
        rel_path = f"pages/{filename}"
        (pages_dir / filename).write_text(f"page {aligned}\n", encoding="utf-8")
        manifest_pages.append(
            {"aligned": aligned, "original": aligned, "file": rel_path}
        )
    manifest = {
        "source_sha256": source_sha256,
        "slug": slug,
        "aligned_page_count": max(aligned_pages) if aligned_pages else 0,
        "pages": manifest_pages,
        "reicat": {"titolo": title},
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


class TestBookPagesAudit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_complete_book_has_no_gaps(self) -> None:
        sha = "a" * 64
        _write_manifest(
            self.data_root,
            sha,
            slug="libro-test",
            title="Libro Test",
            aligned_pages=[1, 2, 3],
        )
        tmp_root = self.data_root / "tmp" / sha
        for stage, suffix in (
            ("stage1OCR", ".txt"),
            ("stage2Vision", ".md"),
            ("stage3Editor", ".md"),
        ):
            stage_dir = tmp_root / stage
            stage_dir.mkdir(parents=True, exist_ok=True)
            for page in (1, 2, 3):
                (stage_dir / f"p.{page:04d}.libro-test{suffix}").write_text(
                    f"content {page}",
                    encoding="utf-8",
                )
        result = audit_book(self.data_root, sha)
        assert result is not None
        self.assertTrue(result["complete"])
        self.assertEqual(result["expected_page_count"], 3)
        self.assertEqual(result["missing_pages"], [])
        self.assertEqual(result["viewer_pages"], [1, 2, 3])

    def test_detects_missing_stage_files(self) -> None:
        sha = "b" * 64
        _write_manifest(
            self.data_root,
            sha,
            slug="incompleto",
            title="Incompleto",
            aligned_pages=[1, 2],
        )
        stage1 = self.data_root / "tmp" / sha / "stage1OCR"
        stage1.mkdir(parents=True, exist_ok=True)
        (stage1 / "p.0001.incompleto.txt").write_text("ok", encoding="utf-8")
        result = audit_book(self.data_root, sha)
        assert result is not None
        self.assertFalse(result["complete"])
        self.assertEqual(result["stages"]["stage1OCR"]["missing"], [2])
        self.assertIn("stage2Vision", result["missing_pages"][0]["missing_in"])

    def test_audit_all_books_summary(self) -> None:
        sha = "c" * 64
        _write_manifest(
            self.data_root,
            sha,
            slug="solo-output",
            title="Solo Output",
            aligned_pages=[1],
        )
        report = audit_all_books(self.data_root)
        self.assertEqual(report["summary"]["book_count"], 1)
        self.assertEqual(report["summary"]["books_with_gaps"], 1)
