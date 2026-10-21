"""Tests for the protocol-based DeepSeek session keepalive worker."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from keepalive import keepalive


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", "abc123"),
        ("  abc123  ", "abc123"),
        ("Bearer abc123", "abc123"),
        ('{"value":"abc123","__version":"1"}', "abc123"),
        ('{"token":"abc123"}', "abc123"),
        ("", ""),
    ],
)
def test_normalize_token(raw, expected):
    assert keepalive._normalize_token(raw) == expected


def test_normalize_token_keeps_malformed_json():
    # A frontend change must not silently blank the token.
    assert keepalive._normalize_token("{not-json") == "{not-json"


class _FakeJar:
    def __init__(self, items):
        self._items = [SimpleNamespace(**i) for i in items]

    def __iter__(self):
        return iter(self._items)


def test_cookie_header_serialises_and_dedupes():
    jar = _FakeJar([
        {"name": "ds_session_id", "value": "s1"},
        {"name": "HWWAFSESID", "value": "w1"},
        {"name": "ds_session_id", "value": "dup"},
        {"name": "", "value": "orphan"},
        {"name": "novalue", "value": None},
    ])
    assert keepalive._cookie_header(jar) == "ds_session_id=s1; HWWAFSESID=w1"


def test_seed_jar_skips_malformed_pairs():
    class Sess:
        def __init__(self):
            self.set_pairs = []
            self.cookies = SimpleNamespace(set=self._set)

        def _set(self, name, value, domain=None):
            self.set_pairs.append((name, value))

    sess = Sess()
    keepalive._seed_jar(sess, "a=1; broken; b=2; =novalue")
    assert sess.set_pairs == [("a", "1"), ("b", "2")]


def _response(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body)


def test_probe_accepts_valid_token(monkeypatch):
    class Sess:
        def get(self, *_a, **_k):
            return _response(200, {"code": 0, "msg": ""})

    result = keepalive.probe(Sess(), "tok")
    assert result.valid is True
    assert result.code == 0


def test_probe_flags_invalid_token(monkeypatch):
    class Sess:
        def get(self, *_a, **_k):
            return _response(200, {"code": 40003, "msg": "Authorization Failed (invalid token)"})

    result = keepalive.probe(Sess(), "tok")
    assert result.valid is False
    assert result.code in keepalive.INVALID_TOKEN_CODES


@pytest.mark.parametrize("status, body", [(202, None), (502, None), (200, {"code": 1})])
def test_probe_treats_other_answers_as_transient(status, body):
    class Sess:
        def get(self, *_a, **_k):
            return _response(status, body)

    result = keepalive.probe(Sess(), "tok")
    assert result.valid is False
    # Not a credential verdict: the worker must retry, not kill the account.
    assert result.code not in keepalive.INVALID_TOKEN_CODES


def test_probe_falls_back_to_next_endpoint():
    """A 202 challenge on one path must not condemn the session."""
    calls = []

    class Sess:
        def get(self, url, **_k):
            calls.append(url)
            if len(calls) == 1:
                return _response(202, None)
            return _response(200, {"code": 0, "msg": ""})

    result = keepalive.probe(Sess(), "tok")
    assert result.valid is True
    assert len(calls) == 2
    assert calls[1].endswith("scope=model")


def test_probe_stops_immediately_on_dead_token():
    """40003 is decisive: no further probes are needed."""
    calls = []

    class Sess:
        def get(self, url, **_k):
            calls.append(url)
            return _response(200, {"code": 40003, "msg": "invalid token"})

    result = keepalive.probe(Sess(), "tok")
    assert result.code == 40003
    assert len(calls) == 1


def test_headers_carry_browser_fingerprint(monkeypatch):
    monkeypatch.setattr(keepalive, "_device_id", lambda: "dev-123")
    headers = keepalive._headers("tok")
    assert headers["X-Client-Version"] == keepalive.CLIENT_VERSION
    assert headers["x-device-id"] == "dev-123"
    assert headers["Sec-Fetch-Site"] == "same-origin"
    assert headers["Origin"] == keepalive.BASE_URL
    assert headers["Authorization"] == "Bearer tok"


def test_device_id_is_persisted(monkeypatch, tmp_path):
    path = tmp_path / "device_id"
    monkeypatch.setattr(keepalive, "DEVICE_ID_PATH", str(path))
    first = keepalive._device_id()
    second = keepalive._device_id()
    assert first and first == second
    assert path.read_text(encoding="utf-8").strip() == first


def test_probe_survives_network_error():
    class Sess:
        def get(self, *_a, **_k):
            raise OSError("connection reset")

    result = keepalive.probe(Sess(), "tok")
    assert result.valid is False
    assert result.code is None
    assert "network" in result.message


@pytest.mark.parametrize("status, valid", [(200, True), (401, False), (503, False)])
def test_zai_probe_classifies_http_status(status, valid):
    class Sess:
        def get(self, url, headers, timeout):
            assert url.endswith("channel=zai-web")
            assert headers["Authorization"] == "Bearer tok"
            assert "token=tok" in headers["Cookie"]
            return _response(status, {"detail": "probe"})

    result = keepalive.zai_probe(Sess(), "tok", "acw_tc=waf; token=stale")
    assert result.valid is valid
    if status == 200:
        assert result.code == 0
    elif status in keepalive.INVALID_HTTP_STATUSES:
        assert result.code == status


def test_zai_cycle_pushes_rotated_cookies(monkeypatch):
    pushed = []
    captured = {}
    monkeypatch.setattr(
        keepalive,
        "_read_full_credentials",
        lambda *a, **k: {
            "provider": "zai",
            "token": "tok",
            "cookies": "acw_tc=old; token=stale",
            "device_id": "uid_existing",
            "user_id": "user-1",
            "proxy_url": "http://127.0.0.1:7890",
        },
    )
    monkeypatch.setattr(
        keepalive,
        "zai_probe",
        lambda *a, **k: keepalive.ProbeResult(200, True, 0, ""),
    )
    monkeypatch.setattr(
        keepalive,
        "push_account_updates",
        lambda _u, _t, _i, **updates: pushed.append(updates),
    )

    class FakeSession:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.cookies = SimpleNamespace(
                jar=_FakeJar([
                    {"name": "acw_tc", "value": "new"},
                    {"name": "cdn_sec_tc", "value": "waf"},
                ])
            )

    monkeypatch.setattr(keepalive.cffi_requests, "Session", FakeSession)

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome150") == 0
    assert captured["proxies"] == {"all": "http://127.0.0.1:7890"}
    assert pushed == [{
        "cookies": "acw_tc=new; token=stale; cdn_sec_tc=waf",
    }]


def test_zai_cycle_stops_on_unauthorized(monkeypatch):
    monkeypatch.setattr(
        keepalive,
        "_read_full_credentials",
        lambda *a, **k: {
            "provider": "zai",
            "token": "tok",
            "cookies": "acw_tc=old",
            "device_id": "uid_existing",
            "user_id": "user-1",
            "proxy_url": "",
        },
    )
    monkeypatch.setattr(
        keepalive,
        "zai_probe",
        lambda *a, **k: keepalive.ProbeResult(401, False, 401, "invalid"),
    )

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome150") == 2


def _patch_cycle(monkeypatch, *, creds, probe_result, jar_items=None):
    pushed: list[str] = []
    monkeypatch.setattr(
        keepalive, "_read_full_credentials", lambda *a, **k: dict(creds)
    )
    monkeypatch.setattr(keepalive, "probe", lambda *a, **k: probe_result)
    monkeypatch.setattr(
        keepalive,
        "push_cookies",
        lambda _u, _t, _i, cookies: pushed.append(cookies),
    )

    class FakeSession:
        def __init__(self, *_a, **_k):
            self.cookies = SimpleNamespace(jar=_FakeJar(jar_items or []))

    monkeypatch.setattr(keepalive.cffi_requests, "Session", FakeSession)
    return pushed


def test_cycle_pushes_rotated_cookies(monkeypatch):
    pushed = _patch_cycle(
        monkeypatch,
        creds={"token": "tok", "cookies": "ds_session_id=old"},
        probe_result=keepalive.ProbeResult(200, True, 0, ""),
        jar_items=[
            {"name": "ds_session_id", "value": "new"},
            {"name": "HWWAFSESID", "value": "w2"},
        ],
    )

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome131") == 0
    assert pushed == ["ds_session_id=new; HWWAFSESID=w2"]


def test_cycle_skips_push_when_cookies_unchanged(monkeypatch):
    pushed = _patch_cycle(
        monkeypatch,
        creds={"token": "tok", "cookies": "ds_session_id=same"},
        probe_result=keepalive.ProbeResult(200, True, 0, ""),
        jar_items=[{"name": "ds_session_id", "value": "same"}],
    )

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome131") == 0
    assert pushed == []


def test_cycle_stops_on_dead_login(monkeypatch):
    pushed = _patch_cycle(
        monkeypatch,
        creds={"token": "tok", "cookies": "ds_session_id=s"},
        probe_result=keepalive.ProbeResult(200, False, 40003, "invalid token"),
    )

    # 2 = stop: a human must sign in again.
    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome131") == 2
    assert pushed == []


def test_cycle_returns_transient_without_touching_account(monkeypatch):
    pushed = _patch_cycle(
        monkeypatch,
        creds={"token": "tok", "cookies": "ds_session_id=s"},
        probe_result=keepalive.ProbeResult(202, False, None, "challenge"),
    )

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome131") == 3
    assert pushed == []


def test_cycle_rejects_missing_token(monkeypatch):
    pushed = _patch_cycle(
        monkeypatch,
        creds={"token": "", "cookies": "ds_session_id=s"},
        probe_result=keepalive.ProbeResult(200, True, 0, ""),
    )

    assert keepalive.refresh_once("http://x", "adm", "acc", "chrome131") == 2
    assert pushed == []
