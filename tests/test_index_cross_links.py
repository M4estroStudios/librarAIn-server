from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.index_cross_links import (
    apply_index_cross_links,
    link_subject_mentions_in_page,
    rewrite_index_md_with_page_links,
)
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    normalize_label,
    parse_index_md,
)
from src.models.request import PageRange, UsefulPagesEnumeration


def _enumeration(mapping: dict[int, int]) -> UsefulPagesEnumeration:
    originals = sorted(mapping)
    return UsefulPagesEnumeration(
        source_sha256="deadbeef",
        original_page_count=max(originals),
        aligned_page_count=len(mapping),
        useful_original_pages=originals,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page={v: k for k, v in mapping.items()},
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=1, end=1),
    )


class IndexCrossLinksTests(unittest.TestCase):
    def test_rewrite_index_links_every_page_including_ranges(self) -> None:
        subjects = [
            RawSubject(
                raw_label="Acqua Claudia",
                original_pages=[39, 50, 52, 53, 54],
                aligned_pages=[39, 50, 52, 53, 54],
            )
        ]
        text = "# INDEX — Test\n\nAcqua Claudia, 39, 50, 52-54\n"
        out, anchors = rewrite_index_md_with_page_links(
            text,
            subjects,
            slug="test-book",
            original_to_aligned={39: 39, 50: 50, 52: 52, 53: 53, 54: 54},
        )
        self.assertIn('<a id="idx-acqua-claudia"></a>Acqua Claudia', out)
        self.assertIn("[39](pages/p.0039.test-book.md)", out)
        self.assertIn("[52](pages/p.0052.test-book.md)", out)
        self.assertIn("[53](pages/p.0053.test-book.md)", out)
        self.assertIn("[54](pages/p.0054.test-book.md)", out)
        self.assertEqual(anchors[normalize_label("Acqua Claudia")], "idx-acqua-claudia")

    def test_parser_still_reads_linked_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "INDEX.md"
            path.write_text(
                "# INDEX — Test\n\n"
                '<a id="idx-acqua-claudia"></a>Acqua Claudia, '
                "[39](pages/p.0039.test-book.md), [50](pages/p.0050.test-book.md)\n",
                encoding="utf-8",
            )
            subjects = parse_index_md(path, _enumeration({39: 39, 50: 50}))
            self.assertEqual(len(subjects), 1)
            self.assertEqual(subjects[0].raw_label, "Acqua Claudia")
            self.assertEqual(subjects[0].original_pages, [39, 50])

    def test_link_subject_mentions_on_page(self) -> None:
        page = "Nel rione scorre l'Acqua Claudia verso il colle.\n"
        updated, unresolved = link_subject_mentions_in_page(
            page, [("Acqua Claudia", "idx-acqua-claudia")]
        )
        self.assertEqual(unresolved, [])
        self.assertIn("[Acqua Claudia](../INDEX.md#idx-acqua-claudia)", updated)

    def test_apply_index_cross_links_end_to_end_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            page_path = pages_dir / "p.0039.test-book.md"
            page_path.write_text("Testo con Acqua Claudia nel paragrafo.\n", encoding="utf-8")
            index_path = root / "INDEX.md"
            index_path.write_text(
                "# INDEX — Test\n\nAcqua Claudia, 39\n",
                encoding="utf-8",
            )
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[BookPageOutput(aligned=39, original=39, file=page_path)],
            )
            settings = MagicMock()
            settings.editor_model = None
            stats = asyncio.run(
                apply_index_cross_links(
                    index_path,
                    book,
                    _enumeration({39: 39}),
                    client=None,
                    settings=settings,
                    request_id="req",
                )
            )
            self.assertEqual(stats["subjects"], 1)
            self.assertEqual(stats["pages_updated"], 1)
            index_text = index_path.read_text(encoding="utf-8")
            self.assertIn("[39](pages/p.0039.test-book.md)", index_text)
            page_text = page_path.read_text(encoding="utf-8")
            self.assertIn("[Acqua Claudia](../INDEX.md#idx-acqua-claudia)", page_text)

    def test_llm_fallback_when_regex_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            page_path = pages_dir / "p.0039.test-book.md"
            page_path.write_text("Testo con A. Claudia nel paragrafo.\n", encoding="utf-8")
            index_path = root / "INDEX.md"
            index_path.write_text("# INDEX — Test\n\nAcqua Claudia, 39\n", encoding="utf-8")
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[BookPageOutput(aligned=39, original=39, file=page_path)],
            )
            settings = MagicMock()
            settings.editor_model = "editor-x"
            client = MagicMock()

            async def _fake_llm(*args, **kwargs):
                return "Testo con [A. Claudia](../INDEX.md#idx-acqua-claudia) nel paragrafo.\n"

            with patch(
                "src.ingestion.index_cross_links.chat_completion_with_retry",
                new=AsyncMock(side_effect=_fake_llm),
            ):
                stats = asyncio.run(
                    apply_index_cross_links(
                        index_path,
                        book,
                        _enumeration({39: 39}),
                        client=client,
                        settings=settings,
                        request_id="req",
                    )
                )
            self.assertEqual(stats["llm_links"], 1)
            self.assertIn("INDEX.md#idx-acqua-claudia", page_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
