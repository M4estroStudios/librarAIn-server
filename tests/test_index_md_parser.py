from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.log import WARNING_LOG_LEVEL
from src.ingestion.polyindex.index_md_parser import (
    RawSubject,
    normalize_label,
    parse_index_md,
    parse_index_md_with_skipped,
    sort_index_md_body,
    write_skipped_lines_report,
)
from src.models.request import PageRange, UsefulPagesEnumeration

SHA = "cafebabe" * 8


def _enumeration(
    page_count: int = 100,
    page_offset: int = 0,
) -> UsefulPagesEnumeration:
    original_pages = list(range(1, page_count + 1))
    mapping = {orig: orig + page_offset for orig in original_pages}
    return UsefulPagesEnumeration(
        source_sha256=SHA,
        original_page_count=page_count,
        aligned_page_count=page_count,
        useful_original_pages=original_pages,
        original_page_to_aligned_page=mapping,
        aligned_page_to_original_page={aligned: orig for orig, aligned in mapping.items()},
        toc_range_aligned=PageRange(start=1, end=page_count),
        index_range_aligned=PageRange(start=page_count, end=page_count),
    )


def _write_index(tmp: Path, body_lines: list[str], title: str = "Test Book") -> Path:
    index_path = tmp / "INDEX.md"
    lines = [f"# INDEX — {title}", ""] + body_lines
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


