from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.api.chat_completions_handler import handle_chat_completions
from src.api.chat_completions_handler import _sse_chunk


class ChatStreamTests(unittest.TestCase):
    def test_sse_chunk_writes_data(self) -> None:
        handler = MagicMock()
        ok = _sse_chunk(handler, {"choices": [{"delta": {"content": "x"}}]})
        self.assertTrue(ok)
        handler.wfile.write.assert_called_once()

    @patch("src.api.chat_completions_handler.ensure_lmstudio_model_loaded")
    @patch("src.api.chat_completions_handler.build_openai_client")
    def test_stream_forwards_chunks(self, build_client_mock, _ensure_model) -> None:
        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=MagicMock(role=None, content="Hi"), finish_reason=None)]
        stream_resp = iter([chunk])
        client = MagicMock()
        msg = MagicMock()
        msg.tool_calls = None
        msg.content = ""
        client.chat.completions.create.side_effect = [
            MagicMock(choices=[MagicMock(message=msg)]),
            iter([chunk]),
        ]
        build_client_mock.return_value = client

        handler = MagicMock()
        body = (
            b'{"messages":[{"role":"user","content":"ciao"}],"stream":true}'
        )
        handler.rfile = MagicMock()
        sent = []

        def read_body(h, max_bytes):  # noqa: ARG001
            return body

        def send_json(h, status, payload):  # noqa: ARG001
            sent.append((status, payload))

        from src.models.settings import Settings

        settings = Settings.model_validate(
            {
                "DATA_ROOT": "data",
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
            }
        )

        handle_chat_completions(
            handler,
            data_root=MagicMock(),
            settings=settings,
            read_json_body=read_body,
            send_json=send_json,
        )
        self.assertTrue(handler.send_response.called)
        self.assertTrue(handler.wfile.write.called)

    @patch("src.api.chat_completions_handler.ensure_lmstudio_model_loaded")
    @patch("src.api.chat_completions_handler.execute_chat_tool", return_value='{"ok":true}')
    @patch("src.api.chat_completions_handler.build_openai_client")
    def test_stream_emits_tool_events(self, build_client_mock, _tool, _ensure_model) -> None:
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "search"
        tc.function.arguments = '{"query":"alpha"}'
        msg_tools = MagicMock()
        msg_tools.tool_calls = [tc]
        msg_tools.content = "Sto cercando…"
        msg_tools.reasoning_content = None
        msg_final = MagicMock()
        msg_final.tool_calls = None
        msg_final.content = "Risposta"
        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=MagicMock(role=None, content="Risposta", reasoning_content=None), finish_reason=None)]
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            MagicMock(choices=[MagicMock(message=msg_tools)]),
            MagicMock(choices=[MagicMock(message=msg_final)]),
            iter([chunk]),
        ]
        build_client_mock.return_value = client

        handler = MagicMock()
        written = []

        def capture_write(data):
            written.append(data.decode("utf-8"))

        handler.wfile.write.side_effect = capture_write
        handler.wfile.flush = MagicMock()

        from src.models.settings import Settings

        settings = Settings.model_validate(
            {
                "DATA_ROOT": "data",
                "OPENAI_PROVIDER": "local",
                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
            }
        )

        handle_chat_completions(
            handler,
            data_root=MagicMock(),
            settings=settings,
            read_json_body=lambda h, n: b'{"messages":[{"role":"user","content":"ciao"}],"stream":true}',
            send_json=lambda h, s, p: None,
        )
        body = "".join(written)
        self.assertIn('"type": "tool_call"', body)
        self.assertIn('"type": "thinking"', body)
        self.assertIn('"content": "Risposta"', body)
