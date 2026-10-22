"""Tests for Tencent Hunyuan provider routing and SSE handling."""
from __future__ import annotations

import pytest

import account_pool as account_pool_module
from account_pool import AccountPool
from adapter import RateLimitError
from hunyuan_adapter import HunyuanAdapter
from provider_registry import (
    PROVIDER_HUNYUAN,
    hunyuan_upstream_model,
    provider_for_model,
)
from proxy_pool import ProxyPool


class _Response:
    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]
        self.closed = False

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _adapter() -> HunyuanAdapter:
    return HunyuanAdapter(
        cookies=(
            "hunyuan_source=web; "
            "hunyuan_user=user-123; "
            "hunyuan_token=token-456"
        )
    )


def test_hunyuan_model_routing_and_cookie_identity():
    adapter = _adapter()

    assert provider_for_model("hunyuan-hy4-preview") == PROVIDER_HUNYUAN
    assert provider_for_model("hy4-preview") == PROVIDER_HUNYUAN
    assert hunyuan_upstream_model("hunyuan-hy4-preview") == "hy4-preview"
    assert adapter.user_id == "user-123"
    assert adapter.token == "token-456"


def test_hunyuan_sse_maps_thought_and_text_channels():
    adapter = _adapter()
    response = _Response([
        "event: speech_type",
        "data: thought",
        "",
        'data: {"type":"text","msg":"推理"}',
        "",
        "event: speech_type",
        "data: text",
        "",
        'data: {"type":"text","msg":"回答"}',
        "",
        'data: {"type":"meta","chatRecordId":"record-1","empty":false}',
        "",
        "data: [DONE]",
        "",
    ])
    adapter._open = lambda *_a, **_k: response

    assert list(adapter.chat_stream("conversation-1", "hello")) == [
        {"__type": "thinking", "content": "推理"},
        "回答",
    ]
    assert response.closed is True


def test_hunyuan_http_200_stream_is_not_consumed_by_json_validation():
    adapter = _adapter()
    response = _Response([
        "event: speech_type",
        "data: text",
        "",
        'data: {"type":"text","msg":"OK"}',
        "",
        "data: [DONE]",
        "",
    ])
    response.status_code = 200
    response.text = ""

    def fail_json():
        raise AssertionError("stream response must not be parsed as JSON")

    response.json = fail_json

    class Client:
        def post(self, *args, **kwargs):
            return response

    adapter._client = Client()

    assert list(adapter.chat_stream("conversation-1", "hello")) == ["OK"]
    assert response.closed is True


def test_hunyuan_error_event_maps_daily_quota_to_rate_limit():
    adapter = _adapter()
    response = _Response([
        "event: error",
        "data: 您今日已达体验限额",
        "",
    ])
    adapter._open = lambda *_a, **_k: response

    with pytest.raises(RateLimitError, match="体验限额"):
        list(adapter.chat_stream("conversation-1", "hello"))
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


def test_hunyuan_account_can_import_with_cookie_only(clean_pool):
    account = clean_pool.add(
        token="",
        cookies="hunyuan_source=web; hunyuan_user=user-123; hunyuan_token=token-456",
        provider=PROVIDER_HUNYUAN,
    )

    assert account.provider == PROVIDER_HUNYUAN
    assert account.user_id == "user-123"
    assert account.token == "token-456"
