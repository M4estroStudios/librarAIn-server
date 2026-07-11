from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from src.export.etaly_adapter import (
    ApprovedMetadata,
    ExportBlockedError,
    build_timeline,
    extract_cited_pages,
    normalize_year,
    rewrite_poh_links,
    sanitize_body,
    to_etaly_article,
)
from src.export.registry import EtalyRegistry
from src.search.postprocess import PostprocessResult, TimelineRowRecord

_ARTICLE = """# Gian Lorenzo Bernini

Testo su [Bernini](poh:bernini-slug) con **grassetto** e un
[riferimento](source:aabbcc:aligned:12) e un altro [rif2](source:ddeeff:aligned:7).

Vedi anche [Roma](poh:roma-slug) e una terza fonte [rif3](source:112233:aligned:3).

## Cronologia

| Periodo | Evento | Fonti |
|---------|--------|-------|
| 1598 | Nascita a Napoli | [f](source:aabbcc:aligned:12) |
| 1618 | Prima opera | |
| 1625 | Baldacchino di San Pietro | |
| 1650 | Fontana dei Fiumi | |
| 1680 | Morte a Roma | |
| -509 | Evento antico | |

## Annotazioni

Nota finale del revisore.
"""


def _empty_postprocess() -> PostprocessResult:
    return PostprocessResult(markdown="")


class EtalyAdapterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.registry = EtalyRegistry(
            registry_path=root / "data" / "etaly" / "registry.json",
            assets_path=root / "assets",
        )
        self.registry.confirm("bernini-slug", "poh_p0001")
        self.registry.confirm("roma-slug", "poh_o0010")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _frontmatter(markdown: str) -> dict:
        parts = markdown.split("---", 2)
        assert len(parts) == 3, "expected a leading YAML frontmatter block"
        return yaml.safe_load(parts[1])


class TestFullConversion(EtalyAdapterTestBase):
    def test_happy_path(self) -> None:
        approved = ApprovedMetadata(poh_id="poh_p0001", poh_type="p")
        article = to_etaly_article(_ARTICLE, _empty_postprocess(), approved, self.registry)

        body = article.markdown
        self.assertIn("[[poh_p0001|Bernini]]", body)
        self.assertIn("[[poh_o0010|Roma]]", body)

        self.assertIn("source:ddeeff:aligned:7", body)
        self.assertIn("source:112233:aligned:3", body)

        self.assertNotIn("## Cronologia", body)
        self.assertNotIn("# Gian Lorenzo Bernini", body)
        self.assertFalse(body.lstrip().startswith("# "))

        self.assertIn("## Annotazioni", body)
        self.assertIn("Nota finale del revisore.", body)

        self.assertEqual(
            article.cited_pages,
            {("aabbcc", 12), ("ddeeff", 7), ("112233", 3)},
        )

        frontmatter = self._frontmatter(body)
        self.assertEqual(frontmatter["id"], "poh_p0001")
        self.assertEqual(frontmatter["name"], "Gian Lorenzo Bernini")
        year_keys = [key for key in frontmatter if isinstance(key, int)]
        self.assertLessEqual(len(year_keys), 5)
        self.assertIn(-509, year_keys)

    def test_frontmatter_is_valid_yaml(self) -> None:
        approved = ApprovedMetadata(
            poh_id="poh_m0001",
            poh_type="m",
            name="Fontana dei Fiumi",
            wiki_url="https://it.wikipedia.org/wiki/Fontana_dei_Quattro_Fiumi",
            poi_id=42,
            lat=41.899,
            lon=12.473,
            region="Lazio",
            category="fontana",
        )
        article = to_etaly_article(_ARTICLE, _empty_postprocess(), approved, self.registry)
        frontmatter = self._frontmatter(article.markdown)
        self.assertEqual(frontmatter["poi_id"], 42)
        self.assertAlmostEqual(frontmatter["lat"], 41.899)
        self.assertEqual(frontmatter["region"], "Lazio")
        self.assertEqual(frontmatter["name"], "Fontana dei Fiumi")


