from __future__ import annotations

import json
import math
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ingestion.polyindex.index_md_parser import RawSubject, normalize_label
from src.ingestion.polyindex.subject_matcher import _parse_llm_same_response, match_subject
from src.models.settings import Settings


def _settings(data_root: str, *, use_ai: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "DATA_ROOT": data_root,
            "OPENAI_PROVIDER": "local",
            "MATCHER_USE_AI": use_ai,
            "MATCHER_EMBEDDING_MODEL": "text-embedding-3-small",
            "MATCHER_SIMILARITY_THRESHOLD": 0.86,
        }
    )


def _hash_embedding(text: str, dim: int = 8) -> list[float]:
    digest = sha256(text.encode("utf-8")).digest()
    values = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create(self, *, model: str, input: str) -> MagicMock:
        del model
        self.calls.append(input)
        item = MagicMock()
        item.embedding = _hash_embedding(input)
        response = MagicMock()
        response.data = [item]
        return response


class FakeChatCompletions:
    def __init__(self, responses: dict[str, bool]) -> None:
        self.responses = responses

    def create(self, **kwargs: object) -> MagicMock:
        messages = kwargs.get("messages")
        assert isinstance(messages, list)
        user_payload = json.loads(str(messages[1]["content"]))
        label_a = user_payload["label_a"]
        label_b = user_payload["label_b"]
        key = f"{label_a}|{label_b}"
        same = self.responses.get(key, False)
        choice = MagicMock()
        choice.message.content = json.dumps(
            {"same": same, "reason": "test"},
            ensure_ascii=False,
        )
        response = MagicMock()
        response.choices = [choice]
        return response


class FakeChat:
    def __init__(self, responses: dict[str, bool]) -> None:
        self.completions = FakeChatCompletions(responses)


def _fake_client(chat_pairs: dict[str, bool] | None = None) -> MagicMock:
    client = MagicMock()
    client.embeddings = FakeEmbeddings()
    client.chat = FakeChat(chat_pairs or {})
    return client


def _empty_state() -> dict:
    return {"schema_version": "1.0", "subjects": {}}


def _state_with_canonical(
    canonical_id: str,
    label: str,
    aliases: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "subjects": {
            canonical_id: {
                "canonical_label": label,
                "aliases": aliases or [],
                "books": {},
            }
        },
    }


class TestSubjectMatcher(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name) / "data"
        self.sqlite_path = str(self.data_root / "db" / "biblioteca.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exact_normalized_match(self) -> None:
        state = _state_with_canonical("marco-polo", "Marco Polo")
        raw = RawSubject("marco polo", [2], [2])
        decision = match_subject(
            raw,
            state,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-1",
        )
        self.assertEqual(decision.action, "match")
        self.assertEqual(decision.canonical_id, "marco-polo")
        self.assertFalse(decision.ai_used)

    def test_alias_list_hit(self) -> None:
        state = _state_with_canonical("venezia", "Venezia", aliases=["Venezia (repubblica)"])
        raw = RawSubject("Venezia (repubblica)", [3], [3])
        decision = match_subject(
            raw,
            state,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-2",
        )
        self.assertEqual(decision.action, "alias")
        self.assertEqual(decision.canonical_id, "venezia")

    def test_fuzzy_borderline_without_ai_matches_lexically(self) -> None:
        state = _state_with_canonical("venezia", "Repubblica di Venezia")
        raw = RawSubject("Repubblica Venezia", [4], [4])
        decision = match_subject(
            raw,
            state,
            _fake_client(),
            self.sqlite_path,
            _settings(str(self.data_root), use_ai=False),
            "req-3",
        )
        self.assertEqual(decision.action, "match")
        self.assertEqual(decision.canonical_id, "venezia")

    @patch("src.ingestion.polyindex.subject_matcher._cosine_similarity", return_value=0.88)
    def test_llm_same_resolves_borderline_embedding(self, _mock_sim: MagicMock) -> None:
        state = _state_with_canonical("roma", "Roma antica")
        client = _fake_client({"Roma|Roma antica": True})
        raw = RawSubject("Roma", [5], [5])
        decision = match_subject(
            raw,
            state,
            client,
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-4",
        )
        self.assertEqual(decision.action, "match")
        self.assertEqual(decision.canonical_id, "roma")
        self.assertTrue(decision.ai_used)

    @patch("src.ingestion.polyindex.subject_matcher._cosine_similarity", return_value=0.88)
    def test_llm_different_creates_new(self, _mock_sim: MagicMock) -> None:
        state = _state_with_canonical("milano", "Milano")
        client = _fake_client({"Milano moderna|Milano": False})
        raw = RawSubject("Milano moderna", [6], [6])
        decision = match_subject(
            raw,
            state,
            client,
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-5",
        )
        self.assertEqual(decision.action, "new")
        self.assertNotEqual(decision.canonical_id, "milano")

    def test_idempotent_decisions(self) -> None:
        state = _empty_state()
        raw = RawSubject("Nuovo soggetto", [1], [1])
        client = _fake_client()
        settings = _settings(str(self.data_root), use_ai=False)
        first = match_subject(raw, state, client, self.sqlite_path, settings, "req-6")
        state["subjects"][first.canonical_id] = {
            "canonical_label": raw.raw_label,
            "aliases": [],
            "books": {},
        }
        second = match_subject(raw, state, client, self.sqlite_path, settings, "req-6b")
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertEqual(first.action, "new")
        self.assertEqual(second.action, "match")

    def test_normalize_label_strips_accents(self) -> None:
        self.assertEqual(normalize_label("Città"), normalize_label("citta"))

    def test_parse_llm_json_embedded_in_prose(self) -> None:
        content = (
            'Certo. Ecco la risposta:\n```json\n'
            '{"same": true, "reason": "stesso toponimo"}\n```\nFine.'
        )
        parsed = _parse_llm_same_response(content)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        same, reason = parsed
        self.assertTrue(same)
        self.assertIn("toponimo", reason)

    @patch("src.ingestion.polyindex.subject_matcher._cosine_similarity", return_value=0.88)
    def test_unparseable_llm_does_not_crash(self, _mock_sim: MagicMock) -> None:
        state = _state_with_canonical("roma", "Roma antica")
        client = _fake_client()

        class BadChatCompletions:
            def create(self, **kwargs: object) -> MagicMock:
                choice = MagicMock()
                choice.message.content = "Sì, sono la stessa entità storica."
                response = MagicMock()
                response.choices = [choice]
                return response

        client.chat.completions = BadChatCompletions()
        raw = RawSubject("Roma", [5], [5])
        decision = match_subject(
            raw,
            state,
            client,
            self.sqlite_path,
            _settings(str(self.data_root)),
            "req-parse-fail",
        )
        self.assertEqual(decision.action, "new")
        self.assertEqual(decision.ai_reason, "llm_response_unparseable")
