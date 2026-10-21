"""Adapter for Kimi web chat's Connect JSON streaming protocol."""
from __future__ import annotations

import base64
import gzip
import json
import os
import struct
import time
import uuid
from typing import Any

from curl_cffi import requests as cffi_requests

from adapter import (
    RateLimitError,
    UpstreamHintError,
    UpstreamTransientError,
)
from provider_registry import kimi_upstream_model, kimi_upstream_model_name

BASE_URL = "https://www.kimi.com"
AUTH_BASE_URL = "https://auth.kimi.com/api"
CHAT_PATH = "/apiv2/kimi.gateway.chat.v1.ChatService/Chat"
HEALTH_PATH = (
    "/apiv2/kimi.gateway.notification.v1.NotificationService/GetUnreadCount"
)
REFRESH_PATH = "/account.gateway.v1.AuthService/RefreshToken"
CLIENT_VERSION = os.environ.get("KIMI_CLIENT_VERSION", "2.3.0")
IMPERSONATE = os.environ.get("KIMI_IMPERSONATE", "chrome150")


class KimiAuthError(RuntimeError):
    """Kimi access/refresh token was rejected."""


class KimiProtocolError(RuntimeError):
    """Kimi returned an invalid Connect response."""


def kimi_token_claims(token: str) -> dict[str, Any]:
    value = (token or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    parts = value.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(
            base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def kimi_device_id_from_token(token: str) -> str:
    return str(kimi_token_claims(token).get("device_id") or "").strip()


def kimi_user_id_from_token(token: str) -> str:
    claims = kimi_token_claims(token)
    return str(
        claims.get("sub")
        or claims.get("abstract_user_id")
        or ""
    ).strip()


def _connect_frame(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return b"\x00" + struct.pack(">I", len(raw)) + raw


def _iter_connect_events(response):
    buffer = b""
    for chunk in response.iter_content():
        if not chunk:
            continue
        buffer += chunk
        while len(buffer) >= 5:
            flags = buffer[0]
            length = int.from_bytes(buffer[1:5], "big")
            if length > 16 * 1024 * 1024:
                raise KimiProtocolError(f"Kimi frame too large: {length}")
            if len(buffer) < 5 + length:
                break
            payload = buffer[5 : 5 + length]
            buffer = buffer[5 + length :]
            if flags & 0x02:
                continue
            if flags & 0x01:
                try:
                    payload = gzip.decompress(payload)
                except OSError as exc:
                    raise KimiProtocolError("Kimi gzip frame is invalid") from exc
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


class KimiAdapter:
    def __init__(
        self,
        token: str,
        refresh_token: str = "",
        cookies: str = "",
        device_id: str = "",
        user_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.access_token = (token or "").strip()
        if self.access_token.lower().startswith("bearer "):
            self.access_token = self.access_token[7:].strip()
        self.refresh_token = (refresh_token or "").strip()
        self.cookies = (cookies or "").strip()
        self.device_id = (
            (device_id or "").strip()
            or kimi_device_id_from_token(self.access_token)
        )
        self.user_id = (
            (user_id or "").strip()
            or kimi_user_id_from_token(self.access_token)
        )
        if not self.access_token:
            raise ValueError("Kimi access token is required")
        if not self.refresh_token:
            raise ValueError("Kimi refresh token is required")
        if not self.device_id:
            raise ValueError("Kimi device_id is required")
        self.proxy = proxy
        self.impersonate = impersonate
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )

    def create_session(self) -> str:
        # Kimi has no separate create-session call; each Chat request creates a
        # server-side conversation.
        return str(uuid.uuid4())

    def _token_exp(self) -> int:
        try:
            return int(kimi_token_claims(self.access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    def _refresh_access_token(self, *, force: bool = False) -> None:
        if not self.refresh_token:
            raise KimiAuthError("Kimi refresh token is missing")
        if not force and self._token_exp() > int(time.time()) + 60:
            return
        response = self._client.post(
            AUTH_BASE_URL + REFRESH_PATH,
            headers={
                **self._common_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"refreshToken": self.refresh_token},
        )
        if response.status_code in {400, 401, 403}:
            raise KimiAuthError("Kimi refresh token 已失效")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"Kimi token refresh HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"Kimi token refresh HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        data = response.json()
        access = str(data.get("accessToken") or "").strip()
        refresh = str(data.get("refreshToken") or "").strip()
        if not access or not refresh:
            raise KimiProtocolError("Kimi refresh response is incomplete")
        self.access_token = access
        self.refresh_token = refresh

    def _common_headers(self) -> dict[str, str]:
        cookie = (self.cookies or "").strip()
        headers = {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "R-Timezone": "Asia/Shanghai",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "X-Language": "zh-CN",
            "x-msh-device-id": self.device_id,
            "x-msh-platform": "web",
            "x-msh-version": CLIENT_VERSION,
            "x-traffic-id": self.user_id,
            "Authorization": f"Bearer {self.access_token}",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _raise_response(self, response, context: str) -> None:
        if response.status_code == 401:
            raise KimiAuthError("Kimi access token 已失效")
        if response.status_code == 429:
            raise RateLimitError("Kimi upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"Kimi {context} HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"Kimi {context} HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

    def check_health(self) -> bool:
        self._refresh_access_token()
        response = self._client.post(
            BASE_URL + HEALTH_PATH,
            headers={
                **self._common_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={},
        )
        if response.status_code == 401:
            self._refresh_access_token(force=True)
            response = self._client.post(
                BASE_URL + HEALTH_PATH,
                headers={
                    **self._common_headers(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={},
            )
        self._raise_response(response, "health")
        return response.status_code == 200

    def _request_body(
        self,
        prompt: str,
        *,
        model: str,
        thinking: bool,
        search: bool,
    ) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        if search:
            tools.append({"type": "TOOL_TYPE_SEARCH", "search": {}})
        return {
            "scenario": "SCENARIO_CHAT",
            "tools": tools,
            "message": {
                "role": "user",
                "blocks": [
                    {
                        "message_id": "",
                        "text": {"content": prompt},
                    }
                ],
                "scenario": "SCENARIO_CHAT",
                "is_goal": False,
            },
            "options": {
                "thinking": bool(thinking),
                "enable_plugin": bool(search),
                "reasoning_effort": (
                    "REASONING_EFFORT_HIGH"
                    if thinking
                    else "REASONING_EFFORT_LOW"
                ),
                "model": kimi_upstream_model(model or kimi_upstream_model_name()),
            },
            "project_id": "",
        }

    def _open_chat(
        self,
        prompt: str,
        *,
        model: str,
        thinking: bool,
        search: bool,
    ):
        self._refresh_access_token()
        headers = {
            **self._common_headers(),
            "Accept": "application/connect+json",
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/connect+json",
        }
        body = _connect_frame(
            self._request_body(
                prompt,
                model=model,
                thinking=thinking,
                search=search,
            )
        )
        response = self._client.post(
            BASE_URL + CHAT_PATH,
            headers=headers,
            data=body,
            stream=True,
        )
        if response.status_code == 401:
            response.close()
            self._refresh_access_token(force=True)
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self._client.post(
                BASE_URL + CHAT_PATH,
                headers=headers,
                data=body,
                stream=True,
            )
        self._raise_response(response, "chat")
        return response

    @staticmethod
    def _event_chunks(event: dict[str, Any]) -> list[tuple[str, str]]:
        mask = str(event.get("mask") or "")
        op = str(event.get("op") or "")
        block = event.get("block")
        if not isinstance(block, dict):
            return []
        if mask == "block.think":
            think = block.get("think")
            if op == "set" and isinstance(think, dict):
                text = think.get("content")
                return [("thinking", text)] if isinstance(text, str) and text else []
            return []
        if mask == "block.think.content":
            think = block.get("think")
            if isinstance(think, dict):
                text = think.get("content")
                return [("thinking", text)] if isinstance(text, str) and text else []
            return []
        if mask == "block.text":
            text_block = block.get("text")
            if op == "set" and isinstance(text_block, dict):
                text = text_block.get("content")
                return [("answer", text)] if isinstance(text, str) and text else []
            return []
        if mask == "block.text.content":
            text_block = block.get("text")
            if isinstance(text_block, dict):
                text = text_block.get("content")
                return [("answer", text)] if isinstance(text, str) and text else []
        return []

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
        for attempt in range(2):
            response = self._open_chat(
                prompt,
                model=upstream_model or kimi_upstream_model_name(),
                thinking=thinking_enabled,
                search=search_enabled,
            )
            yielded = False
            try:
                for event in _iter_connect_events(response):
                    if "done" in event:
                        break
                    for kind, text in self._event_chunks(event):
                        yielded = True
                        if kind == "thinking":
                            yield {"__type": "thinking", "content": text}
                        else:
                            yield text
            finally:
                response.close()
            if yielded:
                return
            if attempt == 0:
                time.sleep(0.4)
        raise UpstreamTransientError("Kimi returned an empty stream")

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
        answer: list[str] = []
        thinking: list[str] = []
        for item in self.chat_stream(
            session_id,
            prompt,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            parent_message_id=parent_message_id,
            ready_out=ready_out,
            upstream_model=upstream_model,
        ):
            if isinstance(item, dict) and item.get("__type") == "thinking":
                thinking.append(str(item.get("content") or ""))
            else:
                answer.append(str(item))
        return "".join(answer), "".join(thinking)
