from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.search.pages_loader import DEFAULT_MAX_CHARS_PER_PAGE, load_pages


def _write_book(
    data_root: Path,
    source_sha256: str,
    *,
    slug: str,
    title: str,
    pages: dict[int, str],
) -> None:
    output_dir = data_root / "output" / source_sha256
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_pages: list[dict[str, object]] = []
    for aligned, content in sorted(pages.items()):
        filename = f"p.{aligned:04d}.{slug}.md"
        rel_path = f"pages/{filename}"
        (pages_dir / filename).write_text(content, encoding="utf-8")
        manifest_pages.append(
            {
                "aligned": aligned,
                "original": aligned,
                "file": rel_path,
            }
        )
    manifest = {
        "source_sha256": source_sha256,
        "slug": slug,
        "pages": manifest_pages,
        "reicat": {"titolo": title},
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


class TestPagesLoader(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_pages_in_sorted_order(self) -> None:
        sha = "a" * 64
        _write_book(
            self.data_root,
            sha,
            slug="libro-a",
            title="Libro A",
            pages={12: "pagina 12", 10: "pagina 10", 11: "pagina 11"},
        )
        result = load_pages({sha: [12, 10, 11]}, self.data_root, request_id="req-1")
        self.assertEqual([page.aligned_page for page in result.pages], [10, 11, 12])
        self.assertEqual(result.loaded_pages, {sha: [10, 11, 12]})
        self.assertEqual(result.pages[0].book_title, "Libro A")
        self.assertEqual(result.pages[0].markdown, "pagina 10")

    def test_multiple_books_sorted_by_sha(self) -> None:
        sha_a = "a" * 64
        sha_b = "b" * 64
        _write_book(
            self.data_root,
            sha_b,
            slug="b",
            title="B",
            pages={1: "b1"},
        )
        _write_book(
            self.data_root,
            sha_a,
            slug="a",
            title="A",
            pages={2: "a2"},
        )
        result = load_pages({sha_b: [1], sha_a: [2]}, self.data_root, request_id="req-2")
        self.assertEqual([page.source_sha256 for page in result.pages], [sha_a, sha_b])
        self.assertEqual(result.total_chars, len("a2") + len("b1"))

    def test_missing_manifest_counts_missing(self) -> None:
        sha = "c" * 64
        result = load_pages({sha: [1, 2]}, self.data_root, request_id="req-3")
        self.assertEqual(result.pages, [])
        self.assertEqual(result.loaded_pages, {})
        self.assertEqual(result.missing_pages, 2)

    def test_missing_page_file_counts_missing(self) -> None:
        sha = "d" * 64
        _write_book(
            self.data_root,
            sha,
            slug="libro",
            title="Libro",
            pages={1: "ok"},
        )
        result = load_pages({sha: [1, 99]}, self.data_root, request_id="req-4")
        self.assertEqual(result.loaded_pages, {sha: [1]})
        self.assertEqual(result.missing_pages, 1)

    def test_truncates_long_page(self) -> None:
        sha = "e" * 64
        long_text = "x" * 100
        _write_book(
            self.data_root,
            sha,
            slug="libro",
            title="Libro",
            pages={5: long_text},
        )
        result = load_pages(
            {sha: [5]},
            self.data_root,
            max_chars_per_page=40,
            request_id="req-5",
        )
        self.assertEqual(result.truncated_pages, 1)
        self.assertTrue(result.pages[0].truncated)
        self.assertLessEqual(len(result.pages[0].markdown), 40)

    def test_normalizes_channel_artifacts(self) -> None:
        sha = "f" * 64
        raw = "<|channel|>thought\n<channel|> output line\nkeep me\n"
        _write_book(
            self.data_root,
            sha,
            slug="libro",
            title="Libro",
            pages={1: raw},
        )
        result = load_pages({sha: [1]}, self.data_root, request_id="req-6")
        self.assertEqual(result.pages[0].markdown, "output line\nkeep me")

    def test_empty_input_returns_empty(self) -> None:
        result = load_pages({}, self.data_root, request_id="req-7")
        self.assertEqual(result.pages, [])
        self.assertEqual(result.missing_pages, 0)
        self.assertEqual(result.truncated_pages, 0)
        self.assertEqual(result.books_dropped, 0)

    def test_max_pages_per_book_truncates_before_load(self) -> None:
        sha = "g" * 64
        _write_book(
            self.data_root,
            sha,
            slug="libro",
            title="Libro",
            pages={1: "p1", 2: "p2", 3: "p3"},
        )
        result = load_pages(
            {sha: [3, 1, 2]},
            self.data_root,
            max_pages_per_book=2,
            request_id="req-8",
        )
        self.assertEqual(result.loaded_pages, {sha: [1, 2]})
        self.assertEqual(len(result.pages), 2)

    def test_max_books_keeps_books_with_more_candidates(self) -> None:
        sha_few = "f" * 64
        sha_many = "e" * 64
        _write_book(
            self.data_root,
            sha_few,
            slug="few",
            title="Few",
            pages={1: "a"},
        )
        _write_book(
            self.data_root,
            sha_many,
            slug="many",
            title="Many",
            pages={1: "b1", 2: "b2", 3: "b3"},
        )
        result = load_pages(
            {sha_few: [1], sha_many: [1, 2, 3]},
            self.data_root,
            max_books=1,
            request_id="req-9",
        )
        self.assertEqual(list(result.loaded_pages.keys()), [sha_many])
        self.assertEqual(result.books_dropped, 1)

    def test_default_max_chars_constant(self) -> None:
        self.assertGreater(DEFAULT_MAX_CHARS_PER_PAGE, 0)


if __name__ == "__main__":
    unittest.main()
