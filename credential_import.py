"""Import DeepSeek / z.ai browser credentials from a HAR or console export.

DeepSeek's password login is protected by an MFA trusted-device signature.
The private key is generated as a non-extractable WebCrypto key inside the
browser's IndexedDB, so a server cannot reproduce the signature.  The
supported flow is therefore browser-assisted:

1. The user signs in at chat.deepseek.com normally.
2. The browser exports ``{token, cookies, device_id}`` (or the user exports
   the DevTools HAR).
3. This module extracts the credentials and the server stores them.

Keeping ``device_id`` is recommended: the browser sends the same
``x-device-id`` on later API calls, and the upstream's device-check endpoint
may reject a mismatch.  We have not proven that a token is hard-bound to one
device, so this is treated as a consistency signal rather than a guarantee.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any


class CredentialImportError(ValueError):
    """Raised when a payload does not contain usable upstream credentials."""


@dataclass
class ImportedCredentials:
    token: str
    cookies: str = ""
    device_id: str = ""
    user_id: str = ""
    provider: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def public_summary(self) -> dict[str, Any]:
        """Return a safe summary without the token/cookie values."""
        return {
            "source": self.source,
            "has_token": bool(self.token),
            "token_length": len(self.token),
            "cookie_names": _cookie_names(self.cookies),
            "has_device_id": bool(self.device_id),
            "device_id_preview": _mask(self.device_id),
            "has_user_id": bool(self.user_id),
            "user_id_preview": _mask(self.user_id),
            "warnings": list(self.warnings),
        }


_TOKEN_MIN_LENGTH = 20
_TOKEN_WRAPPER_KEYS = ("value", "token", "access_token")
_NON_AUTH_TOKEN_KEYS = {
    "mfa_ticket",
    "mfa_challenge",
    "mfa_session_id",
    "x-settings-token",
    "refresh_token",
    "id_token",
}
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_credential_payload(payload: str | dict | list) -> ImportedCredentials:
    """Parse either a DevTools HAR or the browser console export JSON."""
    data = _coerce_json(payload)
    if isinstance(data, dict):
        log = data.get("log")
        if isinstance(log, dict) and isinstance(log.get("entries"), list):
            return _from_har(data)
        if _looks_like_browser_export(data):
            return _from_browser_export(data)
    raise CredentialImportError(
        "无法识别凭据格式。请粘贴 DevTools 导出的 HAR，或使用页面上的"
        "“复制浏览器导出脚本”生成的 JSON。"
    )


def normalize_token(raw: Any) -> str:
    """Return a bare upstream token from a localStorage wrapper/Bearer value."""
    return _normalize_token(raw)


def _coerce_json(payload: str | dict | list) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if not isinstance(payload, str):
        raise CredentialImportError("凭据内容必须是 JSON 字符串或对象")
    text = payload.strip()
    if not text:
        raise CredentialImportError("凭据内容为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialImportError(
            f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc


def _looks_like_browser_export(data: dict) -> bool:
    lowered = {str(key).lower() for key in data}
    return bool(
        lowered
        & {
            "token",
            "access_token",
            "user_token",
            "auth_token",
            "device_id",
            "deviceid",
            "user_id",
            "userid",
            "uid",
            "cookies",
            "cookie",
        }
    )


def _from_browser_export(data: dict) -> ImportedCredentials:
    token = _first_string(
        data,
        "token",
        "access_token",
        "user_token",
        "auth_token",
    )
    token = _normalize_token(token)
    if not token:
        raise CredentialImportError("浏览器导出内容中没有 token")

    cookies = _coerce_cookies(
        _first_value(data, "cookies", "cookie", "cookie_header")
    )
    device_id = _first_string(data, "device_id", "deviceId", "x_device_id")
    user_id = _first_string(data, "user_id", "userId", "uid")
    provider = _first_string(data, "provider", "account_type")
    warnings: list[str] = []
    if not cookies:
        warnings.append("未提供 Cookies；服务端会在首次请求时自动获取 WAF Cookie。")
    if not device_id:
        warnings.append(
            "未提供 device_id；如果上游校验设备，登录态可能很快失效。"
            "建议重新运行导出脚本并保留 device_id。"
        )
    return ImportedCredentials(
        token=token,
        cookies=cookies,
        device_id=_normalize_device_id(device_id),
        user_id=_normalize_identifier(user_id),
        provider=provider,
        source="browser-export",
        warnings=warnings,
    )


def _from_har(har: dict) -> ImportedCredentials:
    entries = har["log"]["entries"]
    cookies: dict[str, str] = {}
    device_id = ""
    token = ""
    token_source = ""

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        req_headers = _header_map(request.get("headers"))
        res_headers = _header_map(response.get("headers"))

        cookie_header = req_headers.get("cookie")
        if cookie_header:
            _merge_cookie_header(cookies, cookie_header)

        # HAR exporters may repeat Set-Cookie as separate header objects.
        for name, value in _header_pairs(response.get("headers")):
            if name == "set-cookie":
                _merge_set_cookie(cookies, value)

        req_device = (req_headers.get("x-device-id") or "").strip()
        if req_device:
            device_id = req_device

        auth = (req_headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            candidate = _normalize_token(auth[7:])
            if _valid_token(candidate):
                token = candidate
                token_source = "request-authorization"

        body = _response_json(response)
        if body is not None:
            candidate, path = _find_token(body)
            if candidate:
                token = candidate
                token_source = f"response:{path}"

    if not token:
        raise CredentialImportError(
            "HAR 中没有找到 DeepSeek 登录 token。请确认导出时包含 "
            "/api/v0/users/mfa/verify_signature 的响应体，"
            "或在登录后使用“浏览器导出脚本”。"
        )

    warnings: list[str] = []
    if not cookies:
        warnings.append("HAR 中没有 Cookie；服务端会在首次请求时自动获取 WAF Cookie。")
    if not device_id:
        warnings.append(
            "HAR 中没有 x-device-id；如果上游校验设备，登录态可能很快失效。"
        )

    return ImportedCredentials(
        token=token,
        cookies=_serialize_cookies(cookies),
        device_id=_normalize_device_id(device_id),
        source=f"har:{token_source}",
        warnings=warnings,
    )


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if not isinstance(headers, list):
        return pairs
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "")
        if name:
            pairs.append((name, value))
    return pairs


def _header_map(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in _header_pairs(headers):
        result[name] = value
    return result


def _response_json(response: dict) -> Any:
    content = response.get("content")
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if str(content.get("encoding") or "").lower() == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _find_token(node: Any, path: str = "") -> tuple[str, str]:
    """Find the most likely auth token in a response body."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if (
                key_l not in _NON_AUTH_TOKEN_KEYS
                and isinstance(value, str)
                and ("token" in key_l or key_l in {"access", "authorization"})
            ):
                candidate = _normalize_token(value)
                if _valid_token(candidate):
                    return candidate, child_path
            found, found_path = _find_token(value, child_path)
            if found:
                return found, found_path
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found, found_path = _find_token(value, f"{path}[{index}]")
            if found:
                return found, found_path
    return "", ""


