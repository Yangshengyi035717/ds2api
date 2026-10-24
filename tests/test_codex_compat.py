"""Codex-specific Responses tool and SSE compatibility regressions."""
from __future__ import annotations

import json
import asyncio
import time

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse
from types import SimpleNamespace

from adapter import UpstreamContextLimitError
from mimo_adapter import MimoAdapter
from responses_format import (
    ResponsesStreamTranslator,
    ResponsesToolAdapter,
    decode_compaction_summary,
    encode_compaction_summary,
    has_compaction_trigger,
    input_to_chat_messages,
    tools_to_chat_tools,
)
from server import (
    MIMO_CODEX_CONTEXT_WINDOW,
    MIMO_SAFE_PROMPT_TOKENS,
    _requires_local_file_action,
    _is_context_limit_error,
    _compact_codex_messages,
)
import server


RAW_TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "description": "Run a command",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
    {
        "type": "namespace",
        "name": "multi_agent_v1",
        "children": [{
            "type": "function",
            "name": "spawn_agent",
            "parameters": {"type": "object", "properties": {}},
        }],
    },
    {
        "type": "custom",
        "name": "apply_patch",
        "description": "Apply a patch",
    },
    {"type": "web_search"},
]


def event_payloads(events):
    return [
        json.loads(line[6:])
        for event in events
        for line in event.splitlines()
        if line.startswith("data: ")
    ]


def test_codex_core_mode_keeps_only_essential_function_and_custom_tools():
    adapter = ResponsesToolAdapter(
        RAW_TOOLS,
        codex_mode=True,
        tool_mode="core",
    )
    names = [tool["function"]["name"] for tool in adapter.chat_tools]
    assert names == ["exec_command", "apply_patch"]


def test_all_mode_flattens_namespace_and_restores_namespace_item():
    adapter = ResponsesToolAdapter(RAW_TOOLS, codex_mode=True, tool_mode="all")
    names = [tool["function"]["name"] for tool in adapter.chat_tools]
    assert "multi_agent_v1__spawn_agent" in names
    item = adapter.restore_tool_call(
        "multi_agent_v1__spawn_agent",
        '{"task":"inspect"}',
        "call_agent",
    )
    assert item["type"] == "function_call"
    assert item["name"] == "spawn_agent"
    assert item["namespace"] == "multi_agent_v1"
    assert item["call_id"] == "call_agent"


def test_custom_tool_round_trips_through_chat_history_and_response_item():
    adapter = ResponsesToolAdapter(RAW_TOOLS, codex_mode=True, tool_mode="all")
    messages = input_to_chat_messages(
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": "*** Begin Patch",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": "Done",
            },
        ],
        tool_adapter=adapter,
    )
    call = messages[0]["tool_calls"][0]
    assert call["function"]["name"] == "apply_patch"
    assert json.loads(call["function"]["arguments"]) == {"input": "*** Begin Patch"}
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call_patch",
        "content": "Done",
    }

    item = adapter.restore_tool_call(
        "apply_patch",
        '{"input":"*** Begin Patch"}',
        "call_patch",
    )
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "apply_patch"
    assert item["input"] == "*** Begin Patch"


def test_namespace_history_is_lowered_to_flattened_chat_name():
    adapter = ResponsesToolAdapter(RAW_TOOLS, codex_mode=True, tool_mode="all")
    messages = input_to_chat_messages(
        [{
            "type": "function_call",
            "call_id": "call_ns",
            "name": "spawn_agent",
            "namespace": "multi_agent_v1",
            "arguments": "{}",
        }],
        tool_adapter=adapter,
    )
    assert messages[0]["tool_calls"][0]["function"]["name"] == (
        "multi_agent_v1__spawn_agent"
    )


def test_backward_tools_function_wrapper_still_supports_flat_function_tools():
    tools = tools_to_chat_tools([RAW_TOOLS[0]])
    assert tools[0]["function"]["name"] == "exec_command"


