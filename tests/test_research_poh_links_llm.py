from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.models.polyindex_index import PolyindexIndexDocument
from src.search.article_llm import build_no_material_article
from src.search.poh_links_llm import (
    PohCandidate,
    PohLinksResult,
    add_poh_links,
    build_poh_candidates,
    build_poh_links_user_payload,
    load_poh_links_prompt,
)
from src.search.request_schema import ResearchPoh
from src.search.subject_lookup import SubjectMatch


def _fake_client(
    content: str = "# Titolo\n\n[Marco Polo](poh:marco-polo) viaggiò [descritto](source:aaa:aligned:1).",
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


def _doc(subjects: dict) -> PolyindexIndexDocument:
    return PolyindexIndexDocument.model_validate(
        {"schema_version": "1.0", "subjects": subjects}
    )


def _subject(label: str, aligned: list[int], aliases: list[str] | None = None) -> dict:
    return {
        "canonical_label": label,
        "aliases": aliases or [],
        "books": {"a" * 64: {"aligned_pages": aligned}},
    }


class TestPohLinksHelpers(unittest.TestCase):
    def test_load_poh_links_prompt_reads_file(self) -> None:
        prompt = load_poh_links_prompt()
        self.assertIn("passo c", prompt)
        self.assertIn("poh:", prompt)

    def test_build_poh_candidates_merges_lookup_and_article(self) -> None:
        document = _doc(
            {
                "marco-polo": _subject("Marco Polo", [12]),
                "kublai-khan": _subject("Kublai Khan", [14], aliases=["Kublai"]),
            }
        )
        article = "# Viaggio\n\nMarco Polo incontrò Kublai Khan."
        lookup_matches = [
            SubjectMatch(
                canonical_id="marco-polo",
                canonical_label="Marco Polo",
                method="exact",
            )
        ]
        candidates = build_poh_candidates(
            document=document,
            subject_matches=lookup_matches,
            article_markdown=article,
            query="Marco Polo in Cina",
        )
        ids = {candidate.poh_id for candidate in candidates}
        self.assertEqual(ids, {"kublai-khan", "marco-polo"})

    def test_build_poh_links_user_payload_includes_article(self) -> None:
        payload = build_poh_links_user_payload(
            query="tema",
            article_markdown="# Articolo\n\ntesto",
            poh_candidates=[
                PohCandidate(poh_id="marco-polo", label="Marco Polo", aliases=("M. Polo",))
            ],
            poh=ResearchPoh(id="marco-polo", label="Marco Polo"),
        )
        self.assertEqual(payload["primary_poh"]["id"], "marco-polo")
        self.assertEqual(payload["poh_candidates"][0]["aliases"], ["M. Polo"])
        self.assertIn("# Articolo", payload["article_markdown"])


class TestAddPohLinks(unittest.TestCase):
    def test_skips_llm_for_no_material_article(self) -> None:
        article = build_no_material_article("tema assente")
        client = _fake_client()
        result = asyncio.run(
            add_poh_links(
                query="tema assente",
                article_markdown=article,
                poh_candidates=[PohCandidate(poh_id="marco-polo", label="Marco Polo")],
                client=client,
                settings=_settings(),
                request_id="req-empty",
            )
        )
        self.assertIsInstance(result, PohLinksResult)
        self.assertTrue(result.skipped_llm)
        self.assertEqual(result.markdown, article)
        client.chat.completions.create.assert_not_called()

    def test_skips_llm_when_no_candidates(self) -> None:
        article = "# Titolo\n\nCorpo."
        client = _fake_client()
        result = asyncio.run(
            add_poh_links(
                query="tema",
                article_markdown=article,
                poh_candidates=[],
                client=client,
                settings=_settings(),
                request_id="req-no-candidates",
            )
        )
        self.assertTrue(result.skipped_llm)
        self.assertEqual(result.markdown, article)
        client.chat.completions.create.assert_not_called()

    def test_calls_llm_with_poh_links_prompt(self) -> None:
        article = "# Marco Polo\n\nMarco Polo viaggiò in Cina."
        linked = (
            "# Marco Polo\n\n"
            "Marco Polo incontrò [Kublai Khan](poh:kublai-khan) "
            "[descritto](source:aaa:aligned:12)."
        )
        client = _fake_client(linked)
        candidates = [
            PohCandidate(poh_id="marco-polo", label="Marco Polo"),
            PohCandidate(poh_id="kublai-khan", label="Kublai Khan"),
        ]
        result = asyncio.run(
            add_poh_links(
                query="Marco Polo in Cina",
                article_markdown=article,
                poh_candidates=candidates,
                client=client,
                settings=_settings(),
                poh=ResearchPoh(id="marco-polo", label="Marco Polo"),
                request_id="req-1",
            )
        )
        self.assertFalse(result.skipped_llm)
        self.assertEqual(result.model, "research-model")
        self.assertIn("poh:kublai-khan", result.markdown)
        client.chat.completions.create.assert_called_once()
        kwargs = client.chat.completions.create.call_args.kwargs
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["content"], load_poh_links_prompt())
        user_payload = json.loads(messages[1]["content"])
        self.assertEqual(user_payload["query"], "Marco Polo in Cina")
        self.assertEqual(user_payload["primary_poh"]["id"], "marco-polo")
        self.assertEqual(len(user_payload["poh_candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
