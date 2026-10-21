"""Diagnostic: is a DeepSeek web token bound to the device id it was issued to?

Prints only verdicts; never the token.

Usage:
    python -m keepalive.device_bind_check --token "<token>" \
        --device-id <id-used-at-login> [--other-device-id <any-uuid>]
"""
from __future__ import annotations

import argparse
import uuid

from curl_cffi import requests as cffi_requests

from keepalive.keepalive import BASE_URL, _headers


def _probe(token: str, device_id: str) -> tuple[int, int | None, str]:
    headers = _headers(token)
    headers["x-device-id"] = device_id
    session = cffi_requests.Session(impersonate="chrome150", timeout=25)
    response = session.get(BASE_URL + "/api/v0/users/current", headers=headers)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}
    code = body.get("code") if isinstance(body, dict) else None
    msg = str(body.get("msg") or "") if isinstance(body, dict) else ""
    return response.status_code, code, msg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--other-device-id", default=str(uuid.uuid4()))
    args = parser.parse_args()

    for label, device_id in (
        ("issued_device", args.device_id or args.other_device_id),
        ("other_device", args.other_device_id),
    ):
        try:
            status, code, msg = _probe(args.token, device_id)
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: NETWORK {type(exc).__name__}: {exc}")
            continue
        verdict = "VALID" if code == 0 else "INVALID" if code == 40003 else "OTHER"
        print(f"{label}: HTTP {status} code={code} -> {verdict} {msg[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
