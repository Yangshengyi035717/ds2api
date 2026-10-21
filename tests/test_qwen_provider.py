"""Tests for Qianwen provider routing and SSE accumulation."""
from __future__ import annotations

import json

import pytest

import account_pool as account_pool_module
from account_pool import AccountPool
from provider_registry import PROVIDER_QWEN, provider_for_model, qwen_upstream_model
from proxy_pool import ProxyPool
from qwen_adapter import QwenAdapter


class _Response:
    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]
        self.closed = False

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _event(content, status="processing"):
    return "data:" + json.dumps({
        "success": True,
        "data": {
            "status": status,
            "messages": [
                {
                    "mime_type": "multi_load/iframe",
                    "status": status,
                    "content": content,
                }
            ],
        },
    }, ensure_ascii=False)


def test_qwen_model_routing():
    assert provider_for_model("qwen3.7-max") == PROVIDER_QWEN
    assert qwen_upstream_model("qwen3.7-max") == "Qwen3.7-Max"


def test_qwen_sse_cumulative_content_is_converted_to_deltas():
    adapter = QwenAdapter(
        cookies="XSRF-TOKEN=x",
        device_id="device",
        user_id="user",
        xsrf_token="x",
        session_id="session",
        topic_id="topic",
    )
    response = _Response([
        _event("你"),
        "",
        _event("你好"),
        "",
        _event("你好！", "complete"),
        "",
    ])
    adapter._open = lambda *_a, **_k: response

    assert list(adapter.chat_stream("session", "hi")) == ["你", "好", "！"]
    assert response.closed is True


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


def test_qwen_account_requires_cookie_and_session(clean_pool):
    with pytest.raises(ValueError, match="cookies"):
        clean_pool.add(
            token="",
            cookies="",
            provider=PROVIDER_QWEN,
            user_id="user",
            device_id="device",
            session_id="session",
        )

    account = clean_pool.add(
        token="",
        cookies="XSRF-TOKEN=x",
        provider=PROVIDER_QWEN,
        user_id="user",
        device_id="device",
        xsrf_token="x",
        session_id="session",
        topic_id="topic",
    )
    assert account.provider == PROVIDER_QWEN
    assert account.session_id == "session"
