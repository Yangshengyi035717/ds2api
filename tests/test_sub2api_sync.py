"""Tests for the DS2API -> Sub2API account/channel payload builder."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.sync_sub2api import build_payloads


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "provider": {"id": "ds2api"},
        "models": [
            {
                "id": "deepseek-chat",
                "display_name": "DeepSeek Chat",
                "pricing": {
                    "currency": "USD",
                    "billing_mode": "token",
                    "input_price": 0.5,
                    "output_price": 1.5,
                    "cache_read_price": 0.1,
                    "cache_write_price": 0.2,
                    "per_request_price": None,
                    "rate_multiplier": 0.8,
                },
            }
        ],
    }


def test_build_payloads_for_deepseek_upstream():
    account, channel = build_payloads(
        _manifest(),
        ds2api_url="https://ds2api.example.com/",
        ds2api_key="sk-ds2api",
        account_name="DS2API",
        channel_name="DS2API",
        platform="deepseek",
        api_protocol="chat_completions",
        group_ids=[7],
        concurrency=3,
        priority=20,
    )

    assert account["platform"] == "deepseek"
    assert account["type"] == "apikey"
    assert account["rate_multiplier"] == 0.8
    assert account["credentials"]["base_url"] == "https://ds2api.example.com"
    assert account["credentials"]["api_protocol"] == "chat_completions"
    assert account["credentials"]["api_base_urls"] == {
        "chat_completions": "https://ds2api.example.com",
        "responses": "https://ds2api.example.com",
        "anthropic": "https://ds2api.example.com",
    }
    assert account["credentials"]["model_mapping"] == {"deepseek-chat": "deepseek-chat"}
    assert account["group_ids"] == [7]

    pricing = channel["model_pricing"][0]
    assert channel["name"] == "DS2API"
    assert pricing["platform"] == "deepseek"
    assert pricing["models"] == ["deepseek-chat"]
    assert pricing["input_price"] == 0.5
    assert pricing["output_price"] == 1.5
    assert pricing["cache_read_price"] == 0.1
    assert "per_request_price" not in pricing
    assert channel["model_mapping"] == {
        "deepseek": {"deepseek-chat": "deepseek-chat"}
    }


def test_build_payloads_rejects_empty_model_catalog():
    with pytest.raises(ValueError):
        build_payloads(
            {"models": []},
            ds2api_url="http://127.0.0.1:8080",
            ds2api_key="key",
            account_name="DS2API",
            channel_name="DS2API",
            platform="deepseek",
            api_protocol="chat_completions",
            group_ids=[],
            concurrency=1,
            priority=0,
        )


def test_sync_script_apply_flow_against_mock_sub2api():
    requests: list[tuple[str, str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(("GET", self.path, {}))
            if self.path.startswith("/v1/provider-manifest"):
                self._json(_manifest())
            elif self.path.startswith("/api/v1/admin/accounts"):
                self._json({"code": 0, "data": {"items": [], "total": 0}})
            elif self.path.startswith("/api/v1/admin/channels"):
                self._json({"code": 0, "data": {"items": [], "total": 0}})
            else:
                self._json({"code": 404, "message": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            requests.append(("POST", self.path, payload))
            if self.path == "/api/v1/admin/accounts":
                self._json({"code": 0, "data": {"id": 11, "name": "DS2API"}})
            elif self.path == "/api/v1/admin/accounts/11/models/sync-upstream":
                self._json({"code": 0, "data": {"models": ["deepseek-chat"]}})
            elif self.path == "/api/v1/admin/channels":
                self._json({"code": 0, "data": {"id": 22, "name": "DS2API"}})
            else:
                self._json({"code": 404, "message": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/sync_sub2api.py",
                "--apply",
                "--ds2api-url",
                f"http://127.0.0.1:{port}",
                "--ds2api-key",
                "sk-test",
                "--sub2api-url",
                f"http://127.0.0.1:{port}",
                "--sub2api-token",
                "admin-token",
                "--group-id",
                "7",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr + result.stdout
    paths = [(method, path.split("?", 1)[0]) for method, path, _ in requests]
    assert ("POST", "/api/v1/admin/accounts") in paths
    assert ("POST", "/api/v1/admin/accounts/11/models/sync-upstream") in paths
    assert ("POST", "/api/v1/admin/channels") in paths
    account_create = next(
        payload
        for method, path, payload in requests
        if method == "POST" and path == "/api/v1/admin/accounts"
    )
    assert account_create["platform"] == "deepseek"
    assert account_create["credentials"]["base_url"] == f"http://127.0.0.1:{port}"
    assert account_create["group_ids"] == [7]
    assert "Sub2API sync completed" in result.stdout
