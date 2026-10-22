"""Tests for Doubao provider routing and SSE handling."""
from __future__ import annotations

import json

import pytest

import account_pool as account_pool_module
from account_pool import AccountPool
from credential_import import parse_credential_payload
from doubao_adapter import DoubaoAdapter, doubao_cookie_value
from provider_registry import (
    PROVIDER_DOUBAO,
    doubao_upstream_model,
    provider_for_model,
)
from proxy_pool import ProxyPool


class _Response:
    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]
        self.closed = False
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _adapter() -> DoubaoAdapter:
    adapter = object.__new__(DoubaoAdapter)
    adapter.cookies = "sessionid=test; ttwid=test; msToken=test"
    adapter.device_id = "7685346952515520051"
    adapter.web_id = "7669368495313798694"
    adapter.tea_uuid = "7669368495313798694"
    adapter.fp = "verify-test"
    adapter.web_tab_id = "tab-test"
    adapter.bot_id = "7338286299411103781"
    adapter.session_id = ""
    adapter.user_id = ""
    return adapter


def _sse(event: str, payload: dict) -> list[str]:
    return [f"event: {event}", "data:" + json.dumps(payload, ensure_ascii=False), ""]


def test_doubao_model_routing_and_cookie_identity():
    assert provider_for_model("doubao-seed") == PROVIDER_DOUBAO
    assert doubao_upstream_model("doubao-seed") == "0"
    assert doubao_cookie_value("sessionid=abc; ttwid=def", "sessionid") == "abc"


def test_doubao_sse_emits_chunk_delta_text():
    adapter = _adapter()
    lines = []
    lines += _sse("SSE_ACK", {"ack_client_meta": {"conversation_id": "38448640867777538"}})
    lines += _sse("CHUNK_DELTA", {"text": "你好"})
    lines += _sse("CHUNK_DELTA", {"text": "呀"})
    lines += _sse("SSE_REPLY_END", {"end_type": 1})
    response = _Response(lines)
    adapter._open = lambda *_a, **_k: response

    assert list(adapter.chat_stream("session", "hi")) == ["你好", "呀"]
    assert response.closed is True


def test_doubao_sse_maps_thinking_block_to_reasoning():
    adapter = _adapter()
    lines = []
    lines += _sse("STREAM_MSG_NOTIFY", {
        "content": {
            "content_block": [{
                "block_type": 10040,
                "block_id": "think-1",
            }]
        }
    })
    lines += _sse("STREAM_CHUNK", {
        "patch_op": [{
            "patch_object": 1,
            "patch_value": {
                "content_block": [{
                    "block_type": 10000,
                    "parent_id": "think-1",
                    "content": {"text_block": {"text": "推理"}},
                }]
            },
        }]
    })
    lines += _sse("CHUNK_DELTA", {"text": "回答"})
    response = _Response(lines)
    adapter._open = lambda *_a, **_k: response

    assert list(adapter.chat_stream("session", "hi", thinking_enabled=True)) == [
        {"__type": "thinking", "content": "推理"},
        "回答",
    ]


def test_doubao_cookie_string_imports_login_identity():
    imported = parse_credential_payload(
        "sessionid=test; ttwid=test-ttwid; device_id=test-device; "
        "web_id=test-web; msToken=test-ms"
    )

    assert imported.provider == "doubao"
    assert imported.user_id == "test-web"
    assert imported.device_id == "test-device"
    assert imported.cookies.startswith("sessionid=test")
    assert imported.source == "doubao-cookie"


def test_doubao_har_imports_login_cookie_and_identity():
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": (
                            "https://www.doubao.com/chat/completion?"
                            "device_id=dev&web_id=web&tea_uuid=tea"
                            "&msToken=ms&a_bogus=sig"
                        ),
                        "headers": [
                            {"name": "Cookie", "value": "sessionid=sid; ttwid=tw"}
                        ],
                        "queryString": [
                            {"name": "device_id", "value": "dev"},
                            {"name": "web_id", "value": "web"},
                            {"name": "tea_uuid", "value": "tea"},
                        ],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"messages":[]}',
                        },
                    },
                    "response": {"headers": [], "content": {}},
                }
            ]
        }
    }
    imported = parse_credential_payload(har)

    assert imported.provider == "doubao"
    assert imported.device_id == "dev"
    assert imported.user_id == "web"
    assert imported.cookies == "sessionid=sid; ttwid=tw"
    assert imported.source == "har:doubao-cookie"


@pytest.fixture
def clean_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("PROXY_STORE_PATH", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(
        account_pool_module,
        "get_proxy_pool",
        lambda: ProxyPool(tmp_path / "proxies.json"),
    )
    return AccountPool()


def test_doubao_account_requires_sessionid(clean_pool):
    with pytest.raises(ValueError):
        clean_pool.add(
            token="",
            cookies="ttwid=test",
            provider=PROVIDER_DOUBAO,
        )

    account = clean_pool.add(
        token="",
        cookies="sessionid=test; ttwid=test; device_id=test-device",
        provider=PROVIDER_DOUBAO,
    )
    assert account.provider == PROVIDER_DOUBAO
    assert account.device_id == "test-device"
