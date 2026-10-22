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
from urllib.parse import unquote, urlsplit


class CredentialImportError(ValueError):
    """Raised when a payload does not contain usable upstream credentials."""


@dataclass
class ImportedCredentials:
    token: str
    cookies: str = ""
    device_id: str = ""
    user_id: str = ""
    refresh_token: str = ""
    provider: str = ""
    source: str = ""
    xsrf_token: str = ""
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
            "has_refresh_token": bool(self.refresh_token),
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
_MIMO_HOST = "aistudio.xiaomimimo.com"
_MIMO_COOKIE_NAMES = ("userId", "xiaomichatbot_serviceToken", "xiaomichatbot_ph")


def parse_credential_payload(payload: str | dict | list) -> ImportedCredentials:
    """Parse either a DevTools HAR or the browser console export JSON."""
    if isinstance(payload, str):
        text = payload.strip()
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1].strip()
        if not text.startswith(("{", "[")) and _looks_like_mimo_cookies(text):
            return _from_mimo_cookies(text, source="mimo-cookie")
        if "hunyuan_token=" in text and "hunyuan_user=" in text:
            return _from_hunyuan_cookie_string(text)
        if "BDUSS=" in text:
            return _from_wenxin_cookie_string(text)
        if "sessionid=" in text and "ttwid=" in text:
            return _from_doubao_cookie_string(text)
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
            "refresh_token",
            "refreshtoken",
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
    cookies = _coerce_cookies(
        _first_value(data, "cookies", "cookie", "cookie_header")
    )
    device_id = _first_string(data, "device_id", "deviceId", "x_device_id")
    user_id = _first_string(data, "user_id", "userId", "uid")
    refresh_token = _first_string(
        data,
        "refresh_token",
        "refreshToken",
    )
    provider = _first_string(data, "provider", "account_type")
    if str(provider).strip().lower() in {"xiaomi", "xiaomimimo", "mimo"} or (
        not provider and _looks_like_mimo_cookies(cookies)
    ):
        imported = _from_mimo_cookies(cookies, source="browser-export")
        imported.token = token
        imported.device_id = _normalize_device_id(device_id)
        return imported
    if str(provider).strip().lower() in {"hunyuan", "tencent", "aistudio"}:
        token = token or _cookie_value(cookies, "hunyuan_token")
        user_id = user_id or _cookie_value(cookies, "hunyuan_user")
        provider = "hunyuan"
    if str(provider).strip().lower() in {"wenxin", "ernie", "baidu"}:
        token = token or _first_string(data, "wenxin_token", "frame_token")
        user_id = user_id or _first_string(data, "lid", "wenxin_lid")
        provider = "wenxin"
    if not token:
        raise CredentialImportError("浏览器导出内容中没有 token")
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
        refresh_token=_normalize_refresh_token(refresh_token),
        provider=provider,
        source="browser-export",
        warnings=warnings,
    )


def _from_har(har: dict) -> ImportedCredentials:
    entries = har["log"]["entries"]
    if any(
        isinstance(entry, dict)
        and _is_mimo_url(str((entry.get("request") or {}).get("url") or ""))
        for entry in entries
    ):
        return _from_mimo_har(entries)
    if any(
        isinstance(entry, dict)
        and (
            "chat.baidu.com/aichat/api/conversation"
            in str((entry.get("request") or {}).get("url") or "")
            or "chat.baidu.com/csaitab/history/list"
            in str((entry.get("request") or {}).get("url") or "")
            or "wenxin.baidu.com" in str((entry.get("request") or {}).get("url") or "")
        )
        for entry in entries
    ):
        return _from_wenxin_har(entries)
    if any(
        isinstance(entry, dict)
        and "doubao.com" in str((entry.get("request") or {}).get("url") or "")
        for entry in entries
    ):
        return _from_doubao_har(entries)
    if any(
        isinstance(entry, dict)
        and "hunyuan.tencent.com" in str((entry.get("request") or {}).get("url") or "")
        for entry in entries
    ):
        return _from_hunyuan_har(entries)
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


def _looks_like_mimo_cookies(cookies: str) -> bool:
    names = {part.split("=", 1)[0].strip() for part in cookies.split(";") if "=" in part}
    return bool(names & set(_MIMO_COOKIE_NAMES[1:]))


