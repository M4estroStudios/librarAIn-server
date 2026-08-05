import unittest

from src.ingestion.biblio_llm import normalize_biblio_entry, parse_biblio_lines_fallback


class BiblioTitleNormalizationTests(unittest.TestCase):
    def test_strips_curators_from_title(self):
        entry = normalize_biblio_entry(
            {
                "authors": "AA.VV.",
                "title": "Il Foro Italico e lo Stadio Olimpico, a cura di M. Caporilli e F. Simeoni",
                "year": 1990,
                "extras": {"publication_place": "Roma"},
            }
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["title"], "Il Foro Italico e lo Stadio Olimpico")
        self.assertEqual(entry["authors"], "AA.VV.")
        self.assertEqual(entry["extras"]["curators"], "M. Caporilli e F. Simeoni")
        self.assertEqual(entry["extras"]["publication_place"], "Roma")

    def test_strips_volumes_from_title(self):
        entry = normalize_biblio_entry(
            {
                "authors": "AA.VV.",
                "title": "Le strade di Roma, 6 voll.",
                "year": 1987,
            }
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["title"], "Le strade di Roma")
        self.assertEqual(entry["extras"]["volumes"], "6 voll.")

    def test_strips_author_prefix_from_title(self):
        entry = normalize_biblio_entry(
            {
                "authors": "ARMELLINI M.",
                "title": "ARMELLINI M., Le chiese di Roma dal secolo IV al XIX, 2 voll.",
                "year": 1942,
            }
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["title"], "Le chiese di Roma dal secolo IV al XIX")
        self.assertEqual(entry["extras"]["volumes"], "2 voll.")

    def test_fallback_parser_splits_metadata(self):
        text = (
            "AA.VV., Il Foro Italico e lo Stadio Olimpico, "
            "a cura di M. Caporilli e F. Simeoni, Roma 1990."
        )
        entries = parse_biblio_lines_fallback(text)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["authors"], "AA.VV.")
        self.assertEqual(entry["title"], "Il Foro Italico e lo Stadio Olimpico")
        self.assertEqual(entry["year"], 1990)
        self.assertEqual(entry["extras"]["curators"], "M. Caporilli e F. Simeoni")
        self.assertEqual(entry["extras"]["publication_place"], "Roma")


if __name__ == "__main__":
    unittest.main()