def test_stream_opens_reasoning_then_message_then_live_tool_call():
    adapter = ResponsesToolAdapter(
        [RAW_TOOLS[0]],
        codex_mode=True,
        tool_mode="core",
    )
    translator = ResponsesStreamTranslator(
        "resp_codex",
        "deepseek-chat",
        {},
        tool_adapter=adapter,
    )
    events = event_payloads(translator.initial_events())
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
    ]

    events += event_payloads(translator.feed_chat_chunk({
        "choices": [{"delta": {"reasoning_content": "think"}}],
    }))
    events += event_payloads(translator.feed_chat_chunk({
        "choices": [{"delta": {"content": "answer"}}],
    }))
    events += event_payloads(translator.feed_chat_chunk({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_exec",
            "function": {
                "name": "exec_command",
                "arguments": '{"cmd":"echo ok"}',
            },
        }]}}],
    }))
    events += event_payloads(translator.final_events())

    completed = events[-1]
    assert completed["type"] == "response.completed"
    assert [item["type"] for item in completed["response"]["output"]] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))

    reasoning_delta = next(
        event for event in events
        if event["type"] == "response.reasoning_summary_text.delta"
    )
    assert reasoning_delta["output_index"] == 0
    text_delta = next(
        event for event in events
        if event["type"] == "response.output_text.delta"
    )
    assert text_delta["output_index"] == 1
    assert text_delta["logprobs"] == []
    tool_added = next(
        event for event in events
        if event["type"] == "response.output_item.added"
        and event["item"]["type"] == "function_call"
    )
    assert tool_added["output_index"] == 2
    args_delta = next(
        event for event in events
        if event["type"] == "response.function_call_arguments.delta"
    )
    assert args_delta["delta"] == '{"cmd":"echo ok"}'
    assert args_delta["call_id"] == "call_exec"


def test_stream_custom_tool_uses_custom_input_lifecycle():
    adapter = ResponsesToolAdapter(
        [RAW_TOOLS[2]],
        codex_mode=True,
        tool_mode="core",
    )
    translator = ResponsesStreamTranslator(
        "resp_custom",
        "deepseek-chat",
        {},
        tool_adapter=adapter,
    )
    events = translator.initial_events()
    events += translator.feed_chat_chunk({"choices": [{"delta": {
        "tool_calls": [{
            "index": 0,
            "id": "call_patch",
            "function": {
                "name": "apply_patch",
                "arguments": '{"input":"*** Begin Patch"}',
            },
        }],
    }}]})
    events += translator.final_events()
    payloads = event_payloads(events)
    completed = payloads[-1]["response"]
    assert completed["output"][0]["type"] == "custom_tool_call"
    assert completed["output"][0]["input"] == "*** Begin Patch"
    assert any(
        event["type"] == "response.custom_tool_call_input.done"
        for event in payloads
    )
    assert not any(
        event["type"] == "response.function_call_arguments.done"
        for event in payloads
    )


def test_compaction_item_round_trips_into_next_chat_history():
    encrypted = encode_compaction_summary("Goal: keep the API healthy.")
    assert decode_compaction_summary(encrypted) == "Goal: keep the API healthy."
    messages = input_to_chat_messages([{
        "type": "compaction",
        "encrypted_content": encrypted,
    }])
    assert messages == [{
        "role": "system",
        "content": "Compacted prior conversation:\nGoal: keep the API healthy.",
    }]
    assert has_compaction_trigger([{"type": "compaction_trigger"}])

    summary_fallback = input_to_chat_messages([{
        "type": "compaction",
        "encrypted_content": "opaque-upstream-ciphertext",
        "summary": [{"type": "summary_text", "text": "Visible summary"}],
    }])
    assert summary_fallback == [{
        "role": "system",
        "content": "Compacted prior conversation:\nVisible summary",
    }]


def test_compaction_body_signal_returns_codex_compaction_sse(monkeypatch):
    def fake_nonstream(*args, **kwargs):
        return {
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": "Compact summary"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }

    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "headers": [(b"user-agent", b"codex_cli_rs/0.164.0")],
        "client": ("unit", 1),
    })
    req = server.ResponsesRequest(
        model="deepseek-chat",
        input=[
            {"type": "message", "role": "user", "content": "Finish the migration."},
            {"type": "compaction_trigger"},
        ],
        stream=True,
    )
    response = asyncio.run(server.responses(req, request))

    async def consume():
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(consume())
    events = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.done",
        "response.completed",
    ]
    item = events[-1]["response"]["output"][0]
    assert item["type"] == "compaction"
    assert decode_compaction_summary(item["encrypted_content"]) == "Compact summary"
    assert item["summary"] == [{"type": "summary_text", "text": "Compact summary"}]
    assert events[-1]["response"]["usage"]["total_tokens"] == 6


def test_compact_alias_returns_nonstream_compaction_item(monkeypatch):
    def fake_nonstream(*args, **kwargs):
        return {
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": "Alias summary"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/responses/compact",
        "headers": [(b"user-agent", b"codex_cli_rs/0.164.0")],
        "client": ("unit", 1),
    })
    req = server.ResponsesRequest(model="deepseek-chat", input=[], stream=False)
    response = asyncio.run(server.responses(req, request))
    body = json.loads(response.body)
    assert body["output"][0]["type"] == "compaction"
    assert decode_compaction_summary(
        body["output"][0]["encrypted_content"]
    ) == "Alias summary"
    assert body["output"][0]["summary"][0]["text"] == "Alias summary"


