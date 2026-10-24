"""Regression tests for the observed MiMo Studio Cookie/SSE protocol."""
from __future__ import annotations

import json
from collections import deque

import pytest
from curl_cffi.requests.cookies import Cookies
from curl_cffi.requests.exceptions import RequestException

import mimo_adapter as module
from adapter import RateLimitError, UpstreamHintError, UpstreamTransientError
from mimo_adapter import (
    MimoAdapter,
    MimoAuthError,
    mimo_cookie_value,
    mimo_user_id_from_cookies,
    mimo_validate_cookies,
)

COOKIES = (
    'userId=unit-user; xiaomichatbot_serviceToken="service+/=="; '
    'xiaomichatbot_ph="ph+/=="'
)


class Response:
    def __init__(self, *, payload=None, events=None, status=200, content_type=None):
        self.payload = payload
        self.events = events
        self.status_code = status
        self.headers = {
            "content-type": content_type or (
                "text/event-stream" if events is not None else "application/json"
            ),
        }
        self.closed = False

    def json(self):
        assert self.events is None, "A streaming body must not be consumed as JSON"
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_lines(self):
        for event, payload in self.events or []:
            yield f"event: {event}\r".encode()
            yield ("data:" + json.dumps(payload, ensure_ascii=False)).encode()
            yield b"\r"

    def close(self):
        self.closed = True


