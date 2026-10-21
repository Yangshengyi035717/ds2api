"""Tests for HAR / browser credential import."""
from __future__ import annotations

import json

import pytest

from credential_import import (
    CredentialImportError,
    parse_credential_payload,
)


TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
DEVICE_ID = "7e533d08-71c6-493d-a508-83a94c896b0e"


def test_browser_export_accepts_object_cookies_and_device_id():
    imported = parse_credential_payload({
        "token": TOKEN,
        "cookies": {"ds_session_id": "s1", "HWWAFSESID": "w1"},
        "device_id": DEVICE_ID,
    })
    assert imported.token == TOKEN
    assert imported.cookies == "ds_session_id=s1; HWWAFSESID=w1"
    assert imported.device_id == DEVICE_ID
    assert imported.source == "browser-export"
    assert imported.warnings == []


def test_browser_export_unwraps_localstorage_token():
    imported = parse_credential_payload(json.dumps({
        "token": json.dumps({"value": TOKEN, "__version": "0"}),
        "cookie": "a=1; b=2",
    }))
    assert imported.token == TOKEN
    assert imported.cookies == "a=1; b=2"
    assert imported.device_id == ""
    assert any("device_id" in warning for warning in imported.warnings)


def test_browser_export_rejects_missing_token():
    with pytest.raises(CredentialImportError):
        parse_credential_payload({"cookies": "a=1"})


def test_har_extracts_token_cookies_and_device_id():
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "headers": [
                            {"name": "x-device-id", "value": DEVICE_ID},
                        ],
                    },
                    "response": {"headers": [], "content": {}},
                },
                {
                    "request": {
                        "headers": [
                            {"name": "x-device-id", "value": DEVICE_ID},
                            {"name": "Cookie", "value": "ds_session_id=s1; HWWAFSESID=w1"},
                        ],
                    },
                    "response": {
                        "headers": [
                            {"name": "Set-Cookie", "value": "extra=1; Path=/; Secure"},
                        ],
                        "content": {
                            "text": json.dumps({
                                "code": 0,
                                "data": {
                                    "biz_code": 0,
                                    "biz_data": {
                                        "user": {"id": "u1", "token": TOKEN},
                                    },
                                },
                            }),
                        },
                    },
                },
            ]
        }
    }
    imported = parse_credential_payload(har)
    assert imported.token == TOKEN
    assert imported.device_id == DEVICE_ID
    assert "ds_session_id=s1" in imported.cookies
    assert "extra=1" in imported.cookies
    assert imported.source.startswith("har:")


def test_har_does_not_mistake_mfa_ticket_for_token():
    har = {
        "log": {
            "entries": [
                {
                    "request": {"headers": []},
                    "response": {
                        "headers": [],
                        "content": {
                            "text": json.dumps({
                                "data": {
                                    "biz_data": {
                                        "mfa_ticket": "x" * 64,
                                        "user": {"id": "u1", "token": TOKEN},
                                    }
                                }
                            })
                        },
                    },
                }
            ]
        }
    }
    imported = parse_credential_payload(har)
    assert imported.token == TOKEN


def test_invalid_json_is_reported_clearly():
    with pytest.raises(CredentialImportError, match="JSON"):
        parse_credential_payload("{not-json")