def test_mimo_query_too_long_maps_to_context_limit_without_poisoning():
    adapter = object.__new__(MimoAdapter)
    adapter.cookies = ""
    with pytest.raises(UpstreamContextLimitError):
        adapter._raise_event_error("query is too long")
    assert _is_context_limit_error(UpstreamContextLimitError("query is too long"))


def test_codex_responses_request_filters_tools_and_disables_web_search(monkeypatch):
    captured = {}

    async def fake_stream(*args, **kwargs):
        captured["tools"] = [tool.function.name for tool in args[2]]
        captured["search"] = kwargs["search_enabled"]

        async def events():
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    monkeypatch.setattr(server, "_handle_stream", fake_stream)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "headers": [(b"user-agent", b"codex_cli_rs/0.80.0")],
        "client": ("unit", 1),
    })
    req = server.ResponsesRequest(
        model="deepseek-chat",
        input="hello",
        stream=True,
        tools=RAW_TOOLS,
    )
    response = asyncio.run(server.responses(req, request))

    async def consume():
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(consume())
    assert "response.completed" in body
    assert captured == {
        "tools": ["exec_command", "apply_patch"],
        "search": False,
    }


def test_codex_mimo_tool_request_routes_ultraspeed_to_pro(monkeypatch):
    captured = {}

    async def fake_stream(*args, **kwargs):
        captured["upstream_model"] = kwargs["upstream_model"]

        async def events():
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    monkeypatch.setattr(server, "_handle_stream", fake_stream)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "headers": [(b"user-agent", b"codex_cli_rs/0.164.0")],
        "client": ("unit", 1),
    })
    req = server.ResponsesRequest(
        model="mimo-v2.6-pro-ultraspeed-studio",
        input="write a file",
        stream=True,
        tools=[RAW_TOOLS[0]],
    )
    response = asyncio.run(server.responses(req, request))

    async def consume():
        return "".join([chunk async for chunk in response.body_iterator])

    asyncio.run(consume())
    assert captured["upstream_model"] == "mimo-v2.6-pro"


def test_codex_manifest_contains_context_and_catalog_contract():
    manifest = server._build_codex_models_manifest()
    entry = next(
        item for item in manifest["models"]
        if item["slug"] == "deepseek-chat"
    )
    assert entry["display_name"] == "DeepSeek Chat"
    assert entry["context_window"] == 128_000
    assert entry["max_context_window"] == 128_000
    assert entry["auto_compact_token_limit"] == 64_000
    assert entry["truncation_policy"] == {"mode": "tokens", "limit": 10_000}
    assert entry["effective_context_window_percent"] == 85
    assert entry["supports_parallel_tool_calls"] is False
    assert entry["supports_search_tool"] is False
    assert entry["model_messages"]["instructions_template"] == ""

    mimo = next(
        item for item in manifest["models"]
        if item["slug"] == "mimo-v2.6-pro-ultraspeed-studio"
    )
    assert mimo["context_window"] == MIMO_CODEX_CONTEXT_WINDOW
    assert mimo["auto_compact_token_limit"] == 12_000


def test_codex_prompt_compacts_large_tool_call_arguments():
    messages = [
        server.ChatMessage(role="system", content="System instructions."),
        server.ChatMessage(role="user", content="Create the animation."),
        server.ChatMessage(
            role="assistant",
            content="",
            tool_calls=[server.ToolCall(
                id="call_write",
                function=server.FunctionCall(
                    name="exec_command",
                    arguments='{"cmd":"' + ("x" * 40_000) + '"}',
                ),
            )],
        ),
        server.ChatMessage(
            role="tool",
            tool_call_id="call_write",
            content="File created.",
        ),
        server.ChatMessage(role="user", content="Continue with the next step."),
    ]

    compacted, prompt, metrics = _compact_codex_messages(
        messages,
        None,
        None,
        provider="mimo",
    )

    assert metrics["original_tokens"] > metrics["compacted_tokens"]
    assert metrics["compacted_tokens"] <= MIMO_SAFE_PROMPT_TOKENS
    assert "truncated to fit the upstream context" in prompt
    assert "Continue with the next step." in prompt
    argument = compacted[2].tool_calls[0].function.arguments
    assert len(argument) < 2_000


