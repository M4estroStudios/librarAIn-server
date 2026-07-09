from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.api.etaly_export_handler import (
    BundleBuildOutcome,
    ConfirmValidationError,
    build_approved_metadata,
    build_export_bundle,
    build_export_list,
    confirm_mapping,
    count_usable_timeline,
    merge_timeline,
    parse_confirm_request,
    parse_metadata_proposal,
    parse_timeline_fill,
    run_metadata_proposal,
    store_proposal,
)
from src.export.etaly_adapter import ExportBlockedError
from src.export.lint import LintGateError
from src.export.registry import EtalyRegistry
from src.search.postprocess import PostprocessResult

_INDEX = {
    "schema_version": "1.0",
    "subjects": {
        "bernini-slug": {
            "canonical_label": "Gian Lorenzo Bernini",
            "aliases": ["Bernini"],
            "time_range": "1598-1680",
            "books": {},
        },
        "roma-slug": {
            "canonical_label": "Roma",
            "aliases": [],
            "time_range": None,
            "books": {},
        },
    },
}


def _empty_postprocess(_markdown: str, _data_root: Path, _request_id: str) -> PostprocessResult:
    return PostprocessResult(markdown="")


class ExportHandlerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_root = self.root / "data"
        self.assets = self.root / "assets"
        (self.data_root / "polyindex").mkdir(parents=True)
        (self.data_root / "polyindex" / "INDEX.json").write_text(
            json.dumps(_INDEX), encoding="utf-8"
        )
        self.registry = EtalyRegistry(
            registry_path=self.data_root / "etaly" / "registry.json",
            assets_path=self.assets,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_article(self, slug: str, markdown: str) -> None:
        articles = self.data_root / "research" / "articles"
        articles.mkdir(parents=True, exist_ok=True)
        (articles / f"{slug}.md").write_text(markdown, encoding="utf-8")

    def _write_catalog(self, articles: dict) -> None:
        path = self.data_root / "research" / "catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"articles": articles}), encoding="utf-8")


class TestExportList(ExportHandlerTestBase):
    def test_list_reports_pending_and_resolved(self) -> None:
        self._write_article("bernini-slug", "# Bernini\n\nTesto.\n")
        self._write_article("roma-slug", "# Roma\n\nTesto.\n")
        self._write_catalog(
            {
                "bernini-slug": {"title": "Gian Lorenzo Bernini"},
                "roma-slug": {"title": "Roma"},
                "no-art": {"title": "Ignoto"},  # no markdown file -> skipped
            }
        )
        items = build_export_list(self.data_root, self.registry)
        slugs = {it["slug"] for it in items}
        self.assertEqual(slugs, {"bernini-slug", "roma-slug"})
        for it in items:
            self.assertEqual(it["mapping"]["status"], "pending")
            self.assertFalse(it["has_metadata_proposal"])

        # After confirming + storing a proposal, the mapping is resolved.
        self.registry.confirm("bernini-slug", "poh_p0001")
        store_proposal(self.data_root, "bernini-slug", {"poh_id": "poh_p0001"})
        items = {it["slug"]: it for it in build_export_list(self.data_root, self.registry)}
        self.assertEqual(items["bernini-slug"]["mapping"]["status"], "resolved")
        self.assertEqual(items["bernini-slug"]["mapping"]["poh_id"], "poh_p0001")
        self.assertTrue(items["bernini-slug"]["has_metadata_proposal"])

    def test_no_material_articles_are_skipped(self) -> None:
        self._write_article("bernini-slug", "# Bernini\n\nTesto.\n")
        self._write_catalog({"bernini-slug": {"title": "B", "no_material": True}})
        self.assertEqual(build_export_list(self.data_root, self.registry), [])


class TestProposalParsing(unittest.TestCase):
    def test_parse_metadata_proposal_valid(self) -> None:
        raw = json.dumps(
            {
                "tipo": "m",
                "name": "Fontana",
                "timeline": [{"anno": "1651", "evento": "Inaugurazione"}],
                "geo_hint": {"lat": 41.9, "lon": 12.47, "note": "Piazza Navona"},
            }
        )
        proposal = parse_metadata_proposal(raw)
        self.assertEqual(proposal["tipo"], "m")
        self.assertEqual(proposal["name"], "Fontana")
        self.assertEqual(len(proposal["timeline"]), 1)
        self.assertAlmostEqual(proposal["geo_hint"]["lat"], 41.9)

    def test_parse_metadata_proposal_strips_fences(self) -> None:
        raw = '```json\n{"tipo":"p","name":"X","timeline":[],"geo_hint":{}}\n```'
        proposal = parse_metadata_proposal(raw)
        self.assertEqual(proposal["tipo"], "p")
        self.assertEqual(proposal["geo_hint"], {"lat": None, "lon": None, "note": None})

    def test_parse_metadata_proposal_invalid_tipo(self) -> None:
        with self.assertRaises(ValueError):
            parse_metadata_proposal('{"tipo":"x","name":"X","timeline":[],"geo_hint":{}}')

    def test_parse_metadata_proposal_not_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_metadata_proposal("questo non e' json")

    def test_parse_timeline_fill_defensive(self) -> None:
        raw = '[{"anno":"1300","evento":"E","needs_review":true},{"bad":1}]'
        items = parse_timeline_fill(raw)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["needs_review"])
        self.assertEqual(parse_timeline_fill("not json"), [])

    def test_merge_timeline_dedup_and_cap(self) -> None:
        existing = [{"anno": "1300", "evento": "A"}, {"anno": 1300, "evento": "dup"}]
        extra = [{"anno": f"1{n:03d}", "evento": str(n)} for n in range(400, 410)]
        merged = merge_timeline(existing, extra)
        self.assertEqual(len(merged), 5)
        self.assertEqual(count_usable_timeline(merged), 5)


