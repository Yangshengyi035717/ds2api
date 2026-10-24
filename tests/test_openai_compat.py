"""Sub2API/OpenAI compatibility tests for the public v1 surface."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse

import server
from api_key_store import ApiKeyStore
from runtime_config import RuntimeConfigStore


@pytest.fixture
def compat_client(monkeypatch, tmp_path):
    key_store = ApiKeyStore(tmp_path / "compat-keys.sqlite3")
    record = key_store.create_key("sub2api")
    runtime = RuntimeConfigStore(
        tmp_path / "runtime.json",
        default_model_name="deepseek-chat",
    )
    monkeypatch.setattr(server, "API_KEY_STORE", key_store)
    monkeypatch.setattr(server, "get_runtime_config", lambda: runtime)
    with TestClient(server.app) as client:
        yield client, record["key"], runtime


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_models_expose_sub2api_metadata(compat_client):
    client, key, _ = compat_client

    response = client.get("/v1/models", headers=_auth(key))

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert {model["id"] for model in body["data"]} == {
        "deepseek-chat",
        "glm-5.3-flash",
        "kimi-k2.6",
        "kimi-k3",
        "kimi-k2.8",
        "qwen3.7-max",
        "hunyuan-hy4-preview",
        "wenxin-smart-mode",
        "doubao-seed",
        "mimo-v2.6-pro",
        "mimo-v2.6-flash",
        "mimo-v2.6-pro-ultraspeed-studio",
    }
    deepseek = next(model for model in body["data"] if model["id"] == "deepseek-chat")
    zai = next(model for model in body["data"] if model["id"] == "glm-5.3-flash")
    assert deepseek["owned_by"] == "deepseek"
    assert deepseek["provider"] == "deepseek"
    assert deepseek["display_name"] == "DeepSeek Chat"
    assert deepseek["reasoning"] is True
    assert deepseek["input_modalities"] == ["text"]
    assert deepseek["supported_endpoints"] == [
        "/chat/completions",
        "/responses",
        "/messages",
    ]
    assert zai["owned_by"] == "zai"
    assert zai["provider"] == "zai"
    assert zai["display_name"] == "GLM-5.3 Flash"
    kimi = next(model for model in body["data"] if model["id"] == "kimi-k2.6")
    assert kimi["provider"] == "kimi"
    assert kimi["display_name"] == "Kimi K2.6"
    qwen = next(model for model in body["data"] if model["id"] == "qwen3.7-max")
    assert qwen["provider"] == "qianwen"
    assert qwen["display_name"] == "Qwen3.7 Max"
    hunyuan = next(
        model for model in body["data"] if model["id"] == "hunyuan-hy4-preview"
    )
    assert hunyuan["provider"] == "hunyuan"
    assert hunyuan["display_name"] == "Hunyuan HY4 Preview"
    wenxin = next(
        model for model in body["data"] if model["id"] == "wenxin-smart-mode"
    )
    assert wenxin["provider"] == "wenxin"
    assert wenxin["display_name"] == "Wenxin Smart Mode"
    doubao = next(
        model for model in body["data"] if model["id"] == "doubao-seed"
    )
    assert doubao["provider"] == "doubao"
    assert doubao["display_name"] == "Doubao Seed"


def test_single_model_lookup_and_openai_404(compat_client):
    client, key, _ = compat_client

    found = client.get("/v1/models/deepseek-chat", headers=_auth(key))
    assert found.status_code == 200
    assert found.json()["id"] == "deepseek-chat"

    zai = client.get("/v1/models/glm-5.3-flash", headers=_auth(key))
    assert zai.status_code == 200
    assert zai.json()["provider"] == "zai"

    kimi = client.get("/v1/models/kimi-k2.6", headers=_auth(key))
    assert kimi.status_code == 200
    assert kimi.json()["provider"] == "kimi"

    qwen = client.get("/v1/models/qwen3.7-max", headers=_auth(key))
    assert qwen.status_code == 200
    assert qwen.json()["provider"] == "qianwen"

    hunyuan = client.get("/v1/models/hunyuan-hy4-preview", headers=_auth(key))
    assert hunyuan.status_code == 200
    assert hunyuan.json()["provider"] == "hunyuan"

    wenxin = client.get("/v1/models/wenxin-smart-mode", headers=_auth(key))
    assert wenxin.status_code == 200
    assert wenxin.json()["provider"] == "wenxin"

    doubao = client.get("/v1/models/doubao-seed", headers=_auth(key))
    assert doubao.status_code == 200
    assert doubao.json()["provider"] == "doubao"

    missing = client.get("/v1/models/not-a-model", headers=_auth(key))
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "not_found_error"
    assert "not-a-model" in missing.json()["error"]["message"]


def test_v1_errors_use_openai_envelope(compat_client):
    client, key, _ = compat_client

    missing_auth = client.get("/v1/models")
    assert missing_auth.status_code == 401
    assert missing_auth.json()["error"]["type"] == "authentication_error"
    assert missing_auth.json()["error"]["code"] == "invalid_api_key"

    invalid_body = client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "deepseek-chat"},
    )
    assert invalid_body.status_code == 422
    assert invalid_body.json()["error"]["type"] == "invalid_request_error"
    assert invalid_body.json()["error"]["code"] == "invalid_request"


def test_provider_manifest_is_authenticated_and_descriptive(compat_client):
    client, key, _ = compat_client

    assert client.get("/v1/provider-manifest").status_code == 401
    response = client.get("/v1/provider-manifest", headers=_auth(key))
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["provider"]["protocols"] == [
        "openai.chat_completions",
        "openai.responses",
        "anthropic.messages",
    ]
    assert body["models"][0]["capabilities"] == {
        "stream": True,
        "thinking": True,
        "search": True,
        "tools": True,
        "responses": True,
        "vision": False,
    }
    assert {model["provider"] for model in body["models"]} == {
        "deepseek",
        "zai",
        "kimi",
        "qianwen",
        "hunyuan",
        "wenxin",
        "doubao",
        "mimo",
    }
    assert body["pricing"]["automatic"] is False


def test_streaming_finish_chunk_contains_usage(monkeypatch):
    class FakeAdapter:
        def chat_stream(self, session_id, prompt, **kwargs):
            ready_out = kwargs["ready_out"]
            ready_out["response_message_id"] = 2
            ready_out["session_id"] = session_id
            yield "hello"
            yield {"__type": "status", "status": "FINISHED"}

    class FakeAcquired:
        acct = SimpleNamespace(id="account-test")
        adapter = FakeAdapter()
        parent_message_id = None

        def prepare_prompt(self, prompt):
            return prompt

        def create_session(self, *args, **kwargs):
            return "session-test"

        def record_message_id(self, *args, **kwargs):
            return None

        def release(self):
            return None

    monkeypatch.setattr(
        server,
        "_acquire",
        lambda cache_key=None, provider="deepseek": FakeAcquired(),
    )

    async def collect() -> str:
        response = await server._handle_stream(
            "chatcmpl-compat",
            "hello",
            include_usage=True,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(collect())
    events = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-2]["usage"]["prompt_tokens"] > 0
    assert events[-2]["usage"]["completion_tokens"] > 0
    assert events[-1]["choices"] == []
    assert events[-1]["usage"] == events[-2]["usage"]


def test_streaming_repairs_malformed_view_image_tool_call(monkeypatch):
    malformed = (
        "<|DSML|tool_calls>\n"
        '  <|DSML|invoke name="view_image">\n'
        '    <|DSML|parameter name="path"><![CDATA[C:\\tmp\\image.png]]>'
        "</|DSML|parameter>\n"
        "  <|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )

    class FakeAdapter:
        def chat_stream(self, session_id, prompt, **kwargs):
            ready_out = kwargs["ready_out"]
            ready_out["response_message_id"] = 2
            ready_out["session_id"] = session_id
            for index in range(0, len(malformed), 7):
                yield malformed[index:index + 7]
            yield {"__type": "status", "status": "FINISHED"}

    class FakeAcquired:
        acct = SimpleNamespace(id="account-test")
        adapter = FakeAdapter()
        parent_message_id = None

        def prepare_prompt(self, prompt):
            return prompt

        def create_session(self, *args, **kwargs):
            return "session-test"

        def record_message_id(self, *args, **kwargs):
            return None

        def release(self):
            return None

    monkeypatch.setattr(
        server,
        "_acquire",
        lambda cache_key=None, provider="deepseek": FakeAcquired(),
    )
    tools = [server.ToolDef(
        type="function",
        function=server.FunctionDef(
            name="view_image",
            description="View a local image",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    )]

    async def collect() -> str:
        response = await server._handle_stream(
            "chatcmpl-view-image",
            "inspect image",
            tools,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(collect())
    events = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    tool_deltas = [
        tool
        for event in events
        for tool in (event.get("choices") or [{}])[0].get("delta", {}).get("tool_calls", [])
    ]

    assert not any(
        "<|DSML|" in str((event.get("choices") or [{}])[0].get("delta", {}).get("content", ""))
        for event in events
    )
    assert any(
        tool.get("function", {}).get("name") == "view_image"
        for tool in tool_deltas
    )
    assert any(
        "image.png" in tool.get("function", {}).get("arguments", "")
        for tool in tool_deltas
    )


def test_responses_nonstream_translation(compat_client, monkeypatch):
    client, key, _ = compat_client

    monkeypatch.setattr(
        server,
        "_handle_nonstream",
        lambda *args, **kwargs: {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello from responses"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        },
    )

    response = client.post(
        "/v1/responses",
        headers=_auth(key),
        json={"model": "deepseek-chat", "input": "hello"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "hello from responses"
    assert body["usage"] == {
        "input_tokens": 5,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 8,
    }


def test_glm_request_routes_to_zai_provider(compat_client, monkeypatch):
    client, key, _ = compat_client
    captured = {}

    def fake_nonstream(*args, **kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-zai",
            "object": "chat.completion",
            "model": kwargs["model_name"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "z.ai response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)

    response = client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "model": "glm-5.3-flash",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "glm-5.3-flash"
    assert captured["provider"] == "zai"
    assert captured["model_name"] == "glm-5.3-flash"
    assert captured["upstream_model"] == "x-preview-l"


def test_hunyuan_request_routes_to_hunyuan_provider(compat_client, monkeypatch):
    client, key, _ = compat_client
    captured = {}

    def fake_nonstream(*args, **kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-hunyuan",
            "object": "chat.completion",
            "model": kwargs["model_name"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hunyuan response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)

    response = client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "model": "hunyuan-hy4-preview",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "hunyuan-hy4-preview"
    assert captured["provider"] == "hunyuan"
    assert captured["model_name"] == "hunyuan-hy4-preview"
    assert captured["upstream_model"] == "hy4-preview"


def test_wenxin_request_routes_to_wenxin_provider(compat_client, monkeypatch):
    client, key, _ = compat_client
    captured = {}

    def fake_nonstream(*args, **kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-wenxin",
            "object": "chat.completion",
            "model": kwargs["model_name"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "wenxin response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)

    response = client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "model": "wenxin-smart-mode",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "wenxin-smart-mode"
    assert captured["provider"] == "wenxin"
    assert captured["model_name"] == "wenxin-smart-mode"
    assert captured["upstream_model"] == "smartMode"


def test_doubao_request_routes_to_doubao_provider(compat_client, monkeypatch):
    client, key, _ = compat_client
    captured = {}

    def fake_nonstream(*args, **kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-doubao",
            "object": "chat.completion",
            "model": kwargs["model_name"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "doubao response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    monkeypatch.setattr(server, "_handle_nonstream", fake_nonstream)

    response = client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "model": "doubao-seed",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == "doubao-seed"
    assert captured["provider"] == "doubao"
    assert captured["model_name"] == "doubao-seed"
    assert captured["upstream_model"] == "0"


def test_responses_stream_translation(compat_client, monkeypatch):
    client, key, _ = compat_client

    async def fake_stream(*args, **kwargs):
        async def events():
            yield (
                'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
                '"model":"deepseek-chat","choices":[{"index":0,'
                '"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            )
            yield (
                'data: {"id":"chatcmpl-test","object":"chat.completion.chunk",'
                '"model":"deepseek-chat","choices":[{"index":0,'
                '"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(server, "_handle_stream", fake_stream)

    response = client.post(
        "/v1/responses",
        headers=_auth(key),
        json={"model": "deepseek-chat", "input": "hello", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.output_text.delta" in response.text
    assert '"delta": "hello"' in response.text
    assert "event: response.completed" in response.text
    assert '"total_tokens": 3' in response.text


def test_responses_nonstream_function_call(compat_client, monkeypatch):
    client, key, _ = compat_client
    monkeypatch.setattr(
        server,
        "_handle_nonstream",
        lambda *args, **kwargs: {
            "id": "chatcmpl-tool",
            "object": "chat.completion",
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{\"city\":\"Beijing\"}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 4,
                "total_tokens": 11,
            },
        },
    )

    response = client.post(
        "/v1/responses",
        headers=_auth(key),
        json={
            "model": "deepseek-chat",
            "input": "weather?",
            "tools": [{
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }],
        },
    )

    assert response.status_code == 200
    item = response.json()["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "get_weather"
    assert item["call_id"] == "call_1"
    assert item["arguments"] == "{\"city\":\"Beijing\"}"


def test_responses_alias_uses_openai_error_envelope(compat_client):
    client, _, _ = compat_client
    response = client.post("/responses", json={"input": "hello"})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


@pytest.mark.parametrize("failure", ["exception", "eof", "done_only", "error", "malformed"])
def test_responses_stream_failure_never_emits_completed(compat_client, monkeypatch, failure):
    client, key, _ = compat_client
    closed = []

    async def fake_stream(*args, **kwargs):
        async def events():
            try:
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                if failure == "exception":
                    raise RuntimeError("private transport details")
                if failure == "done_only":
                    yield "data: [DONE]\n\n"
                if failure == "error":
                    yield 'data: {"error":{"code":"upstream_temporarily_unavailable","message":"interrupted"}}\n\n'
                if failure == "malformed":
                    yield 'data: {broken}\n\n'
            finally:
                closed.append(True)
        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(server, "_handle_stream", fake_stream)
    response = client.post("/v1/responses", headers=_auth(key), json={"input": "test", "stream": True})
    assert response.status_code == 200
    assert response.text.count("event: error\n") == 1
    assert response.text.count("event: response.failed\n") == 1
    assert "response.completed" not in response.text
    assert "private transport details" not in response.text
    assert closed == [True]


def test_responses_heartbeat_is_not_lost_and_utf8_chunks_are_incremental(compat_client, monkeypatch):
    client, key, _ = compat_client

    async def fake_stream(*args, **kwargs):
        async def events():
            yield ": keep-alive\n\n"
            raw = ('data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "想一下"}}]}, ensure_ascii=False) + '\r\n\r\n').encode()
            for byte in raw:
                yield bytes([byte])
            yield 'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(server, "_handle_stream", fake_stream)
    response = client.post("/v1/responses", headers=_auth(key), json={"input": "test", "stream": True})
    assert response.text.count("event: response.in_progress\n") == 2
    assert "event: response.reasoning_summary_text.delta" in response.text
    assert '"delta": "想一下"' in response.text
    assert response.text.count("event: response.completed\n") == 1
    assert "event: error\n" not in response.text