class TestUnresolvedGate(EtalyAdapterTestBase):
    def test_unresolved_slug_raises(self) -> None:
        article = _ARTICLE.replace("poh:roma-slug", "poh:missing-slug")
        approved = ApprovedMetadata(poh_id="poh_p0001", poh_type="p")
        with self.assertRaises(ExportBlockedError) as ctx:
            to_etaly_article(article, _empty_postprocess(), approved, self.registry)
        self.assertIn("missing-slug", str(ctx.exception))
        self.assertEqual(ctx.exception.unresolved_slugs, ["missing-slug"])


class TestSanitize(EtalyAdapterTestBase):
    def test_file_embed_and_http_link(self) -> None:
        cleaned, warnings = sanitize_body(
            "Testo [[File:foo.jpg]] e un [x](http://y) qui."
        )
        self.assertNotIn("File:foo.jpg", cleaned)
        self.assertNotIn("http://y", cleaned)
        self.assertIn(" x ", f" {cleaned} ")
        self.assertTrue(any("media embed" in w for w in warnings))
        self.assertTrue(any("web link" in w for w in warnings))

    def test_bold_and_wikilink_preserved(self) -> None:
        cleaned, _ = sanitize_body("**forte** e [[poh_p0001|Bernini]] restano.")
        self.assertIn("**forte**", cleaned)
        self.assertIn("[[poh_p0001|Bernini]]", cleaned)


class TestUnitHelpers(EtalyAdapterTestBase):
    def test_normalize_year(self) -> None:
        self.assertEqual(normalize_year("1598"), (1598, False))
        self.assertEqual(normalize_year("1680/12"), (1680, False))
        self.assertEqual(normalize_year("1680/12/25"), (1680, False))
        self.assertEqual(normalize_year("-509"), (-509, True))
        self.assertEqual(normalize_year("-43/01/01"), (-43, True))
        self.assertEqual(normalize_year("509 a.C."), (None, False))
        self.assertEqual(normalize_year("epoca imprecisata"), (None, False))

    def test_is_valid_period(self) -> None:
        from src.export.etaly_adapter import is_valid_period

        self.assertTrue(is_valid_period("1946"))
        self.assertTrue(is_valid_period("-36"))
        self.assertTrue(is_valid_period("1447/03"))
        self.assertTrue(is_valid_period("-43/01/01"))
        self.assertFalse(is_valid_period("36 a.C."))
        self.assertFalse(is_valid_period("V secolo"))
        self.assertFalse(is_valid_period("1271–1295"))

    def test_rewrite_poh_links_reports_unresolved(self) -> None:
        text = "[A](poh:bernini-slug) [B](poh:nope)"
        rewritten, unresolved = rewrite_poh_links(text, self.registry)
        self.assertIn("[[poh_p0001|A]]", rewritten)
        self.assertEqual(unresolved, ["nope"])

    def test_extract_cited_pages_dedup(self) -> None:
        text = "[a](source:aa:aligned:1) [b](source:aa:aligned:1) [c](source:bb:aligned:2)"
        self.assertEqual(extract_cited_pages(text), {("aa", 1), ("bb", 2)})

    def test_approved_timeline_preferred(self) -> None:
        approved = ApprovedMetadata(
            poh_id="poh_p0001",
            poh_type="p",
            timeline=[{"anno": 1598, "evento": "Nascita"}, {"anno": 1680, "evento": "Morte"}],
        )
        warnings: list[str] = []
        entries = build_timeline(_ARTICLE, approved, _empty_postprocess(), warnings)
        self.assertEqual([e.year for e in entries], [1598, 1680])

    def test_postprocess_rows_fallback(self) -> None:
        approved = ApprovedMetadata(poh_id="poh_p0001", poh_type="p")
        result = PostprocessResult(
            markdown="",
            timeline_rows=[
                TimelineRowRecord(period="1600", event="Evento", source_links=[]),
            ],
        )
        warnings: list[str] = []
        entries = build_timeline("Nessuna cronologia qui.", approved, result, warnings)
        self.assertEqual([e.year for e in entries], [1600])


if __name__ == "__main__":
    unittest.main()
