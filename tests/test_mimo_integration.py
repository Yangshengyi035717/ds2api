"""MiMo provider integration; all credentials and HTTP responses are synthetic."""
from __future__ import annotations

import importlib
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import provider_registry as registry
from credential_import import CredentialImportError, parse_credential_payload


USER_ID = "synthetic-mimo-user-0123456789"
SERVICE_TOKEN = "synthetic-service+token=="
PH = "synthetic+ph=="
COOKIES = (
    f'userId="{USER_ID}"; xiaomichatbot_serviceToken="{SERVICE_TOKEN}"; '
    f'xiaomichatbot_ph="{PH}"'
)
HOST = "https://aistudio.xiaomimimo.com"
MODELS = [
    "mimo-v2.6-pro",
    "mimo-v2.6-flash",
    "mimo-v2.6-pro-ultraspeed-studio",
]


@pytest.fixture(autouse=True)
def mimo_model_defaults(monkeypatch):
    for name in ("MIMO_MODEL_NAME", "MIMO_UPSTREAM_MODEL", "MIMO_MODEL_DISPLAY_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("alias", ["xiaomi", "xiaomimimo", "mimo", " MiMo "])
def test_mimo_aliases_route_to_public_default(alias):
    assert registry.normalize_provider(alias) == registry.PROVIDER_MIMO
    assert registry.provider_for_model(alias) == registry.PROVIDER_MIMO
    assert registry.mimo_public_model(alias) == MODELS[0]
    assert registry.mimo_upstream_model(alias) == MODELS[0]
    assert registry.provider_label(alias) == "小米 MiMo"


@pytest.mark.parametrize("model", MODELS)
def test_mimo_models_keep_their_upstream_ids(model):
    assert registry.provider_for_model(model) == registry.PROVIDER_MIMO
    assert registry.mimo_public_model(model) == model
    assert registry.mimo_upstream_model(model) == model


def test_mimo_catalog_contains_observed_models():
    assert registry.PROVIDER_MIMO in registry.SUPPORTED_PROVIDERS
    assert registry.mimo_model_name() == MODELS[0]
    assert registry.mimo_upstream_model_name() == MODELS[0]
    assert registry.available_mimo_models() == MODELS


def test_mimo_model_configuration_does_not_remap_flash(monkeypatch):
    monkeypatch.setenv("MIMO_MODEL_NAME", "local-mimo-pro")
    monkeypatch.setenv("MIMO_UPSTREAM_MODEL", "mimo-v2.6-pro")
    assert registry.provider_for_model("local-mimo-pro") == registry.PROVIDER_MIMO
    assert registry.mimo_upstream_model("local-mimo-pro") == MODELS[0]
    assert registry.mimo_upstream_model(MODELS[1]) == MODELS[1]
    assert registry.available_mimo_models() == ["local-mimo-pro", MODELS[1], MODELS[2]]


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("deepseek-chat", "deepseek"), ("glm-5.3-flash", "zai"),
        ("kimi-k2.6", "kimi"), ("qwen3.7-max", "qianwen"),
        ("hunyuan-hy4-preview", "hunyuan"), ("wenxin-smart-mode", "wenxin"),
        ("doubao-seed", "doubao"),
    ],
)
def test_mimo_does_not_change_existing_provider_routes(model, provider):
    assert registry.provider_for_model(model) == provider


@pytest.fixture
def mimo():
    # The adapter is implemented independently. Never replace its cookie
    # helpers with test implementations merely to make integration tests pass.
    return pytest.importorskip("mimo_adapter")


def _entry(url, *, cookies="", query=None, response=None):
    return {
        "request": {
            "url": url,
            "headers": [{"name": "Cookie", "value": cookies}] if cookies else [],
            "queryString": query or [],
        },
        "response": response or {"headers": []},
    }


def _har(*entries):
    return {"log": {"entries": list(entries)}}


