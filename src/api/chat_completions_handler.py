from __future__ import annotations

import json
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from src.api.chat_tools import CHAT_TOOL_DEFINITIONS, execute_chat_tool
from src.core.lmstudio_models import ensure_lmstudio_model_loaded
from src.core.openai_client import build_openai_client, build_chat_completion_extra_body
from src.core.log import ERROR_LOG_LEVEL, INFO_LOG_LEVEL, Log
from src.models.settings import Settings
from src.search.article_llm import research_model

SendJson = Callable[[BaseHTTPRequestHandler, int, Any], None]
ReadJsonBody = Callable[[BaseHTTPRequestHandler, int], bytes]
_MAX_TOOL_ROUNDS = 6
_TOOL_RESULT_PREVIEW = 4000


def _resolve_chat_model(settings: Settings, raw: object) -> str:
    if isinstance(raw, str):
        alias = raw.strip().lower()
        if alias in ("", "research", "chat"):
            return research_model(settings)
        return raw.strip()
    return research_model(settings)


def _sse_chunk(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> bool:
    try:
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        handler.wfile.write(line.encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _sse_done(handler: BaseHTTPRequestHandler) -> None:
    try:
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _emit_librarain_event(
    handler: BaseHTTPRequestHandler,
    *,
    completion_id: str,
    created: int,
    model: str,
    event: dict[str, Any],
) -> bool:
    return _sse_chunk(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            "librarain": event,
        },
    )


def _message_thinking(message: Any) -> str:
    for attr in ("reasoning_content", "thinking", "reasoning"):
        val = getattr(message, attr, None)
        if val:
            return str(val)
    return ""


def _delta_content_and_thinking(delta: Any) -> tuple[str, str]:
    content = str(getattr(delta, "content", None) or "")
    thinking_parts: list[str] = []
    for attr in ("reasoning_content", "thinking", "reasoning"):
        val = getattr(delta, attr, None)
        if val:
            thinking_parts.append(str(val))
    return content, "".join(thinking_parts)


def _preview_tool_result(result: str) -> str:
    if len(result) <= _TOOL_RESULT_PREVIEW:
        return result
    return result[:_TOOL_RESULT_PREVIEW] + "…"


def handle_chat_completions(
    handler: BaseHTTPRequestHandler,
    *,
    data_root: Path,
    settings: Settings,
    read_json_body: ReadJsonBody,
    send_json: SendJson,
) -> None:
    try:
        body = read_json_body(handler, 4 * 1024 * 1024)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"error": f"invalid JSON body: {exc}"})
        return

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        send_json(handler, 400, {"error": "messages array is required"})
        return

    stream = bool(payload.get("stream"))
    model = _resolve_chat_model(settings, payload.get("model"))
    tools = payload.get("tools")
    if tools is None:
        tools = CHAT_TOOL_DEFINITIONS
    tool_choice = payload.get("tool_choice", "auto")
    Log(
        INFO_LOG_LEVEL,
        "chat completions request",
        {
            "model": model,
            "stream": stream,
            "message_count": len(messages),
            "has_tools": bool(tools),
        },
    )

    try:
        ensure_lmstudio_model_loaded(settings, model)
    except RuntimeError as exc:
        send_json(handler, 503, {"error": str(exc)})
        return

    client = build_openai_client(settings)
    extra_body = build_chat_completion_extra_body(
        reasoning_effort=settings.reasoning_effort_research,
        reasoning_enable_thinking=settings.reasoning_enable_thinking_research,
    )
    working_messages = list(messages)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(__import__("time").time())
    sse_started = False

    def start_sse() -> None:
        nonlocal sse_started
        if sse_started:
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        sse_started = True

    def emit_event(event: dict[str, Any]) -> bool:
        if not stream:
            return True
        start_sse()
        return _emit_librarain_event(
            handler,
            completion_id=completion_id,
            created=created,
            model=model,
            event=event,
        )

    for round_idx in range(_MAX_TOOL_ROUNDS):
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": working_messages,
            "temperature": settings.research_temperature,
            "max_tokens": 4096,
            "stream": False,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = tool_choice

        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            Log(ERROR_LOG_LEVEL, "chat completions API call failed", {"error": str(exc)})
            if sse_started:
                emit_event({"type": "error", "message": str(exc)})
                _sse_done(handler)
            else:
                send_json(handler, 502, {"error": str(exc)})
            return

        choice = response.choices[0]
        message = choice.message
        tool_calls = message.tool_calls or []

        if tool_calls:
            thinking = _message_thinking(message)
            if thinking:
                emit_event({"type": "thinking", "content": thinking, "round": round_idx})
            working_messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                args = tc.function.arguments or "{}"
                if not emit_event(
                    {
                        "type": "tool_call",
                        "status": "start",
                        "name": name,
                        "arguments": args,
                        "round": round_idx,
                    }
                ):
                    return
                result = execute_chat_tool(data_root, name, args)
                if not emit_event(
                    {
                        "type": "tool_call",
                        "status": "result",
                        "name": name,
                        "arguments": args,
                        "result": _preview_tool_result(result),
                        "round": round_idx,
                    }
                ):
                    return
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
            continue

        content = message.content or ""
        if stream:
            start_sse()
            create_kwargs["stream"] = True
            try:
                stream_resp = client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                Log(ERROR_LOG_LEVEL, "chat completions stream create failed", {"error": str(exc)})
                emit_event({"type": "error", "message": str(exc)})
                _sse_done(handler)
                return
            role_sent = False
            for chunk in stream_resp:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content_delta, thinking_delta = _delta_content_and_thinking(delta)
                if thinking_delta and not emit_event({"type": "thinking", "content": thinking_delta, "stream": True}):
                    return
                payload_delta: dict[str, Any] = {}
                if delta.role and not role_sent:
                    payload_delta["role"] = delta.role
                    role_sent = True
                if content_delta:
                    payload_delta["content"] = content_delta
                if not payload_delta:
                    continue
                chunk_payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": payload_delta,
                            "finish_reason": None,
                        }
                    ],
                }
                if not _sse_chunk(handler, chunk_payload):
                    return
            finish_payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            _sse_chunk(handler, finish_payload)
            _sse_done(handler)
            Log(INFO_LOG_LEVEL, "chat completions stream done", {"rounds": round_idx + 1})
            return

        send_json(
            handler,
            200,
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
        return

    if sse_started:
        emit_event({"type": "error", "message": "tool loop exceeded max rounds"})
        _sse_done(handler)
    else:
        send_json(handler, 500, {"error": "tool loop exceeded max rounds"})
