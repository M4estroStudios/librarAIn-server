from __future__ import annotations

import unittest

from src.export.etaly_adapter import EtalyArticle
from src.export.lint import (
    CODE_DANGLING_CITATION,
    CODE_FRONTMATTER,
    CODE_UNRESOLVED_LINK,
    CODE_UNSUPPORTED_SYNTAX,
    LintGateError,
    assert_exportable,
    format_report,
    lint_article,
    lint_bundle,
)

_GOOD_MARKDOWN = """---
id: poh_p0001
name: Gian Lorenzo Bernini
1598: "Nascita a Napoli"
1680: "Morte a Roma"
---

Testo con **grassetto**, un [[poh_o0010|Roma]] e una fonte
[riferimento](source:aabbcc:aligned:12).

## Annotazioni

Nota finale del revisore.
"""

_GOOD_PAGES = {("aabbcc", 12)}


def _article(markdown: str, poh_id: str = "poh_p0001") -> EtalyArticle:
    return EtalyArticle(poh_id=poh_id, markdown=markdown)


class TestWellFormed(unittest.TestCase):
    def test_clean_article_is_ok(self) -> None:
        report = lint_article(_article(_GOOD_MARKDOWN), available_pages=_GOOD_PAGES)
        self.assertTrue(report.ok)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.error_codes, [])


class TestUnresolvedLinks(unittest.TestCase):
    def test_residual_poh_link(self) -> None:
        md = _GOOD_MARKDOWN + "\nVedi [Bernini](poh:bernini-slug) qui.\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNRESOLVED_LINK, report.error_codes)

    def test_invalid_wikilink_target(self) -> None:
        md = _GOOD_MARKDOWN + "\nLink rotto [[not-an-id|Testo]].\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNRESOLVED_LINK, report.error_codes)


class TestFrontmatter(unittest.TestCase):
    def test_missing_name(self) -> None:
        md = """---
id: poh_p0001
1598: "Nascita"
---

Corpo.
"""
        report = lint_article(_article(md), available_pages=set())
        self.assertFalse(report.ok)
        self.assertIn(CODE_FRONTMATTER, report.error_codes)

    def test_no_timeline_year(self) -> None:
        md = """---
id: poh_p0001
name: Bernini
---

Corpo.
"""
        report = lint_article(_article(md), available_pages=set())
        self.assertFalse(report.ok)
        self.assertIn(CODE_FRONTMATTER, report.error_codes)

    def test_invalid_id(self) -> None:
        md = """---
id: poh_x1
name: Bernini
1598: "Nascita"
---

Corpo.
"""
        report = lint_article(_article(md), available_pages=set())
        self.assertFalse(report.ok)
        self.assertIn(CODE_FRONTMATTER, report.error_codes)


class TestUnsupportedSyntax(unittest.TestCase):
    def test_file_embed(self) -> None:
        md = _GOOD_MARKDOWN + "\n[[File:foo.jpg]]\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNSUPPORTED_SYNTAX, report.error_codes)

    def test_raw_html(self) -> None:
        md = _GOOD_MARKDOWN + "\nTesto <b>grassetto</b> qui.\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNSUPPORTED_SYNTAX, report.error_codes)

    def test_gfm_table_separator(self) -> None:
        md = _GOOD_MARKDOWN + "\n| Periodo | Evento |\n|---|---|\n| 1598 | Nascita |\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNSUPPORTED_SYNTAX, report.error_codes)

    def test_http_link(self) -> None:
        md = _GOOD_MARKDOWN + "\nVedi [sito](https://example.com) qui.\n"
        report = lint_article(_article(md), available_pages=_GOOD_PAGES)
        self.assertFalse(report.ok)
        self.assertIn(CODE_UNSUPPORTED_SYNTAX, report.error_codes)


class TestDanglingCitations(unittest.TestCase):
    def test_missing_page_is_error(self) -> None:
        report = lint_article(_article(_GOOD_MARKDOWN), available_pages=set())
        self.assertFalse(report.ok)
        self.assertIn(CODE_DANGLING_CITATION, report.error_codes)

    def test_page_present_is_ok(self) -> None:
        report = lint_article(_article(_GOOD_MARKDOWN), available_pages={("AABBCC", 12)})
        self.assertTrue(report.ok)


class TestGate(unittest.TestCase):
    def test_assert_exportable_raises_with_poh_and_codes(self) -> None:
        bad = lint_article(_article(_GOOD_MARKDOWN), available_pages=set())
        good = lint_article(
            _article(_GOOD_MARKDOWN, poh_id="poh_o0010"), available_pages=_GOOD_PAGES
        )
        reports = lint_bundle([bad, good])
        with self.assertRaises(LintGateError) as ctx:
            assert_exportable(reports)
        self.assertIn("poh_p0001", str(ctx.exception))
        self.assertIn(CODE_DANGLING_CITATION, str(ctx.exception))
        self.assertEqual(ctx.exception.failures["poh_p0001"], [CODE_DANGLING_CITATION])

    def test_assert_exportable_passes_when_ok(self) -> None:
        good = lint_article(_article(_GOOD_MARKDOWN), available_pages=_GOOD_PAGES)
        assert_exportable([good])

    def test_format_report_includes_poh_and_codes(self) -> None:
        bad = lint_article(_article(_GOOD_MARKDOWN), available_pages=set())
        rendered = format_report([bad])
        self.assertIn("poh_p0001", rendered)
        self.assertIn(CODE_DANGLING_CITATION, rendered)


if __name__ == "__main__":
    unittest.main()