@pytest.mark.parametrize(
    "payload",
    [
        COOKIES,
        f"Cookie: {COOKIES}",
        {"provider": "xiaomi", "cookies": COOKIES},
        {"provider": "mimo", "token": "", "cookies": COOKIES},
        {"cookies": COOKIES},
        {"provider": "xiaomimimo", "cookies": {
            "userId": USER_ID,
            "xiaomichatbot_serviceToken": SERVICE_TOKEN,
            "xiaomichatbot_ph": PH,
        }},
    ],
)
def test_mimo_cookie_import_accepts_quotes_plus_equals_and_empty_token(mimo, payload):
    imported = parse_credential_payload(payload)
    assert imported.provider == "mimo"
    assert imported.token == ""
    assert imported.user_id == USER_ID
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_serviceToken") == SERVICE_TOKEN
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_ph") == PH
    summary = imported.public_summary()
    assert summary["has_token"] is False
    assert summary["has_user_id"] is True
    serialized = json.dumps(summary)
    for secret in (SERVICE_TOKEN, PH, USER_ID):
        assert secret not in serialized


def test_mimo_browser_export_identity_always_comes_from_cookie(mimo):
    imported = parse_credential_payload({
        "provider": "mimo", "cookies": COOKIES, "user_id": "wrong-export-identity",
    })
    assert imported.user_id == USER_ID


@pytest.mark.parametrize("missing", ["userId", "xiaomichatbot_serviceToken", "xiaomichatbot_ph"])
def test_mimo_import_requires_all_cookie_fields(mimo, missing):
    values = {"userId": USER_ID, "xiaomichatbot_serviceToken": SERVICE_TOKEN, "xiaomichatbot_ph": PH}
    values.pop(missing)
    with pytest.raises(CredentialImportError) as error:
        parse_credential_payload({"provider": "mimo", "cookies": values})
    assert missing in str(error.value)
    assert SERVICE_TOKEN not in str(error.value)
    assert PH not in str(error.value)


@pytest.mark.parametrize("query", [
    "ph=synthetic+ph==", "ph=synthetic%2Bph%3D%3D",
    "xiaomichatbot_ph=synthetic+ph==",
    "xiaomichatbot_ph=synthetic%2Bph%3D%3D",
])
def test_mimo_har_url_ph_preserves_plus_and_padding(mimo, query):
    imported = parse_credential_payload(_har(_entry(
        f"{HOST}/open-apis/chat/conversation/list?{query}",
        cookies=f'userId="{USER_ID}"; xiaomichatbot_serviceToken="{SERVICE_TOKEN}"',
    )))
    assert imported.token == ""
    assert imported.provider == "mimo"
    assert imported.source == "har:mimo-cookie"
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_ph") == PH


def test_mimo_har_query_array_ph_and_response_cookie_rotation(mimo):
    entry = _entry(
        f"{HOST}/open-apis/chat/conversation/list",
        query=[{"name": "xiaomichatbot_ph", "value": PH}],
        response={"headers": [{
            "name": "Set-Cookie",
            "value": f'xiaomichatbot_serviceToken="{SERVICE_TOKEN}"; Path=/; HttpOnly',
        }]},
    )
    entry["request"]["cookies"] = [{"name": "userId", "value": USER_ID}]
    imported = parse_credential_payload(_har(entry))
    assert imported.user_id == USER_ID
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_serviceToken") == SERVICE_TOKEN
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_ph") == PH


@pytest.mark.parametrize(
    "url",
    [
        "https://aistudio.xiaomimimo.com.evil.test/open-apis/chat/conversation/list",
        "https://evil.test/?next=https://aistudio.xiaomimimo.com",
        "https://aistudio.xiaomimimo.com@evil.test/open-apis/chat/conversation/list",
        "https://telemetry.xiaomimimo.com/collect",
        f"{HOST}/telemetry",
        f"{HOST}/api/analytics/collect",
        f"{HOST}/%74elemetry",
    ],
)
def test_mimo_har_never_uses_foreign_or_telemetry_cookies(mimo, url):
    with pytest.raises(CredentialImportError):
        parse_credential_payload(_har(
            _entry(f"{HOST}/open-apis/chat/conversation/list?ph=redacted"),
            _entry(url, cookies=COOKIES),
        ))


def test_mimo_har_ignores_unrelated_rotation_and_preserves_cookie_ph(mimo):
    imported = parse_credential_payload(_har(
        _entry(f"{HOST}/open-apis/chat/conversation/list?ph=stale", cookies=COOKIES),
        _entry("https://evil.test/?ph=bad", cookies="userId=wrong", response={
            "headers": [{"name": "Set-Cookie", "value": "xiaomichatbot_serviceToken=wrong"}],
        }),
        _entry(f"{HOST}/telemetry?ph=wrong", cookies="userId=wrong"),
    ))
    assert imported.user_id == USER_ID
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_serviceToken") == SERVICE_TOKEN
    assert mimo.mimo_cookie_value(imported.cookies, "xiaomichatbot_ph") == PH


