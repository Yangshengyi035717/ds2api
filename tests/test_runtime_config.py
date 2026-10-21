"""Tests for persisted WebUI runtime configuration."""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin
from runtime_config import RuntimeConfigStore


def test_model_name_defaults_to_constructor_or_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_NAME", "env-model")
    store = RuntimeConfigStore(tmp_path / "runtime.json")

    assert store.model_name == "env-model"
    assert store.snapshot() == {
        "model_name": "env-model",
        "source": "env",
        "persisted": False,
        "default_model_name": "env-model",
    }


def test_model_name_is_persisted_and_reloaded(tmp_path):
    path = tmp_path / "runtime.json"
    store = RuntimeConfigStore(path, default_model_name="deepseek-chat")

    assert store.set_model_name("  custom-model  ") == "custom-model"
    assert store.model_name == "custom-model"
    assert store.snapshot()["source"] == "webui"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "model_name": "custom-model",
    }

    reopened = RuntimeConfigStore(path, default_model_name="deepseek-chat")
    assert reopened.model_name == "custom-model"
    assert reopened.snapshot()["persisted"] is True
    assert reopened.snapshot()["source"] == "webui"


@pytest.mark.parametrize("name", ["", "   ", "x" * 129, "bad\nname"])
def test_invalid_model_names_are_rejected(tmp_path, name):
    store = RuntimeConfigStore(tmp_path / "runtime.json", default_model_name="stable")

    with pytest.raises(ValueError):
        store.set_model_name(name)
    assert store.model_name == "stable"


def test_malformed_persisted_config_falls_back_safely(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text("{not-json", encoding="utf-8")

    store = RuntimeConfigStore(path, default_model_name="fallback")

    assert store.model_name == "fallback"
    assert store.snapshot()["persisted"] is False


@pytest.fixture
def runtime_admin_client(monkeypatch, tmp_path):
    store = RuntimeConfigStore(
        tmp_path / "runtime.json",
        default_model_name="deepseek-chat",
    )
    monkeypatch.setattr(admin, "get_runtime_config", lambda: store)

    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    app = FastAPI()
    app.include_router(admin.router)
    try:
        with TestClient(app) as client:
            yield client, store, token
    finally:
        admin._tokens.discard(token)


def test_runtime_config_admin_routes_require_auth(runtime_admin_client):
    client, _, _ = runtime_admin_client

    assert client.get("/admin/api/runtime-config").status_code == 401
    assert client.put(
        "/admin/api/runtime-config",
        json={"model_name": "unauthorized"},
    ).status_code == 401


def test_runtime_config_admin_route_updates_immediately(runtime_admin_client):
    client, store, token = runtime_admin_client
    headers = {"Authorization": f"Bearer {token}"}

    current = client.get("/admin/api/runtime-config", headers=headers)
    assert current.status_code == 200
    assert current.json()["model_name"] == "deepseek-chat"

    updated = client.put(
        "/admin/api/runtime-config",
        json={"model_name": "my-custom-model"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["model_name"] == "my-custom-model"
    assert updated.json()["source"] == "webui"
    assert store.model_name == "my-custom-model"
