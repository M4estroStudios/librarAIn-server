from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.persistence.book_page_exclude import PageExcludeError, exclude_book_page, load_book_exclusions
from src.persistence.book_pages_audit import audit_book


def _write_book(
    data_root: Path,
    source_sha256: str,
    *,
    slug: str,
    title: str,
    original_page_count: int,
    pages: dict[int, int],
) -> None:
    output_dir = data_root / "output" / source_sha256
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_pages: list[dict[str, object]] = []
    for aligned, original in sorted(pages.items()):
        filename = f"p.{aligned:04d}.{slug}.md"
        rel_path = f"pages/{filename}"
        (pages_dir / filename).write_text(f"page {aligned}\n", encoding="utf-8")
        manifest_pages.append(
            {"aligned": aligned, "original": original, "file": rel_path}
        )
    manifest = {
        "source_sha256": source_sha256,
        "slug": slug,
        "original_page_count": original_page_count,
        "aligned_page_count": len(pages),
        "pages": manifest_pages,
        "pages_to_remove": [],
        "excluded_aligned_pages": [],
        "reicat": {"titolo": title},
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_root = data_root / "tmp" / source_sha256
    for stage, suffix in (
        ("stage1OCR", ".txt"),
        ("stage2Vision", ".md"),
        ("stage3Editor", ".md"),
    ):
        stage_dir = tmp_root / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        for aligned in pages:
            (stage_dir / f"p.{aligned:04d}.{slug}{suffix}").write_text(
                f"content {aligned}",
                encoding="utf-8",
            )


class TestBookPageExclude(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        self.sha = "d" * 64

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exclude_updates_manifest_and_removes_files(self) -> None:
        _write_book(
            self.data_root,
            self.sha,
            slug="libro",
            title="Libro",
            original_page_count=10,
            pages={1: 1, 2: 3, 3: 4},
        )
        tmp_root = self.data_root / "tmp" / self.sha
        render_dir = tmp_root / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        (render_dir / "p.0002.png").write_bytes(b"png")
        result = exclude_book_page(self.data_root, self.sha, 2)
        self.assertEqual(result["aligned_page"], 2)
        self.assertEqual(result["original_page"], 3)
        manifest = json.loads(
            (self.data_root / "output" / self.sha / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["excluded_aligned_pages"], [2])
        self.assertEqual(manifest["pages_to_remove"], [3])
        self.assertEqual(len(manifest["pages"]), 2)
        self.assertFalse((tmp_root / "stage1OCR" / "p.0002.libro.txt").is_file())
        self.assertFalse((self.data_root / "output" / self.sha / "pages" / "p.0002.libro.md").is_file())
        excluded_aligned, pages_to_remove = load_book_exclusions(
            self.data_root, self.sha, manifest=manifest
        )
        self.assertEqual(excluded_aligned, [2])
        self.assertEqual(pages_to_remove, [3])

    def test_audit_skips_excluded_pages(self) -> None:
        _write_book(
            self.data_root,
            self.sha,
            slug="libro",
            title="Libro",
            original_page_count=5,
            pages={1: 1, 2: 2},
        )
        tmp_root = self.data_root / "tmp" / self.sha
        (tmp_root / "stage2Vision" / "p.0002.libro.md").unlink(missing_ok=True)
        (tmp_root / "stage3Editor" / "p.0002.libro.md").unlink(missing_ok=True)
        before = audit_book(self.data_root, self.sha)
        assert before is not None
        self.assertEqual(len(before["missing_pages"]), 1)
        exclude_book_page(self.data_root, self.sha, 2)
        after = audit_book(self.data_root, self.sha)
        assert after is not None
        self.assertEqual(after["missing_pages"], [])

    def test_exclude_twice_raises(self) -> None:
        _write_book(
            self.data_root,
            self.sha,
            slug="libro",
            title="Libro",
            original_page_count=5,
            pages={1: 1, 2: 2},
        )
        exclude_book_page(self.data_root, self.sha, 2)
        with self.assertRaises(PageExcludeError):
            exclude_book_page(self.data_root, self.sha, 2)