def test_mimo_redacted_har_requires_supplemental_login_cookies(mimo):
    with pytest.raises(CredentialImportError, match="脱敏 HAR.*Cookie"):
        parse_credential_payload(_har(_entry(
            f"{HOST}/open-apis/chat/conversation/list?ph=redacted"
        )))


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path, mimo):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for variable, filename in {
        "ACCOUNT_STORE_PATH": "accounts.json",
        "PROXY_STORE_PATH": "proxies.json",
        "API_KEY_STORE_PATH": "api_keys.sqlite3",
        "RUNTIME_CONFIG_PATH": "runtime_config.json",
        "ADMIN_PASSWORD_STORE_PATH": "admin_auth.json",
        "KEEPALIVE_STORE_PATH": "keepalive.sqlite3",
    }.items():
        monkeypatch.setenv(variable, str(tmp_path / filename))
    for variable in ("API_KEYS", "DEEPSEEK_API_KEY", "DEEPSEEK_ENCRYPTION_KEY"):
        monkeypatch.setenv(variable, "")
    monkeypatch.setenv("KEEPALIVE_ENABLED", "false")
    monkeypatch.setenv("KEEPALIVE_STARTUP_JITTER", "0")
    monkeypatch.setenv("DISABLE_AUTO_RECOVER", "true")
    account_pool = importlib.import_module("account_pool")
    proxy_pool = importlib.import_module("proxy_pool").ProxyPool(tmp_path / "proxies.json")
    monkeypatch.setattr(account_pool, "get_proxy_pool", lambda: proxy_pool)
    return SimpleNamespace(
        module=account_pool,
        pool=account_pool.AccountPool(),
        proxies=proxy_pool,
        mimo=mimo,
        path=tmp_path,
    )


def _add_mimo(runtime, **kwargs):
    return runtime.pool.add(token="", cookies=COOKIES, provider="mimo", **kwargs)


def test_mimo_pool_roundtrips_empty_token_and_cookie_identity(isolated_runtime):
    runtime = isolated_runtime
    account = _add_mimo(runtime)
    assert account.token == ""
    assert account.user_id == USER_ID
    loaded = runtime.module.AccountPool().get_by_id(account.id)
    assert loaded.provider == "mimo"
    assert loaded.token == ""
    assert loaded.user_id == USER_ID
    assert loaded.state == "idle"
    assert runtime.pool.find_by_token("", provider="mimo") is None
    assert runtime.pool.find_by_user_id(USER_ID, provider="xiaomi") is account


def test_mimo_pool_loads_cookie_only_alias_and_quarantines_missing_fields(isolated_runtime):
    runtime = isolated_runtime
    (runtime.path / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "valid", "provider": "xiaomimimo", "cookies": COOKIES, "user_id": "stale-id"},
        {"id": "invalid", "provider": "mimo", "cookies": f"userId={USER_ID}"},
    ]}), encoding="utf-8")
    loaded = runtime.module.AccountPool()
    assert loaded.get_by_id("valid").user_id == USER_ID
    invalid = loaded.get_by_id("invalid")
    assert invalid is not None
    assert invalid.state == "error"
    assert "xiaomichatbot_serviceToken" in invalid.last_error
    assert loaded.acquire("mimo").id == "valid"
    assert loaded.acquire("mimo") is None


@pytest.mark.parametrize("operation", ["add", "update", "keepalive"])
def test_mimo_pool_validates_before_mutation(isolated_runtime, operation):
    runtime = isolated_runtime
    account = _add_mimo(runtime)
    before = (runtime.path / "accounts.json").read_bytes()
    with pytest.raises(ValueError):
        if operation == "add":
            runtime.pool.add(token="", cookies=f"userId={USER_ID}", provider="mimo")
        elif operation == "update":
            runtime.pool.update(account.id, cookies=f"userId={USER_ID}")
        else:
            runtime.pool.update_keepalive_credentials(account.id, cookies=f"userId={USER_ID}")
    assert account.cookies == COOKIES
    assert account.state == "idle"
    assert (runtime.path / "accounts.json").read_bytes() == before


