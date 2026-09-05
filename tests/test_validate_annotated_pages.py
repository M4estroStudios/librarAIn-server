from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_annotated_pages import validate


class ValidateAnnotatedPagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.pages / "p.0009.book.md").write_text(
            "{\n**LO SCAVO**\ncorpo del box\n}\n",
            encoding="utf-8",
        )
        (self.pages / "p.0007.book.md").write_text(
            "ROMA PRIMA DI ROMA\n\nPREMESSA\ncorpo\n",
            encoding="utf-8",
        )
        self.annotations = self.root / "draft.json"
        self.annotations.write_text(
            json.dumps(
                {
                    "state": {
                        "annotations": [
                            {
                                "page": 12,
                                "elements": [
                                    {
                                        "name": "Box Curiosità",
                                        "type": "bbox",
                                        "coords": [1, 2, 3, 4],
                                    }
                                ],
                            },
                            {
                                "page": 10,
                                "elements": [
                                    {
                                        "name": "Capitolo (#)",
                                        "type": "bbox",
                                        "coords": [1, 2, 3, 4],
                                    },
                                    {
                                        "name": "Sezione (##)",
                                        "type": "bbox",
                                        "coords": [5, 6, 7, 8],
                                    },
                                ],
                            },
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "pages": [
                        {"original": 10, "aligned": 7, "file": "pages/p.0007.book.md"},
                        {"original": 12, "aligned": 9, "file": "pages/p.0009.book.md"},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_aside_passes_and_missing_headings_fail(self) -> None:
        report = validate(
            sha="abc",
            pages_dir=self.pages,
            annotations_path=self.annotations,
            manifest_path=self.manifest,
            only_original=None,
            stage2_dir=None,
            stage3_dir=None,
            min_pass=0.9,
        )
        by_orig = {row["original"]: row for row in report["pages"]}
        self.assertTrue(by_orig[12]["ok"])
        self.assertFalse(by_orig[10]["ok"])
        failed = {item["rule"] for item in by_orig[10]["checks"] if not item["ok"]}
        self.assertIn("capitolo_h1", failed)
        self.assertIn("sezione_h2", failed)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 1)
