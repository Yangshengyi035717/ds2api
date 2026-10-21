#!/usr/bin/env python3
"""Synchronize a DS2API provider manifest into a Sub2API DeepSeek upstream.

Examples:
    python scripts/sync_sub2api.py --dry-run
    python scripts/sync_sub2api.py --apply \
      --sub2api-url http://127.0.0.1:18080 \
      --sub2api-token "$SUB2API_ADMIN_TOKEN"
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx


def _arg(args: argparse.Namespace, name: str, env_name: str, default: str = "") -> str:
    return str(getattr(args, name) or os.environ.get(env_name, default)).strip()


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        return payload.get("data")
    return payload


def _list_items(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _find_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in items if str(item.get("name") or "") == name), None)


def build_payloads(
    manifest: dict[str, Any],
    *,
    ds2api_url: str,
    ds2api_key: str,
    account_name: str,
    channel_name: str,
    platform: str,
    api_protocol: str,
    group_ids: list[int],
    concurrency: int,
    priority: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    models = manifest.get("models") if isinstance(manifest.get("models"), list) else []
    model_ids = [
        str(model.get("id") or "").strip()
        for model in models
        if isinstance(model, dict) and str(model.get("id") or "").strip()
    ]
    if not model_ids:
        raise ValueError("DS2API provider manifest contains no models")

    pricing = next(
        (
            model.get("pricing")
            for model in models
            if isinstance(model, dict) and isinstance(model.get("pricing"), dict)
        ),
        {},
    )
    rate_multiplier = float(pricing.get("rate_multiplier", 1.0) or 0)
    model_mapping = {model_id: model_id for model_id in model_ids}

    account_payload = {
        "name": account_name,
        "notes": "Managed by DS2API Sub2API sync",
        "platform": platform,
        "type": "apikey",
        "credentials": {
            "api_key": ds2api_key,
            "base_url": ds2api_url.rstrip("/"),
            "account_mode": "payg",
            "api_protocol": api_protocol,
            "api_base_urls": {
                "chat_completions": ds2api_url.rstrip("/"),
                "responses": ds2api_url.rstrip("/"),
                "anthropic": ds2api_url.rstrip("/"),
            },
            "model_mapping": model_mapping,
        },
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": rate_multiplier,
        "group_ids": group_ids,
    }

    model_pricing: dict[str, Any] = {
        "platform": platform,
        "models": model_ids,
        "billing_mode": str(pricing.get("billing_mode") or "token"),
    }
    for source, target in (
        ("input_price", "input_price"),
        ("output_price", "output_price"),
        ("cache_read_price", "cache_read_price"),
        ("cache_write_price", "cache_write_price"),
        ("per_request_price", "per_request_price"),
    ):
        value = pricing.get(source)
        if value is not None:
            model_pricing[target] = value

    channel_payload = {
        "name": channel_name,
        "description": "Managed by DS2API Sub2API sync",
        "group_ids": group_ids,
        "model_pricing": [model_pricing],
        "model_mapping": {platform: model_mapping},
        "billing_model_source": "requested",
        "restrict_models": False,
    }
    return account_payload, channel_payload


class Sub2APIClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool):
        auth_headers = (
            {"x-api-key": token}
            if token.startswith("admin-")
            else {"Authorization": f"Bearer {token}"}
        )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=auth_headers,
            timeout=30,
            verify=verify_tls,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:500]
            raise RuntimeError(f"Sub2API {method} {path} failed: {response.status_code} {detail}")
        return response.json() if response.content else {}

    def find_account(self, name: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            "/api/v1/admin/accounts",
            params={"search": name, "page_size": 100, "lite": "true"},
        )
        return _find_by_name(_list_items(payload), name)

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(_unwrap(self._request("POST", "/api/v1/admin/accounts", json=payload)) or {})

    def update_account(self, account_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        update = {key: value for key, value in payload.items() if key != "platform"}
        return dict(
            _unwrap(
                self._request(
                    "PUT",
                    f"/api/v1/admin/accounts/{account_id}",
                    json=update,
                )
            )
            or {}
        )

    def sync_models(self, account_id: int | str) -> Any:
        return self._request(
            "POST",
            f"/api/v1/admin/accounts/{account_id}/models/sync-upstream",
        )

    def find_channel(self, name: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            "/api/v1/admin/channels",
            params={"search": name, "page_size": 100},
        )
        return _find_by_name(_list_items(payload), name)

    def create_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(_unwrap(self._request("POST", "/api/v1/admin/channels", json=payload)) or {})

    def update_channel(self, channel_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        update = {key: value for key, value in payload.items() if key != "platform"}
        return dict(
            _unwrap(
                self._request(
                    "PUT",
                    f"/api/v1/admin/channels/{channel_id}",
                    json=update,
                )
            )
            or {}
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ds2api-url", default=os.environ.get("DS2API_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--ds2api-key", default=os.environ.get("DS2API_KEY", ""))
    parser.add_argument("--sub2api-url", default=os.environ.get("SUB2API_URL", ""))
    parser.add_argument("--sub2api-token", default=os.environ.get("SUB2API_ADMIN_TOKEN", ""))
    parser.add_argument("--account-name", default="DS2API")
    parser.add_argument("--channel-name", default="DS2API")
    parser.add_argument("--platform", default="deepseek")
    parser.add_argument(
        "--api-protocol",
        choices=["chat_completions", "responses", "anthropic", "adaptive"],
        default="chat_completions",
    )
    parser.add_argument("--group-id", action="append", type=int, default=[])
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--priority", type=int, default=10)
    parser.add_argument("--skip-channel", action="store_true")
    parser.add_argument("--skip-model-sync", action="store_true")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly print payloads without writing (default when --apply is omitted).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to Sub2API. Without this flag only prints the planned payloads.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    ds2api_url = _arg(args, "ds2api_url", "DS2API_URL", "http://127.0.0.1:8080")
    ds2api_key = _arg(args, "ds2api_key", "DS2API_KEY")
    if not ds2api_key:
        raise SystemExit("DS2API key is required via --ds2api-key or DS2API_KEY")

    with httpx.Client(timeout=30, verify=not args.insecure) as client:
        response = client.get(
            f"{ds2api_url.rstrip('/')}/v1/provider-manifest",
            headers={"Authorization": f"Bearer {ds2api_key}"},
        )
        response.raise_for_status()
        manifest = response.json()

    account_payload, channel_payload = build_payloads(
        manifest,
        ds2api_url=ds2api_url,
        ds2api_key=ds2api_key,
        account_name=args.account_name,
        channel_name=args.channel_name,
        platform=args.platform,
        api_protocol=args.api_protocol,
        group_ids=args.group_id,
        concurrency=args.concurrency,
        priority=args.priority,
    )

    print("=== DS2API Provider Manifest ===")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("=== Sub2API Account Payload ===")
    print(json.dumps(account_payload, ensure_ascii=False, indent=2))
    print("=== Sub2API Channel Payload ===")
    print(json.dumps(channel_payload, ensure_ascii=False, indent=2))

    if not args.apply:
        print("dry-run: no Sub2API changes were made")
        return 0

    sub2api_url = _arg(args, "sub2api_url", "SUB2API_URL")
    sub2api_token = _arg(args, "sub2api_token", "SUB2API_ADMIN_TOKEN")
    if not sub2api_url or not sub2api_token:
        raise SystemExit("--sub2api-url and --sub2api-token are required with --apply")

    sub2api = Sub2APIClient(sub2api_url, sub2api_token, verify_tls=not args.insecure)
    try:
        account = sub2api.find_account(args.account_name)
        if account:
            account = sub2api.update_account(account["id"], account_payload)
            print(f"updated account id={account.get('id')}")
        else:
            account = sub2api.create_account(account_payload)
            print(f"created account id={account.get('id')}")

        account_id = account.get("id")
        if not account_id:
            raise RuntimeError("Sub2API did not return the created/updated account id")
        if not args.skip_model_sync:
            sub2api.sync_models(account_id)
            print(f"synced upstream models for account id={account_id}")

        if not args.skip_channel:
            channel = sub2api.find_channel(args.channel_name)
            if channel:
                channel = sub2api.update_channel(channel["id"], channel_payload)
                print(f"updated channel id={channel.get('id')}")
            else:
                channel = sub2api.create_channel(channel_payload)
                print(f"created channel id={channel.get('id')}")
    finally:
        sub2api.close()

    print("Sub2API sync completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