def test_codex_tool_guard_requires_local_file_operations():
    messages = [
        server.ChatMessage(role="system", content="System instructions."),
        server.ChatMessage(role="user", content="Create snake.html."),
    ]
    tools = [server.ToolDef(
        type="function",
        function=server.FunctionDef(
            name="exec_command",
            description="Run a command",
            parameters={"type": "object"},
        ),
    )]

    _, prompt, _ = _compact_codex_messages(
        messages,
        tools,
        "auto",
        provider="mimo",
        codex_mode=True,
    )

    assert "operating on the user's local Codex machine" in prompt
    assert "MUST call the appropriate tool" in prompt
    assert "multiple chunks" in prompt


def test_local_file_action_stays_required_until_write_succeeds():
    messages = [
        server.ChatMessage(role="system", content="System instructions."),
        server.ChatMessage(role="user", content="创建一个 snake.html 文件"),
        server.ChatMessage(
            role="assistant",
            content="",
            tool_calls=[server.ToolCall(
                id="call_read",
                function=server.FunctionCall(
                    name="exec_command",
                    arguments='{"cmd":"Get-ChildItem"}',
                ),
            )],
        ),
        server.ChatMessage(
            role="tool",
            tool_call_id="call_read",
            content="outputs",
        ),
    ]
    assert _requires_local_file_action(messages)

    messages.extend([
        server.ChatMessage(
            role="assistant",
            content="",
            tool_calls=[server.ToolCall(
                id="call_write",
                function=server.FunctionCall(
                    name="exec_command",
                    arguments='{"cmd":"Set-Content snake.html ..."}',
                ),
            )],
        ),
        server.ChatMessage(
            role="tool",
            tool_call_id="call_write",
            content="Process exited with code 0",
        ),
    ])
    assert not _requires_local_file_action(messages)


def test_codex_prompt_summarizes_older_dialogue_over_budget():
    messages = [server.ChatMessage(role="system", content="System instructions.")]
    for index in range(12):
        messages.append(server.ChatMessage(
            role="user",
            content=f"older-{index} " + ("u" * 8_000),
        ))
        messages.append(server.ChatMessage(
            role="assistant",
            content=f"answer-{index} " + ("a" * 8_000),
        ))
    messages.append(server.ChatMessage(
        role="user",
        content="Keep this latest request.",
    ))

    compacted, prompt, metrics = _compact_codex_messages(
        messages,
        None,
        None,
        provider="mimo",
    )

    assert metrics["dropped_messages"] > 0
    assert metrics["compacted_tokens"] <= MIMO_SAFE_PROMPT_TOKENS
    assert "Compacted prior conversation:" in prompt
    assert "Keep this latest request." in prompt


def test_large_context_provider_keeps_larger_budget():
    messages = [
        server.ChatMessage(role="system", content="System instructions."),
        server.ChatMessage(role="user", content="u" * 80_000),
    ]

    _, _, metrics = _compact_codex_messages(
        messages,
        None,
        None,
        provider="deepseek",
    )

    assert metrics["compacted_tokens"] > MIMO_SAFE_PROMPT_TOKENS
    assert metrics["compacted_tokens"] <= 64_000


def test_models_endpoint_returns_codex_manifest_for_catalog_request(monkeypatch):
    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/models",
        "query_string": b"client_version=0.164.0",
        "headers": [(b"user-agent", b"codex_cli_rs/0.164.0")],
        "client": ("unit", 1),
    })
    response = asyncio.run(server.list_models(request))
    body = json.loads(response.body)
    assert "models" in body
    assert "data" not in body
    assert any(item["slug"] == "deepseek-chat" for item in body["models"])


def test_models_endpoint_keeps_openai_list_for_non_codex_clients(monkeypatch):
    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/models",
        "query_string": b"client_version=some-client",
        "headers": [(b"user-agent", b"openai-python/1.0")],
        "client": ("unit", 1),
    })
    response = asyncio.run(server.list_models(request))
    body = json.loads(response.body)
    assert "data" in body
    assert "models" not in body


def test_acquire_waits_for_a_busy_provider_account(monkeypatch):
    released = []
    account = SimpleNamespace(
        id="wait-account",
        provider="deepseek",
        adapter=object(),
    )
    attempts = {"count": 0}
    fake_pool = SimpleNamespace(
        acquire=lambda provider=None: (
            None if attempts.__setitem__("count", attempts["count"] + 1) is None
            and attempts["count"] < 3
            else account
        ),
        count=lambda provider=None: 1,
        release=lambda item: released.append(item.id),
    )
    monkeypatch.setattr(server, "pool", fake_pool)
    monkeypatch.setattr(server, "ACCOUNT_WAIT_SECS", 0.5)

    started = time.monotonic()
    acquired = server._acquire(provider="deepseek")
    elapsed = time.monotonic() - started
    acquired.release()

    assert elapsed >= 0.15
    assert released == ["wait-account"]
