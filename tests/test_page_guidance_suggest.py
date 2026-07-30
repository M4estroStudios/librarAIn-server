import unittest
from random import Random

from PIL import Image

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


if __name__ == "__main__":
    unittest.main()