class Client:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.cookies = Cookies()
        self.responses = deque()
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.popleft()
        if callable(result):
            return result(url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(module.cffi_requests, "Session", Client)
    monkeypatch.setattr(module, "_fastchat_unavailable_until", 0.0)
    instance = MimoAdapter(cookies=COOKIES, proxy="http://unit-proxy:8080")
    yield instance
    instance.close()


def test_quoted_cookies_preserve_literal_plus_slash_and_padding():
    assert mimo_cookie_value(COOKIES, "xiaomichatbot_ph") == "ph+/=="
    assert mimo_cookie_value(COOKIES, "xiaomichatbot_serviceToken") == "service+/=="
    assert mimo_user_id_from_cookies(COOKIES) == "unit-user"
    mimo_validate_cookies(COOKIES)


@pytest.mark.parametrize("missing", module.REQUIRED_COOKIES)
def test_cookie_validation_rejects_each_missing_login_component(missing):
    incomplete = "; ".join(
        f"{name}=unit-value" for name in module.REQUIRED_COOKIES if name != missing
    )
    with pytest.raises(ValueError, match=missing):
        mimo_validate_cookies(incomplete)


def test_cookie_validation_rejects_header_injection():
    with pytest.raises(ValueError, match="控制字符"):
        mimo_validate_cookies(COOKIES + "\r\nInjected: value")


def test_health_is_read_only_cookie_authenticated_and_preserves_ph(adapter):
    response = Response(payload={"code": 0, "data": {"dataList": [], "total": 0}})
    adapter._client.responses.append(response)
    assert adapter.check_health()
    assert len(adapter._client.calls) == 1
    url, request = adapter._client.calls[0]
    assert url == module.HEALTH_URL
    assert request["json"] == {"pageInfo": {"pageNum": 1, "pageSize": 1}}
    assert request["params"] == {"xiaomichatbot_ph": "ph+/=="}
    assert request["allow_redirects"] is False
    assert "Authorization" not in request["headers"]
    assert adapter.token == ""
    assert adapter._client.options["proxies"] == {"all": "http://unit-proxy:8080"}
    assert response.closed


def test_health_rejects_unexpected_success_shape(adapter):
    adapter._client.responses.append(Response(payload={"code": 0, "data": {}}))
    with pytest.raises(UpstreamTransientError, match="conversation list"):
        adapter.check_health()


def test_rotated_cookie_is_reused_by_the_next_request(adapter):
    def rotate(_url, _request):
        adapter._client.cookies.set(
            "xiaomichatbot_ph", "rotated+/==", domain=module.COOKIE_DOMAIN, path="/"
        )
        return Response(payload={"code": 0, "data": {"dataList": []}})

    adapter._client.responses.extend([
        rotate,
        Response(payload={"code": 0, "data": {"dataList": []}}),
    ])
    assert adapter.check_health()
    assert mimo_cookie_value(adapter.cookies, "xiaomichatbot_ph") == "rotated+/=="
    assert adapter.check_health()
    assert adapter._client.calls[-1][1]["params"]["xiaomichatbot_ph"] == "rotated+/=="


def test_parent_domain_rotation_replaces_the_old_host_cookie(adapter):
    adapter._client.cookies.set(
        "xiaomichatbot_serviceToken", "new-service", domain=".xiaomimimo.com", path="/"
    )
    adapter._sync_cookies()
    tokens = [
        cookie.value for cookie in adapter._client.cookies.jar
        if cookie.name == "xiaomichatbot_serviceToken"
    ]
    assert tokens == ["new-service"]
    assert mimo_cookie_value(adapter.cookies, "xiaomichatbot_serviceToken") == "new-service"


def test_each_conversation_is_saved_with_a_fresh_id(adapter):
    def confirm(_url, request):
        return Response(payload={"code": 0, "data": request["json"]})

    adapter._client.responses.extend([confirm, confirm])
    first, second = adapter.create_session(), adapter.create_session()
    assert len(first) == len(second) == 32
    assert first != second
    assert all(call[0] == module.CREATE_URL for call in adapter._client.calls)


def test_create_requires_the_authoritative_response_id(adapter):
    adapter._client.responses.append(
        Response(payload={"code": 0, "data": {"conversationId": "another-session"}})
    )
    with pytest.raises(UpstreamTransientError, match="confirm"):
        adapter.create_session()


def test_stream_splits_reasoning_and_text_and_ignores_metadata(adapter):
    response = Response(events=[
        ("dialogId", {"content": "123456"}),
        ("message", {"messageType": "assistant", "reasoningContent": "想一想"}),
        ("message", {"messageType": "user", "content": "Do not echo this"}),
        ("message", {"messageType": "assistant", "content": "完整"}),
        ("message", {"messageType": "assistant", "content": "回复"}),
        ("usage", {"promptTokens": 8, "completionTokens": 2}),
        ("usage", {"promptTokens": 8, "completionTokens": 2}),
        ("finish", {"content": "[DONE]"}),
        ("message", {"messageType": "assistant", "content": "must be ignored"}),
    ])
    adapter._client.responses.append(response)
    assert adapter.chat("fresh-session", "hi", upstream_model="mimo-v2.6-flash") == (
        "完整回复", "想一想"
    )
    url, request = adapter._client.calls[-1]
    assert url == module.COMPLETION_URL
    assert request["stream"] is True
    assert request["json"]["conversationId"] == "fresh-session"
    assert request["json"]["modelConfig"]["model"] == "mimo-v2.6-flash"
    assert response.closed


def test_stream_preserves_both_fields_when_they_share_a_frame(adapter):
    adapter._client.responses.append(Response(events=[
        ("message", {"messageType": "assistant", "reasoningContent": "R", "content": "A"}),
        ("finish", {"content": "[DONE]"}),
    ]))
    assert adapter.chat("fresh-session", "hi") == ("A", "R")


def test_ultraspeed_uses_fastchat_and_splits_inline_think_tags(adapter):
    response = Response(events=[
        ("message", {"type": "text", "content": "<think>\x00想一想"}),
        ("message", {"type": "text", "content": "</think>\x00完整"}),
        ("message", {"type": "text", "content": "回复"}),
        ("finish", {"content": "[DONE]"}),
    ])
    adapter._client.responses.append(response)
    assert adapter.chat(
        "fresh-session",
        "hi",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("完整回复", "想一想")
    url, request = adapter._client.calls[-1]
    assert url == module.FASTCHAT_COMPLETION_URL
    assert request["json"]["modelConfig"]["model"] == "mimo-v2.6-pro-ultraspeed-studio"
    assert response.closed


def test_ultraspeed_transient_failure_falls_back_to_regular_model(adapter):
    adapter._client.responses.extend([
        Response(status=503),
        Response(events=[
            ("message", {"messageType": "assistant", "content": "fallback ok"}),
            ("finish", {"content": "[DONE]"}),
        ]),
    ])

    assert adapter.chat(
        "fresh-session",
        "hi",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("fallback ok", "")

    assert [call[0] for call in adapter._client.calls] == [
        module.FASTCHAT_COMPLETION_URL,
        module.COMPLETION_URL,
    ]
    assert adapter._client.calls[-1][1]["json"]["modelConfig"]["model"] == (
        module.ULTRASPEED_FALLBACK_MODEL
    )


def test_ultraspeed_opens_circuit_after_transient_failure(adapter):
    adapter._client.responses.extend([
        Response(status=503),
        Response(events=[
            ("message", {"messageType": "assistant", "content": "first fallback"}),
            ("finish", {"content": "[DONE]"}),
        ]),
        Response(events=[
            ("message", {"messageType": "assistant", "content": "second direct"}),
            ("finish", {"content": "[DONE]"}),
        ]),
    ])

    assert adapter.chat(
        "session-1",
        "first",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("first fallback", "")
    assert adapter.chat(
        "session-2",
        "second",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("second direct", "")

    assert [call[0] for call in adapter._client.calls] == [
        module.FASTCHAT_COMPLETION_URL,
        module.COMPLETION_URL,
        module.COMPLETION_URL,
    ]


def test_ultraspeed_falls_back_after_reasoning_only_failure(adapter):
    adapter._client.responses.extend([
        Response(events=[
            ("message", {"type": "text", "content": "<think>buffered"}),
        ]),
        Response(events=[
            ("message", {"messageType": "assistant", "content": "fallback ok"}),
            ("finish", {"content": "[DONE]"}),
        ]),
    ])
    assert adapter.chat(
        "fresh-session",
        "hi",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("fallback ok", "")
    assert len(adapter._client.calls) == 2


def test_ultraspeed_does_not_fallback_after_answer_output(adapter):
    adapter._client.responses.append(Response(events=[
        ("message", {"type": "text", "content": "partial answer"}),
    ]))
    stream = adapter.chat_stream(
        "fresh-session",
        "hi",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    )
    assert next(stream) == "partial answer"
    with pytest.raises(UpstreamTransientError, match="before its finish"):
        next(stream)
    assert len(adapter._client.calls) == 1


def test_ultraspeed_think_tags_can_span_sse_chunks(adapter):
    adapter._client.responses.append(Response(events=[
        ("message", {"type": "text", "content": "<thi"}),
        ("message", {"type": "text", "content": "nk>\x00推理"}),
        ("message", {"type": "text", "content": "</thi"}),
        ("message", {"type": "text", "content": "nk>\x00正文"}),
        ("finish", {"content": "[DONE]"}),
    ]))
    assert adapter.chat(
        "fresh-session",
        "hi",
        upstream_model="mimo-v2.6-pro-ultraspeed-studio",
    ) == ("正文", "推理")


@pytest.mark.parametrize("events", [
    [("finish", {"content": "[DONE]"})],
    [("dialogId", {"content": "1234"}), ("finish", {"content": "[DONE]"})],
])
def test_metadata_only_stream_is_not_a_success(adapter, events):
    response = Response(events=events)
    adapter._client.responses.append(response)
    with pytest.raises(UpstreamTransientError, match="empty stream"):
        list(adapter.chat_stream("fresh-session", "hi"))
    assert response.closed


def test_truncated_stream_with_partial_text_is_not_reported_as_stop(adapter):
    response = Response(events=[("message", {"content": "partial"})])
    adapter._client.responses.append(response)
    stream = adapter.chat_stream("fresh-session", "hi")
    assert next(stream) == "partial"
    with pytest.raises(UpstreamTransientError, match="before its finish"):
        next(stream)
    assert response.closed


@pytest.mark.parametrize("response,error_type", [
    (Response(status=401), MimoAuthError),
    (Response(status=302), MimoAuthError),
    (Response(status=429), RateLimitError),
    (Response(status=503), UpstreamTransientError),
    (Response(payload={"code": 401, "loginUrl": "https://example.invalid/login"}), MimoAuthError),
    (Response(payload={"code": 429, "msg": "quota exceeded"}), RateLimitError),
    (Response(payload={"code": 123, "msg": "account has no model permission"}), UpstreamHintError),
    (Response(payload=ValueError("invalid")), UpstreamTransientError),
])
def test_http_and_json_errors_map_to_existing_error_types(adapter, response, error_type):
    adapter._client.responses.append(response)
    with pytest.raises(error_type):
        adapter.check_health()
    assert response.closed


def test_chat_json_error_is_not_misreported_as_empty_stream(adapter):
    response = Response(payload={"code": 401, "loginUrl": "https://example.invalid/login"})
    adapter._client.responses.append(response)
    with pytest.raises(MimoAuthError):
        list(adapter.chat_stream("fresh-session", "hi"))
    assert response.closed


def test_stream_quota_error_is_not_normal_completion(adapter):
    response = Response(events=[("error", {"content": "请求过于频繁，请稍后重试"})])
    adapter._client.responses.append(response)
    with pytest.raises(RateLimitError):
        list(adapter.chat_stream("fresh-session", "hi"))
    assert response.closed


def test_credential_values_are_redacted_from_business_errors(adapter):
    adapter._client.responses.append(
        Response(payload={"code": 123, "msg": "Rejected ph+/== and service+/=="})
    )
    with pytest.raises(UpstreamHintError) as captured:
        adapter.check_health()
    assert "ph+/==" not in str(captured.value)
    assert "service+/==" not in str(captured.value)


def test_network_exception_does_not_expose_credential_query(adapter):
    adapter._client.responses.append(RequestException("url?xiaomichatbot_ph=ph+/=="))
    with pytest.raises(UpstreamTransientError) as captured:
        adapter.check_health()
    assert "ph+/==" not in str(captured.value)