class TestParseIndexMd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_comma_separated_pages(self) -> None:
        index_path = _write_index(self.tmp, ["Marco Polo, 12, 45, 88"])
        subjects = parse_index_md(index_path, _enumeration())

        self.assertEqual(
            subjects,
            [
                RawSubject(
                    raw_label="Marco Polo",
                    original_pages=[12, 45, 88],
                    aligned_pages=[12, 45, 88],
                )
            ],
        )

    def test_page_range_expansion(self) -> None:
        index_path = _write_index(self.tmp, ["Venezia, 12-15"])
        subjects = parse_index_md(index_path, _enumeration())

        self.assertEqual(
            subjects,
            [
                RawSubject(
                    raw_label="Venezia",
                    original_pages=[12, 13, 14, 15],
                    aligned_pages=[12, 13, 14, 15],
                )
            ],
        )

    def test_em_dash_format_with_mixed_range_and_single_page(self) -> None:
        index_path = _write_index(self.tmp, ["Dogi — 12-15, 22"])
        subjects = parse_index_md(index_path, _enumeration())

        self.assertEqual(
            subjects,
            [
                RawSubject(
                    raw_label="Dogi",
                    original_pages=[12, 13, 14, 15, 22],
                    aligned_pages=[12, 13, 14, 15, 22],
                )
            ],
        )

    def test_vedi_cross_reference_sets_alias_of(self) -> None:
        index_path = _write_index(self.tmp, ["Lemma vedi Altro Lemma"])
        subjects = parse_index_md(index_path, _enumeration())

        self.assertEqual(
            subjects,
            [
                RawSubject(
                    raw_label="Lemma",
                    original_pages=[],
                    aligned_pages=[],
                    alias_of="Altro Lemma",
                )
            ],
        )

    def test_normalize_label_strips_accents_and_punctuation(self) -> None:
        self.assertEqual(normalize_label("  Città,  "), "citta")
        self.assertEqual(normalize_label("République."), "republique")

        index_path = _write_index(self.tmp, ["Città, 5"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10))

        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0].raw_label, "Città")
        self.assertEqual(normalize_label(subjects[0].raw_label), "citta")

    @patch("src.ingestion.polyindex.index_md_parser.Log")
    def test_unmapped_page_skipped_with_warning(self, mock_log) -> None:
        index_path = _write_index(self.tmp, ["Soggetto, 5, 999"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10))

        self.assertEqual(
            subjects,
            [
                RawSubject(
                    raw_label="Soggetto",
                    original_pages=[5],
                    aligned_pages=[5],
                )
            ],
        )
        mock_log.assert_called_once_with(
            WARNING_LOG_LEVEL,
            "index subject page not in mapping",
            {"line": "Soggetto, 5, 999", "original_page": 999},
        )

    def test_skips_subject_when_all_pages_unmapped(self) -> None:
        index_path = _write_index(self.tmp, ["Fantasma, 500, 501"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10))
        self.assertEqual(subjects, [])

    def test_skips_index_header_and_separators(self) -> None:
        index_path = _write_index(
            self.tmp,
            [
                "---",
                "# Sezione",
                "Roma, 3",
            ],
        )
        subjects = parse_index_md(index_path, _enumeration(page_count=10))
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0].raw_label, "Roma")

    def test_page_offset_maps_to_aligned_pages(self) -> None:
        index_path = _write_index(self.tmp, ["Milano, 4"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10, page_offset=100))

        self.assertEqual(subjects[0].original_pages, [4])
        self.assertEqual(subjects[0].aligned_pages, [104])

    def test_semicolon_and_colon_separators(self) -> None:
        index_path = _write_index(self.tmp, ["Tevere; 7", "Aventino: 9"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10))
        self.assertEqual(
            [(s.raw_label, s.original_pages) for s in subjects],
            [("Tevere", [7]), ("Aventino", [9])],
        )

    def test_trailing_pages_without_separator(self) -> None:
        index_path = _write_index(self.tmp, ["Foro Romano 7 9"])
        subjects = parse_index_md(index_path, _enumeration(page_count=10))
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0].raw_label, "Foro Romano")
        self.assertEqual(subjects[0].original_pages, [7, 9])

    def test_skipped_lines_are_reported_with_reason(self) -> None:
        index_path = _write_index(
            self.tmp,
            [
                "Roma, 3",
                "riga senza pagine e senza separatore valido",
                "Fantasma, 999",
            ],
        )
        subjects, skipped = parse_index_md_with_skipped(
            index_path, _enumeration(page_count=10)
        )
        self.assertEqual(len(subjects), 1)
        self.assertEqual(len(skipped), 2)
        reasons = {item.reason for item in skipped}
        self.assertIn("no_label_pages_separator", reasons)
        self.assertIn("all_pages_out_of_mapping", reasons)

    def test_all_caps_heading_not_reported_as_skipped(self) -> None:
        index_path = _write_index(self.tmp, ["ACQUEDOTTI", "Roma, 3"])
        _, skipped = parse_index_md_with_skipped(index_path, _enumeration(page_count=10))
        self.assertEqual(skipped, [])

    def test_write_skipped_lines_report(self) -> None:
        index_path = _write_index(self.tmp, ["riga ignorata senza numeri"])
        _, skipped = parse_index_md_with_skipped(index_path, _enumeration(page_count=10))
        report_path = write_skipped_lines_report(index_path, skipped)
        self.assertEqual(report_path.name, "INDEX.skipped.md")
        content = report_path.read_text(encoding="utf-8")
        self.assertIn("riga ignorata senza numeri", content)
        self.assertIn("no_label_pages_separator", content)


class TestSortIndexMdBody(unittest.TestCase):
    def test_sorts_entries_alphabetically_and_keeps_prefix(self) -> None:
        body = (
            "Bibliografia intro\n\n"
            "---\n\n"
            "Venezia, 4\n"
            "Marco Polo, 12, 45\n"
            "Colosseo — 5\n"
            "Lemma vedi Altro Lemma\n"
            "ACQUEDOTTI\n"
        )
        sorted_body = sort_index_md_body(body)
        self.assertTrue(sorted_body.startswith("Bibliografia intro"))
        entries = sorted_body.split("Bibliografia intro", 1)[-1].strip().splitlines()
        self.assertEqual(
            entries,
            [
                "ACQUEDOTTI",
                "Colosseo — 5",
                "Lemma vedi Altro Lemma",
                "Marco Polo, 12, 45",
                "Venezia, 4",
            ],
        )

    def test_sort_is_accent_insensitive(self) -> None:
        body = "Zürich, 1\nCittà, 2\n"
        sorted_body = sort_index_md_body(body)
        self.assertEqual(sorted_body.splitlines(), ["Città, 2", "Zürich, 1"])
