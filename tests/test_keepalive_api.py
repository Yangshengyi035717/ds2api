"""Admin API tests for the narrow keepalive credential interface."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import account_pool as account_pool_module
import admin
from account_pool import AccountPool
from proxy_pool import ProxyPool


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJFUzI1NiJ9.{encoded}.signature"


@pytest.fixture
def keepalive_client(monkeypatch, tmp_path):
    monkeypatch.setenv("KEEPALIVE_SHARED_SECRET", "keepalive-test-secret")
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    monkeypatch.setattr(admin, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    pool = AccountPool()
    monkeypatch.setattr(admin, "get_pool", lambda: pool)
    account = pool.add(
        token=_jwt({"id": "zai-user"}),
        cookies="acw_tc=old",
        device_id="uid_old",
        provider="zai",
    )
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        yield client, pool, account


def test_keepalive_credentials_require_secret(keepalive_client):
    client, _, account = keepalive_client
    response = client.get(f"/admin/api/keepalive/accounts/{account.id}")
    assert response.status_code == 401


def test_keepalive_secret_reads_only_account_credentials(keepalive_client):
    client, _, account = keepalive_client
    response = client.get(
        f"/admin/api/keepalive/accounts/{account.id}",
        headers={"X-Keepalive-Secret": "keepalive-test-secret"},
    )
    assert response.status_code == 200
    payload = response.json()["account"]
    assert payload["token"] == account.token
    assert payload["cookies"] == "acw_tc=old"
    assert payload["provider"] == "zai"
    assert payload["user_id"] == "zai-user"


def test_keepalive_secret_updates_only_cookies_and_device(keepalive_client):
    client, pool, account = keepalive_client
    response = client.put(
        f"/admin/api/keepalive/accounts/{account.id}",
        headers={"X-Keepalive-Secret": "keepalive-test-secret"},
        json={
            "cookies": "acw_tc=new; cdn_sec_tc=waf",
            "device_id": "uid_new",
            "token": "must-not-change",
        },
    )
    assert response.status_code == 200
    updated = pool.get_by_id(account.id)
    assert updated is not None
    assert updated.cookies == "acw_tc=new; cdn_sec_tc=waf"
    assert updated.device_id == "uid_new"
    assert updated.token == account.token
    assert updated.provider == "zai"
    assert updated.user_id == "zai-user"
