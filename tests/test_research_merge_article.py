from __future__ import annotations

import unittest
from unittest.mock import patch

from src.api.research_merge_article import consecutive_duplicate_ratio, handle_merge_article_request


class ResearchMergeArticleTests(unittest.TestCase):
    def test_consecutive_duplicate_ratio_low_when_different(self) -> None:
        original = "Line one.\nLine two.\nLine three.\nLine four."
        merged = "# Title\nCompletely new content.\nOther paragraph."
        ratio = consecutive_duplicate_ratio(original, merged)
        self.assertLess(ratio, 0.4)

    def test_consecutive_duplicate_ratio_high_when_copied(self) -> None:
        block = "Same line one.\nSame line two.\nSame line three."
        original = block + "\nExtra."
        merged = "# Updated\n" + block
        ratio = consecutive_duplicate_ratio(original, merged)
        self.assertGreater(ratio, 0.2)

    @patch("src.api.research_merge_article.publish_poh_article")
    @patch("src.api.research_merge_article.merge_article_markdown")
    def test_handle_merge_publishes(self, merge_mock, publish_mock) -> None:
        merge_mock.return_value = "# Merged\nNew content."
        publish_mock.return_value = {"url": "/articolo/x.html", "poh_id": "x"}
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            (data_root / "polyindex").mkdir(parents=True)
            (data_root / "polyindex" / "INDEX.json").write_text(
                '{"schema_version":"1.0","subjects":{"x":{"canonical_label":"X","aliases":[],"books":{}}}}',
                encoding="utf-8",
            )
            from src.models.settings import Settings

            settings = Settings.model_validate(
                {
                    "DATA_ROOT": str(data_root),
                    "OPENAI_PROVIDER": "local",
                    "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
                }
            )
            result = handle_merge_article_request(
                data_root,
                settings,
                {
                    "target_poh_id": "x",
                    "existing_markdown": "# Old\nOld line.",
                    "new_pages": [{"text": "new page"}],
                    "reicat": {"titolo": "Libro"},
                    "operator_notes": "note",
                },
                request_id="req-1",
            )
            self.assertTrue(result["ok"])
            publish_mock.assert_called_once()
