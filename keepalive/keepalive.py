"""Keep a DeepSeek Chat web session alive over plain HTTP (no browser).

Why protocol instead of a browser
---------------------------------
Measured against the live site:

  * ``GET /`` answers **202** with a WAF challenge page (needs JS).
  * ``POST /api/v0/chat/create_pow_challenge`` answers **200**.
  * ``GET /api/v0/users/current`` answers ``40003 Authorization Failed``
    when the bearer token is invalid, and 200 when it is valid.

So the API surface is reachable without executing JS, which is exactly what
``adapter.py`` already relies on (curl_cffi + TLS impersonation). A headless
Chrome would add ~400MB RSS and a 2GB image for no benefit.

What this worker does
---------------------
Each cycle, using the stored account credentials:

  1. ``GET /api/v0/users/current`` — the cheap authenticated probe. A 200
     proves the token is alive; it also lets curl_cffi merge any refreshed
     ``Set-Cookie`` (WAF/session cookies) into the jar.
  2. If the probe succeeds and the jar changed, push the merged cookie string
     back to DS2API via ``PUT /admin/api/accounts/{id}``.
  3. If it fails with ``40003``, report ``SESSION_INVALID`` and stop. It does
     **not** retry in a loop: an invalid login needs a human to sign in again.

It never creates a chat session and never sends a prompt, so it costs no
upstream quota and does not look like automation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as cffi_requests

BASE_URL = "https://chat.deepseek.com"

#: Authenticated, side-effect-free endpoints used to prove the token is alive.
#: ``users/current`` is not present in the HAR (the frontend may have dropped
#: it), so the keepalive tries the cheapest read-only endpoints in order and
#: accepts the first decisive answer.
PROBE_PATHS = (
    "/api/v0/users/current",
    "/api/v0/client/settings?scope=model",
)

DEFAULT_IMPERSONATE = os.environ.get("DEEPSEEK_IMPERSONATE", "chrome150")

#: Client version advertised by the web frontend (HAR 2026-10-21: 2.5.0).
CLIENT_VERSION = os.environ.get("DEEPSEEK_CLIENT_VERSION", "2.5.0")

#: Stable per-account device id. The frontend sends ``x-device-id`` on every
#: API call; omitting it while presenting a browser fingerprint is itself a
#: tell. Persisted in the config volume so it survives restarts.
DEVICE_ID_PATH = os.environ.get("KEEPALIVE_DEVICE_ID_FILE", "/data/device_id")

#: Upstream codes meaning "this token is dead" (as opposed to "slow down").
INVALID_TOKEN_CODES = {40003}


@dataclass
class ProbeResult:
    status: int
    valid: bool
    code: int | None
    message: str


def _normalize_token(raw: str) -> str:
    """Accept a bare token or the ``{"value": ..., "__version": ...}`` wrapper."""
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, dict):
            inner = parsed.get("value") or parsed.get("token")
            if isinstance(inner, str):
                value = inner.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _cookie_header(jar) -> str:
    """Serialise the curl_cffi cookie jar back into a ``k=v; k=v`` blob."""
    pairs: list[str] = []
    seen: set[str] = set()
    for cookie in jar:
        name = getattr(cookie, "name", None)
        value = getattr(cookie, "value", None)
        if not name or value is None or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _seed_jar(session, cookies: str) -> None:
    for part in (cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        try:
            session.cookies.set(name, value.strip(), domain=".deepseek.com")
        except Exception:  # noqa: BLE001 - a bad cookie must not abort the cycle
            continue


def _headers(token: str, device_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Authorization": f"Bearer {token}",
        "X-Client-Version": CLIENT_VERSION,
        "X-Client-Platform": "web",
        "X-Client-Locale": "zh_CN",
        "X-Client-Timezone-Offset": "28800",
        "x-client-bundle-id": "com.deepseek.chat",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Priority": "u=1, i",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    resolved_device_id = (device_id or "").strip() or _device_id()
    if resolved_device_id:
        headers["x-device-id"] = resolved_device_id
        headers["x-device-model"] = ""
    return headers


def _device_id() -> str:
    """Return a stable device id, creating and persisting one on first use."""
    try:
        path = DEVICE_ID_PATH
        if os.path.exists(path):
            value = open(path, encoding="utf-8").read().strip()
            if value:
                return value
        value = str(uuid.uuid4())
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        return value
    except OSError:
        # A read-only filesystem must not break the keepalive.
        return os.environ.get("KEEPALIVE_DEVICE_ID", "")


def probe(session, token: str, device_id: str | None = None) -> ProbeResult:
    """Probe the cheapest authenticated endpoint(s) and classify the answer.

    A dead token is decisive and stops immediately. Anything else (WAF
    challenge, upstream error, unknown shape) falls through to the next probe
    path so a single endpoint change cannot make a healthy session look dead.
    """
    last = ProbeResult(0, False, None, "no probe attempted")
    for path in PROBE_PATHS:
        try:
            response = session.get(
                BASE_URL + path,
                headers=_headers(token, device_id=device_id),
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - network error is not a verdict
            last = ProbeResult(0, False, None, f"network: {exc}")
            continue

        try:
            body: Any = response.json()
        except Exception:  # noqa: BLE001
            body = None

        code = body.get("code") if isinstance(body, dict) else None
        message = ""
        if isinstance(body, dict):
            message = str(body.get("msg") or body.get("message") or "")

        if response.status_code == 200 and code == 0:
            return ProbeResult(response.status_code, True, 0, message)
        if code in INVALID_TOKEN_CODES:
            return ProbeResult(
                response.status_code, False, code, message or "invalid token"
            )
        # 202 = WAF challenge, 5xx = upstream trouble: retry later, do not
        # declare the credentials dead.
        last = ProbeResult(
            response.status_code,
            False,
            code,
            message or f"unexpected status {response.status_code}",
        )
    return last


def push_cookies(
    dsapi_url: str,
    admin_token: str,
    account_id: str,
    cookies: str,
) -> None:
    response = cffi_requests.put(
        f"{dsapi_url.rstrip('/')}/admin/api/accounts/{account_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"cookies": cookies},
        timeout=30,
    )
    response.raise_for_status()


def refresh_once(
    dsapi_url: str,
    admin_token: str,
    account_id: str,
    impersonate: str,
    device_id: str | None = None,
) -> int:
    """One keepalive cycle. Returns a process exit code."""
    creds = _read_full_credentials(dsapi_url, admin_token, account_id)
    token = _normalize_token(creds["token"])
    original_cookies = creds["cookies"]

    if not token:
        print(f"SESSION_INVALID account={account_id} reason=no_token", flush=True)
        return 2

    session = cffi_requests.Session(impersonate=impersonate, timeout=20)
    _seed_jar(session, original_cookies)

    result = probe(
        session,
        token,
        device_id=device_id or creds.get("device_id") or None,
    )

    if result.valid:
        merged = _cookie_header(session.cookies.jar)
        if merged and merged != original_cookies:
            push_cookies(dsapi_url, admin_token, account_id, merged)
            print(
                f"REFRESHED account={account_id} cookies={len(merged)}",
                flush=True,
            )
        else:
            print(f"ALIVE account={account_id}", flush=True)
        return 0

    if result.code in INVALID_TOKEN_CODES:
        print(
            f"SESSION_INVALID account={account_id} code={result.code} "
            f"msg={result.message[:120]}",
            flush=True,
        )
        return 2

    print(
        f"TRANSIENT account={account_id} status={result.status} "
        f"msg={result.message[:120]}",
        flush=True,
    )
    return 3


def _read_full_credentials(
    dsapi_url: str,
    admin_token: str,
    account_id: str,
) -> dict[str, str]:
    """Read the stored token/cookies via the account-detail endpoint."""
    response = cffi_requests.get(
        f"{dsapi_url.rstrip('/')}/admin/api/accounts/{account_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    if response.status_code == 404:
        raise RuntimeError("credential detail endpoint unavailable")
    response.raise_for_status()
    data = response.json()
    payload = data.get("account", data)
    return {
        "token": str(payload.get("token") or ""),
        "cookies": str(payload.get("cookies") or ""),
        "device_id": str(payload.get("device_id") or ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsapi-url", default=os.environ.get("DS2API_URL", "http://127.0.0.1:3011"))
    parser.add_argument("--admin-token", default=os.environ.get("DS2API_ADMIN_TOKEN", ""))
    parser.add_argument("--account-id", default=os.environ.get("DS2API_ACCOUNT_ID", ""))
    parser.add_argument("--interval", type=int, default=int(os.environ.get("KEEPALIVE_INTERVAL", "900")))
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.admin_token or not args.account_id:
        print("DS2API_ADMIN_TOKEN and DS2API_ACCOUNT_ID are required", file=sys.stderr)
        return 1

    while True:
        try:
            code = refresh_once(
                args.dsapi_url,
                args.admin_token,
                args.account_id,
                args.impersonate,
            )
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            code = 3

        # 0 = alive/refreshed, 3 = transient (retry next cycle).
        # 2 = dead login (stop; a human must sign in again).
        if code == 2 or args.once:
            return code
        time.sleep(max(args.interval, 60))


if __name__ == "__main__":
    raise SystemExit(main())
