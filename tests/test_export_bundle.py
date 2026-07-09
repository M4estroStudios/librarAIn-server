from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from src.export.bundle import BundleItem, build_bundle
from src.export.etaly_adapter import EtalyArticle
from src.export.time_range import parse_time_range

_SHA = "a" * 64
_TITLE = "I rioni di Roma"

_MARKDOWN = """---
id: poh_p0500
name: Piazza Navona
---

Corpo dell'articolo su [[poh_o0001|Piazza Navona]].
"""


def _make_pdf(path: Path, page_count: int) -> None:
    pages = []
    for index in range(page_count):
        image = Image.new("RGB", (600, 800), (255, 255, 255))
        for y in range(50 * index, 50 * index + 40):
            for x in range(50, 550):
                image.putpixel((x, y), (0, 0, 0))
        pages.append(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:])


def _write_manifest(data_root: Path, sha: str, page_count: int, title: str) -> None:
    manifest = {
        "source_sha256": sha,
        "slug": "i-rioni-di-roma",
        "original_page_count": page_count,
        "aligned_page_count": page_count,
        "pages": [
            {"aligned": n, "original": n, "file": f"pages/p.{n:04d}.i-rioni-di-roma.md"}
            for n in range(1, page_count + 1)
        ],
        "reicat": {"titolo": title},
    }
    manifest_path = data_root / "output" / sha / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


class BundleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_root = self.root / "data"
        self.assets = self.root / "assets"
        self.out_zip = self.root / "bundle.zip"
        _make_pdf(_processed_pdf(self.data_root, _SHA), 3)
        _write_manifest(self.data_root, _SHA, 3, _TITLE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _article(self, poh_id: str, pages: set[tuple[str, int]]) -> EtalyArticle:
        return EtalyArticle(poh_id=poh_id, markdown=_MARKDOWN, cited_pages=set(pages))

    def _seed_etaly_asset(self, poh_id: str) -> None:
        asset = self.assets / "timeline" / "data" / "text" / "ITA" / f"{poh_id}.md"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text("esistente", encoding="utf-8")


def _processed_pdf(data_root: Path, sha: str) -> Path:
    return data_root / "input" / "processed" / f"{sha}.pdf"


class TestBuildBundle(BundleTestBase):
    def test_produces_expected_layout_and_new_action(self) -> None:
        item = BundleItem(
            article=self._article("poh_p0500", {(_SHA, 1), (_SHA, 2)}),
            poh_type="p",
            name="Piazza Navona",
            time_range="1500-1600",
        )
        result = build_bundle(
            [item],
            self.out_zip,
            etaly_assets_path=self.assets,
            data_root=self.data_root,
        )

        self.assertEqual(result.poh_count, 1)
        self.assertEqual(result.rendered_pages, 2)
        self.assertEqual(result.missing_pages, [])

        with zipfile.ZipFile(self.out_zip) as archive:
            names = set(archive.namelist())
            self.assertIn("text/ITA/poh_p0500.md", names)
            self.assertIn(f"sources/{_SHA}/p1.webp", names)
            self.assertIn(f"sources/{_SHA}/p2.webp", names)
            self.assertIn("MANIFEST.json", names)
            self.assertIn("patch/poh_p.csv", names)
            self.assertIn("patch/registry.json", names)

            for page in (1, 2):
                data = archive.read(f"sources/{_SHA}/p{page}.webp")
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    self.assertEqual(image.format, "WEBP")

            manifest = json.loads(archive.read("MANIFEST.json"))
            self.assertEqual(manifest["books"][_SHA]["title"], _TITLE)
            poh = manifest["poh"][0]
            self.assertEqual(poh["poh_id"], "poh_p0500")
            self.assertEqual(poh["action"], "new")
            self.assertEqual(
                poh["sources"],
                [{"sha": _SHA, "page": 1}, {"sha": _SHA, "page": 2}],
            )
            self.assertTrue(poh["md_sha256"])

            csv_text = archive.read("patch/poh_p.csv").decode("utf-8")
        self.assertIn("name,id_code,beginning,end,shelf", csv_text)
        self.assertIn("poh_p0500", csv_text)
        self.assertIn("1500/01/01", csv_text)
        self.assertIn("1600/01/01", csv_text)
        self.assertTrue(csv_text.strip().endswith(","))

    def test_dedup_same_page_across_articles(self) -> None:
        items = [
            BundleItem(self._article("poh_p0500", {(_SHA, 1)}), "p", name="Uno"),
            BundleItem(self._article("poh_p0501", {(_SHA, 1)}), "p", name="Due"),
        ]
        result = build_bundle(
            items,
            self.out_zip,
            etaly_assets_path=self.assets,
            data_root=self.data_root,
        )
        self.assertEqual(result.rendered_pages, 1)
        with zipfile.ZipFile(self.out_zip) as archive:
            source_pages = [n for n in archive.namelist() if n.startswith("sources/")]
        self.assertEqual(source_pages, [f"sources/{_SHA}/p1.webp"])

    def test_unrenderable_page_reported_not_crashing(self) -> None:
        item = BundleItem(
            self._article("poh_p0500", {(_SHA, 1), (_SHA, 99)}),
            "p",
            name="Piazza Navona",
        )
        result = build_bundle(
            [item],
            self.out_zip,
            etaly_assets_path=self.assets,
            data_root=self.data_root,
        )
        self.assertEqual(result.rendered_pages, 1)
        self.assertEqual(result.missing_pages, [(_SHA, 99)])
        with zipfile.ZipFile(self.out_zip) as archive:
            names = set(archive.namelist())
        self.assertIn(f"sources/{_SHA}/p1.webp", names)
        self.assertNotIn(f"sources/{_SHA}/p99.webp", names)

    def test_overwrite_action_when_asset_exists(self) -> None:
        self._seed_etaly_asset("poh_p0500")
        item = BundleItem(
            self._article("poh_p0500", {(_SHA, 1)}),
            "p",
            name="Piazza Navona",
            time_range="1500-1600",
        )
        result = build_bundle(
            [item],
            self.out_zip,
            etaly_assets_path=self.assets,
            data_root=self.data_root,
        )
        self.assertEqual(result.poh_count, 1)
        with zipfile.ZipFile(self.out_zip) as archive:
            manifest = json.loads(archive.read("MANIFEST.json"))
            names = set(archive.namelist())
        self.assertEqual(manifest["poh"][0]["action"], "overwrite")
        self.assertNotIn("patch/poh_p.csv", names)


class TestParseTimeRange(unittest.TestCase):
    def test_year_range(self) -> None:
        self.assertEqual(parse_time_range("1618-1619"), ("1618/01/01", "1619/01/01"))

    def test_bce_range(self) -> None:
        self.assertEqual(
            parse_time_range("509 a.C. - 27 a.C."), ("-509/01/01", "-27/01/01")
        )

    def test_bce_to_ce(self) -> None:
        self.assertEqual(
            parse_time_range("753 a.C. - 476 d.C."), ("-753/01/01", "476/01/01")
        )

    def test_single_year(self) -> None:
        self.assertEqual(parse_time_range("1500"), ("1500/01/01", "1500/01/01"))

    def test_unparseable_returns_empty(self) -> None:
        self.assertEqual(parse_time_range("I secolo a.C."), ("", ""))
        self.assertEqual(parse_time_range(None), ("", ""))


if __name__ == "__main__":
    unittest.main()