class TestConfirmParsing(unittest.TestCase):
    def test_missing_slug(self) -> None:
        with self.assertRaises(ConfirmValidationError):
            parse_confirm_request({"poh_type": "p"})

    def test_bad_poh_type(self) -> None:
        with self.assertRaises(ConfirmValidationError):
            parse_confirm_request({"slug": "s", "poh_type": "z"})

    def test_poh_id_type_mismatch(self) -> None:
        with self.assertRaises(ConfirmValidationError):
            parse_confirm_request({"slug": "s", "poh_type": "p", "poh_id": "poh_o0001"})

    def test_valid(self) -> None:
        req = parse_confirm_request(
            {
                "slug": "s",
                "poh_type": "m",
                "name": "N",
                "geo": {"lat": 1.0},
                "timeline": [{"anno": 1500, "evento": "E"}],
            }
        )
        self.assertEqual(req.slug, "s")
        self.assertEqual(req.poh_type, "m")
        self.assertIsNone(req.poh_id)


class TestRunProposal(ExportHandlerTestBase):
    def test_metadata_and_timeline_fill_merge(self) -> None:
        self._write_article("bernini-slug", "# Bernini\n\nNato nel 1598.\n")

        def fake_llm(_settings, *, system_prompt, user_message, stage, request_id):
            if stage == "etaly_metadata":
                return json.dumps(
                    {
                        "tipo": "p",
                        "name": "",
                        "timeline": [{"anno": "1598", "evento": "Nascita"}],
                        "geo_hint": {"lat": None, "lon": None, "note": None},
                    }
                )
            return json.dumps([{"anno": "1680", "evento": "Morte", "needs_review": True}])

        proposal = run_metadata_proposal(
            "bernini-slug",
            data_root=self.data_root,
            settings=object(),
            llm=fake_llm,
        )
        self.assertEqual(proposal["tipo"], "p")
        # name defaulted from canonical label
        self.assertEqual(proposal["name"], "Gian Lorenzo Bernini")
        annos = [t["anno"] for t in proposal["timeline"]]
        self.assertIn("1598", annos)
        self.assertIn("1680", annos)


class TestConfirmMapping(ExportHandlerTestBase):
    def test_new_poh_gets_next_id_and_stores_metadata(self) -> None:
        req = parse_confirm_request(
            {
                "slug": "bernini-slug",
                "poh_type": "p",
                "name": "Gian Lorenzo Bernini",
                "timeline": [{"anno": "1598", "evento": "Nascita"}],
            }
        )
        result = confirm_mapping(req, data_root=self.data_root, registry=self.registry)
        self.assertTrue(result["poh_id"].startswith("poh_p"))
        self.assertEqual(result["status"], "resolved")
        self.assertIsNotNone(self.registry.resolve("bernini-slug"))
        stored_path = self.data_root / "etaly" / "proposals" / "bernini-slug.json"
        self.assertTrue(stored_path.is_file())
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["timeline"], [{"anno": 1598, "evento": "Nascita"}])


