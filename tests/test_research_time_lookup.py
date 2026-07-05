from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.search.poh_time_range import (
    clear_poh_time_range_cache,
    get_poh_time_range_index,
    lookup_poh_time_range,
)
from src.search.request_schema import ResearchPoh
from src.search.time_lookup import lookup_time


SHA = "a" * 64


def _time_index(
    *,
    years: dict | None = None,
    dates: dict | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "years": years or {},
        "dates": dates or {},
    }


def _year_entry(pages: list[int]) -> dict:
    return {"books": {SHA: {"aligned_pages": pages, "original_pages": pages}}}


class TestTimeLookup(unittest.TestCase):
    def test_no_time_refs_returns_unchanged(self) -> None:
        candidates = {SHA: [10, 20]}
        result = lookup_time(
            "chi era Marco Polo",
            None,
            candidates,
            _time_index(),
            request_id="req-1",
        )
        self.assertEqual(result.pages, candidates)
        self.assertEqual(result.timeline_candidates, [])
        self.assertEqual(result.matched_labels, 0)

    def test_year_lookup_enriches_pages_and_timeline(self) -> None:
        candidates = {SHA: [10]}
        result = lookup_time(
            "cosa accadde nel 1848",
            None,
            candidates,
            _time_index(years={"1848": _year_entry([50, 51])}),
            request_id="req-2",
        )
        self.assertEqual(result.pages, {SHA: [10, 50, 51]})
        self.assertEqual(len(result.timeline_candidates), 1)
        self.assertEqual(result.timeline_candidates[0].label, "1848")
        self.assertEqual(result.timeline_candidates[0].aligned_pages, [50, 51])

    def test_date_lookup_with_year_fallback(self) -> None:
        result = lookup_time(
            "il 12 marzo 1848",
            None,
            {},
            _time_index(
                years={"1848": _year_entry([30])},
                dates={},
            ),
            request_id="req-3",
        )
        self.assertEqual(result.pages, {SHA: [30]})
        self.assertEqual(result.timeline_candidates[0].label, "12 marzo 1848")
        self.assertEqual(result.fallback_labels, 1)

    def test_period_range_end_year_fallback_to_start(self) -> None:
        result = lookup_time(
            "eventi nel periodo 1271-1295",
            None,
            {},
            _time_index(years={"1271": _year_entry([100, 101])}),
            request_id="req-4",
        )
        self.assertEqual(result.pages, {SHA: [100, 101]})
        labels = {item.label for item in result.timeline_candidates}
        self.assertIn("1271–1295", labels)
        self.assertEqual(result.fallback_labels, 1)

    def test_poh_time_range_used_for_lookup(self) -> None:
        poh = ResearchPoh(label="Marco Polo", time_range="1271-1295")
        result = lookup_time(
            "viaggi in oriente",
            poh,
            {},
            _time_index(years={"1271": _year_entry([5])}),
            request_id="req-5",
        )
        self.assertEqual(result.pages, {SHA: [5]})
        self.assertTrue(any(item.label == "1271–1295" for item in result.timeline_candidates))

    def test_date_hit_in_dates_section(self) -> None:
        result = lookup_time(
            "il 12 marzo 1848",
            None,
            {},
            _time_index(
                dates={"12 marzo 1848": _year_entry([44])},
                years={"1848": _year_entry([99])},
            ),
            request_id="req-6",
        )
        self.assertEqual(result.pages, {SHA: [44]})
        self.assertEqual(result.fallback_labels, 0)

    def test_unknown_label_no_enrichment(self) -> None:
        candidates = {SHA: [10]}
        result = lookup_time(
            "nel 9999",
            None,
            candidates,
            _time_index(),
            request_id="req-7",
        )
        self.assertEqual(result.pages, candidates)
        self.assertEqual(result.timeline_candidates, [])

    def test_adds_book_not_in_candidates(self) -> None:
        other_sha = "b" * 64
        result = lookup_time(
            "nel 1848",
            None,
            {SHA: [10]},
            _time_index(
                years={
                    "1848": {
                        "books": {
                            SHA: {"aligned_pages": [50]},
                            other_sha: {"aligned_pages": [70]},
                        }
                    }
                }
            ),
            request_id="req-8",
        )
        self.assertEqual(result.pages[SHA], [10, 50])
        self.assertEqual(result.pages[other_sha], [70])


class TestPohTimeRangeLookup(unittest.TestCase):
    def setUp(self) -> None:
        clear_poh_time_range_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.polyindex_dir = Path(self.tmp.name) / "polyindex"
        self.polyindex_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        clear_poh_time_range_cache()
        self.tmp.cleanup()

    def _write_time_index(self, payload: dict) -> None:
        path = self.polyindex_dir / "TIME_INDEX.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_lookup_by_subject_list(self) -> None:
        self._write_time_index(
            {
                "schema_version": "1.0",
                "years": {
                    "63": {"subjects": ["augusto"]},
                    "14": {"subjects": ["tiberio"]},
                },
            }
        )
        self.assertEqual(lookup_poh_time_range(self.polyindex_dir, "augusto", "Augusto"), "63")

    def test_lookup_by_pages_map(self) -> None:
        self._write_time_index(
            {
                "schema_version": "1.0",
                "years": {
                    "1271": {"pages": {"book-a": {"marco-polo": [10]}}},
                },
            }
        )
        self.assertEqual(
            lookup_poh_time_range(self.polyindex_dir, "marco-polo", "Marco Polo"),
            "1271",
        )

    def test_lookup_label_fallback(self) -> None:
        self._write_time_index(
            {
                "schema_version": "1.0",
                "years": {"1848": {"subjects": []}},
            }
        )
        self.assertEqual(
            lookup_poh_time_range(self.polyindex_dir, "unknown", "eventi nel 1848"),
            "1848",
        )

    def test_index_is_cached_until_mtime_changes(self) -> None:
        self._write_time_index(
            {
                "schema_version": "1.0",
                "years": {"100": {"subjects": ["alpha"]}},
            }
        )
        first = get_poh_time_range_index(self.polyindex_dir)
        second = get_poh_time_range_index(self.polyindex_dir)
        self.assertIs(first, second)
        self._write_time_index(
            {
                "schema_version": "1.0",
                "years": {"200": {"subjects": ["beta"]}},
            }
        )
        third = get_poh_time_range_index(self.polyindex_dir)
        self.assertIsNot(first, third)
        self.assertEqual(third.lookup("beta", "Beta"), "200")


if __name__ == "__main__":
    unittest.main()
