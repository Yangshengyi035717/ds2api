"""Tests for the Kimi provider protocol adapter and routing."""
from __future__ import annotations

import base64
import json
import struct
from types import SimpleNamespace

import pytest

import account_pool as account_pool_module
from account_pool import AccountPool
from kimi_adapter import KimiAdapter, kimi_device_id_from_token, kimi_user_id_from_token
from provider_registry import (
    PROVIDER_KIMI,
    kimi_public_model,
    kimi_upstream_model,
    provider_for_model,
)
from proxy_pool import ProxyPool


def _token(exp: int = 2000000000) -> str:
    payload = {
        "sub": "kimi-user",
        "abstract_user_id": "kimi-user",
        "device_id": "kimi-device",
        "iat": 1000000000,
        "exp": exp,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzUxMiJ9.{encoded}.sig"


def _frame(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False).encode()
    return b"\x00" + struct.pack(">I", len(raw)) + raw


class _Response:
    status_code = 200
    text = ""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def iter_content(self):
        yield from self._chunks

    def close(self):
        self.closed = True


def test_kimi_model_mapping_and_jwt_claims():
    token = _token()
    assert provider_for_model("kimi-k2.6") == PROVIDER_KIMI
    assert provider_for_model("kimi-k3") == PROVIDER_KIMI
    assert kimi_public_model("kimi") == "kimi-k2.6"
    assert kimi_upstream_model("kimi-k2.6") == "k2d6-chat"
    assert kimi_upstream_model("kimi-k3") == "k3-agent"
    assert kimi_device_id_from_token(token) == "kimi-device"
    assert kimi_user_id_from_token(token) == "kimi-user"


def test_kimi_connect_stream_maps_thinking_and_answer():
    adapter = KimiAdapter(
        token=_token(),
        refresh_token="refresh-token",
    )
    response = _Response([
        _frame({"heartbeat": {}}),
        _frame({
            "op": "set",
            "mask": "block.think",
            "block": {"id": "1", "think": {"content": "think-"}},
        }),
        _frame({
            "op": "append",
            "mask": "block.think.content",
            "block": {"id": "1", "think": {"content": "part"}},
        }),
        _frame({
            "op": "set",
            "mask": "block.text",
            "block": {"id": "2", "text": {"content": "answer-"}},
        }),
        _frame({
            "op": "append",
            "mask": "block.text.content",
            "block": {"id": "2", "text": {"content": "part"}},
        }),
        _frame({"done": {}}),
    ])
    adapter._open_chat = lambda *_a, **_k: response

    items = list(adapter.chat_stream("session", "hello"))

    assert items == [
        {"__type": "thinking", "content": "think-"},
        {"__type": "thinking", "content": "part"},
        "answer-",
        "part",
    ]
    assert response.closed is True


def test_kimi_refresh_rotates_both_tokens():
    adapter = KimiAdapter(
        token=_token(exp=1),
        refresh_token="old-refresh",
    )

    class Client:
        def post(self, url, headers, json):
            assert url.endswith("/AuthService/RefreshToken")
            assert json["refreshToken"] == "old-refresh"
            return _Response([])

        def close(self):
            pass

    class RefreshResponse(_Response):
        status_code = 200
        text = ""

        def json(self):
            return {
                "accessToken": _token(),
                "refreshToken": "new-refresh",
            }

    client = Client()
    adapter._client = client
    adapter._client.post = lambda *a, **k: RefreshResponse([])
    adapter._refresh_access_token(force=True)

    assert adapter.refresh_token == "new-refresh"
    assert adapter.access_token == _token()


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


def test_kimi_account_requires_refresh_token_and_derives_identity(clean_pool):
    with pytest.raises(ValueError, match="refresh_token"):
        clean_pool.add(
            token=_token(),
            cookies="",
            provider=PROVIDER_KIMI,
        )

    account = clean_pool.add(
        token=_token(),
        cookies="",
        provider=PROVIDER_KIMI,
        refresh_token="refresh-token",
    )
    assert account.user_id == "kimi-user"
    assert account.device_id == "kimi-device"
    assert account.refresh_token == "refresh-token"

    account._adapter = SimpleNamespace(
        access_token=_token(exp=2100000000),
        refresh_token="rotated-refresh",
    )
    assert clean_pool.persist_adapter_tokens(account) is True
    assert account.refresh_token == "rotated-refresh"