def _normalize_token(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in _TOKEN_WRAPPER_KEYS:
                inner = parsed.get(key)
                if isinstance(inner, str) and inner.strip():
                    value = inner.strip()
                    break
    return value


def _valid_token(value: str) -> bool:
    if len(value) < _TOKEN_MIN_LENGTH or len(value) > 4096:
        return False
    if any(ch.isspace() for ch in value):
        return False
    return value.lower() not in {"undefined", "null", "none", "false"}


def _normalize_device_id(value: Any) -> str:
    return _normalize_identifier(value)


def _normalize_identifier(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 128 or any(ch.isspace() for ch in value):
        return ""
    return value


def _first_value(data: dict, *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _first_string(data: dict, *keys: str) -> str:
    value = _first_value(data, *keys)
    return value if isinstance(value, str) else ""


def _coerce_cookies(value: Any) -> str:
    if isinstance(value, str):
        cookies: dict[str, str] = {}
        _merge_cookie_header(cookies, value)
        return _serialize_cookies(cookies)
    if isinstance(value, dict):
        return _serialize_cookies(
            {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
        )
    if isinstance(value, list):
        cookies = {}
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                val = item.get("value")
                if name and val is not None:
                    cookies[name] = str(val).strip()
        return _serialize_cookies(cookies)
    return ""


def _merge_cookie_header(target: dict[str, str], header: str) -> None:
    for part in str(header).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name or name.startswith("$"):
            continue
        target[name] = value.strip()


def _merge_set_cookie(target: dict[str, str], header: str) -> None:
    first = str(header).split(";", 1)[0].strip()
    _merge_cookie_header(target, first)


def _serialize_cookies(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _cookie_names(cookies: str) -> list[str]:
    names: list[str] = []
    for part in (cookies or "").split(";"):
        part = part.strip()
        if "=" in part:
            name = part.split("=", 1)[0].strip()
            if name:
                names.append(name)
    return names[:8]


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"
