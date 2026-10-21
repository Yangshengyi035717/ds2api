"""Tests for the managed upstream proxy pool and account bindings."""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import account_pool as account_pool_module
import admin
from account_pool import AccountPool
from proxy_pool import ProxyPool, normalize_proxy_url, proxy_preview


def test_proxy_url_validation_and_masking():
    assert normalize_proxy_url("http://user:pass@127.0.0.1:8080") == "http://user:pass@127.0.0.1:8080"
    assert proxy_preview("socks5://user:pass@proxy.example:1080") == "socks5://***:***@proxy.example:1080"
    with pytest.raises(ValueError):
        normalize_proxy_url("ftp://proxy.example:21")
    with pytest.raises(ValueError):
        normalize_proxy_url("not-a-proxy")


def test_proxy_pool_persists_and_rejects_duplicates(tmp_path):
    path = tmp_path / "proxies.json"
    pool = ProxyPool(path)
    entry = pool.add("primary", "http://user:pass@127.0.0.1:8080")

    assert pool.resolve_url(entry.id) == "http://user:pass@127.0.0.1:8080"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1

    reopened = ProxyPool(path)
    loaded = reopened.get_by_id(entry.id)
    assert loaded is not None
    assert loaded.url == "http://user:pass@127.0.0.1:8080"

    with pytest.raises(ValueError, match="代理已存在"):
        reopened.add("duplicate", "http://user:pass@127.0.0.1:8080")


def test_proxy_pool_encrypts_url_at_rest(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DEEPSEEK_ENCRYPTION_KEY", key)
    path = tmp_path / "proxies-encrypted.json"
    raw_url = "http://user:secret-password@proxy.example:8080"

    pool = ProxyPool(path)
    entry = pool.add("encrypted", raw_url)
    file_text = path.read_text(encoding="utf-8")
    assert "secret-password" not in file_text
    assert json.loads(file_text)["version"] == 2

    reopened = ProxyPool(path)
    loaded = reopened.get_by_id(entry.id)
    assert loaded is not None
    assert loaded.url == raw_url


def test_accounts_can_bind_a_proxy_and_enforce_exclusive_assignment(monkeypatch, tmp_path):
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    proxy = proxy_pool.add("account-a", "http://127.0.0.1:18080")
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))

    pool = AccountPool()
    first = pool.add(
        token="first-token",
        cookies="first-cookie",
        email="first",
        proxy_id=proxy.id,
    )
    assert first.to_dict()["proxy_id"] == proxy.id
    assert first.to_dict()["proxy_preview"] == "http://127.0.0.1:18080"

    with pytest.raises(ValueError, match="代理已绑定其他账号"):
        pool.add(
            token="second-token",
            cookies="second-cookie",
            email="second",
            proxy_id=proxy.id,
        )

    pool.update(first.id, proxy_id="")
    second = pool.add(
        token="second-token",
        cookies="second-cookie",
        email="second",
        proxy_id=proxy.id,
    )
    assert second.to_dict()["proxy_id"] == proxy.id


def test_account_adapter_uses_bound_proxy(monkeypatch, tmp_path):
    proxy_pool = ProxyPool(tmp_path / "proxies.json")
    proxy = proxy_pool.add("adapter", "http://127.0.0.1:19090")
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    captured: dict[str, str | None] = {}

    class FakeAdapter:
        def __init__(self, token: str, cookies: str, proxy: str | None = None):
            captured.update(token=token, cookies=cookies, proxy=proxy)

    monkeypatch.setattr(account_pool_module, "DeepSeekAdapter", FakeAdapter)
    account = account_pool_module.Account(
        token="token",
        cookies="cookies",
        proxy_id=proxy.id,
    )
    assert account.adapter is not None
    assert captured["proxy"] == "http://127.0.0.1:19090"


@pytest.fixture
def proxy_admin_client(monkeypatch, tmp_path):
    proxy_pool = ProxyPool(tmp_path / "admin-proxies.json")
    monkeypatch.setattr(admin, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setattr(account_pool_module, "get_proxy_pool", lambda: proxy_pool)
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    account_pool = AccountPool()
    monkeypatch.setattr(admin, "get_pool", lambda: account_pool)

    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    app = FastAPI()
    app.include_router(admin.router)
    try:
        with TestClient(app) as client:
            yield client, proxy_pool, account_pool, token
    finally:
        admin._tokens.discard(token)


def test_proxy_admin_routes_require_auth(proxy_admin_client):
    client, _, _, _ = proxy_admin_client
    assert client.get("/admin/api/proxies").status_code == 401
    assert client.post("/admin/api/proxies", json={"url": "http://127.0.0.1:1"}).status_code == 401


def test_proxy_admin_crud_and_delete_guard(proxy_admin_client):
    client, _, account_pool, token = proxy_admin_client
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/admin/api/proxies",
        json={"label": "primary", "url": "http://127.0.0.1:20000"},
        headers=headers,
    )
    assert created.status_code == 200
    proxy = created.json()["proxy"]
    assert "127.0.0.1:20000" in proxy["url_preview"]

    account_pool.add(
        token="bound-token",
        cookies="bound-cookie",
        email="bound",
        proxy_id=proxy["id"],
    )
    blocked = client.delete(f"/admin/api/proxies/{proxy['id']}", headers=headers)
    assert blocked.status_code == 409

    account_pool.update(account_pool.get_all()[0]["id"], proxy_id="")
    removed = client.delete(f"/admin/api/proxies/{proxy['id']}", headers=headers)
    assert removed.status_code == 200