class TestBuildBundle(ExportHandlerTestBase):
    def _prepare_confirmed(self, slug: str, markdown: str) -> None:
        self._write_article(slug, markdown)
        self.registry.confirm(slug, "poh_p0500")
        store_proposal(
            self.data_root,
            slug,
            {
                "poh_id": "poh_p0500",
                "poh_type": "p",
                "name": "Soggetto",
                "timeline": [{"anno": 1500, "evento": "Nascita"}],
                "time_range": "1500-1600",
            },
        )

    def test_happy_path_produces_zip(self) -> None:
        self._prepare_confirmed("bernini-slug", "# Soggetto\n\nTesto senza link.\n")
        out_zip = self.root / "out.zip"
        outcome = build_export_bundle(
            ["bernini-slug"],
            data_root=self.data_root,
            registry=self.registry,
            output_zip=out_zip,
            article_loader=lambda dr, slug: (dr / "research" / "articles" / f"{slug}.md").read_text(
                encoding="utf-8"
            ),
            postprocess_deriver=_empty_postprocess,
        )
        self.assertIsInstance(outcome, BundleBuildOutcome)
        self.assertEqual(outcome.included, ["bernini-slug"])
        self.assertTrue(out_zip.is_file())
        with zipfile.ZipFile(out_zip) as archive:
            self.assertIn("text/ITA/poh_p0500.md", archive.namelist())

    def test_pending_and_unconfirmed_are_excluded(self) -> None:
        self._prepare_confirmed("bernini-slug", "# Soggetto\n\nTesto senza link.\n")
        # roma-slug resolved but WITHOUT a stored proposal -> no_metadata
        self._write_article("roma-slug", "# Roma\n\nTesto.\n")
        self.registry.confirm("roma-slug", "poh_o0010")
        # ghost-slug never confirmed -> pending
        out_zip = self.root / "out.zip"
        outcome = build_export_bundle(
            ["bernini-slug", "roma-slug", "ghost-slug"],
            data_root=self.data_root,
            registry=self.registry,
            output_zip=out_zip,
            article_loader=lambda dr, slug: (dr / "research" / "articles" / f"{slug}.md").read_text(
                encoding="utf-8"
            ),
            postprocess_deriver=_empty_postprocess,
        )
        self.assertEqual(outcome.included, ["bernini-slug"])
        reasons = {e["slug"]: e["reason"] for e in outcome.excluded}
        self.assertEqual(reasons["roma-slug"], "no_metadata")
        self.assertEqual(reasons["ghost-slug"], "pending")

    def test_unresolved_poh_link_blocks(self) -> None:
        self._prepare_confirmed(
            "bernini-slug", "# Soggetto\n\nVedi [X](poh:missing-slug).\n"
        )
        with self.assertRaises(ExportBlockedError) as ctx:
            build_export_bundle(
                ["bernini-slug"],
                data_root=self.data_root,
                registry=self.registry,
                output_zip=self.root / "out.zip",
                article_loader=lambda dr, slug: (
                    dr / "research" / "articles" / f"{slug}.md"
                ).read_text(encoding="utf-8"),
                postprocess_deriver=_empty_postprocess,
            )
        self.assertIn("missing-slug", ctx.exception.unresolved_slugs)

    def test_lint_failure_blocks(self) -> None:
        # No timeline anywhere -> frontmatter has no year key -> lint gate fails.
        self._write_article("bernini-slug", "# Soggetto\n\nTesto senza date.\n")
        self.registry.confirm("bernini-slug", "poh_p0500")
        store_proposal(
            self.data_root,
            "bernini-slug",
            {"poh_id": "poh_p0500", "poh_type": "p", "name": "Soggetto", "timeline": []},
        )
        with self.assertRaises(LintGateError) as ctx:
            build_export_bundle(
                ["bernini-slug"],
                data_root=self.data_root,
                registry=self.registry,
                output_zip=self.root / "out.zip",
                article_loader=lambda dr, slug: (
                    dr / "research" / "articles" / f"{slug}.md"
                ).read_text(encoding="utf-8"),
                postprocess_deriver=_empty_postprocess,
            )
        self.assertIn("poh_p0500", ctx.exception.failures)

    def test_no_confirmed_slug_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_export_bundle(
                ["ghost-slug"],
                data_root=self.data_root,
                registry=self.registry,
                output_zip=self.root / "out.zip",
                article_loader=lambda dr, slug: "",
                postprocess_deriver=_empty_postprocess,
            )


class TestApprovedMetadata(unittest.TestCase):
    def test_build_approved_metadata_from_stored(self) -> None:
        stored = {
            "poh_id": "poh_m0001",
            "poh_type": "m",
            "name": "Fontana",
            "timeline": [{"anno": 1651, "evento": "Inaugurazione"}],
            "geo": {"lat": 41.9, "lon": 12.47, "region": "Lazio", "poi_id": 5},
            "wiki_title": "Fontana dei Fiumi",
        }
        approved = build_approved_metadata(stored, entry=None)
        self.assertEqual(approved.poh_id, "poh_m0001")
        self.assertEqual(approved.poh_type, "m")
        self.assertAlmostEqual(approved.lat, 41.9)
        self.assertEqual(approved.poi_id, 5)
        self.assertEqual(len(approved.timeline), 1)


if __name__ == "__main__":
    unittest.main()