def test_mimo_pool_updates_cookie_identity_and_can_clear_optional_token(isolated_runtime):
    runtime = isolated_runtime
    account = _add_mimo(runtime)
    updated = runtime.pool.update(
        account.id, token="", cookies=COOKIES.replace(USER_ID, "next-cookie-user")
    )
    assert updated.token == ""
    assert updated.user_id == "next-cookie-user"
    with pytest.raises(ValueError):
        runtime.pool.update(account.id, cookies="")


def test_mimo_pool_ignores_explicit_user_identity_on_add_and_update(isolated_runtime):
    runtime = isolated_runtime
    account = _add_mimo(runtime, user_id="wrong-add-identity")
    assert account.user_id == USER_ID
    runtime.pool.update(account.id, user_id="wrong-update-identity")
    assert account.user_id == USER_ID


def test_mimo_account_factory_passes_cookie_auth_and_proxy(isolated_runtime, monkeypatch):
    runtime = isolated_runtime
    captured = {}

    class FakeAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runtime.module, "MimoAdapter", FakeAdapter)
    account = _add_mimo(runtime, device_id="browser-device", session_id="saved-session")
    account.proxy = "http://synthetic-proxy.invalid:8080"
    assert isinstance(account.adapter, FakeAdapter)
    assert captured == {
        "token": "", "cookies": COOKIES, "user_id": USER_ID,
        "device_id": "browser-device", "session_id": "saved-session",
        "proxy": account.proxy,
    }


def test_mimo_pool_selection_and_identity_lookup_are_provider_scoped(isolated_runtime):
    runtime = isolated_runtime
    deepseek = runtime.pool.add(token="synthetic-token", cookies="", user_id=USER_ID)
    mimo_account = _add_mimo(runtime)
    assert runtime.pool.find_by_user_id(USER_ID, "deepseek") is deepseek
    assert runtime.pool.find_by_user_id(USER_ID, "mimo") is mimo_account
    assert runtime.pool.acquire_by_id(deepseek.id, provider="mimo") is None
    assert runtime.pool.acquire("mimo") is mimo_account
    assert runtime.pool.acquire("mimo") is None
    assert runtime.pool.acquire("deepseek") is deepseek


