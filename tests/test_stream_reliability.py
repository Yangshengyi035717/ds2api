"""Offline regression matrix: terminal signals, idle streams and cancellation."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException

import adapter as deepseek
import server
from adapter import DeepSeekAdapter, UpstreamTransientError
from doubao_adapter import DoubaoAdapter
from hunyuan_adapter import HunyuanAdapter
from kimi_adapter import KimiAdapter, _connect_frame
from mimo_adapter import MimoAdapter
from qwen_adapter import QwenAdapter
from responses_format import ResponsesStreamTranslator
from streaming import HeartbeatStream
from streaming import run_owned_setup
from wenxin_adapter import WenxinAdapter
from zai_adapter import ZaiAdapter


def sse(value, event=None):
    return ((f"event: {event}\n" if event else "") + "data: " +
            (value if isinstance(value, str) else json.dumps(value)) + "\n\n")


class Response:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, text="", *, binary=None, interrupted=False):
        self.text = text
        self.binary = binary
        self.interrupted = interrupted
        self.closed = False

    def iter_lines(self):
        yield from self.text.encode().splitlines()
        if self.interrupted:
            raise RequestException("synthetic transport failure")

    def iter_content(self):
        yield self.binary
        if self.interrupted:
            raise RequestException("synthetic transport failure")

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True


PROVIDERS = ["deepseek", "zai", "kimi", "qianwen", "hunyuan", "wenxin", "doubao", "mimo"]


def provider_case(provider, terminal=True, interrupted=False):
    classes = dict(zip(PROVIDERS, [DeepSeekAdapter, ZaiAdapter, KimiAdapter, QwenAdapter,
                                  HunyuanAdapter, WenxinAdapter, DoubaoAdapter, MimoAdapter]))
    instance = object.__new__(classes[provider])
    response = Response(interrupted=interrupted)
    if provider == "deepseek":
        response.text = sse({"v": "part"}) + (sse({"p": "response/status", "v": "FINISHED"}) if terminal else "")
        instance._pow_headers = lambda *a: {}
        instance._msg_counters = {}
        instance._client = SimpleNamespace(post=lambda *a, **kw: response)
        instance._send_completion = lambda *a, **kw: response
    elif provider == "zai":
        response.text = sse({"type": "chat:completion", "data": {"phase": "answer", "delta_content": "part"}})
        if terminal:
            response.text += sse({"type": "chat:completion", "data": {"phase": "done", "done": True}})
        instance._open_stream = lambda *a, **kw: response
    elif provider == "kimi":
        response.binary = _connect_frame({"op": "append", "mask": "block.text.content", "block": {"text": {"content": "part"}}})
        if terminal:
            response.binary += _connect_frame({"done": {}})
        instance._open_chat = lambda *a, **kw: response
    elif provider == "qianwen":
        response.text = sse({"success": True, "data": {"messages": [{"mime_type": "text", "content": "part"}], "status": "complete" if terminal else "processing"}})
        instance._open = lambda *a, **kw: response
    elif provider == "hunyuan":
        response.text = sse({"type": "text", "msg": "part"}) + (sse("[DONE]") if terminal else "")
        instance._open = lambda *a, **kw: response
    elif provider == "wenxin":
        response.text = sse({"status": 0, "data": {"message": {
            "metaData": {"state": "generate-complete" if terminal else "generating-resp"},
            "content": {"generator": {"component": "markdown-yiyan", "data": {"value": "part"}, "isFinished": terminal}},
        }}}, "message")
        instance._open = lambda *a, **kw: response
    elif provider == "doubao":
        response.text = sse({"text": "part"}, "CHUNK_DELTA") + (sse({"end_type": 1}, "SSE_REPLY_END") if terminal else "")
        instance._open = lambda *a, **kw: response
    else:
        response.text = sse({"content": "part"}, "message") + (sse({"content": "[DONE]"}, "finish") if terminal else "")
        instance._request = lambda *a, **kw: response
        instance._sync_cookies = lambda: None
    return instance, response


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("failure", [None, "eof", "transport"])
def test_provider_terminal_matrix(monkeypatch, provider, stream, failure):
    monkeypatch.setattr(deepseek, "RATE_LIMIT_RETRY_DELAYS", [])
    instance, response = provider_case(provider, terminal=failure is None, interrupted=failure == "transport")
    # Nonstream DeepSeek buffers SSE in .text, so simulate failure at its read boundary.
    if provider == "deepseek" and not stream and failure == "transport":
        def fail(*args, **kwargs):
            raise RequestException("DeepSeek response transport failed")
        instance._send_completion = fail
    if failure:
        with pytest.raises(UpstreamTransientError):
            if stream:
                list(instance.chat_stream("session", "prompt"))
            else:
                instance.chat("session", "prompt")
    elif stream:
        result = list(instance.chat_stream("session", "prompt"))
        assert "part" in result
    else:
        assert instance.chat("session", "prompt") == ("part", "")
    if provider != "deepseek" or stream:
        assert response.closed


@pytest.mark.parametrize("error", [UpstreamTransientError("interrupted"), deepseek.RateLimitError("limited")])
def test_deepseek_never_replays_partial_output(monkeypatch, error):
    instance = object.__new__(DeepSeekAdapter)
    calls = []
    def once(*args, **kwargs):
        calls.append(1)
        yield "part"
        raise error
    instance._chat_stream_once = once
    instance.create_session = lambda: pytest.fail("must not retry visible content")
    monkeypatch.setattr(deepseek, "RATE_LIMIT_RETRY_DELAYS", [0, 0])
    stream = instance.chat_stream("session", "prompt")
    assert next(stream) == "part"
    with pytest.raises(type(error)):
        next(stream)
    assert calls == [1]


def test_wenxin_thinking_component_finish_is_not_answer_finish():
    instance, response = provider_case("wenxin", terminal=False)
    event = json.loads(response.text.split("data: ")[1])
    generator = event["data"]["message"]["content"]["generator"]
    generator.update(component="deep_think", isFinished=True)
    response.text = sse(event, "message")
    with pytest.raises(UpstreamTransientError):
        list(instance.chat_stream("session", "prompt"))


def payloads(events):
    return [json.loads(line[6:]) for event in events for line in event.splitlines() if line.startswith("data: ")]


def test_reasoning_is_immediate_and_indices_and_sequences_are_stable():
    translator = ResponsesStreamTranslator("resp_test", "test", {})
    initial = translator.initial_events()
    first = translator.feed_chat_chunk({"choices": [{"delta": {"reasoning_content": "think"}}]})
    assert any(event["type"] == "response.reasoning_summary_text.delta" for event in payloads(first))
    second = translator.feed_chat_chunk({"choices": [{"delta": {"reasoning_content": " more", "content": "answer", "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "test", "arguments": "{}"}}]}}]})
    events = payloads(initial + first + second + translator.final_events())
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    output = events[-1]["response"]["output"]
    assert [item["type"] for item in output] == ["reasoning", "message", "function_call"]
    assert output[0]["summary"][0]["text"] == "think more"
    reasoning_events = [event for event in events if event["type"].startswith("response.reasoning_summary")]
    assert all(event["item_id"] == output[0]["id"] and event["output_index"] == 0 for event in reasoning_events)
    assert translator.final_events() == []


def test_translator_cannot_complete_after_error():
    translator = ResponsesStreamTranslator("resp_test", "test", {})
    translator.initial_events()
    translator.error_event("failure")
    assert translator.final_events() == []
    assert translator.feed_chat_chunk({"choices": [{"delta": {"content": "late"}}]}) == []


def test_heartbeat_preserves_pending_read_and_cleanup_order():
    entered, unblock = threading.Event(), threading.Event()
    state = []
    def source():
        try:
            entered.set()
            assert unblock.wait(3)
            yield "answer"
        finally:
            state.append("closed")
    async def exercise():
        stream = HeartbeatStream(source(), interval=0.005, on_close=lambda: state.append("released"))
        try:
            assert await anext(stream) == ": keep-alive\n\n"
            pending = stream.pending
            assert entered.is_set()
            assert await anext(stream) == ": keep-alive\n\n"
            assert stream.pending is pending
            await stream.aclose()
            assert state == []  # Never release an account whose curl read is still running.
        finally:
            unblock.set()
            await stream.cleanup
        assert state == ["closed", "released"]
    asyncio.run(exercise())


def test_heartbeat_normal_and_error_endings_do_not_leak():
    async def exercise(fail):
        state = []
        def source():
            try:
                yield "answer"
                if fail:
                    raise UpstreamTransientError("interrupted")
            finally:
                state.append("closed")
        stream = HeartbeatStream(source(), on_close=lambda: state.append("released"))
        assert await anext(stream) == "answer"
        with pytest.raises(UpstreamTransientError if fail else StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        assert state == ["closed", "released"]
    asyncio.run(exercise(False))
    asyncio.run(exercise(True))


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("raw_transport", [False, True])
def test_server_failure_releases_account_without_poisoning_pool(monkeypatch, provider, protocol, raw_transport):
    state = {"released": False, "closed": False}
    def tokens(*args, **kwargs):
        try:
            yield "partial"
            if raw_transport:
                raise RequestException("transport details must not escape")
            raise UpstreamTransientError("synthetic interrupted stream")
        finally:
            state["closed"] = True
    acquired = SimpleNamespace(
        acct=SimpleNamespace(id="unit-account"), adapter=SimpleNamespace(chat_stream=tokens),
        parent_message_id=None, prepare_prompt=lambda p: p, create_session=lambda *a, **k: "session",
        release=lambda: state.update(released=True), record_message_id=lambda *a, **k: None,
    )
    monkeypatch.setattr(server, "_acquire", lambda **kw: acquired)
    monkeypatch.setattr(server.pool, "mark_error", lambda *a: pytest.fail("transient errors must not poison accounts"))
    monkeypatch.setattr(server, "_check_api_auth", lambda req: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda req: {})
    monkeypatch.setattr(server, "provider_for_model", lambda model: provider)
    monkeypatch.setattr(server, "_public_model_for_request", lambda *a: "unit-model")
    monkeypatch.setattr(server, "_upstream_model_for_request", lambda *a: "unit-model")
    async def exercise():
        if protocol == "chat":
            response = await server._handle_stream("test", "prompt", provider=provider)
        elif protocol == "anthropic":
            response = await server._anthropic_stream("test", "prompt", [], provider=provider)
        else:
            request = server.Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "client": ("unit", 1)})
            response = await server.responses(server.ResponsesRequest(input="prompt", stream=True), request)
        body = "".join([chunk async for chunk in response.body_iterator])
        assert "error" in body
        assert "transport details" not in body
        assert "response.completed" not in body
        assert '"finish_reason": "stop"' not in body
        assert 'event: message_stop' not in body
        if protocol == "responses":
            assert "event: response.failed" in body
        assert response.headers["X-Accel-Buffering"] == "no"
    asyncio.run(exercise())
    assert state == {"released": True, "closed": True}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("anthropic", [False, True])
def test_nonstream_transport_errors_are_503_not_bad_credentials(monkeypatch, provider, anthropic):
    released = []
    def fail(*args, **kwargs):
        raise RequestException("private transport details")
    acquired = SimpleNamespace(
        acct=SimpleNamespace(id="unit-account"), adapter=SimpleNamespace(chat=fail), parent_message_id=None,
        prepare_prompt=lambda p: p, create_session=lambda *a, **kw: "session",
        release=lambda: released.append(True),
    )
    monkeypatch.setattr(server, "_acquire", lambda **kw: acquired)
    monkeypatch.setattr(server.pool, "mark_error", lambda *a: pytest.fail("network failure is not bad credentials"))
    handler = server._anthropic_nonstream if anthropic else server._handle_nonstream
    with pytest.raises(server.HTTPException) as error:
        handler("test", "prompt", [], provider=provider)
    assert error.value.status_code == 503
    assert "private transport" not in error.value.detail
    assert error.value.headers["Retry-After"]
    assert released == [True]


def test_setup_cancellation_reclaims_late_acquisition():
    entered, unblock, released = threading.Event(), threading.Event(), threading.Event()
    def setup():
        entered.set()
        assert unblock.wait(3)
        return "account"
    async def exercise():
        task = asyncio.create_task(run_owned_setup(setup, lambda value: released.set()))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not released.is_set()
        finally:
            unblock.set()
        assert await asyncio.to_thread(released.wait, 2)
    asyncio.run(exercise())


def test_cancelling_an_active_read_does_not_release_it_early():
    entered, unblock = threading.Event(), threading.Event()
    state = []
    def source():
        try:
            entered.set()
            assert unblock.wait(3)
            yield "late"
        finally:
            state.append("closed")
    async def exercise():
        stream = HeartbeatStream(source(), interval=10, on_close=lambda: state.append("released"))
        task = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state == []
        finally:
            unblock.set()
            await stream.cleanup
        assert state == ["closed", "released"]
    asyncio.run(exercise())


def test_slow_stream_does_not_block_another_stream():
    unblock = threading.Event()
    def slow():
        assert unblock.wait(3)
        yield "slow"
    async def exercise():
        a = HeartbeatStream(slow(), interval=0.01)
        b = HeartbeatStream(iter(["fast"]))
        try:
            assert await anext(a) == ": keep-alive\n\n"
            assert await asyncio.wait_for(anext(b), 0.5) == "fast"
            assert await anext(a) == ": keep-alive\n\n"
        finally:
            unblock.set()
            await a.aclose()
            await a.cleanup
            await b.aclose()
    asyncio.run(exercise())


def test_kimi_persist_failure_still_releases_account_and_slot(monkeypatch):
    account = SimpleNamespace(id="unit-account", provider="kimi", adapter=object())
    acquired = server.AcquiredAccount(account)
    limiter = threading.BoundedSemaphore(1)
    limiter.acquire()
    released = []
    monkeypatch.setattr(server, "_UPSTREAM_LIMITER", limiter)
    monkeypatch.setattr(server.pool, "persist_adapter_tokens", lambda acct: (_ for _ in ()).throw(OSError("synthetic")))
    monkeypatch.setattr(server.pool, "release", lambda acct: released.append(acct.id))
    acquired.release()
    acquired.release()
    assert released == [account.id]
    assert limiter.acquire(blocking=False)
    assert not limiter.acquire(blocking=False)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_asgi_send_failure_always_reclaims_stream(fail_at):
    from starlette.background import BackgroundTask
    from starlette.requests import ClientDisconnect
    from streaming import ReliableStreamingResponse
    state = []
    def source():
        try:
            yield "part"
        finally:
            state.append("closed")
    async def exercise():
        stream = HeartbeatStream(source(), on_close=lambda: state.append("released"))
        response = ReliableStreamingResponse(stream, background=BackgroundTask(stream.aclose))
        sends = 0
        async def send(message):
            nonlocal sends
            sends += 1
            if sends == fail_at:
                raise OSError("client disconnected")
        async def receive():
            return {"type": "http.disconnect"}
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert state == (["closed", "released"] if fail_at == 2 else ["released"])
    asyncio.run(exercise())


@pytest.mark.parametrize("value, expected", [(None, 300), ("bad", 300), ("0", 30), ("600", 600), ("99999", 1800)])
def test_stream_timeout_is_bounded(monkeypatch, value, expected):
    from upstream_http import stream_timeout
    if value is None:
        monkeypatch.delenv("UPSTREAM_STREAM_IDLE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("UPSTREAM_STREAM_IDLE_TIMEOUT", value)
    assert stream_timeout()[0] == 15
    assert sum(stream_timeout()) == expected


@pytest.mark.parametrize("terminal", [
    ({"p": "response/status", "v": "FINISHED"}, None),
    ({"p": "response", "o": "BATCH", "v": [{"p": "status", "v": "FINISHED"}]}, None),
    ({"v": {"response": {"status": "FINISHED"}}}, None),
    ({"click_behavior": "none", "auto_resume": False}, "close"),
])
@pytest.mark.parametrize("stream", [True, False])
def test_deepseek_accepts_explicit_terminal_variants(monkeypatch, terminal, stream):
    monkeypatch.setattr(deepseek, "RATE_LIMIT_RETRY_DELAYS", [])
    instance, response = provider_case("deepseek", terminal=False)
    response.text += sse(*terminal)
    if stream:
        assert "part" in list(instance.chat_stream("session", "prompt"))
    else:
        assert instance.chat("session", "prompt") == ("part", "")


def test_deepseek_auto_resume_close_is_not_success(monkeypatch):
    monkeypatch.setattr(deepseek, "RATE_LIMIT_RETRY_DELAYS", [])
    instance, response = provider_case("deepseek", terminal=False)
    response.text += sse({"click_behavior": "resume", "auto_resume": True}, "close")
    with pytest.raises(UpstreamTransientError):
        list(instance.chat_stream("session", "prompt"))


def test_zai_accepts_phase_done_without_duplicate_text():
    instance, response = provider_case("zai", terminal=False)
    response.text += sse({"type": "chat:completion", "data": {"phase": "done"}})
    assert list(instance.chat_stream("session", "prompt")) == ["part"]
