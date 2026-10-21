"""Admin API tests for credential import and device-id persistence."""
from __future__ import annotations

import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import account_pool as account_pool_module
import admin
from account_pool import AccountPool
from proxy_pool import ProxyPool


TOKEN = "imported-token-" + "x" * 60
DEVICE_ID = "7e533d08-71c6-493d-a508-83a94c896b0e"


@pytest.fixture
def import_client(monkeypatch, tmp_path):
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    monkeypatch.setattr(admin, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    pool = AccountPool()
    monkeypatch.setattr(admin, "get_pool", lambda: pool)

    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    app = FastAPI()
    app.include_router(admin.router)
    try:
        with TestClient(app) as client:
            yield client, pool, token
    finally:
        admin._tokens.discard(token)


def test_import_requires_admin_auth(import_client):
    client, _, _ = import_client
    response = client.post("/admin/api/accounts/import", json={"payload": {}})
    assert response.status_code == 401


def test_account_list_rejects_unknown_provider(import_client):
    client, _, token = import_client
    response = client.get(
        "/admin/api/accounts?provider=unknown",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "不支持的账号类型" in response.json()["detail"]


def test_import_creates_account_without_echoing_secrets(import_client):
    client, pool, token = import_client
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={
            "payload": {
                "token": TOKEN,
                "cookies": "ds_session_id=s1; HWWAFSESID=w1",
                "device_id": DEVICE_ID,
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "created"
    assert body["account"]["has_device_id"] is True
    assert body["import"]["has_token"] is True
    assert "token" not in body["import"]
    assert TOKEN not in response.text
    assert "ds_session_id=s1" not in response.text

    stored = pool.find_by_token(TOKEN)
    assert stored is not None
    assert stored.device_id == DEVICE_ID


def test_reimport_same_token_updates_instead_of_duplicating(import_client):
    client, pool, token = import_client
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={"payload": {"token": TOKEN, "cookies": "a=1", "device_id": DEVICE_ID}},
    )
    assert first.json()["action"] == "created"

    second = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={"payload": {"token": TOKEN, "cookies": "a=2", "device_id": DEVICE_ID}},
    )
    assert second.status_code == 200
    assert second.json()["action"] == "updated"
    assert pool.count() == 1
    assert pool.find_by_token(TOKEN).cookies == "a=2"


def test_import_rejects_payload_without_token(import_client):
    client, _, token = import_client
    response = client.post(
        "/admin/api/accounts/import",
        headers={"Authorization": f"Bearer {token}"},
        json={"payload": {"cookies": "a=1"}},
    )
    assert response.status_code == 400


def test_zai_import_uses_exported_provider_and_user_id(import_client):
    client, pool, token = import_client
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={
            "payload": {
                "provider": "zai",
                "token": TOKEN,
                "user_id": "263f39cd-1fd9-4f77-81c6-fe157b22ebdf",
                "device_id": "uid_0123456789abcdef01234567",
                "cookies": {"acw_tc": "waf"},
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["provider"] == "zai"
    assert body["account"]["has_user_id"] is True
    assert body["import"]["has_user_id"] is True
    stored = pool.find_by_token(TOKEN, provider="zai")
    assert stored is not None
    assert stored.user_id == "263f39cd-1fd9-4f77-81c6-fe157b22ebdf"


def test_reimport_is_scoped_by_provider(import_client):
    client, pool, token = import_client
    headers = {"Authorization": f"Bearer {token}"}

    deepseek = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={
            "provider": "deepseek",
            "payload": {"token": TOKEN, "cookies": "ds_session_id=s1"},
        },
    )
    zai = client.post(
        "/admin/api/accounts/import",
        headers=headers,
        json={
            "payload": {
                "provider": "zai",
                "token": TOKEN,
                "user_id": "zai-user",
                "cookies": {"acw_tc": "waf"},
            }
        },
    )

    assert deepseek.json()["action"] == "created"
    assert zai.json()["action"] == "created"
    assert pool.count("deepseek") == 1
    assert pool.count("zai") == 1


def test_adapter_receives_stored_device_id(import_client, monkeypatch):
    _, pool, _ = import_client
    captured: dict[str, str] = {}

    class FakeAdapter:
        def __init__(self, token, cookies, proxy=None, device_id=""):
            captured.update(
                token=token,
                cookies=cookies,
                proxy=proxy or "",
                device_id=device_id,
            )

    monkeypatch.setattr(account_pool_module, "DeepSeekAdapter", FakeAdapter)
    account = pool.add(
        token="device-token",
        cookies="a=1",
        device_id=DEVICE_ID,
    )
    assert account.adapter is not None
    assert captured["device_id"] == DEVICE_ID
