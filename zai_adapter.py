"""Adapter for the z.ai web chat protocol.

The web client signs each completion with a short-lived HMAC and sends a
browser fingerprint as query parameters.  This adapter reproduces the request
shape from the captured front-end bundle while keeping the same
``create_session/chat/chat_stream`` interface as ``DeepSeekAdapter``.
"""
from __future__ import annotations

import base64
import datetime as _datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException
from upstream_http import stream_timeout

from adapter import (
    RateLimitError,
    UpstreamHintError,
    UpstreamTransientError,
)
from provider_registry import zai_model_name, zai_upstream_model

BASE_URL = "https://zchat.z.ai"
COMPLETION_PATH = os.environ.get(
    "ZAI_COMPLETION_PATH",
    "/api/v2/chat/completions",
)
SIGNATURE_SALT = "key-@@@@)))()((9))-xxxx&&&%%%%%"
SIGNATURE_WINDOW_MS = 5 * 60 * 1000
FE_VERSION = os.environ.get("ZAI_FE_VERSION", "prod-fe-1.1.112")
FE_QUERY_VERSION = os.environ.get("ZAI_FE_QUERY_VERSION", "0.0.1")
IMPERSONATE = os.environ.get("ZAI_IMPERSONATE", "chrome150")
HEALTH_PATH = "/api/v1/chats/?page=1&type=all&channel=zai-web"
CHAT_CREATE_PATH = "/api/v1/chats/new"


class ZaiAuthError(RuntimeError):
    """The z.ai token/cookie was rejected."""


class ZaiProtocolError(RuntimeError):
    """The upstream returned a malformed or unexpected response."""


def _device_id(value: str) -> str:
    value = (value or "").strip()
    if value:
        return value
    return f"uid_{secrets.token_hex(12)}"


