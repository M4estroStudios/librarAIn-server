from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.search.article_llm import build_no_material_article
from src.search.pages_loader import LoadedPage
from src.search.request_schema import ResearchPoh
from src.search.time_lookup import TimelineCandidate
from src.search.timeline_llm import (
    TimelineResult,
    add_timeline,
    build_timeline_user_payload,
    load_timeline_prompt,
)

SHA = "a" * 64


def _fake_client(
    content: str = (
        "# Titolo\n\n"
        "Corpo [descritto](source:aaa:aligned:12).\n\n"
        "## Cronologia\n\n"
        "| Periodo | Evento | Fonti |\n"
        "|---------|--------|-------|\n"
        "| 1271 | Partenza del viaggio. | [Fonte](source:aaa:aligned:12) |"
    ),
) -> MagicMock:
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


def _loaded_page(*, aligned: int = 12, text: str = "Nel 1271 partì il viaggio.") -> LoadedPage:
    return LoadedPage(
        source_sha256=SHA,
        aligned_page=aligned,
        book_title="Libro Test",
        book_slug="libro-test",
        markdown=text,
        truncated=False,
    )


def _timeline_candidate(
    *,
    label: str = "1271",
    aligned_pages: list[int] | None = None,
) -> TimelineCandidate:
    return TimelineCandidate(
        label=label,
        source_sha256=SHA,
        aligned_pages=aligned_pages or [12],
    )


class TestTimelineHelpers(unittest.TestCase):
    def test_load_timeline_prompt_reads_file(self) -> None:
        prompt = load_timeline_prompt()
        self.assertIn("passo d", prompt)
        self.assertIn("## Cronologia", prompt)
        self.assertIn("timeline_candidates", prompt)

    def test_build_timeline_user_payload_includes_candidates_and_pages(self) -> None:
        payload = build_timeline_user_payload(
            query="  Marco Polo  ",
            article_markdown="# Articolo\n\ntesto",
            timeline_candidates=[_timeline_candidate()],
            pages=[_loaded_page()],
            poh=ResearchPoh(id="marco-polo", label="Marco Polo", time_range="1254–1324"),
        )
        self.assertEqual(payload["query"], "Marco Polo")
        self.assertEqual(payload["primary_poh"]["id"], "marco-polo")
        self.assertEqual(payload["timeline_candidates"][0]["label"], "1271")
        self.assertEqual(payload["timeline_candidates"][0]["aligned_pages"], [12])
        self.assertEqual(payload["pages"][0]["aligned_page"], 12)
        self.assertIn("# Articolo", payload["article_markdown"])


class TestAddTimeline(unittest.TestCase):
    def test_skips_llm_for_no_material_article(self) -> None:
        article = build_no_material_article("tema assente")
        client = _fake_client()
        result = asyncio.run(
            add_timeline(
                query="tema assente",
                article_markdown=article,
                timeline_candidates=[_timeline_candidate()],
                pages=[_loaded_page()],
                client=client,
                settings=_settings(),
                request_id="req-empty",
            )
        )
        self.assertIsInstance(result, TimelineResult)
        self.assertTrue(result.skipped_llm)
        self.assertEqual(result.markdown, article)
        client.chat.completions.create.assert_not_called()

    def test_calls_llm_with_timeline_prompt(self) -> None:
        article = "# Marco Polo\n\nViaggio in Cina [descritto](source:aaa:aligned:12)."
        linked = (
            f"{article}\n\n"
            "## Cronologia\n\n"
            "| Periodo | Evento | Fonti |\n"
            "|---------|--------|-------|\n"
            "| 1271 | Partenza. | [Fonte](source:{SHA}:aligned:12) |"
        )
        client = _fake_client(linked)
        result = asyncio.run(
            add_timeline(
                query="Marco Polo in Cina",
                article_markdown=article,
                timeline_candidates=[_timeline_candidate()],
                pages=[_loaded_page()],
                client=client,
                settings=_settings(),
                poh=ResearchPoh(id="marco-polo", label="Marco Polo"),
                request_id="req-1",
            )
        )
        self.assertFalse(result.skipped_llm)
        self.assertEqual(result.model, "research-model")
        self.assertIn("## Cronologia", result.markdown)
        self.assertIn("| Periodo | Evento | Fonti |", result.markdown)
        client.chat.completions.create.assert_called_once()
        kwargs = client.chat.completions.create.call_args.kwargs
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["content"], load_timeline_prompt())
        user_payload = json.loads(messages[1]["content"])
        self.assertEqual(user_payload["query"], "Marco Polo in Cina")
        self.assertEqual(user_payload["primary_poh"]["id"], "marco-polo")
        self.assertEqual(len(user_payload["timeline_candidates"]), 1)
        self.assertEqual(len(user_payload["pages"]), 1)

    def test_calls_llm_when_timeline_candidates_empty(self) -> None:
        article = "# Titolo\n\nCorpo."
        client = _fake_client(f"{article}\n\n## Cronologia\n\n| Periodo | Evento | Fonti |\n|---|---|---|")
        result = asyncio.run(
            add_timeline(
                query="tema",
                article_markdown=article,
                timeline_candidates=[],
                pages=[_loaded_page()],
                client=client,
                settings=_settings(),
                request_id="req-no-candidates",
            )
        )
        self.assertFalse(result.skipped_llm)
        self.assertIn("## Cronologia", result.markdown)
        client.chat.completions.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
