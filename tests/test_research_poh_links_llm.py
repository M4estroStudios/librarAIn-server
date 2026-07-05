from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from src.core.openai_client import _ClientState, _client_states
from src.core.rate_limit import AsyncTokenBucket
from src.models.polyindex_index import PolyindexIndexDocument
from src.search.article_llm import build_no_material_article
from src.search.poh_links_llm import (
    PohLinkTask,
    PohLinksResult,
    _WORD_CHAR,
    group_link_tasks_by_paragraph,
    add_poh_links,
    apply_paragraph_updates,
    build_poh_link_tasks,
    build_poh_links_paragraph_payload,
    chunk_article_text,
    dedupe_vector_hits,
    discover_poh_link_tasks,
    load_poh_links_prompt,
    split_article_paragraphs,
)
from src.search.request_schema import ResearchPoh


def _fake_client(
    content: str = "Marco Polo incontrò [Kublai Khan](poh:kublai-khan).",
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
    settings.matcher_embedding_model = overrides.get(
        "matcher_embedding_model", "embedding-model"
    )
    settings.sqlite_path = overrides.get("sqlite_path", ":memory:")
    settings.research_temperature = overrides.get("research_temperature", 0.3)
    settings.max_parallel_request = overrides.get("max_parallel_request", 8)
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


class TestPohLinksChunking(unittest.TestCase):
    def test_chunk_trims_partial_words_on_overlap(self) -> None:
        text = "xxalpha beta gamma delta epsilon zeta omega"
        chunks = chunk_article_text(text, size=12, overlap=4)
        self.assertGreater(len(chunks), 1)
        for start, end, chunk in chunks:
            if start > 0:
                self.assertFalse(_WORD_CHAR.match(chunk[0]) and _WORD_CHAR.match(text[start - 1]))
            if end < len(text):
                self.assertFalse(_WORD_CHAR.match(chunk[-1]) and _WORD_CHAR.match(text[end]))

    def test_dedupe_keeps_first_offset(self) -> None:
        hits = [
            ("b", 200, 0.9),
            ("a", 100, 0.5),
            ("a", 50, 0.8),
        ]
        deduped = dedupe_vector_hits(hits)
        self.assertEqual(deduped, [("a", 50, 0.8), ("b", 200, 0.9)])

    def test_build_tasks_maps_offset_to_paragraph(self) -> None:
        article = "# Titolo\n\nPrimo paragrafo lungo.\n\nSecondo paragrafo."
        document = _doc({"kublai-khan": _subject("Kublai Khan", [1])})
        hits = [("kublai-khan", article.index("Secondo"), 0.91)]
        tasks = build_poh_link_tasks(
            document=document,
            article_markdown=article,
            hits=hits,
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].paragraph_index, 2)

    def test_group_link_tasks_by_paragraph(self) -> None:
        tasks = [
            PohLinkTask("a", "A", (), 1, 10, 0.9),
            PohLinkTask("b", "B", (), 2, 20, 0.8),
            PohLinkTask("c", "C", (), 1, 30, 0.7),
        ]
        grouped = group_link_tasks_by_paragraph(tasks)
        self.assertEqual([task.poh_id for task in grouped[1]], ["a", "c"])
        self.assertEqual([task.poh_id for task in grouped[2]], ["b"])

    def test_apply_paragraph_updates_replaces_only_target_blocks(self) -> None:
        article = "# Titolo\n\nVecchio.\n\nAltro."
        paragraphs = split_article_paragraphs(article)
        updated = apply_paragraph_updates(article, paragraphs, {1: "Nuovo."})
        self.assertIn("Nuovo.", updated)
        self.assertNotIn("Vecchio.", updated)
        self.assertIn("Altro.", updated)


class TestPohLinksHelpers(unittest.TestCase):
    def test_load_poh_links_prompt_reads_file(self) -> None:
        prompt = load_poh_links_prompt()
        self.assertIn("paragrafo", prompt)
        self.assertIn("poh:", prompt)

    def test_build_poh_links_paragraph_payload(self) -> None:
        task = PohLinkTask(
            poh_id="kublai-khan",
            label="Kublai Khan",
            aliases=("Kublai",),
            paragraph_index=1,
            first_offset=10,
            similarity=0.9,
        )
        payload = build_poh_links_paragraph_payload(
            query="tema",
            subject=task,
            paragraph_markdown="Testo.",
            poh=ResearchPoh(id="marco-polo", label="Marco Polo"),
            is_lead_paragraph=False,
        )
        self.assertEqual(payload["subject"]["id"], "kublai-khan")
        self.assertFalse(payload["is_lead_paragraph"])


