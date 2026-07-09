from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.export.registry import (
    EtalyRegistry,
    MatchCandidate,
    parse_poh_id,
    scan_etaly_assets,
)

_CSV_P = """name,id_code,beginning,end,shelf
Gian Lorenzo Bernini,poh_p0001,1618/01/01,1619/01/01,222
Augusto,poh_p0012,-403/01/01,-303/01/01,4
"""

_CSV_O = """name,id_code,beginning,end,shelf
Repubblica Romana,poh_o0010,-524/01/01,289/01/01,5
"""

_CSV_M = """name,id_code,beginning,end,shelf
Palazzo delle Esposizioni,poh_m0001,1877/01/01,2030/01/01,1595
"""

_MD_AUGUSTO = """---
id: poh_p0012
name: Augusto
wiki_title: Augusto
wiki_url: https://it.wikipedia.org/wiki/Augusto
wikidata_qid: Q1405
---

Testo dell'articolo su Augusto.
"""

_MD_BERNINI = """---
id: poh_p0001
name: Gian Lorenzo Bernini
wiki_title: Gian Lorenzo Bernini
wikidata_qid: Q5591
---

Testo dell'articolo su Bernini.
"""


class EtalyRegistryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.assets = self.root / "assets"
        self.registry_path = self.root / "data" / "etaly" / "registry.json"
        self._seed_assets()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_assets(self) -> None:
        csv_dir = self.assets / "timeline" / "data" / "csv"
        md_dir = self.assets / "timeline" / "data" / "text" / "ITA"
        csv_dir.mkdir(parents=True)
        md_dir.mkdir(parents=True)
        (csv_dir / "poh_p.csv").write_text(_CSV_P, encoding="utf-8")
        (csv_dir / "poh_o.csv").write_text(_CSV_O, encoding="utf-8")
        (csv_dir / "poh_m.csv").write_text(_CSV_M, encoding="utf-8")
        (md_dir / "poh_p0012.md").write_text(_MD_AUGUSTO, encoding="utf-8")
        (md_dir / "poh_p0001.md").write_text(_MD_BERNINI, encoding="utf-8")

    def _registry(self, *, fuzzy_threshold: float = 0.82) -> EtalyRegistry:
        return EtalyRegistry(
            registry_path=self.registry_path,
            assets_path=self.assets,
            fuzzy_threshold=fuzzy_threshold,
        )


class TestAssetScan(EtalyRegistryTestBase):
    def test_scan_builds_indexes(self) -> None:
        index = scan_etaly_assets(self.assets)
        self.assertEqual(index.qid_to_poh["Q1405"], "poh_p0012")
        self.assertEqual(index.max_number["p"], 12)
        self.assertEqual(index.max_number["o"], 10)
        self.assertEqual(index.max_number["m"], 1)


class TestAutoMatch(EtalyRegistryTestBase):
    def test_wikidata_qid_exact_match_resolves(self) -> None:
        registry = self._registry()
        entry = registry.auto_match("augusto", "Ottaviano Augusto", wikidata_qid="Q1405")
        self.assertEqual(entry.poh_id, "poh_p0012")
        self.assertEqual(entry.poh_type, "p")
        self.assertEqual(entry.status, "resolved")
        self.assertEqual(entry.source, "auto")
        self.assertEqual(registry.resolve("augusto"), entry)

    def test_similar_label_without_qid_is_pending_with_score(self) -> None:
        registry = self._registry()
        entry = registry.auto_match("bernini", "Gianlorenzo Bernini")
        self.assertEqual(entry.poh_id, "poh_p0001")
        self.assertEqual(entry.status, "pending")
        self.assertEqual(entry.source, "auto")
        self.assertIsNotNone(entry.score)
        assert entry.score is not None
        self.assertGreaterEqual(entry.score, registry.fuzzy_threshold)
        self.assertIsNone(registry.resolve("bernini"))

    def test_no_candidate_is_pending_without_poh_id(self) -> None:
        registry = self._registry(fuzzy_threshold=0.99)
        entry = registry.auto_match("ignoto", "Entita Totalmente Sconosciuta 9999")
        self.assertIsNone(entry.poh_id)
        self.assertEqual(entry.status, "pending")
        fresh_id = registry.next_id("p")
        self.assertEqual(fresh_id, "poh_p0013")


class TestNextId(EtalyRegistryTestBase):
    def test_next_id_monotonic_and_persistent(self) -> None:
        registry = self._registry()
        first = registry.next_id("p")
        second = registry.next_id("p")
        self.assertEqual(first, "poh_p0013")
        self.assertEqual(second, "poh_p0014")
        _, first_n = parse_poh_id(first) or ("p", 0)
        _, second_n = parse_poh_id(second) or ("p", 0)
        self.assertGreater(second_n, first_n)

        reloaded = self._registry()
        third = reloaded.next_id("p")
        self.assertEqual(third, "poh_p0015")
        _, third_n = parse_poh_id(third) or ("p", 0)
        self.assertGreater(third_n, second_n)
        self.assertEqual(reloaded.document.counters["p"], 15)

    def test_next_id_rejects_unknown_type(self) -> None:
        registry = self._registry()
        with self.assertRaises(ValueError):
            registry.next_id("x")


class TestProposeAndConfirm(EtalyRegistryTestBase):
    def test_propose_then_confirm(self) -> None:
        registry = self._registry()
        proposed = registry.propose(
            "apollo",
            MatchCandidate(poh_id="poh_p0099", name="Apollo", score=0.5),
        )
        self.assertEqual(proposed.status, "pending")
        self.assertEqual(proposed.poh_type, "p")
        self.assertIsNone(registry.resolve("apollo"))

        confirmed = registry.confirm("apollo", "poh_p0099", confirmed_by="tester")
        self.assertEqual(confirmed.status, "resolved")
        self.assertEqual(confirmed.source, "manual")
        self.assertEqual(confirmed.confirmed_by, "tester")
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(registry.resolve("apollo"), confirmed)


if __name__ == "__main__":
    unittest.main()