@pytest.fixture
def admin_client(isolated_runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    runtime = isolated_runtime
    admin = importlib.import_module("admin")
    monkeypatch.setattr(admin, "_pool", runtime.pool)
    monkeypatch.setattr(admin, "get_pool", lambda: runtime.pool)
    monkeypatch.setattr(admin, "get_proxy_pool", lambda: runtime.proxies)
    monkeypatch.setattr(admin, "_check_auth", lambda request: None)
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        yield client, runtime


def test_mimo_admin_import_rotates_by_user_without_echoing_cookies(admin_client):
    client, runtime = admin_client
    runtime.pool.add(token="other-provider-token", cookies="", user_id=USER_ID)
    responses = []
    for cookies in (COOKIES, COOKIES.replace(SERVICE_TOKEN, "rotated+service==")):
        responses.append(client.post("/admin/api/accounts/import", json={"payload": cookies}))
    assert [response.json()["action"] for response in responses] == ["created", "updated"]
    assert all(response.status_code == 200 for response in responses)
    assert runtime.pool.count("mimo") == 1
    assert runtime.pool.count("deepseek") == 1
    account = runtime.pool.find_by_user_id(USER_ID, "mimo")
    assert account.token == ""
    assert "rotated+service==" in account.cookies
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["import"]["has_token"] is False
        assert response.json()["account"]["provider_label"] == "小米 MiMo"
        for secret in (SERVICE_TOKEN, "rotated+service==", PH, USER_ID):
            assert secret not in response.text


def test_mimo_admin_reimport_uses_cookie_identity_despite_override(admin_client):
    client, runtime = admin_client
    original = _add_mimo(runtime)
    other_user = "other-cookie-user"
    other = runtime.pool.add(
        token="", cookies=COOKIES.replace(USER_ID, other_user), provider="mimo",
    )
    response = client.post("/admin/api/accounts/import", json={
        "user_id": other_user,
        "payload": {
            "provider": "mimo", "user_id": "wrong-export-user",
            "cookies": COOKIES.replace(SERVICE_TOKEN, "rotated+service=="),
        },
    })
    assert response.status_code == 200
    assert response.json()["action"] == "updated"
    assert response.json()["account"]["id"] == original.id
    assert original.user_id == USER_ID
    assert "rotated+service==" in original.cookies
    assert other.cookies == COOKIES.replace(USER_ID, other_user)
    assert runtime.pool.count("mimo") == 2


def test_mimo_admin_accepts_omitted_token_and_displays_model_settings(admin_client):
    client, runtime = admin_client
    response = client.post("/admin/api/accounts", json={"provider": "xiaomi", "cookies": COOKIES})
    assert response.status_code == 200
    assert runtime.pool.count("mimo") == 1
    env = client.get("/admin/api/env")
    assert env.status_code == 200
    body = env.json()
    assert body["mimo_model_name"] == MODELS[0]
    assert body["mimo_upstream_model"] == MODELS[0]
    assert body["mimo_models"] == MODELS
    overrides = {item["name"]: item["value"] for item in body["env_overrides"]}
    assert overrides["MIMO_MODEL_NAME"] == MODELS[0]
    assert overrides["MIMO_UPSTREAM_MODEL"] == MODELS[0]
    assert SERVICE_TOKEN not in env.text


@pytest.mark.parametrize("outcome", ["success", "expired", "network"])
def test_mimo_keepalive_checks_without_token_rotates_jar_and_closes(
    isolated_runtime, monkeypatch, outcome
):
    from curl_cffi.requests.cookies import Cookies
    from keepalive_scheduler import KeepaliveScheduler
    from keepalive_store import KeepaliveJobStore

    runtime = isolated_runtime
    account = _add_mimo(runtime, device_id="browser-device")
    account.proxy = "http://synthetic-proxy.invalid:8080"
    checked = []
    closed = []
    rotated = "rotated+service=="

    class FakeAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cookies = kwargs["cookies"]
            self._client = SimpleNamespace(cookies=Cookies())

        def check_health(self):
            checked.append(self.kwargs)
            if outcome == "expired":
                raise runtime.mimo.MimoAuthError(f"sensitive-detail-{SERVICE_TOKEN}")
            if outcome == "network":
                raise OSError("synthetic network failure")
            self._client.cookies.set(
                "xiaomichatbot_serviceToken", rotated, domain="aistudio.xiaomimimo.com"
            )
            self.cookies = self.cookies.replace(SERVICE_TOKEN, rotated)
            return True

        def create_session(self):
            pytest.fail("Keepalive must not create a conversation")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(runtime.mimo, "MimoAdapter", FakeAdapter)
    monkeypatch.setenv("KEEPALIVE_MIMO_CONCURRENCY", "1")
    store = KeepaliveJobStore(runtime.path / "jobs.sqlite3")
    scheduler = KeepaliveScheduler(runtime.pool, store=store)
    try:
        assert scheduler.tick_once() == 1
    finally:
        scheduler.stop()
    assert len(checked) == 1
    assert checked[0]["token"] == ""
    assert checked[0]["cookies"] == COOKIES
    assert checked[0]["proxy"] == account.proxy
    assert closed == [True]
    job = store.get_job(account.id)
    assert job["provider"] == "mimo"
    assert account.token == ""
    if outcome == "success":
        assert job["state"] == "active"
        assert job["last_status"] == 200
        loaded = runtime.module.AccountPool().get_by_id(account.id)
        assert runtime.mimo.mimo_cookie_value(loaded.cookies, "xiaomichatbot_serviceToken") == rotated
    elif outcome == "expired":
        assert job["state"] == "relogin_required"
        assert account.state == "error"
        assert SERVICE_TOKEN not in job["last_error"]
    else:
        assert job["state"] == "cooldown"
        assert account.state == "idle"
        assert account.cookies == COOKIES


def test_mimo_keepalive_interval_and_slots_are_independent(isolated_runtime, monkeypatch):
    from keepalive_scheduler import KeepaliveScheduler
    from keepalive_store import KeepaliveJobStore

    runtime = isolated_runtime
    monkeypatch.setenv("KEEPALIVE_DEEPSEEK_INTERVAL", "120")
    monkeypatch.setenv("KEEPALIVE_MIMO_INTERVAL", "900")
    monkeypatch.setenv("KEEPALIVE_MIMO_CONCURRENCY", "1")
    store = KeepaliveJobStore(runtime.path / "independent-jobs.sqlite3")
    store.sync_accounts([("mimo-job", "mimo"), ("deepseek-job", "deepseek")])
    assert store.get_job("mimo-job")["interval_secs"] == 900
    assert store.get_job("deepseek-job")["interval_secs"] == 120
    scheduler = KeepaliveScheduler(runtime.pool, store=store)
    try:
        assert scheduler._provider_slots["mimo"].acquire(blocking=False)
        assert not scheduler._provider_slots["mimo"].acquire(blocking=False)
        assert scheduler._provider_slots["deepseek"].acquire(blocking=False)
    finally:
        scheduler.stop()


@pytest.fixture
def server_client(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    runtime = isolated_runtime
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "pool", runtime.pool)
    monkeypatch.setattr(server, "_check_api_auth", lambda request: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda request: {})
    monkeypatch.setattr(server, "count_text", lambda text: len(text or ""))
    monkeypatch.setattr(server, "SESSION_CACHE", server.SessionCache())
    monkeypatch.setattr(server, "_UPSTREAM_LIMITER", threading.BoundedSemaphore(2))
    runtime.pool.add(token="synthetic-deepseek-token", cookies="")
    account = _add_mimo(runtime)
    calls = []
    state = SimpleNamespace(failure=None, rotate_at=None, instances=[], sessions=0)

    class FakeAdapter:
        def __init__(self, **kwargs):
            self.cookies = kwargs["cookies"]
            self.stream_open = False
            self.closed = False
            state.instances.append(self)
            assert kwargs["token"] == ""
            assert runtime.mimo.mimo_user_id_from_cookies(self.cookies) == USER_ID

        def create_session(self):
            if state.rotate_at == "session":
                self.cookies = self.cookies.replace(SERVICE_TOKEN, "rotated+service==")
            if state.failure == "session":
                raise runtime.mimo.MimoAuthError(f"sensitive-{SERVICE_TOKEN}")
            state.sessions += 1
            return f"mimo-session-{state.sessions}"

        def chat(self, session_id, prompt, **kwargs):
            calls.append({"session_id": session_id, "prompt": prompt, **kwargs})
            if state.rotate_at == "chat":
                self.cookies = self.cookies.replace(SERVICE_TOKEN, "rotated+service==")
            if state.failure == "chat":
                raise runtime.mimo.MimoAuthError(f"sensitive-{SERVICE_TOKEN}")
            kwargs["ready_out"].update(response_message_id=2, session_id=session_id)
            return "MiMo integration reply", "MiMo integration reasoning"

        def chat_stream(self, session_id, prompt, **kwargs):
            self.stream_open = True
            try:
                content, thinking = self.chat(session_id, prompt, **kwargs)
                yield {"__type": "thinking", "content": thinking}
                yield content
            finally:
                self.stream_open = False
                if state.rotate_at == "stream-finally":
                    self.cookies = self.cookies.replace(SERVICE_TOKEN, "rotated+service==")

        def close(self):
            assert not self.stream_open, "The upstream generator must close before its client"
            self.closed = True

    monkeypatch.setattr(runtime.module, "MimoAdapter", FakeAdapter)
    with TestClient(server.app) as client:
        yield SimpleNamespace(
            client=client, server=server, runtime=runtime,
            account=account, calls=calls, state=state,
        )


def _request_body(path, model, stream):
    body = {"model": model, "stream": stream}
    if path == "/v1/responses":
        body["input"] = [{"role": "user", "content": "first user message"}]
        body["reasoning"] = {"effort": "high"}
    else:
        body["messages"] = [{"role": "user", "content": "first user message"}]
        if path == "/v1/messages":
            body["max_tokens"] = 64
            body["thinking"] = {"type": "enabled", "budget_tokens": 32}
        else:
            body["thinking_mode"] = True
    return body


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", MODELS)
def test_mimo_three_apis_route_models_and_preserve_complete_prompt(server_client, path, stream, model):
    ctx = server_client
    body = _request_body(path, model, stream)
    headers = {"X-Conversation-Id": "synthetic-conversation"}
    first = ctx.client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    assert "MiMo integration reply" in first.text
    history = body["input"] if path == "/v1/responses" else body["messages"]
    history.extend([
        {"role": "assistant", "content": "MiMo integration reply"},
        {"role": "user", "content": "second user message"},
    ])
    second = ctx.client.post(path, json=body, headers=headers)
    assert second.status_code == 200
    assert len(ctx.calls) == 2
    assert all(call["upstream_model"] == model for call in ctx.calls)
    assert all(call["parent_message_id"] is None for call in ctx.calls)
    assert ctx.calls[0]["session_id"] != ctx.calls[1]["session_id"]
    assert "first user message" in ctx.calls[1]["prompt"]
    assert "second user message" in ctx.calls[1]["prompt"]
    assert ctx.server.SESSION_CACHE.stats()["size"] == 0
    assert ctx.account.state == "idle"
    assert ctx.runtime.pool.count("deepseek") == 1


def test_mimo_catalog_and_manifest_advertise_observed_models(server_client):
    client = server_client.client
    models = client.get("/v1/models").json()["data"]
    mimo_models = [model for model in models if model["provider"] == "mimo"]
    assert [model["id"] for model in mimo_models] == MODELS
    for model in mimo_models:
        assert model["supported_endpoints"] == ["/chat/completions", "/responses", "/messages"]
        assert client.get(f"/v1/models/{model['id']}").status_code == 200
    manifest = client.get("/v1/provider-manifest").json()
    listed = [model for model in manifest["models"] if model["provider"] == "mimo"]
    assert [model["id"] for model in listed] == MODELS
    assert all(model["provider_label"] == "小米 MiMo" for model in listed)
    assert all(model["capabilities"]["search"] is False for model in listed)
    assert all(model["capabilities"]["vision"] is False for model in listed)


def test_mimo_ignores_cached_parent_chain_from_prior_turn(server_client):
    ctx = server_client
    key = "synthetic-cache-key"
    cached = ctx.server.ChatSession(
        chat_session_id="must-not-reuse", account_id=ctx.account.id,
        provider="mimo", parent_message_id=777, sent_prompt="User: first",
    )
    ctx.server.SESSION_CACHE.put(key, cached)
    acquired = ctx.server._acquire(cache_key=key, provider="mimo")
    try:
        assert acquired.prepare_prompt("User: first\nUser: second") == "User: first\nUser: second"
        assert acquired.create_session() != "must-not-reuse"
        assert acquired.parent_message_id is None
        acquired.record_message_id(2, "new-session", full_prompt="new prompt")
        assert ctx.server.SESSION_CACHE.get(key) is cached
        assert cached.parent_message_id == 777
    finally:
        acquired.release()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["session", "chat"])