class TestDiscoverPohLinkTasks(unittest.TestCase):
    @patch("src.search.poh_links_llm._load_poh_embedding_index")
    @patch("src.search.poh_links_llm.embedding_with_retry_sync")
    def test_discover_returns_deduped_tasks(
        self,
        mock_embed: MagicMock,
        mock_index: MagicMock,
    ) -> None:
        mock_index.return_value = [
            ("marco-polo", [1.0, 0.0]),
            ("kublai-khan", [0.0, 1.0]),
        ]
        mock_embed.side_effect = [
            [1.0, 0.0],
            [1.0, 0.0],
        ]
        document = _doc(
            {
                "marco-polo": _subject("Marco Polo", [12]),
                "kublai-khan": _subject("Kublai Khan", [14]),
            }
        )
        article = "# Marco Polo\n\nMarco Polo viaggiò in Cina con molti racconti."
        tasks = asyncio.run(
            discover_poh_link_tasks(
                article_markdown=article,
                document=document,
                client=_fake_client(),
                settings=_settings(),
                sqlite_path=":memory:",
                request_id="req-discover",
            )
        )
        ids = {task.poh_id for task in tasks}
        self.assertIn("marco-polo", ids)


class TestAddPohLinks(unittest.TestCase):
    def test_skips_llm_for_no_material_article(self) -> None:
        article = build_no_material_article("tema assente")
        client = _fake_client()
        result = asyncio.run(
            add_poh_links(
                query="tema assente",
                article_markdown=article,
                document=_doc({}),
                client=client,
                settings=_settings(),
                link_tasks=[],
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
                document=_doc({}),
                client=client,
                settings=_settings(),
                link_tasks=[],
                request_id="req-no-candidates",
            )
        )
        self.assertTrue(result.skipped_llm)
        self.assertEqual(result.markdown, article)
        client.chat.completions.create.assert_not_called()

    def test_calls_llm_per_subject_with_paragraph_payload(self) -> None:
        article = "# Marco Polo\n\nMarco Polo viaggiò in Cina."
        linked = "Marco Polo incontrò [Kublai Khan](poh:kublai-khan) in Cina."
        client = _fake_client(linked)
        tasks = [
            PohLinkTask(
                poh_id="kublai-khan",
                label="Kublai Khan",
                aliases=(),
                paragraph_index=1,
                first_offset=20,
                similarity=0.88,
            )
        ]
        result = asyncio.run(
            add_poh_links(
                query="Marco Polo in Cina",
                article_markdown=article,
                document=_doc({"kublai-khan": _subject("Kublai Khan", [1])}),
                client=client,
                settings=_settings(),
                link_tasks=tasks,
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
        self.assertEqual(user_payload["subject"]["id"], "kublai-khan")
        self.assertIn("Marco Polo", user_payload["paragraph_markdown"])

    def test_calls_llm_in_parallel_across_paragraphs(self) -> None:
        article = "# Titolo\n\nPrimo con Kublai.\n\nSecondo con Marco Polo."
        client = _fake_client()
        responses = [
            "Primo con [Kublai Khan](poh:kublai-khan).",
            "Secondo con [Marco Polo](poh:marco-polo).",
        ]

        def _side_effect(**_kwargs: object) -> MagicMock:
            resp = MagicMock()
            resp.choices[0].message.content = responses.pop(0)
            return resp

        client.chat.completions.create.side_effect = _side_effect
        tasks = [
            PohLinkTask("kublai-khan", "Kublai Khan", (), 1, 10, 0.9),
            PohLinkTask("marco-polo", "Marco Polo", (), 2, 30, 0.8),
        ]
        result = asyncio.run(
            add_poh_links(
                query="tema",
                article_markdown=article,
                document=_doc(
                    {
                        "kublai-khan": _subject("Kublai Khan", [1]),
                        "marco-polo": _subject("Marco Polo", [2]),
                    }
                ),
                client=client,
                settings=_settings(),
                link_tasks=tasks,
                request_id="req-parallel",
            )
        )
        self.assertIn("poh:kublai-khan", result.markdown)
        self.assertIn("poh:marco-polo", result.markdown)
        self.assertEqual(client.chat.completions.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
