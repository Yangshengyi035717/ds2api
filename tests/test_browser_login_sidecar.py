"""Tests for the browser-login sidecar API (Playwright is not required)."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _load_sidecar(monkeypatch, secret: str = "sidecar-secret"):
    monkeypatch.setenv("BROWSER_LOGIN_SHARED_SECRET", secret)
    path = Path(__file__).resolve().parent.parent / "browser-login" / "browser_login.py"
    spec = importlib.util.spec_from_file_location("ds2api_browser_login", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_health_is_public(monkeypatch):
    module = _load_sidecar(monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_control_endpoints_require_shared_secret(monkeypatch):
    module = _load_sidecar(monkeypatch)
    with TestClient(module.app) as client:
        assert client.get("/sessions/status").status_code == 401
        assert client.post("/sessions/start", json={}).status_code == 401
        assert client.post("/sessions/stop").status_code == 401
        assert client.get("/session/view-token").status_code == 401


def test_view_auth_accepts_only_the_current_token(monkeypatch):
    module = _load_sidecar(monkeypatch)
    module.state.view_token = "view-abc"
    module.state.view_expires_at = time.time() + 600
    with TestClient(module.app) as client:
        assert client.get(
            "/view-auth", headers={"X-Browser-Login-View": "view-abc"}
        ).status_code == 204
        assert client.get("/view-auth?token=view-abc").status_code == 204
        assert client.get(
            "/view-auth", headers={"X-Browser-Login-View": "wrong"}
        ).status_code == 401


def test_expired_view_token_is_rejected(monkeypatch):
    module = _load_sidecar(monkeypatch)
    module.state.view_token = "view-abc"
    module.state.view_expires_at = time.time() - 1
    with TestClient(module.app) as client:
        assert client.get(
            "/view-auth", headers={"X-Browser-Login-View": "view-abc"}
        ).status_code == 401


def test_start_reports_503_when_playwright_is_missing(monkeypatch):
    module = _load_sidecar(monkeypatch)
    monkeypatch.setattr(module, "async_playwright", None)
    with TestClient(module.app) as client:
        response = client.post(
            "/sessions/start",
            json={"reset_profile": True},
            headers={"X-Browser-Login-Secret": "sidecar-secret"},
        )
    assert response.status_code == 503


def test_view_token_endpoint_hides_token_without_secret(monkeypatch):
    module = _load_sidecar(monkeypatch)
    module.state.view_token = "view-abc"
    module.state.view_expires_at = time.time() + 600
    with TestClient(module.app) as client:
        ok = client.get(
            "/session/view-token",
            headers={"X-Browser-Login-Secret": "sidecar-secret"},
        )
    assert ok.status_code == 200
    assert ok.json()["view_token"] == "view-abc"


def test_normalize_token_unwraps_and_rejects_empty_wrapper(monkeypatch):
    module = _load_sidecar(monkeypatch)
    assert module._normalize_token('{"value":"abcdefghijklmnopqrstuvwx","__version":"0"}') == "abcdefghijklmnopqrstuvwx"
    assert module._normalize_token('{"value":"","__version":"0"}') == ""
    assert module._valid_token("abcdefghijklmnopqrstuvwx") is True
    assert module._valid_token("") is False
    assert module._valid_token('{"value":""}') is False


def test_issue_view_token_is_hmac_verifiable(monkeypatch):
    import hashlib
    import hmac

    module = _load_sidecar(monkeypatch, secret="abc123")
    token, expires = module.issue_view_token()
    exp, _, mac = token.partition(".")
    assert int(exp) == int(expires)
    assert mac == hmac.new(b"abc123", exp.encode(), hashlib.sha1).hexdigest()


def test_cookie_serializer_only_keeps_deepseek_domain(monkeypatch):
    module = _load_sidecar(monkeypatch)
    cookies = [
        {"name": "ds_session_id", "value": "s1", "domain": ".deepseek.com"},
        {"name": "telemetry", "value": "t1", "domain": ".example.com"},
        {"name": "HWWAFSESID", "value": "w1", "domain": "chat.deepseek.com"},
        {"name": "dup", "value": "first", "domain": ".deepseek.com"},
        {"name": "dup", "value": "second", "domain": ".deepseek.com"},
    ]
    assert module._serialize_cookies(cookies) == (
        "ds_session_id=s1; HWWAFSESID=w1; dup=first"
    )
