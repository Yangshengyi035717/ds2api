"""Tests for the persistent WebUI administrator password."""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin
from admin_password import AdminPasswordStore


def test_environment_password_is_the_initial_fallback(tmp_path):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")

    assert store.verify("initial-secret") is True
    assert store.verify("wrong") is False
    assert store.source == "env"
    assert store.is_weak() is False
    assert not (tmp_path / "admin_auth.json").exists()


def test_common_or_short_password_is_weak(tmp_path):
    assert AdminPasswordStore(
        tmp_path / "admin_auth.json", "admin"
    ).is_weak() is True
    assert AdminPasswordStore(
        tmp_path / "admin_auth.json", "password123"
    ).is_weak() is True


def test_change_persists_only_a_salted_verifier(tmp_path):
    path = tmp_path / "admin_auth.json"
    store = AdminPasswordStore(path, "initial-secret")

    store.change("initial-secret", "new strong password 42")

    assert store.verify("new strong password 42") is True
    assert store.verify("initial-secret") is False
    assert store.source == "webui"
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["algorithm"] == "pbkdf2_sha256"
    assert payload["iterations"] >= 100_000
    assert "new strong password 42" not in raw
    assert "initial-secret" not in raw
    assert set(payload) == {
        "version",
        "algorithm",
        "iterations",
        "salt",
        "hash",
        "updated_at",
    }

    reopened = AdminPasswordStore(path, "initial-secret")
    assert reopened.source == "webui"
    assert reopened.verify("new strong password 42") is True
    assert reopened.verify("initial-secret") is False


@pytest.mark.parametrize(
    "new_password",
    ["short", "password", "password123", "x" * 257],
)
def test_invalid_new_password_is_rejected(tmp_path, new_password):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")

    with pytest.raises(ValueError):
        store.change("initial-secret", new_password)
    assert store.verify("initial-secret") is True
    assert not (tmp_path / "admin_auth.json").exists()


def test_wrong_current_password_is_rejected(tmp_path):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")

    with pytest.raises(PermissionError):
        store.change("wrong-current", "new strong password 42")
    assert store.verify("initial-secret") is True


def test_overlong_password_is_rejected_before_hashing(tmp_path):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")

    assert store.verify("x" * 257) is False


def test_same_password_is_rejected(tmp_path):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")

    with pytest.raises(ValueError, match="相同"):
        store.change("initial-secret", "initial-secret")


@pytest.fixture
def password_admin_client(monkeypatch, tmp_path):
    store = AdminPasswordStore(tmp_path / "admin_auth.json", "initial-secret")
    monkeypatch.setattr(admin, "get_admin_password_store", lambda: store)

    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    app = FastAPI()
    app.include_router(admin.router)
    try:
        with TestClient(app) as client:
            yield client, store, token
    finally:
        admin._tokens.discard(token)
        admin._tokens.clear()


def test_password_routes_require_auth(password_admin_client):
    client, _, _ = password_admin_client

    assert client.get("/admin/api/admin-password").status_code == 401
    assert client.put(
        "/admin/api/admin-password",
        json={
            "current_password": "initial-secret",
            "new_password": "new strong password 42",
        },
    ).status_code == 401


def test_change_route_revokes_sessions_and_changes_login(password_admin_client):
    client, store, token = password_admin_client
    headers = {"Authorization": f"Bearer {token}"}

    status = client.get("/admin/api/admin-password", headers=headers)
    assert status.status_code == 200
    assert status.json()["source"] == "env"

    changed = client.put(
        "/admin/api/admin-password",
        headers=headers,
        json={
            "current_password": "initial-secret",
            "new_password": "new strong password 42",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["relogin_required"] is True
    assert changed.json()["source"] == "webui"
    assert admin._tokens == set()

    assert client.get("/admin/api/admin-password", headers=headers).status_code == 401
    assert client.post(
        "/admin/api/login",
        json={"password": "initial-secret"},
    ).status_code == 403
    login = client.post(
        "/admin/api/login",
        json={"password": "new strong password 42"},
    )
    assert login.status_code == 200
    assert login.json()["token"]
    assert store.verify("new strong password 42") is True


def test_change_route_wrong_current_password_keeps_session(password_admin_client):
    client, _, token = password_admin_client
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/admin/api/admin-password",
        headers=headers,
        json={
            "current_password": "wrong",
            "new_password": "new strong password 42",
        },
    )
    assert response.status_code == 403
    assert client.get("/admin/api/admin-password", headers=headers).status_code == 200
