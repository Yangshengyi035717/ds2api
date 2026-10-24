"""Provider routing, signature, and z.ai stream parsing tests."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException

import account_pool as account_pool_module
from account_pool import AccountPool
from adapter import UpstreamTransientError
from provider_registry import (
    PROVIDER_DEEPSEEK,
    PROVIDER_ZAI,
    provider_for_model,
    zai_upstream_model,
)
from proxy_pool import ProxyPool
from zai_adapter import (
    HEALTH_PATH,
    ZaiAdapter,
    _signature,
    zai_user_id_from_token,
)


class _FakeResponse:
    def __init__(self, events: list[dict]):
        self._lines = [
            f"data: {json.dumps(event, ensure_ascii=False)}".encode("utf-8")
            for event in events
        ]
        self.closed = False

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJFUzI1NiJ9.{encoded}.signature"


@pytest.fixture
def clean_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("PROXY_STORE_PATH", str(tmp_path / "proxies.json"))
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    return AccountPool()


def test_signature_matches_captured_browser_request():
    assert _signature(
        "你好",
        "263f39cd-1fd9-4f77-81c6-fe157b22ebdf",
        "401b90c5-19cf-4658-b864-3d1b2598a08a",
        1792591390387,
    ) == "7d9ea3ec5b3e1cad314bf2762a571869232e89a829b80f63869c52fd9de89e8e"


def test_provider_for_model_accepts_glm_alias_and_configured_name(monkeypatch):
    assert provider_for_model("deepseek-chat") == PROVIDER_DEEPSEEK
    assert provider_for_model("glm-5.3-flash") == PROVIDER_ZAI
    assert provider_for_model("zai") == PROVIDER_ZAI

    monkeypatch.setenv("ZAI_MODEL_NAME", "company-glm")
    assert provider_for_model("company-glm") == PROVIDER_ZAI
    assert zai_upstream_model("zai") == "x-preview-l"
    monkeypatch.setenv("ZAI_UPSTREAM_MODEL", "company-upstream")
    assert zai_upstream_model("company-glm") == "company-upstream"
    assert zai_upstream_model("zai") == "company-upstream"
    assert zai_upstream_model("glm-5.3-flash") == "glm-5.3-flash"


def test_zai_user_id_is_extracted_from_jwt_and_used_by_adapter():
    token = _jwt({
        "id": "263f39cd-1fd9-4f77-81c6-fe157b22ebdf",
        "email": "masked@example.invalid",
    })

    assert zai_user_id_from_token(token) == "263f39cd-1fd9-4f77-81c6-fe157b22ebdf"
    adapter = ZaiAdapter(token=token, device_id="test-device")
    assert adapter.user_id == "263f39cd-1fd9-4f77-81c6-fe157b22ebdf"


def test_zai_health_check_uses_verified_chat_list_endpoint():
    adapter = ZaiAdapter(
        token=_jwt({"id": "health-user"}),
        cookies="acw_tc=waf; token=stale",
        device_id="test-device",
    )
    captured = {}

    class FakeClient:
        def get(self, url, headers):
            captured.update(url=url, headers=headers)
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    adapter._client = FakeClient()

    assert adapter.check_health() is True
    assert captured["url"].endswith(HEALTH_PATH)
    assert captured["headers"]["Accept"] == "application/json"
    assert "token=" in captured["headers"]["Cookie"]


def test_zai_session_creation_reuses_server_message_ids():
    adapter = ZaiAdapter(
        token=_jwt({"id": "session-user"}),
        device_id="uid_0123456789abcdef01234567",
    )
    calls = []

    class CreateResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "server-chat-id", "chat": {"id": "server-chat-id"}}

    class StreamResponse(_FakeResponse):
        status_code = 200
        text = ""

    stream_response = StreamResponse([
        {
            "type": "chat:completion",
            "data": {"delta_content": "ok", "phase": "answer"},
        },
        {
            "type": "chat:completion",
            "data": {"phase": "done", "done": True},
        },
    ])

    class FakeClient:
        def post(self, url, headers, json, stream=False, **kwargs):
            calls.append({"url": url, "body": json})
            if url.endswith("/api/v1/chats/new"):
                return CreateResponse()
            return stream_response

    adapter._client = FakeClient()
    session_id = adapter.create_session(
        "hello",
        model="glm-5.3-flash",
        thinking_enabled=True,
    )
    assert session_id == "server-chat-id"

    create_body = calls[0]["body"]["chat"]
    user_message_id = create_body["history"]["currentId"]
    assert create_body["models"] == ["x-preview-l"]
    assert create_body["enable_thinking"] is True
    assert create_body["type"] == "chat"
    assert create_body["features"] == []
    assert "flags" not in create_body

    assert list(
        adapter.chat_stream(
            session_id,
            "hello",
            thinking_enabled=True,
            upstream_model="glm-5.3-flash",
        )
    ) == ["ok"]
    completion_body = calls[1]["body"]
    assert completion_body["model"] == "x-preview-l"
    assert completion_body["current_user_message_id"] == user_message_id
    assert completion_body["id"] != user_message_id
    assert completion_body["features"]["flags"] == []


def test_zai_stream_separates_thinking_and_answer():
    adapter = ZaiAdapter(token="test-token", user_id="test-user", device_id="test-device")
    response = _FakeResponse([
        {
            "type": "chat:completion",
            "data": {"delta_content": "reasoning", "phase": "thinking"},
        },
        {
            "type": "chat:completion",
            "data": {"delta_content": "answer", "phase": "answer"},
        },
        {
            "type": "chat:completion",
            "data": {"phase": "done", "done": True},
        },
    ])
    captured: dict = {}

    def fake_open_stream(**kwargs):
        captured.update(kwargs)
        return response

    adapter._open_stream = fake_open_stream

    tokens = list(
        adapter.chat_stream(
            "chat-id",
            "hello",
            thinking_enabled=True,
            upstream_model="glm-5.3-flash",
        )
    )

    assert tokens == [
        {"__type": "thinking", "content": "reasoning"},
        "answer",
    ]
    assert captured["model"] == "glm-5.3-flash"
    assert captured["thinking"] is True
    assert response.closed is True


def test_zai_stream_rejects_end_without_done_event():
    adapter = ZaiAdapter(
        token="test-token",
        user_id="test-user",
        device_id="test-device",
    )
    response = _FakeResponse([
        {
            "type": "chat:completion",
            "data": {"delta_content": "partial answer", "phase": "answer"},
        },
    ])
    adapter._open_stream = lambda **_kwargs: response

    with pytest.raises(UpstreamTransientError, match="done event"):
        list(adapter.chat_stream("chat-id", "prompt"))
    assert response.closed is True


def test_zai_stream_wraps_incomplete_read_as_transient_error():
    adapter = ZaiAdapter(
        token="test-token",
        user_id="test-user",
        device_id="test-device",
    )

    class BrokenResponse(_FakeResponse):
        def iter_lines(self):
            yield (
                b'data: {"type":"chat:completion","data":'
                b'{"delta_content":"partial","phase":"answer"}}'
            )
            raise RequestException("transfer closed")

    response = BrokenResponse([])
    adapter._open_stream = lambda **_kwargs: response

    with pytest.raises(UpstreamTransientError, match="interrupted"):
        list(adapter.chat_stream("chat-id", "prompt"))
    assert response.closed is True


def test_zai_nonstream_collects_both_phases():
    adapter = ZaiAdapter(token="test-token", user_id="test-user", device_id="test-device")
    response = _FakeResponse([
        {
            "type": "chat:completion",
            "data": {"delta_content": "think", "phase": "thinking"},
        },
        {
            "type": "chat:completion",
            "data": {"delta_content": "hello", "phase": "answer"},
        },
        {
            "type": "chat:completion",
            "data": {"phase": "done", "done": True},
        },
    ])
    adapter._open_stream = lambda **_kwargs: response

    assert adapter.chat("chat-id", "prompt") == ("hello", "think")
    assert response.closed is True


def test_zai_nonstream_rejects_end_without_done_event():
    adapter = ZaiAdapter(
        token="test-token",
        user_id="test-user",
        device_id="test-device",
    )
    response = _FakeResponse([
        {
            "type": "chat:completion",
            "data": {"delta_content": "partial answer", "phase": "answer"},
        },
    ])
    adapter._open_stream = lambda **_kwargs: response

    with pytest.raises(UpstreamTransientError, match="done event"):
        adapter.chat("chat-id", "prompt")
    assert response.closed is True


def test_zai_request_body_uses_distinct_message_ids():
    adapter = ZaiAdapter(token="test-token", user_id="test-user", device_id="test-device")

    body = adapter._request_body(
        session_id="chat-id",
        prompt="hello",
        model="glm-5.3-flash",
        thinking=True,
        search=False,
        assistant_message_id="assistant-id",
        user_message_id="user-id",
    )

    assert body["id"] == "assistant-id"
    assert body["current_user_message_id"] == "user-id"
    assert body["signature_prompt"] == "hello"
    assert body["model"] == "glm-5.3-flash"
    assert body["features"]["reasoning_effort"] == "low"
    assert "request_id" not in body


def test_zai_request_uses_v2_completion_path():
    adapter = ZaiAdapter(token="test-token", user_id="test-user", device_id="test-device")

    url = adapter._request_url("request-id", 1792591390387, "chat-id")

    assert url.startswith("https://zchat.z.ai/api/v2/chat/completions?")
    assert "signature_timestamp=1792591390387" in url
    assert "requestId=request-id" in url
    assert "user_id=test-user" in url
    assert "request_id=" not in url
    assert "device_id=" not in url
    assert "version=0.0.1" in url
    assert "pathname=%2Fc%2Fchat-id" in url


def test_zai_account_requires_user_id_and_filters_by_provider(clean_pool):
    with pytest.raises(ValueError, match="User ID"):
        clean_pool.add(
            token="zai-token",
            cookies="",
            provider=PROVIDER_ZAI,
            user_id="",
        )

    deepseek = clean_pool.add(
        token="shared-token",
        cookies="shared-cookie",
        email="deepseek-account",
    )
    zai = clean_pool.add(
        token="shared-token",
        cookies="shared-cookie",
        email="zai-account",
        provider=PROVIDER_ZAI,
        user_id="user-1",
    )

    assert clean_pool.count(PROVIDER_DEEPSEEK) == 1
    assert clean_pool.count(PROVIDER_ZAI) == 1
    assert [item["id"] for item in clean_pool.get_all(PROVIDER_DEEPSEEK)] == [deepseek.id]
    assert [item["id"] for item in clean_pool.get_all(PROVIDER_ZAI)] == [zai.id]

    acquired_deepseek = clean_pool.acquire(PROVIDER_DEEPSEEK)
    acquired_zai = clean_pool.acquire(PROVIDER_ZAI)
    assert acquired_deepseek.id == deepseek.id
    assert acquired_zai.id == zai.id
    clean_pool.release(acquired_deepseek)
    clean_pool.release(acquired_zai)


def test_zai_account_can_derive_user_id_from_jwt(clean_pool):
    token = _jwt({"id": "derived-user"})

    account = clean_pool.add(
        token=token,
        cookies="acw_tc=waf",
        provider=PROVIDER_ZAI,
    )

    assert account.user_id == "derived-user"
    assert account.to_dict()["has_user_id"] is True


def test_zai_account_duplicate_is_scoped_to_user_id(clean_pool):
    clean_pool.add(
        token="zai-token",
        cookies="cookies",
        provider=PROVIDER_ZAI,
        user_id="user-1",
    )

    with pytest.raises(ValueError, match="already exists"):
        clean_pool.add(
            token="zai-token",
            cookies="cookies",
            provider=PROVIDER_ZAI,
            user_id="user-1",
        )

    second = clean_pool.add(
        token="zai-token",
        cookies="cookies",
        provider=PROVIDER_ZAI,
        user_id="user-2",
    )
    assert second.provider == PROVIDER_ZAI
