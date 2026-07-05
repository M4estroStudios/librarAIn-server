from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.search.article_finalize_llm import (
    ArticleFinalizeResult,
    build_article_finalize_user_payload,
    finalize_article,
    load_article_finalize_prompt,
)
from src.search.article_llm import build_no_material_article
from src.search.request_schema import ResearchPoh


def _fake_client(content: str = "# Titolo\n\nTesto finale.") -> MagicMock:
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
    settings.research_model = overrides.get("research_model", "research-model")
    settings.editor_model = overrides.get("editor_model", "editor-fallback")
    settings.matcher_llm_model = overrides.get("matcher_llm_model")
    settings.research_temperature = overrides.get("research_temperature", 0.3)
    settings.reasoning_effort_research = overrides.get("reasoning_effort_research")
    settings.reasoning_enable_thinking_research = overrides.get(
        "reasoning_enable_thinking_research"
    )
    return settings


class TestArticleFinalize(unittest.TestCase):
    def test_load_prompt(self) -> None:
        prompt = load_article_finalize_prompt()
        self.assertIn("draft_markdown", prompt)
        self.assertIn("enriched_markdown", prompt)

    def test_build_payload_includes_both_versions(self) -> None:
        payload = build_article_finalize_user_payload(
            query="tema",
            draft_markdown="# Bozza\n\nTesto.",
            enriched_markdown="# Arricchito\n\nTesto [fonte](source:aa:aligned:1).",
            poh=ResearchPoh(id="alpha", label="Alpha"),
        )
        self.assertIn("Bozza", payload["draft_markdown"])
        self.assertIn("Arricchito", payload["enriched_markdown"])
        self.assertEqual(payload["primary_poh"]["id"], "alpha")

    def test_skips_llm_for_no_material(self) -> None:
        enriched = build_no_material_article("tema")
        client = _fake_client()
        result = asyncio.run(
            finalize_article(
                query="tema",
                draft_markdown=enriched,
                enriched_markdown=enriched,
                client=client,
                settings=_settings(),
                request_id="req-empty",
            )
        )
        self.assertTrue(result.skipped_llm)
        self.assertEqual(result.markdown, enriched)
        client.chat.completions.create.assert_not_called()

    def test_calls_llm_with_draft_and_enriched(self) -> None:
        draft = "# Titolo\n\nBozza iniziale."
        enriched = "# Titolo\n\nVersione [arricchita](source:aa:aligned:1)."
        final = "# Titolo\n\nVersione finale [arricchita](source:aa:aligned:1)."
        client = _fake_client(final)
        result = asyncio.run(
            finalize_article(
                query="tema",
                draft_markdown=draft,
                enriched_markdown=enriched,
                client=client,
                settings=_settings(),
                request_id="req-1",
            )
        )
        self.assertIsInstance(result, ArticleFinalizeResult)
        self.assertFalse(result.skipped_llm)
        self.assertEqual(result.markdown, final)
        client.chat.completions.create.assert_called_once()
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["content"], load_article_finalize_prompt())
        user_payload = json.loads(messages[1]["content"])
        self.assertEqual(user_payload["draft_markdown"], draft)
        self.assertEqual(user_payload["enriched_markdown"], enriched)


if __name__ == "__main__":
    unittest.main()
