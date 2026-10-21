"""Regression tests for Anthropic non-streaming error mapping."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import server
from adapter import RateLimitError, UpstreamEmptyError


class _Account:
    id = "test-account"


class _Acquired:
    acct = _Account()
    parent_message_id = None

    def __init__(self, error):
        self.adapter = self
        self.error = error
        self.released = False

    def prepare_prompt(self, prompt):
        return prompt

    def create_session(self, *args, **kwargs):
        return "session"

    def chat(self, *args, **kwargs):
        raise self.error

    def release(self):
        self.released = True


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (RateLimitError("too frequent"), 429),
        (UpstreamEmptyError("empty"), 502),
    ],
)
def test_anthropic_nonstream_maps_upstream_errors(monkeypatch, error, status_code):
    acquired = _Acquired(error)
    monkeypatch.setattr(
        server,
        "_acquire",
        lambda cache_key=None, provider="deepseek": acquired,
    )

    with pytest.raises(HTTPException) as exc_info:
        server._anthropic_nonstream(
            "msg_test", "User: hello", [], cache_key="nonstream:test"
        )

    assert exc_info.value.status_code == status_code
    assert acquired.released is True


def test_anthropic_nonstream_marks_protocol_hint_as_bad_gateway(monkeypatch):
    from adapter import UpstreamHintError

    acquired = _Acquired(UpstreamHintError("upstream hint"))
    marked = []
    monkeypatch.setattr(
        server,
        "_acquire",
        lambda cache_key=None, provider="deepseek": acquired,
    )
    monkeypatch.setattr(
        server.pool,
        "mark_error",
        lambda account, message: marked.append((account, message)),
    )

    with pytest.raises(HTTPException) as exc_info:
        server._anthropic_nonstream(
            "msg_test", "User: hello", [], cache_key="nonstream:test"
        )

    assert exc_info.value.status_code == 502
    assert marked == [(acquired.acct, "upstream hint")]
    assert acquired.released is True
