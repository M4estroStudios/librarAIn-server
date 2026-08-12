from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.index_cross_links import (
    allocate_subject_keys,
    apply_index_cross_links,
    audit_index_cross_links_readiness,
    book_index_json_path,
    link_subject_mentions_in_page,
)
from src.ingestion.index_md_links import (
    POLYINDEX_REF_PLACEHOLDER,
    repair_placeholder_subject_links,
    rewrite_index_md_with_page_links,
)
from src.ingestion.output_writer import BookOutput, BookPageOutput
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    normalize_label,
    parse_index_md,
)
from src.models.request import PageRange, UsefulPagesEnumeration


def _enumeration(
    mapping: dict[int, int],
    *,
    index_range: tuple[int, int] | None = None,
) -> UsefulPagesEnumeration:
    originals = sorted(mapping)
    if index_range is None:
        index_start = index_end = min(mapping.values())
    else:
        index_start, index_end = index_range
    return UsefulPagesEnumeration(
        source_sha256="deadbeef",
        original_page_count=max(originals),
        aligned_page_count=len(mapping),
        useful_original_pages=originals,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page={v: k for k, v in mapping.items()},
        toc_range_aligned=PageRange(start=1, end=1),
        index_range_aligned=PageRange(start=index_start, end=index_end),
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

    def test_rewrite_index_links_use_aligned_page_labels_with_offset(self) -> None:
        subjects = [
            RawSubject(
                raw_label="Milano",
                original_pages=[4],
                aligned_pages=[104],
            )
        ]
        subject_keys = allocate_subject_keys(subjects)
        out = rewrite_index_md_with_page_links(
            "Milano, 104\n",
            subject_keys,
            slug="test-book",
            original_to_aligned={4: 104},
            aligned_to_original={104: 4},
        )
        self.assertIn("[104](pages/p.0104.test-book.md)", out)
        self.assertNotIn("[4](pages/p.0104.test-book.md)", out)
        self.assertNotIn("p.0004", out)

    def test_rewrite_index_aligned_label_keeps_aligned_href_with_offset(self) -> None:
        subjects = [
            RawSubject(
                raw_label="a Ahmed Shawky",
                original_pages=[138],
                aligned_pages=[136],
            )
        ]
        subject_keys = allocate_subject_keys(subjects)
        out = rewrite_index_md_with_page_links(
            "a Ahmed Shawky, 136\n",
            subject_keys,
            slug="test-book",
            original_to_aligned={138: 136, 136: 134},
            aligned_to_original={136: 138, 134: 136},
        )
        self.assertIn("[136](pages/p.0136.test-book.md)", out)
        self.assertNotIn("p.0134", out)

    def test_rewrite_index_source_page_uses_sibling_hrefs(self) -> None:
        subjects = [
            RawSubject(
                raw_label="Acqua Claudia",
                original_pages=[39],
                aligned_pages=[39],
            )
        ]
        subject_keys = allocate_subject_keys(subjects)
        out = rewrite_index_md_with_page_links(
            "Acqua Claudia, 39\n",
            subject_keys,
            slug="test-book",
            original_to_aligned={39: 39},
            from_pages_dir=True,
        )
        self.assertIn("[39](p.0039.test-book.md)", out)
        self.assertNotIn("pages/", out)

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
        updated, report = link_subject_mentions_in_page(
            page, [("Acqua Claudia", "p.0100.test-book.md")]
        )
        self.assertEqual(report.failed_regex, [])
        self.assertEqual(report.success_regex, ["Acqua Claudia"])
        self.assertIn("[Acqua Claudia](p.0100.test-book.md)", updated)
        self.assertNotIn("<a id=", updated)
        self.assertNotIn("INDEX.md#", updated)

    def test_link_all_mentions_without_anchors(self) -> None:
        page = "Acqua Claudia e ancora Acqua Claudia.\n"
        updated, report = link_subject_mentions_in_page(
            page, [("Acqua Claudia", "p.0100.test-book.md")]
        )
        self.assertEqual(report.failed_regex, [])
        self.assertEqual(updated.count("[Acqua Claudia](p.0100.test-book.md)"), 2)

    def test_repair_placeholder_subject_link(self) -> None:
        page = "Villa [Borghese](Borghese) nel parco.\n"
        updated, report = link_subject_mentions_in_page(
            page, [("Borghese", "p.0784.test-book.md")]
        )
        self.assertEqual(report.failed_regex, [])
        self.assertIn("[Borghese](p.0784.test-book.md)", updated)
        self.assertNotIn("[Borghese](Borghese)", updated)

    def test_article_prefix_label_matches_body(self) -> None:
        page = "Monumento ad Ahmed Shawky presso il viale.\n"
        updated, report = link_subject_mentions_in_page(
            page, [("a Ahmed Shawky", "p.0784.test-book.md")]
        )
        self.assertEqual(report.failed_regex, ["a Ahmed Shawky"])
        self.assertEqual(report.success_regex, ["Ahmed Shawky"])
        self.assertIn("[Ahmed Shawky](p.0784.test-book.md)", updated)

    def test_regex_miss_records_failed_regex_bucket(self) -> None:
        page = "Nel palazzo compaiono solo i Barberini.\n"
        _, report = link_subject_mentions_in_page(
            page,
            [("a Bertel Thorvaldsen a Palazzo Barberini", "p.0100.test-book.md")],
        )
        self.assertEqual(report.success_regex, [])
        self.assertEqual(report.failed_regex, ["a Bertel Thorvaldsen a Palazzo Barberini"])
        self.assertEqual(report.regex_resolved_labels, [])

    def test_index_core_label_links_shorter_page_mention(self) -> None:
        page = "Qui c'è un *monumento a Bertel Thorvaldsen* nel giardino.\n"
        updated, report = link_subject_mentions_in_page(
            page,
            [("a Bertel Thorvaldsen a Palazzo Barberini", "p.0790.test-book.md")],
        )
        self.assertEqual(report.failed_regex, ["a Bertel Thorvaldsen a Palazzo Barberini"])
        self.assertEqual(report.success_regex, ["Bertel Thorvaldsen"])
        self.assertEqual(report.regex_resolved_labels, ["a Bertel Thorvaldsen a Palazzo Barberini"])
        self.assertIn("[Bertel Thorvaldsen](p.0790.test-book.md)", updated)

    def test_same_href_does_not_resolve_other_subject(self) -> None:
        page = (
            "Monumento ad [Ahmed Shawky](p.0790.test-book.md) e "
            "Monumento a Firdausi nel parco.\n"
        )
        updated, report = link_subject_mentions_in_page(
            page,
            [
                ("a Ahmed Shawky", "p.0790.test-book.md"),
                ("a Firdusi", "p.0790.test-book.md"),
            ],
        )
        self.assertEqual(report.success_regex, ["Ahmed Shawky"])
        self.assertIn("a Firdusi", report.failed_regex)
        self.assertNotIn("a Firdusi", report.regex_resolved_labels)
        self.assertNotIn("[Firdausi]", updated)

    def test_fuzzy_already_linked_counts_as_ai_not_regex(self) -> None:
        page = "Il monumento a [Firdausi](p.0790.test-book.md) nel parco.\n"
        _, report = link_subject_mentions_in_page(
            page, [("a Firdusi", "p.0790.test-book.md")]
        )
        self.assertEqual(report.success_regex, [])
        self.assertEqual(report.success_ai, ["Firdausi"])
        self.assertEqual(report.failed_regex, ["a Firdusi"])
        self.assertIn("a Firdusi", report.regex_resolved_labels)

    def test_unwrap_links_to_page_hrefs(self) -> None:
        from src.ingestion.output_writer import unwrap_links_to_page_hrefs

        page = (
            "Monumento ad [Ahmed Shawky](p.0790.test-book.md) e "
            "[Firdausi](p.0790.test-book.md); resta [39](p.0039.test-book.md).\n"
        )
        out = unwrap_links_to_page_hrefs(page, {"p.0790.test-book.md"})
        self.assertEqual(
            out,
            "Monumento ad Ahmed Shawky e Firdausi; resta [39](p.0039.test-book.md).\n",
        )

    def test_sanitize_llm_keeps_only_subject_links(self) -> None:
        from src.ingestion.index_page_subject_links import _sanitize_llm_subject_links

        before = (
            "Il *Monumento a Firdausi* e il *Monumento a Victor Hugo* nel parco.\n"
            "Anche [Ahmed Shawky](p.0790.test-book.md).\n"
        )
        after = (
            "Il [*Monumento a Firdausi*](p.0790.test-book.md) e il "
            "[*Monumento a Victor Hugo*](p.0790.test-book.md) nel parco.\n"
            "Anche [Ahmed Shawky](p.0790.test-book.md).\n"
        )
        sanitized = _sanitize_llm_subject_links(
            before,
            after,
            label="a Firdusi",
            href="p.0790.test-book.md",
        )
        self.assertIsNotNone(sanitized)
        text, kept = sanitized or ("", [])
        self.assertEqual(kept, ["*Monumento a Firdausi*"])
        self.assertIn("[*Monumento a Firdausi*](p.0790.test-book.md)", text)
        self.assertIn("*Monumento a Victor Hugo*", text)
        self.assertNotIn("[*Monumento a Victor Hugo*]", text)
        self.assertIn("[Ahmed Shawky](p.0790.test-book.md)", text)

    def test_rewrite_index_joins_split_label_and_pages(self) -> None:
        subjects = [
            RawSubject(
                raw_label="Abbazia delle Tre Fontane",
                original_pages=[693, 694, 695],
                aligned_pages=[693, 694, 695],
            )
        ]
        subject_keys = allocate_subject_keys(subjects)
        out = rewrite_index_md_with_page_links(
            "Abbazia delle Tre Fontane,\n693-695\n",
            subject_keys,
            slug="test-book",
            original_to_aligned={693: 693, 694: 694, 695: 695},
            from_pages_dir=True,
        )
        self.assertIn("[693](p.0693.test-book.md)", out)
        self.assertNotIn("693-695\n", out)

    def test_apply_index_cross_links_two_way(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            index_page = pages_dir / "p.0100.test-book.md"
            content_page = pages_dir / "p.0039.test-book.md"
            index_page.write_text("Acqua Claudia, 39\n", encoding="utf-8")
            content_page.write_text(
                "Testo con Acqua Claudia nel paragrafo.\n", encoding="utf-8"
            )
            index_path = root / "INDEX.md"
            index_path.write_text(
                "# INDEX — Test\n\nAcqua Claudia, 39\n",
                encoding="utf-8",
            )
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[
                    BookPageOutput(aligned=39, original=39, file=content_page),
                    BookPageOutput(aligned=100, original=100, file=index_page),
                ],
            )
            mapping = {39: 39, 100: 100}
            settings = MagicMock()
            settings.editor_model = None
            events: list[dict] = []
            stats = asyncio.run(
                apply_index_cross_links(
                    index_path,
                    book,
                    _enumeration(mapping, index_range=(100, 100)),
                    client=None,
                    settings=settings,
                    request_id="req",
                    progress=events.append,
                )
            )
            self.assertEqual(stats["subjects"], 1)
            self.assertEqual(stats["pages_updated"], 1)
            self.assertEqual(stats["index_pages_updated"], 1)
            page_steps = [e for e in events if e.get("status") == "page_progress"]
            self.assertEqual(len(page_steps), 3)
            self.assertEqual(page_steps[0]["page_total"], 3)
            self.assertIn("Regex INDEX", page_steps[0]["message"])
            self.assertEqual(page_steps[1]["aligned_page"], 100)
            self.assertEqual(page_steps[2]["aligned_page"], 39)
            index_text = index_path.read_text(encoding="utf-8")
            self.assertIn(f"[Acqua Claudia]({POLYINDEX_REF_PLACEHOLDER})", index_text)
            self.assertIn("[39](pages/p.0039.test-book.md)", index_text)
            self.assertIn("[39](p.0039.test-book.md)", index_page.read_text(encoding="utf-8"))
            page_text = content_page.read_text(encoding="utf-8")
            self.assertIn("[Acqua Claudia](p.0100.test-book.md)", page_text)
            index_json = book_index_json_path(root, "test-book")
            data = json.loads(index_json.read_text(encoding="utf-8"))
            self.assertEqual(data["subjects"]["acqua-claudia"]["source_index_page"], 100)
            self.assertEqual(data["page_subjects"]["39"], ["acqua-claudia"])

    def test_llm_fallback_when_regex_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            index_page = pages_dir / "p.0100.test-book.md"
            content_page = pages_dir / "p.0039.test-book.md"
            index_page.write_text("Acqua Claudia, 39\n", encoding="utf-8")
            content_page.write_text("Testo con A. Claudia nel paragrafo.\n", encoding="utf-8")
            index_path = root / "INDEX.md"
            index_path.write_text("# INDEX — Test\n\nAcqua Claudia, 39\n", encoding="utf-8")
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[
                    BookPageOutput(aligned=39, original=39, file=content_page),
                    BookPageOutput(aligned=100, original=100, file=index_page),
                ],
            )
            settings = MagicMock()
            settings.editor_model = "editor-x"
            client = MagicMock()

            async def _fake_llm(*args, **kwargs):
                return "Testo con [A. Claudia](p.0100.test-book.md) nel paragrafo.\n"

            with patch(
                "src.ingestion.index_page_subject_links.chat_completion_with_retry",
                new=AsyncMock(side_effect=_fake_llm),
            ):
                stats = asyncio.run(
                    apply_index_cross_links(
                        index_path,
                        book,
                        _enumeration({39: 39, 100: 100}, index_range=(100, 100)),
                        client=client,
                        settings=settings,
                        request_id="req",
                    )
                )
            self.assertEqual(stats["llm_links"], 1)
            self.assertIn(
                "[A. Claudia](p.0100.test-book.md)",
                content_page.read_text(encoding="utf-8"),
            )

    def test_audit_index_cross_links_readiness_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages_dir = root / "pages"
            pages_dir.mkdir()
            index_page = pages_dir / "p.0100.test-book.md"
            content_page = pages_dir / "p.0039.test-book.md"
            index_page.write_text("Acqua Claudia, 39\n", encoding="utf-8")
            content_page.write_text("Testo con Acqua Claudia.\n", encoding="utf-8")
            index_path = root / "INDEX.md"
            index_path.write_text("Acqua Claudia, 39\n", encoding="utf-8")
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[
                    BookPageOutput(aligned=39, original=39, file=content_page),
                    BookPageOutput(aligned=100, original=100, file=index_page),
                ],
            )
            audit = audit_index_cross_links_readiness(
                index_path,
                book,
                _enumeration({39: 39, 100: 100}, index_range=(100, 100)),
            )
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["stats"]["subjects_with_aligned_pages"], 1)

    def test_audit_index_cross_links_readiness_missing_index_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = BookOutput(
                output_dir=root,
                manifest_path=root / "manifest.json",
                slug="test-book",
                pages=[],
            )
            audit = audit_index_cross_links_readiness(
                root / "INDEX.md",
                book,
                _enumeration({1: 1}, index_range=(1, 1)),
            )
            self.assertFalse(audit["ok"])
            self.assertIn("INDEX.md missing", audit["issues"][0])


if __name__ == "__main__":
    unittest.main()
