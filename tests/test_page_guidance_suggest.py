import unittest
from pathlib import Path
from random import Random
from unittest.mock import MagicMock, patch

from PIL import Image

from src.api.page_guidance_http import ensure_ingest_ai_page_guidance
from src.api.page_guidance_suggest import (
    choose_sample_pages,
    flatten_annotations_on_image,
    normalize_annotations,
)


class ChooseSamplePagesTests(unittest.TestCase):
    def test_excludes_annotated_and_caps_at_five(self) -> None:
        samples = choose_sample_pages(
            20,
            [1, 2, 3],
            sample_count=5,
            rng=Random(0),
        )
        self.assertEqual(len(samples), 5)
        self.assertTrue(all(page not in {1, 2, 3} for page in samples))
        self.assertEqual(samples, sorted(samples))

    def test_short_pdf_returns_all_remaining(self) -> None:
        samples = choose_sample_pages(4, [2], sample_count=5, rng=Random(1))
        self.assertEqual(samples, [1, 3, 4])

    def test_all_annotated_returns_empty(self) -> None:
        samples = choose_sample_pages(3, [1, 2, 3], sample_count=5, rng=Random(2))
        self.assertEqual(samples, [])


class NormalizeAnnotationsTests(unittest.TestCase):
    def test_keeps_valid_primitives(self) -> None:
        normalized = normalize_annotations(
            [
                {
                    "page": 2,
                    "elements": [
                        {"id": "a", "name": "titolo", "type": "bbox", "coords": [10, 20, 100, 80]},
                        {"name": "pin", "type": "point", "coords": [50, 60]},
                        {"name": "path", "type": "trail", "coords": [[1, 2], [3, 4]]},
                        {"name": "bad", "type": "circle", "coords": [1, 2]},
                    ],
                },
                {"page": 0, "elements": []},
            ]
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["page"], 2)
        self.assertEqual(len(normalized[0]["elements"]), 3)


class FlattenAnnotationsTests(unittest.TestCase):
    def test_draws_without_error(self) -> None:
        image = Image.new("RGB", (200, 300), (240, 240, 240))
        out = flatten_annotations_on_image(
            image,
            [
                {"name": "box", "type": "bbox", "coords": [100, 100, 400, 500]},
                {"name": "dot", "type": "point", "coords": [500, 500]},
                {"name": "route", "type": "trail", "coords": [[100, 100], [800, 800]]},
            ],
        )
        self.assertEqual(out.size, (200, 300))
        self.assertEqual(out.mode, "RGB")


class EnsureIngestAiPageGuidanceTests(unittest.TestCase):
    def test_keeps_existing_guidance(self) -> None:
        payload = {"ai_page_guidance": "  already there  "}
        ensure_ingest_ai_page_guidance(
            Path("unused.pdf"),
            MagicMock(),
            payload,
            {"notes": "ignored"},
        )
        self.assertEqual(payload["ai_page_guidance"], "already there")

    def test_generates_when_missing(self) -> None:
        payload: dict = {}
        with patch(
            "src.api.page_guidance_http.suggest_page_guidance",
            return_value={"guidance": "generated tip"},
        ) as mock_suggest:
            ensure_ingest_ai_page_guidance(
                Path("book.pdf"),
                MagicMock(),
                payload,
                {
                    "notes": "general",
                    "index_notes": "index",
                    "page_notes": "pages",
                    "annotations_json": '[{"page":1,"elements":[]}]',
                },
            )
        self.assertEqual(payload["ai_page_guidance"], "generated tip")
        mock_suggest.assert_called_once()
        kwargs = mock_suggest.call_args.kwargs
        self.assertEqual(kwargs["notes"], "general")
        self.assertEqual(kwargs["index_notes"], "index")
        self.assertEqual(kwargs["page_notes"], "pages")
        self.assertEqual(kwargs["annotations"], [{"page": 1, "elements": []}])

    def test_rejects_invalid_annotations_json(self) -> None:
        with self.assertRaises(ValueError):
            ensure_ingest_ai_page_guidance(
                Path("book.pdf"),
                MagicMock(),
                {},
                {"annotations_json": "{bad"},
            )


if __name__ == "__main__":
    unittest.main()
