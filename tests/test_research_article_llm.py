from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.search.article_llm import (
    ArticleGenerationResult,
    build_article_user_payload,
    build_no_material_article,
    generate_article,
    load_article_prompt,
    query_log_fields,
    research_model,
    strip_article_markdown_fences,
)
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh


SHA = "a" * 64


def _fake_client(content: str = "# Titolo\n\nCorpo con [fatto](source:aaa:aligned:1).") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = content
    client.chat.completions.create.return_value = resp
    _client_states[client] = _ClientState(
        token_bucket=AsyncTokenBucket(60),
        retry_attempts=0,
    )
    return client


def _settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.research_model = overrides.get("research_model")
    settings.editor_model = overrides.get("editor_model", "editor-fallback")
    settings.matcher_llm_model = overrides.get("matcher_llm_model")
    settings.research_temperature = overrides.get("research_temperature", 0.3)
    settings.reasoning_effort_research = overrides.get("reasoning_effort_research")
    settings.reasoning_enable_thinking_research = overrides.get(
        "reasoning_enable_thinking_research"
    )
    return settings


def _loaded_page(*, aligned: int = 12, text: str = "Testo pagina.") -> LoadedPage:
    return LoadedPage(
        source_sha256=SHA,
        aligned_page=aligned,
        book_title="Libro Test",
        book_slug="libro-test",
        markdown=text,
        truncated=False,
    )


class TestArticleLlmHelpers(unittest.TestCase):
    def test_load_article_prompt_reads_file(self) -> None:
        prompt = load_article_prompt()
        self.assertIn("stile Wikipedia", prompt)
        self.assertIn("source:", prompt)

    def test_build_no_material_article_includes_query_preview(self) -> None:
        article = build_no_material_article("chi era Marco Polo")
        self.assertIn("# Materiale insufficiente", article)
        self.assertIn("Marco Polo", article)

    def test_strip_article_markdown_fences(self) -> None:
        raw = "```markdown\n# Titolo\n\nparagrafo\n```"
        self.assertEqual(strip_article_markdown_fences(raw), "# Titolo\n\nparagrafo")

    def test_query_log_fields_hashes_and_truncates(self) -> None:
        long_query = "q" * 120
        fields = query_log_fields(long_query)
        self.assertEqual(len(fields["query_hash"]), 64)
        self.assertLessEqual(len(fields["query_preview"]), 80)

    def test_research_model_prefers_research_then_editor(self) -> None:
        self.assertEqual(
            research_model(_settings(research_model="research-v1")),
            "research-v1",
        )
        self.assertEqual(
            research_model(_settings(research_model=None, editor_model="editor-v2")),
            "editor-v2",
        )

    def test_build_article_user_payload_includes_pages(self) -> None:
        page = _loaded_page()
        payload = build_article_user_payload(
            query="  eventi principali  ",
            poh=ResearchPoh(id="marco-polo", label="Marco Polo"),
            pages=[page],
        )
        self.assertEqual(payload["query"], "eventi principali")
        self.assertEqual(payload["poh"]["id"], "marco-polo")
        self.assertEqual(payload["pages"][0]["aligned_page"], 12)
        self.assertEqual(payload["pages"][0]["text"], "Testo pagina.")


class TestGenerateArticle(unittest.TestCase):
    def test_skips_llm_when_no_pages(self) -> None:
        client = _fake_client()
        result = asyncio.run(
            generate_article(
                query="tema assente",
                pages=[],
                client=client,
                settings=_settings(),
                request_id="req-empty",
            )
        )
        self.assertIsInstance(result, ArticleGenerationResult)
        self.assertTrue(result.skipped_llm)
        self.assertIsNone(result.model)
        self.assertIn("# Materiale insufficiente", result.markdown)
        client.chat.completions.create.assert_not_called()

    def test_calls_llm_with_article_prompt_and_returns_markdown(self) -> None:
        client = _fake_client("# Marco Polo\n\nViaggio [descritto](source:aaa:aligned:12).")
        result = asyncio.run(
            generate_article(
                query="Marco Polo in Cina",
                pages=[_loaded_page()],
                client=client,
                settings=_settings(research_model="research-model"),
                poh=ResearchPoh(label="Marco Polo"),
                request_id="req-1",
            )
        )
        self.assertFalse(result.skipped_llm)
        self.assertEqual(result.model, "research-model")
        self.assertIn("Marco Polo", result.markdown)
        client.chat.completions.create.assert_called_once()
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "research-model")
        self.assertEqual(kwargs["temperature"], 0.3)
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], load_article_prompt())
        user_payload = json.loads(messages[1]["content"])
        self.assertEqual(user_payload["query"], "Marco Polo in Cina")
        self.assertEqual(user_payload["pages"][0]["source_sha256"], SHA)

    def test_strips_markdown_fences_from_model_output(self) -> None:
        client = _fake_client("```md\n# Titolo\n```")
        result = asyncio.run(
            generate_article(
                query="tema",
                pages=[_loaded_page()],
                client=client,
                settings=_settings(),
                request_id="req-2",
            )
        )
        self.assertEqual(result.markdown, "# Titolo")


if __name__ == "__main__":
    unittest.main()