def test_mimo_auth_failures_map_to_api_errors_without_cookie_leaks(server_client, path, stream, failure):
    ctx = server_client
    ctx.state.failure = failure
    response = ctx.client.post(path, json=_request_body(path, MODELS[0], stream))
    if stream and failure == "chat":
        assert response.status_code == 200
        assert "error" in response.text
        if path == "/v1/messages":
            assert "authentication_error" in response.text
        else:
            assert "invalid_api_key" in response.text
    else:
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"
    assert ctx.server.MIMO_AUTH_MESSAGE in response.text
    assert SERVICE_TOKEN not in response.text
    assert ctx.account.state == "error"
    assert SERVICE_TOKEN not in ctx.account.last_error
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
    assert not ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_mimo_chat_persists_rotated_cookies_at_release(server_client, path, stream):
    ctx = server_client
    ctx.state.rotate_at = "stream-finally" if stream else "chat"
    response = ctx.client.post(path, json=_request_body(path, MODELS[0], stream))
    assert response.status_code == 200
    loaded = ctx.runtime.module.AccountPool().get_by_id(ctx.account.id)
    assert ctx.runtime.mimo.mimo_cookie_value(loaded.cookies, "xiaomichatbot_serviceToken") == "rotated+service=="
    assert loaded.token == ""
    assert ctx.account.state == "idle"
    assert ctx.account._adapter is None
    assert ctx.state.instances[0].closed is True
    assert ctx.account._mimo_in_use is False


