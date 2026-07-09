from __future__ import annotations

import unittest

from src.export.prompts import (
    _ETALY_METADATA_PROMPT_PATH,
    _TIMELINE_FILL_PROMPT_PATH,
    load_etaly_metadata_prompt,
    load_timeline_fill_prompt,
)


class ExportPromptsTests(unittest.TestCase):
    def test_prompt_files_exist(self) -> None:
        self.assertTrue(_ETALY_METADATA_PROMPT_PATH.is_file())
        self.assertTrue(_TIMELINE_FILL_PROMPT_PATH.is_file())

    def test_load_etaly_metadata_prompt_returns_non_empty(self) -> None:
        prompt = load_etaly_metadata_prompt()
        self.assertIsInstance(prompt, str)
        self.assertTrue(prompt.strip())

    def test_load_timeline_fill_prompt_returns_non_empty(self) -> None:
        prompt = load_timeline_fill_prompt()
        self.assertIsInstance(prompt, str)
        self.assertTrue(prompt.strip())


if __name__ == "__main__":
    unittest.main()
