"""Regression tests for transient upstream outages (``generation_err``).

The upstream answers HTTP 200 + SSE ``event: hint`` with
``finish_reason=generation_err`` and '服务器暂时不可用' when its own backend
is unhealthy. That used to surface as an opaque 502 and was never retried,
so a brief upstream blip failed the client request instead of healing.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import adapter
import server
from adapter import (
    DeepSeekAdapter,
    RateLimitError,
    UpstreamHintError,
    UpstreamTransientError,
)


@pytest.mark.parametrize(
    "content, finish_reason",
    [
        ("服务器暂时不可用", "generation_err"),
        ("服务暂时不可用", "generation_err"),
        ("temporarily unavailable", "generation_err"),
        ("服务器暂时不可用", "upstream_hint_error"),
        ("whatever", "generation_err"),
    ],
)
def test_generation_err_is_transient(content, finish_reason):
    with pytest.raises(UpstreamTransientError):
        DeepSeekAdapter._raise_hint_error(content, finish_reason)


def test_rate_limit_is_still_rate_limit_error():
    with pytest.raises(RateLimitError):
        DeepSeekAdapter._raise_hint_error(
            "消息发送过于频繁，请稍后重试", "rate_limit_reached"
        )


def test_unknown_hint_stays_generic():
    with pytest.raises(UpstreamHintError) as exc:
        DeepSeekAdapter._raise_hint_error("some unrelated failure", "other_reason")
    assert not isinstance(exc.value, (UpstreamTransientError, RateLimitError))


def test_transient_is_hint_subclass_but_not_rate_limit():
    # Ordering matters: server.py catches RateLimitError and
    # UpstreamTransientError before the generic UpstreamHintError.
    assert issubclass(UpstreamTransientError, UpstreamHintError)
    assert not issubclass(UpstreamTransientError, RateLimitError)


def _stream_adapter(monkeypatch, failures: int):
    """Adapter whose first ``failures`` streaming attempts raise generation_err."""
    calls = {"n": 0}
    adapter_obj = DeepSeekAdapter.__new__(DeepSeekAdapter)

    def fake_once(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise UpstreamTransientError(
                "upstream_hint_error: 服务器暂时不可用 (generation_err)"
            )
        yield "OK"

    monkeypatch.setattr(adapter_obj, "_chat_stream_once", fake_once)
    monkeypatch.setattr(adapter_obj, "create_session", lambda: "fresh-session")
    monkeypatch.setattr(adapter, "RATE_LIMIT_RETRY_DELAYS", [0.0, 0.0])
    return adapter_obj, calls


def test_stream_retries_transient_then_succeeds(monkeypatch):
    adapter_obj, calls = _stream_adapter(monkeypatch, failures=1)

    tokens = list(adapter_obj.chat_stream("sess", "prompt"))

    assert tokens == ["OK"]
    assert calls["n"] == 2


def test_stream_gives_up_after_retry_budget(monkeypatch):
    adapter_obj, calls = _stream_adapter(monkeypatch, failures=99)

    with pytest.raises(UpstreamTransientError):
        list(adapter_obj.chat_stream("sess", "prompt"))
    # Two configured delays => initial attempt + 2 retries.
    assert calls["n"] == 3


def test_nonstream_retries_transient_then_succeeds(monkeypatch):
    adapter_obj = DeepSeekAdapter.__new__(DeepSeekAdapter)
    calls = {"n": 0}

    def fake_once(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UpstreamTransientError("generation_err")
        return "OK", ""

    monkeypatch.setattr(adapter_obj, "_chat_once", fake_once)
    monkeypatch.setattr(adapter_obj, "create_session", lambda: "fresh-session")
    monkeypatch.setattr(adapter, "RATE_LIMIT_RETRY_DELAYS", [0.0, 0.0])

    assert adapter_obj.chat("sess", "prompt") == ("OK", "")
    assert calls["n"] == 2


@pytest.fixture
def transient_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from api_key_store import ApiKeyStore
    from runtime_config import RuntimeConfigStore

    store = ApiKeyStore(tmp_path / "keys.sqlite3")
    key = store.create_key("transient")["key"]
    runtime = RuntimeConfigStore(
        tmp_path / "runtime.json", default_model_name="deepseek-chat"
    )
    monkeypatch.setattr(server, "API_KEY_STORE", store)
    monkeypatch.setattr(server, "get_runtime_config", lambda: runtime)
    with TestClient(server.app) as client:
        yield client, key


def test_nonstream_transient_maps_to_503_with_retry_after(
    transient_client, monkeypatch
):
    client, key = transient_client

    def boom(*_args, **_kwargs):
        raise UpstreamTransientError(
            "upstream_hint_error: 服务器暂时不可用 (generation_err)"
        )

    class FakeAcquired:
        acct = SimpleNamespace(id="acct-transient")
        adapter = SimpleNamespace(chat=boom)
        parent_message_id = None

        def prepare_prompt(self, prompt):
            return prompt

        def create_session(self):
            return "sess"

        def release(self):
            return None

    monkeypatch.setattr(server, "_acquire", lambda cache_key=None: FakeAcquired())

    marked: list[str] = []
    monkeypatch.setattr(
        server.pool, "mark_error", lambda acct, msg: marked.append(str(acct.id))
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == server.TRANSIENT_RETRY_AFTER_SECS
    assert response.json()["error"]["type"] == "api_error"
    # A transient outage must not poison the account pool.
    assert marked == []