def _from_mimo_cookies(cookies: str, *, source: str) -> ImportedCredentials:
    from mimo_adapter import mimo_user_id_from_cookies, mimo_validate_cookies

    normalized = _coerce_cookies(cookies)
    try:
        mimo_validate_cookies(normalized)
    except ValueError:
        if source.startswith("har:"):
            raise CredentialImportError(
                "HAR 中缺少 MiMo 登录 Cookie；脱敏 HAR 需要补充完整 Cookie"
                "（userId、xiaomichatbot_serviceToken、xiaomichatbot_ph）。"
            ) from None
        raise CredentialImportError(
            "MiMo Cookie 必须包含 userId、xiaomichatbot_serviceToken 和 xiaomichatbot_ph"
        ) from None
    return ImportedCredentials(
        token="",
        cookies=normalized,
        user_id=mimo_user_id_from_cookies(normalized),
        provider="mimo",
        source=source,
        warnings=["MiMo 使用登录 Cookie；Cookie 失效后需重新导入。"],
    )


def _is_mimo_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname == _MIMO_HOST
    except ValueError:
        return False


def _from_mimo_har(entries: list[Any]) -> ImportedCredentials:
    from mimo_adapter import mimo_cookie_value

    cookies: dict[str, str] = {}
    device_id = ""
    ph = ""
    telemetry_segments = {
        "telemetry", "analytics", "collect", "beacon", "monitor",
        "metrics", "report", "track", "log", "logs", "event", "events",
    }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        url = str(request.get("url") or "")
        if not _is_mimo_url(url):
            continue
        parsed = urlsplit(url)
        if set(unquote(parsed.path).lower().split("/")) & telemetry_segments:
            continue
        response = entry.get("response") or {}
        headers = _header_map(request.get("headers"))
        for item in request.get("cookies") or []:
            if isinstance(item, dict) and item.get("name"):
                cookies[str(item["name"])] = str(item.get("value") or "")
        _merge_cookie_header(cookies, headers.get("cookie", ""))
        for item in response.get("cookies") or []:
            if isinstance(item, dict) and item.get("name"):
                cookies[str(item["name"])] = str(item.get("value") or "")
        for name, value in _header_pairs(response.get("headers")):
            if name == "set-cookie":
                _merge_set_cookie(cookies, value)
        device_id = headers.get("x-device-id", "").strip() or device_id
        # HAR queryString values are already decoded. Raw URL query values
        # need percent decoding only: unquote_plus would corrupt literal '+'.
        entry_ph = ""
        for item in request.get("queryString") or []:
            if isinstance(item, dict) and item.get("name") in {"ph", "xiaomichatbot_ph"}:
                entry_ph = str(item.get("value") or "")
        for part in parsed.query.split("&"):
            name, separator, value = part.partition("=")
            if separator and unquote(name) in {"ph", "xiaomichatbot_ph"}:
                entry_ph = unquote(value)
        ph = entry_ph or ph
    if ph and not mimo_cookie_value(_serialize_cookies(cookies), "xiaomichatbot_ph"):
        cookies["xiaomichatbot_ph"] = ph
    imported = _from_mimo_cookies(_serialize_cookies(cookies), source="har:mimo-cookie")
    imported.device_id = _normalize_device_id(device_id)
    return imported


def _from_hunyuan_cookie_string(cookies: str) -> ImportedCredentials:
    normalized = _coerce_cookies(cookies)
    token = _cookie_value(normalized, "hunyuan_token")
    user_id = _cookie_value(normalized, "hunyuan_user")
    if not token or not user_id:
        raise CredentialImportError(
            "Hunyuan Cookie 必须同时包含 hunyuan_token 和 hunyuan_user"
        )
    return ImportedCredentials(
        token=token,
        cookies=normalized,
        user_id=user_id,
        provider="hunyuan",
        source="hunyuan-cookie",
        warnings=["Hunyuan 登录 Cookie 为 HttpOnly，退出登录后需要重新导入。"],
    )


def _from_hunyuan_har(entries: list[Any]) -> ImportedCredentials:
    cookies: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        req_headers = _header_map(request.get("headers"))
        cookie_header = req_headers.get("cookie")
        if cookie_header:
            _merge_cookie_header(cookies, cookie_header)
        for _, value in _header_pairs(response.get("headers")):
            if "hunyuan_token=" in value or "hunyuan_user=" in value:
                _merge_set_cookie(cookies, value)
        for item in request.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                cookies[name] = str(item.get("value") or "").strip()
        for item in response.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                cookies[name] = str(item.get("value") or "").strip()

    normalized = _serialize_cookies(cookies)
    token = _cookie_value(normalized, "hunyuan_token")
    user_id = _cookie_value(normalized, "hunyuan_user")
    if not token or not user_id:
        raise CredentialImportError(
            "HAR 中没有可用的 Hunyuan Cookie。请在 DevTools 的 "
            "api.hunyuan.tencent.com 请求中复制完整 Cookie 请求头。"
        )
    return ImportedCredentials(
        token=token,
        cookies=normalized,
        user_id=user_id,
        provider="hunyuan",
        source="har:hunyuan-cookie",
        warnings=["Hunyuan 登录 Cookie 为 HttpOnly，退出登录后需要重新导入。"],
    )


