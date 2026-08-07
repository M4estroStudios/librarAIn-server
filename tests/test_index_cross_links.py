from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.index_cross_links import (
    POLYINDEX_REF_PLACEHOLDER,
    allocate_subject_keys,
    apply_index_cross_links,
    book_index_json_path,
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
    def test_normalize_label_is_url_safe(self) -> None:
        self.assertEqual(normalize_label("Acqua Claudia"), "acqua-claudia")
        self.assertEqual(normalize_label("  Città,  "), "citta")

    def test_allocate_subject_keys_disambiguates(self) -> None:
        subjects = [
            RawSubject("Augusto", [1], [1]),
            RawSubject("Augusto", [2], [2]),
        ]
        keys = [key for _, key in allocate_subject_keys(subjects)]
        self.assertEqual(keys, ["augusto", "augusto-2"])

    def test_rewrite_index_links_label_and_pages_without_fragments(self) -> None:
        subjects = [
            RawSubject(
                raw_label="Acqua Claudia",
                original_pages=[39, 50, 52, 53, 54],
                aligned_pages=[39, 50, 52, 53, 54],
            )
        ]
        subject_keys = allocate_subject_keys(subjects)
        text = "# INDEX — Test\n\nAcqua Claudia, 39, 50, 52-54\n"
        out = rewrite_index_md_with_page_links(
            text,
            subject_keys,
            slug="test-book",
            original_to_aligned={39: 39, 50: 50, 52: 52, 53: 53, 54: 54},
        )
        self.assertIn(f"[Acqua Claudia]({POLYINDEX_REF_PLACEHOLDER})", out)
        self.assertNotIn("<a id=", out)
        self.assertIn("[39](pages/p.0039.test-book.md)", out)
        self.assertIn("[52](pages/p.0052.test-book.md)", out)
        self.assertNotIn("#acqua-claudia", out)

    def test_parser_still_reads_linked_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "INDEX.md"
            path.write_text(
                "# INDEX — Test\n\n"
                f"[Acqua Claudia]({POLYINDEX_REF_PLACEHOLDER}), "
                "[39](pages/p.0039.test-book.md), "
                "[50](pages/p.0050.test-book.md)\n",
                encoding="utf-8",
            )
            subjects = parse_index_md(path, _enumeration({39: 39, 50: 50}))
            self.assertEqual(len(subjects), 1)
            self.assertEqual(subjects[0].raw_label, "Acqua Claudia")
            self.assertEqual(subjects[0].original_pages, [39, 50])

    def test_link_subject_mentions_on_page(self) -> None:
        page = "Nel rione scorre l'Acqua Claudia verso il colle.\n"
        updated, unresolved = link_subject_mentions_in_page(
            page, [("Acqua Claudia", "acqua-claudia")]
        )
        self.assertEqual(unresolved, [])
        self.assertIn("[Acqua Claudia](<Acqua Claudia>)", updated)
        self.assertNotIn("<a id=", updated)
        self.assertNotIn("INDEX.md#", updated)

    def test_link_all_mentions_without_anchors(self) -> None:
        page = "Acqua Claudia e ancora Acqua Claudia.\n"
        updated, unresolved = link_subject_mentions_in_page(
            page, [("Acqua Claudia", "acqua-claudia")]
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(updated.count("[Acqua Claudia](<Acqua Claudia>)"), 2)

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
            self.assertIn(f"[Acqua Claudia]({POLYINDEX_REF_PLACEHOLDER})", index_text)
            self.assertIn("[39](pages/p.0039.test-book.md)", index_text)
            self.assertNotIn("#", index_text.split("pages/", 1)[-1].split(")", 1)[0])
            page_text = page_path.read_text(encoding="utf-8")
            self.assertIn("[Acqua Claudia](<Acqua Claudia>)", page_text)
            index_json = book_index_json_path(root, "test-book")
            self.assertTrue(index_json.is_file())
            self.assertEqual(index_json.name, "INDEX_test-book.json")
            data = json.loads(index_json.read_text(encoding="utf-8"))
            self.assertIn("acqua-claudia", data["subjects"])
            self.assertEqual(data["subjects"]["acqua-claudia"]["global_ref"], None)
            self.assertEqual(data["page_subjects"]["39"], ["acqua-claudia"])

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
                return "Testo con [A. Claudia](<Acqua Claudia>) nel paragrafo.\n"

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
            self.assertIn("[A. Claudia](<Acqua Claudia>)", page_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
