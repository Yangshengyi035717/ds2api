"""Tests for the browser-login control client and ingest boundary."""
from __future__ import annotations

import asyncio
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import account_pool as account_pool_module
import admin
import browser_login_client
from account_pool import AccountPool
from proxy_pool import ProxyPool


def test_client_disabled_without_config(monkeypatch):
    monkeypatch.delenv("BROWSER_LOGIN_URL", raising=False)
    monkeypatch.delenv("BROWSER_LOGIN_SHARED_SECRET", raising=False)
    assert browser_login_client.enabled() is False


def test_client_is_enabled_with_config(monkeypatch):
    monkeypatch.setenv("BROWSER_LOGIN_URL", "http://browser-login:8000/")
    monkeypatch.setenv("BROWSER_LOGIN_SHARED_SECRET", "shared")
    assert browser_login_client.enabled() is True


def test_client_start_sends_shared_secret(monkeypatch):
    seen: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "starting"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, headers=None, **kwargs):
            seen.update(method=method, url=url, headers=headers, kwargs=kwargs)
            return FakeResponse()

    monkeypatch.setenv("BROWSER_LOGIN_URL", "http://browser-login:8000")
    monkeypatch.setenv("BROWSER_LOGIN_SHARED_SECRET", "shared")
    monkeypatch.setattr(browser_login_client.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(browser_login_client.start(reset_profile=False))
    assert result["status"] == "starting"
    assert seen["method"] == "POST"
    assert seen["url"] == "http://browser-login:8000/sessions/start"
    assert seen["headers"] == {"X-Browser-Login-Secret": "shared"}
    assert seen["kwargs"]["json"] == {"reset_profile": False}


@pytest.fixture
def ingest_client(monkeypatch, tmp_path):
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    monkeypatch.setattr(admin, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BROWSER_LOGIN_SHARED_SECRET", "ingest-secret")
    pool = AccountPool()
    monkeypatch.setattr(admin, "get_pool", lambda: pool)

    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        yield client, pool


def test_ingest_rejects_wrong_secret(ingest_client):
    client, _ = ingest_client
    response = client.post(
        "/admin/api/browser-login/ingest",
        headers={"X-Browser-Login-Secret": "wrong"},
        json={"token": "t" * 40, "cookies": "a=1"},
    )
    assert response.status_code == 401


def test_ingest_creates_and_updates_without_admin_session(ingest_client):
    client, pool = ingest_client
    headers = {"X-Browser-Login-Secret": "ingest-secret"}
    token = "browser-login-token-" + secrets.token_hex(16)

    first = client.post(
        "/admin/api/browser-login/ingest",
        headers=headers,
        json={"token": token, "cookies": "a=1", "device_id": "dev-1"},
    )
    assert first.status_code == 200
    assert first.json()["action"] == "created"
    assert pool.count() == 1

    second = client.post(
        "/admin/api/browser-login/ingest",
        headers=headers,
        json={"token": token, "cookies": "a=2", "device_id": "dev-1"},
    )
    assert second.status_code == 200
    assert second.json()["action"] == "updated"
    assert pool.count() == 1
    assert pool.find_by_token(token).cookies == "a=2"


def test_browser_login_status_disabled_without_config(ingest_client, monkeypatch):
    client, _ = ingest_client
    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    monkeypatch.delenv("BROWSER_LOGIN_URL", raising=False)
    try:
        response = client.get(
            "/admin/api/browser-login/status",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        admin._tokens.discard(token)
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_ingest_normalizes_localstorage_token_wrapper(ingest_client):
    client, pool = ingest_client
    token = "normalized-token-" + secrets.token_hex(16)
    response = client.post(
        "/admin/api/browser-login/ingest",
        headers={"X-Browser-Login-Secret": "ingest-secret"},
        json={
            "token": '{"value":"%s","__version":"0"}' % token,
            "cookies": "a=1",
            "device_id": "dev-1",
        },
    )
    assert response.status_code == 200
    assert pool.find_by_token(token) is not None