def _from_wenxin_har(entries: list[Any]) -> ImportedCredentials:
    cookies: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        url = str(request.get("url") or "")
        if not (
            "chat.baidu.com/aichat/api/conversation" in url
            or "chat.baidu.com/csaitab/history/list" in url
            or "wenxin.baidu.com" in url
        ):
            continue
        for item in request.get("cookies") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    cookies[name] = str(item.get("value") or "").strip()
        req_headers = _header_map(request.get("headers"))
        cookie_header = req_headers.get("cookie")
        if cookie_header:
            _merge_cookie_header(cookies, cookie_header)
    if not _cookie_value(_serialize_cookies(cookies), "BDUSS"):
        raise CredentialImportError(
            "HAR 中缺少百度登录 Cookie（BDUSS）。请导出包含 chat.baidu.com 请求 "
            "Cookie 的 HAR，并在浏览器中保持已登录状态。"
        )
    return ImportedCredentials(
        token="",
        cookies=_serialize_cookies(cookies),
        user_id=_cookie_value(_serialize_cookies(cookies), "BAIDUID").split(
            ":", 1
        )[0].strip(),
        provider="wenxin",
        source="har:wenxin-cookie",
        warnings=["文心账号会在服务端自动获取页面 token/lid；Cookie 失效后需重新导入。"],
    )


def _from_wenxin_cookie_string(cookies: str) -> ImportedCredentials:
    normalized = _coerce_cookies(cookies)
    if not _cookie_value(normalized, "BDUSS"):
        raise CredentialImportError("文心 Cookie 必须包含 BDUSS")
    return ImportedCredentials(
        token="",
        cookies=normalized,
        user_id=_cookie_value(normalized, "BAIDUID").split(":", 1)[0].strip(),
        provider="wenxin",
        source="wenxin-cookie",
        warnings=["文心账号会在服务端自动获取页面 token/lid；Cookie 失效后需重新导入。"],
    )


def _from_doubao_cookie_string(cookies: str) -> ImportedCredentials:
    normalized = _coerce_cookies(cookies)
    if not _cookie_value(normalized, "sessionid"):
        raise CredentialImportError("Doubao Cookie 必须包含 sessionid")
    # 实测：登录 Cookie 足够；服务端可用自生成 query 直连 chat/completion，
    # 不需要浏览器 a_bogus。msToken 由 Cookie 携带，缺失时上游也会重签。
    return ImportedCredentials(
        token="",
        cookies=normalized,
        user_id=_cookie_value(normalized, "web_id")
        or _cookie_value(normalized, "ttwid"),
        device_id=_cookie_value(normalized, "device_id")
        or "7685346952515520051",
        provider="doubao",
        source="doubao-cookie",
        warnings=["豆包账号依赖登录 Cookie（sessionid）；Cookie 失效后需重新导入。"],
    )


def _from_doubao_har(entries: list[Any]) -> ImportedCredentials:
    cookies: dict[str, str] = {}
    device_id = ""
    web_id = ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        url = str(request.get("url") or "")
        if "doubao.com" not in url:
            continue
        for item in request.get("cookies") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    cookies[name] = str(item.get("value") or "").strip()
        headers = _header_map(request.get("headers"))
        cookie_header = headers.get("cookie")
        if cookie_header:
            _merge_cookie_header(cookies, cookie_header)
        for item in request.get("queryString") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name == "device_id":
                device_id = value
            elif name == "web_id":
                web_id = value
    normalized = _serialize_cookies(cookies)
    if not _cookie_value(normalized, "sessionid"):
        raise CredentialImportError(
            "HAR 中缺少豆包登录 Cookie（sessionid）。请在已登录浏览器导出 "
            "包含 www.doubao.com 请求 Cookie 的 HAR。"
        )
    return ImportedCredentials(
        token="",
        cookies=normalized,
        user_id=web_id or _cookie_value(normalized, "web_id"),
        device_id=device_id or _cookie_value(normalized, "device_id"),
        provider="doubao",
        source="har:doubao-cookie",
        warnings=["豆包账号依赖登录 Cookie 与浏览器签名参数；Cookie 失效后需重新导入。"],
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


def _normalize_refresh_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 8192 or any(ch.isspace() for ch in value):
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


def _cookie_value(cookies: str, name: str) -> str:
    parsed: dict[str, str] = {}
    _merge_cookie_header(parsed, cookies)
    return parsed.get(name, "").strip()


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
