"""One-off diagnostic: do these credentials still reach the upstream API?

Prints the classification only — never the token or cookie values.

Usage:
    python -m keepalive.probe_check --creds creds.json [--impersonate chrome150]

``creds.json`` is ``{"token": "...", "cookies": "k=v; k=v"}``.
"""
from __future__ import annotations

import argparse
import json

from curl_cffi import requests as cffi_requests

from keepalive.keepalive import (
    BASE_URL,
    PROBE_PATHS,
    _cookie_header,
    _headers,
    _seed_jar,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--creds", required=True, help="JSON file with token/cookies")
    parser.add_argument("--impersonate", default="chrome150")
    args = parser.parse_args()

    with open(args.creds, encoding="utf-8") as handle:
        creds = json.load(handle)
    token = str(creds["token"]).strip()
    cookies = str(creds.get("cookies") or "")

    session = cffi_requests.Session(impersonate=args.impersonate, timeout=25)
    _seed_jar(session, cookies)

    for path in PROBE_PATHS:
        try:
            response = session.get(BASE_URL + path, headers=_headers(token), timeout=25)
        except Exception as exc:  # noqa: BLE001
            print(f"{path}: NETWORK {type(exc).__name__}: {exc}")
            continue
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        msg = str(body.get("msg") or "") if isinstance(body, dict) else ""
        print(f"{path}: HTTP {response.status_code} code={code} msg={msg[:80]}")
        if code == 0:
            merged = _cookie_header(session.cookies.jar)
            print(f"VERDICT: ALIVE cookies_out_len={len(merged)} changed={merged != cookies}")
            return 0
        if code == 40003:
            print("VERDICT: INVALID (40003)")
            return 2
    print("VERDICT: INCONCLUSIVE (retry later)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