def test_mimo_release_does_not_save_unchanged_cookies(server_client, monkeypatch):
    ctx = server_client
    writes = []
    monkeypatch.setattr(ctx.runtime.pool, "_save_persisted_accounts_locked", lambda: writes.append(True))
    response = ctx.client.post("/v1/chat/completions", json=_request_body("/v1/chat/completions", MODELS[0], False))
    assert response.status_code == 200
    assert writes == []
    assert ctx.account._adapter is ctx.state.instances[0]
    assert ctx.state.instances[0].closed is False


@pytest.mark.parametrize("failure", ["session", "chat"])
@pytest.mark.parametrize("stream", [False, True])
def test_mimo_auth_error_still_persists_rotation_and_releases(server_client, failure, stream):
    ctx = server_client
    ctx.state.rotate_at = "session"
    ctx.state.failure = failure
    response = ctx.client.post("/v1/chat/completions", json=_request_body("/v1/chat/completions", MODELS[0], stream))
    assert response.status_code == (200 if stream and failure == "chat" else 401)
    loaded = ctx.runtime.module.AccountPool().get_by_id(ctx.account.id)
    assert "rotated+service==" in loaded.cookies
    assert ctx.account.state == "error"
    assert ctx.account._mimo_in_use is False
    assert ctx.state.instances[0].closed is True