def zai_user_id_from_token(token: str) -> str:
    """Extract the z.ai user id from a JWT access token when available."""
    value = (token or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    parts = value.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(
            base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    user_id = str(data.get("id") or data.get("user_id") or "").strip()
    if not user_id or len(user_id) > 128 or any(ch.isspace() for ch in user_id):
        return ""
    return user_id


def _merge_cookies(raw: str, token: str) -> str:
    cookies: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip():
            cookies[name.strip()] = value.strip()
    if token:
        cookies["token"] = token
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _signature_payload(request_id: str, timestamp: int, user_id: str) -> str:
    return (
        f"requestId,{request_id},"
        f"timestamp,{timestamp},"
        f"user_id,{user_id}"
    )


def _signature(prompt: str, user_id: str, request_id: str, timestamp: int) -> str:
    bucket = timestamp // SIGNATURE_WINDOW_MS
    key = hmac.new(
        SIGNATURE_SALT.encode("utf-8"),
        str(bucket).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    prompt_b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    message = (
        f"{_signature_payload(request_id, timestamp, user_id)}|"
        f"{prompt_b64}|{timestamp}"
    )
    return hmac.new(
        key.encode("ascii"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _fingerprint_params(
    *,
    token: str,
    user_id: str,
    request_id: str,
    timestamp: int,
    session_id: str,
) -> dict[str, str]:
    now = _datetime.datetime.now().astimezone()
    utc_now = now.astimezone(_datetime.timezone.utc)
    return {
        "version": FE_QUERY_VERSION,
        "platform": "web",
        "token": token,
        "user_id": user_id,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/155.0.0.0 Safari/537.36"
        ),
        "language": "zh-CN",
        "languages": "zh-CN,zh,en",
        "timezone": "Asia/Shanghai",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "screen_resolution": "1536x864",
        "viewport_height": "695",
        "viewport_width": "987",
        "viewport_size": "987x695",
        "color_depth": "24",
        "pixel_ratio": "1.25",
        "current_url": f"{BASE_URL}/c/{session_id}",
        "pathname": f"/c/{session_id}",
        "search": "",
        "hash": "",
        "host": "zchat.z.ai",
        "hostname": "zchat.z.ai",
        "protocol": "https:",
        "referrer": "",
        "title": "Z.ai - Advanced AI Chatbot & Agent powered by GLM-5.3-Flash",
        "timezone_offset": str(int(-now.utcoffset().total_seconds() // 60))
        if now.utcoffset()
        else "0",
        "local_time": utc_now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "utc_time": utc_now.strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        ),
        "is_mobile": "false",
        "is_touch": "false",
        "max_touch_points": "10",
        "browser_name": "Chrome",
        "os_name": "Windows",
        "requestId": request_id,
        "timestamp": str(timestamp),
        "signature_timestamp": str(timestamp),
    }


class ZaiAdapter:
    def __init__(
        self,
        token: str,
        cookies: str = "",
        device_id: str = "",
        user_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.token = (token or "").strip()
        if self.token.lower().startswith("bearer "):
            self.token = self.token[7:].strip()
        self.cookies = (cookies or "").strip()
        self.device_id = _device_id(device_id)
        self.user_id = (user_id or "").strip() or zai_user_id_from_token(self.token)
        if not self.user_id:
            raise ValueError("z.ai 账号需要 user_id，且无法从 token 自动提取")
        self.proxy = proxy
        self.impersonate = impersonate
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )
        self._lock = threading.Lock()
        self._pending_messages: dict[str, tuple[str, str]] = {}
        self._base_headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "Sec-CH-UA": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-FE-Version": FE_VERSION,
            "X-Region": "domestic",
            "x-device-id": self.device_id,
        }
        merged = _merge_cookies(self.cookies, self.token)
        if merged:
            self._base_headers["Cookie"] = merged

    def create_session(
        self,
        prompt: str = "",
        *,
        model: str = "",
        thinking_enabled: bool = False,
        search_enabled: bool = False,
    ) -> str:
        model_id = zai_upstream_model(model or zai_model_name())
        user_message_id = str(uuid.uuid4())
        assistant_message_id = str(uuid.uuid4())
        timestamp_secs = int(time.time())
        timestamp_ms = int(time.time() * 1000)
        title = prompt.strip().splitlines()[0][:30] if prompt.strip() else "New Chat"
        payload = {
            "chat": {
                "id": "",
                "title": title,
                "models": [model_id],
                "params": {},
                "history": {
                    "messages": {
                        user_message_id: {
                            "id": user_message_id,
                            "parentId": None,
                            "childrenIds": [],
                            "role": "user",
                            "content": prompt,
                            "timestamp": timestamp_secs,
                            "models": [model_id],
                        }
                    },
                    "currentId": user_message_id,
                },
                "tags": [],
                "flags": ["general_agent"],
                "features": [
                    {
                        "server": "web_search_h",
                        "status": "hidden",
                        "type": "web_search",
                    },
                    {
                        "server": "tool_selector_h",
                        "status": "hidden",
                        "type": "tool_selector",
                    },
                    {
                        "server": "hidden-thinking",
                        "status": "hidden",
                        "type": "hidden-thinking",
                    },
                ],
                "mcp_servers": [],
                "enable_thinking": bool(thinking_enabled),
                **(
                    {"reasoning_effort": "max"}
                    if thinking_enabled
                    else {}
                ),
                "auto_web_search": bool(search_enabled),
                "message_version": 1,
                "extra": {},
                "timestamp": timestamp_ms,
                "type": "general_agent",
            }
        }
        headers = {
            **self._request_headers(with_signature=False),
            "Accept": "application/json",
        }
        response = self._client.post(
            f"{BASE_URL}{CHAT_CREATE_PATH}",
            headers=headers,
            json=payload,
        )
        if response.status_code in {401, 403}:
            raise ZaiAuthError("z.ai token/cookie 已失效")
        if response.status_code == 429:
            raise RateLimitError("z.ai upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"z.ai chat create HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"z.ai chat create HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise ZaiProtocolError("z.ai chat create returned invalid JSON") from exc
        data_obj = data if isinstance(data, dict) else {}
        chat = data_obj.get("chat")
        session_id = str(
            data_obj.get("id")
            or (chat.get("id") if isinstance(chat, dict) else "")
            or ""
        ).strip()
        if not session_id:
            raise ZaiProtocolError("z.ai chat create response did not contain an id")
        with self._lock:
            self._pending_messages[session_id] = (
                assistant_message_id,
                user_message_id,
            )
        return session_id

    def check_health(self) -> bool:
        response = self._client.get(
            f"{BASE_URL}{HEALTH_PATH}",
            headers={
                **self._request_headers(with_signature=False),
                "Accept": "application/json",
            },
        )
        if response.status_code in {401, 403}:
            raise ZaiAuthError("z.ai token/cookie 已失效")
        response.raise_for_status()
        return True

    def _request_headers(
        self,
        *,
        with_signature: bool,
        request_id: str = "",
        timestamp: int = 0,
        prompt: str = "",
    ) -> dict[str, str]:
        headers = dict(self._base_headers)
        headers["Authorization"] = f"Bearer {self.token}"
        if with_signature:
            headers["X-Signature"] = _signature(
                prompt,
                self.user_id,
                request_id,
                timestamp,
            )
        return headers

    def _request_url(
        self,
        request_id: str,
        timestamp: int,
        session_id: str,
    ) -> str:
        params = _fingerprint_params(
            token=self.token,
            user_id=self.user_id,
            request_id=request_id,
            timestamp=timestamp,
            session_id=session_id,
        )
        query = "&".join(
            f"{k}={quote(str(v), safe='')}"
            for k, v in params.items()
        )
        return f"{BASE_URL}{COMPLETION_PATH}?{query}"

    def _request_body(
        self,
        *,
        session_id: str,
        prompt: str,
        model: str,
        thinking: bool,
        search: bool,
        assistant_message_id: str,
        user_message_id: str,
    ) -> dict[str, Any]:
        now = _datetime.datetime.now().astimezone()
        return {
            "stream": True,
            "model": model or zai_model_name(),
            "messages": [{"role": "user", "content": prompt}],
            "signature_prompt": prompt,
            "params": {},
            "extra": {},
            "features": {
                "image_generation": False,
                "web_search": bool(search),
                "auto_web_search": False,
                "preview_mode": False,
                "flags": ["general_agent"],
                "vlm_tools_enable": False,
                "vlm_web_search_enable": False,
                "vlm_website_mode": False,
                "enable_thinking": bool(thinking),
                **({"reasoning_effort": "max"} if thinking else {}),
            },
            "variables": {
                "{{USER_NAME}}": "",
                "{{USER_LOCATION}}": "Unknown",
                "{{CURRENT_DATETIME}}": now.strftime("%Y-%m-%d %H:%M:%S"),
                "{{CURRENT_DATE}}": now.strftime("%Y-%m-%d"),
                "{{CURRENT_TIME}}": now.strftime("%H:%M:%S"),
                "{{CURRENT_WEEKDAY}}": now.strftime("%A"),
                "{{CURRENT_TIMEZONE}}": "Asia/Shanghai",
                "{{USER_LANGUAGE}}": "zh-CN",
            },
            "chat_id": session_id,
            "id": assistant_message_id,
            "current_user_message_id": user_message_id,
            "current_user_message_parent_id": None,
            "background_tasks": {
                "title_generation": False,
                "tags_generation": False,
            },
        }

    def _open_stream(
        self,
        *,
        session_id: str,
        prompt: str,
        model: str,
        thinking: bool,
        search: bool,
    ):
        model = zai_upstream_model(model or zai_model_name())
        request_id = str(uuid.uuid4())
        with self._lock:
            message_ids = self._pending_messages.pop(session_id, None)
        assistant_message_id, user_message_id = (
            message_ids
            if message_ids is not None
            else (str(uuid.uuid4()), str(uuid.uuid4()))
        )
        timestamp = int(time.time() * 1000)
        headers = self._request_headers(
            with_signature=True,
            request_id=request_id,
            timestamp=timestamp,
            prompt=prompt,
        )
        body = self._request_body(
            session_id=session_id,
            prompt=prompt,
            model=model,
            thinking=thinking,
            search=search,
            assistant_message_id=assistant_message_id,
            user_message_id=user_message_id,
        )
        response = self._client.post(
            self._request_url(request_id, timestamp, session_id),
            headers=headers,
            json=body,
            stream=True,
            timeout=stream_timeout(),
        )
        if response.status_code == 401:
            raise ZaiAuthError("z.ai token/cookie 已失效")
        if response.status_code == 429:
            raise RateLimitError("z.ai upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"z.ai upstream HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"z.ai upstream HTTP {response.status_code}: {response.text[:300]}"
            )
        return response

    def _iter_events(self, response):
        for line in response.iter_lines():
            if isinstance(line, (bytes, bytearray)):
                line = line.decode("utf-8", errors="replace")
            line = str(line).strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"error", "chat:error"}:
                raise UpstreamHintError(
                    f"z.ai upstream error: {event.get('message') or event.get('data')}"
                )
            data = event.get("data")
            if isinstance(data, dict) and event.get("type") == "chat:completion":
                error = data.get("error")
                if isinstance(error, dict):
                    raise UpstreamHintError(
                        "z.ai completion error: "
                        f"{error.get('code') or 'unknown'}: "
                        f"{error.get('detail') or error.get('message') or ''}"
                    )
                yield data

    def chat(
        self,
        session_id: str,
        prompt: str,
        *,
        model_type: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        parent_message_id: int | None = None,
        ready_out: dict | None = None,
        upstream_model: str | None = None,
    ) -> tuple[str, str]:
        response = self._open_stream(
            session_id=session_id,
            prompt=prompt,
            model=upstream_model or zai_model_name(),
            thinking=thinking_enabled,
            search=search_enabled,
        )
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        done = False
        try:
            try:
                for data in self._iter_events(response):
                    delta = data.get("delta_content")
                    phase = data.get("phase")
                    if isinstance(delta, str) and delta:
                        if phase == "thinking":
                            thinking_parts.append(delta)
                        elif phase in {"answer", "final"}:
                            content_parts.append(delta)
                    if data.get("done") is True or phase == "done":
                        done = True
                        break
            except RequestException:
                raise UpstreamTransientError(
                    "z.ai stream was interrupted before completion"
                ) from None
        finally:
            response.close()
        if not content_parts and not thinking_parts:
            raise UpstreamTransientError("z.ai returned an empty response")
        if not done:
            raise UpstreamTransientError(
                "z.ai stream ended before its done event"
            )
        return "".join(content_parts), "".join(thinking_parts)

    def chat_stream(
        self,
        session_id: str,
        prompt: str,
        *,
        model_type: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        parent_message_id: int | None = None,
        ready_out: dict | None = None,
        upstream_model: str | None = None,
    ):
        response = self._open_stream(
            session_id=session_id,
            prompt=prompt,
            model=upstream_model or zai_model_name(),
            thinking=thinking_enabled,
            search=search_enabled,
        )
        try:
            yielded = False
            done = False
            try:
                for data in self._iter_events(response):
                    delta = data.get("delta_content")
                    phase = data.get("phase")
                    if isinstance(delta, str) and delta:
                        yielded = True
                        if phase == "thinking":
                            yield {"__type": "thinking", "content": delta}
                        elif phase in {"answer", "final"}:
                            yield delta
                    if data.get("done") is True or phase == "done":
                        done = True
                        break
            except RequestException:
                raise UpstreamTransientError(
                    "z.ai stream was interrupted before completion"
                ) from None
            if not yielded:
                raise UpstreamTransientError("z.ai returned an empty stream")
            if not done:
                raise UpstreamTransientError(
                    "z.ai stream ended before its done event"
                )
        finally:
            response.close()
