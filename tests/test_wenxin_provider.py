"""Tests for Baidu Wenxin provider routing and SSE handling."""
from __future__ import annotations

import base64
import json

import pytest

import account_pool as account_pool_module
from account_pool import AccountPool
from credential_import import parse_credential_payload
from provider_registry import (
    PROVIDER_WENXIN,
    provider_for_model,
    wenxin_upstream_model,
)
from proxy_pool import ProxyPool
from wenxin_adapter import WenxinAdapter, wenxin_chat_token


class _Response:
    def __init__(self, lines):
        self._lines = [line.encode() for line in lines]
        self.closed = False
        self.status_code = 200

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _adapter() -> WenxinAdapter:
    adapter = object.__new__(WenxinAdapter)
    adapter.token = "frame-token"
    adapter.lid = "9300932197340871047"
    adapter.cookies = ""
    adapter.device_id = ""
    adapter.user_id = ""
    adapter.session_id = ""
    return adapter


def _message(value: str, *, finished: bool = False) -> str:
    return "data:" + json.dumps(
        {
            "status": 0,
            "seq_id": 1,
            "data": {
                "message": {
                    "metaData": {
                        "state": "generate-complete" if finished else "generating-resp"
                    },
                    "content": {
                        "generator": {
                            "component": "markdown-yiyan",
                            "isFinished": finished,
                            "data": {"value": value},
                        }
                    },
                }
            },
        },
        ensure_ascii=False,
    )


def test_wenxin_model_routing_and_chat_token():
    assert provider_for_model("wenxin-smart-mode") == PROVIDER_WENXIN
    assert wenxin_upstream_model("wenxin-smart-mode") == "smartMode"
    token = wenxin_chat_token("frame-token", "9300932197340871047", "hi")
    decoded = base64.b64decode(token.split("-", 1)[0]).decode()
    assert decoded.startswith("frame-token|")
    assert decoded.endswith("|9300932197340871047")


def test_wenxin_sse_emits_incremental_generator_text():
    adapter = _adapter()
    response = _Response([
        'event:basedata',
        'data:{"status":0,"user":{"isUserLogin":true}}',
        "",
        "event:message",
        _message("你"),
        "",
        "event:message",
        _message("好"),
        "",
        "event:message",
        _message("", finished=True),
        "",
    ])
    adapter._open = lambda *_a, **_k: response

    assert list(adapter.chat_stream("session", "hi")) == ["你", "好"]
    assert response.closed is True


def test_wenxin_basedata_logged_out_raises_auth_error():
    from wenxin_adapter import WenxinAuthError

    adapter = _adapter()
    response = _Response([
        'event:basedata',
        'data:{"status":0,"user":{"isUserLogin":false}}',
        "",
    ])
    adapter._open = lambda *_a, **_k: response

    with pytest.raises(WenxinAuthError):
        list(adapter.chat_stream("session", "hi"))


def _wenxin_har() -> dict:
    chat_token = wenxin_chat_token("frame-token", "9300932197340871047", "hi")
    body = {
        "message": {
            "searchInfo": {
                "chatParams": {"chat_token": chat_token}
            }
        }
    }
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://chat.baidu.com/aichat/api/conversation",
                        "headers": [{"name": "Cookie", "value": "BDUSS=test-cookie"}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": json.dumps(body),
                        },
                    },
                    "response": {"headers": [], "content": {}},
                }
            ]
        }
    }


def test_wenxin_har_import_extracts_token_and_lid():
    har = _wenxin_har()
    har["log"]["entries"][0]["request"]["headers"] = [
        {"name": "Cookie", "value": "BDUSS=test-cookie; BAIDUID=test-baiduid"}
    ]
    imported = parse_credential_payload(har)

    assert imported.provider == "wenxin"
    assert imported.token == ""
    assert imported.user_id == "test-baiduid"
    assert imported.cookies == "BDUSS=test-cookie; BAIDUID=test-baiduid"
    assert imported.source == "har:wenxin-cookie"


def test_wenxin_har_import_uses_cookie_when_chat_token_is_anonymous():
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://chat.baidu.com/csaitab/history/list",
                        "headers": [
                            {
                                "name": "Cookie",
                                "value": "BDUSS=test-bduss; BAIDUID=test-baiduid",
                            }
                        ],
                    },
                    "response": {"headers": [], "content": {}},
                }
            ]
        }
    }
    imported = parse_credential_payload(har)

    assert imported.provider == "wenxin"
    assert imported.user_id == "test-baiduid"
    assert imported.cookies == "BDUSS=test-bduss; BAIDUID=test-baiduid"


def test_wenxin_cookie_string_imports_login_identity():
    imported = parse_credential_payload(
        "BDUSS=test-bduss; BAIDUID=test-baiduid"
    )

    assert imported.provider == "wenxin"
    assert imported.token == ""
    assert imported.user_id == "test-baiduid"
    assert imported.cookies == "BDUSS=test-bduss; BAIDUID=test-baiduid"
    assert imported.source == "wenxin-cookie"


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


def test_wenxin_account_requires_token_and_lid(clean_pool):
    with pytest.raises(ValueError, match="BDUSS"):
        clean_pool.add(
            token="",
            cookies="",
            provider=PROVIDER_WENXIN,
            user_id="lid",
        )

    account = clean_pool.add(
        token="",
        cookies="BDUSS=test; BAIDUID=test-baiduid",
        provider=PROVIDER_WENXIN,
    )
    assert account.provider == PROVIDER_WENXIN
    assert account.user_id == "test-baiduid"