async def _start_api_stream(ctx, path):
    request = ctx.server.Request({
        "type": "http", "method": "POST", "path": path, "headers": [],
        "client": ("testclient", 1),
    })
    body = _request_body(path, MODELS[0], True)
    if path == "/v1/responses":
        return await ctx.server.responses(ctx.server.ResponsesRequest(**body), request)
    if path == "/v1/messages":
        return await ctx.server.messages(ctx.server.AnthropicRequest(**body), request)
    return await ctx.server.chat_completions(ctx.server.ChatCompletionRequest(**body), request)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("cancel", [False, True])
def test_mimo_stream_cancellation_flushes_jar_before_releasing(server_client, path, cancel):
    ctx = server_client
    ctx.state.rotate_at = "stream-finally"

    async def exercise():
        response = await _start_api_stream(ctx, path)
        iterator = response.body_iterator
        for _ in range(20):
            await anext(iterator)
            if ctx.state.instances[0].stream_open:
                break
        assert ctx.state.instances[0].stream_open
        assert ctx.account._mimo_in_use is True
        assert "rotated+service==" not in ctx.account.cookies
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await iterator.athrow(asyncio.CancelledError())
        else:
            await iterator.aclose()

    asyncio.run(exercise())
    assert ctx.account.state == "idle"
    assert ctx.account._mimo_in_use is False
    assert ctx.account._adapter is None
    assert ctx.state.instances[0].closed is True
    loaded = ctx.runtime.module.AccountPool().get_by_id(ctx.account.id)
    assert "rotated+service==" in loaded.cookies
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
    assert not ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)


def test_mimo_responses_initial_cancellation_releases_unstarted_chat(server_client):
    ctx = server_client
    ctx.state.rotate_at = "session"

    async def exercise():
        response = await _start_api_stream(ctx, "/v1/responses")
        await anext(response.body_iterator)
        assert ctx.calls == []
        await response.body_iterator.aclose()

    asyncio.run(exercise())
    assert ctx.account.state == "idle"
    assert ctx.account._mimo_in_use is False
    assert ctx.state.instances[0].closed is True
    assert "rotated+service==" in ctx.account.cookies


def test_mimo_concurrent_rotation_retires_client_after_stream_closes(server_client):
    ctx = server_client
    ctx.state.rotate_at = "stream-finally"
    newer_cookies = COOKIES.replace(SERVICE_TOKEN, "newer-import+token==")

    async def exercise():
        response = await _start_api_stream(ctx, "/v1/chat/completions")
        iterator = response.body_iterator
        for _ in range(10):
            await anext(iterator)
            if ctx.state.instances[0].stream_open:
                break
        ctx.runtime.pool.update_keepalive_credentials(ctx.account.id, cookies=newer_cookies)
        assert ctx.state.instances[0].closed is False
        assert ctx.account.state == "busy"
        assert ctx.runtime.pool.acquire("mimo") is None
        await iterator.aclose()

    asyncio.run(exercise())
    assert ctx.state.instances[0].closed is True
    assert ctx.account.cookies == newer_cookies
    assert ctx.account.state == "idle"
    loaded = ctx.runtime.module.AccountPool().get_by_id(ctx.account.id)
    assert loaded.cookies == newer_cookies


def test_mimo_cookie_write_failure_does_not_leak_account_or_slot(server_client, monkeypatch):
    ctx = server_client
    ctx.state.rotate_at = "chat"

    def fail_write():
        raise OSError("synthetic store error")

    monkeypatch.setattr(ctx.runtime.pool, "_save_persisted_accounts_locked", fail_write)
    response = ctx.client.post("/v1/chat/completions", json=_request_body("/v1/chat/completions", MODELS[0], False))
    assert response.status_code == 200
    assert ctx.account.state == "idle"
    assert ctx.account._mimo_in_use is False
    assert ctx.state.instances[0].closed is True
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
    assert ctx.server._UPSTREAM_LIMITER.acquire(blocking=False)
