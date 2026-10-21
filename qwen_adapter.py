"""Adapter for the Qianwen web chat SSE protocol."""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from curl_cffi import requests as cffi_requests

from adapter import RateLimitError, UpstreamHintError, UpstreamTransientError
from provider_registry import qwen_model_name, qwen_upstream_model

CHAT_URL = "https://chat2.qianwen.com/api/v2/chat"
SESSION_URL = "https://chat2-api.qianwen.com/api/v1/session/get"
IMPERSONATE = os.environ.get("QWEN_IMPERSONATE", "chrome150")


class QwenAuthError(RuntimeError):
    pass


class QwenAdapter:
    def __init__(
        self,
        token: str = "",
        cookies: str = "",
        device_id: str = "",
        user_id: str = "",
        xsrf_token: str = "",
        session_id: str = "",
        topic_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.token = (token or "").strip()
        self.cookies = (cookies or "").strip()
        self.device_id = (device_id or "").strip()
        self.user_id = (user_id or "").strip()
        self.xsrf_token = (xsrf_token or "").strip()
        self.session_id = (session_id or "").strip()
        self.topic_id = (topic_id or "").strip()
        if not self.cookies:
            raise ValueError("千问账号需要 cookies")
        if not self.device_id or not self.user_id:
            raise ValueError("千问账号需要 device_id 和 user_id")
        if not self.session_id:
            raise ValueError("千问账号需要 session_id")
        self.proxy = proxy
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )

    def create_session(self) -> str:
        return self.session_id

    def _query(self) -> dict[str, str]:
        return {
            "biz_id": "ai_qwen",
            "chat_client": "h5",
            "device": "pc",
            "fr": "pc",
            "pr": "qwen",
            "ut": self.device_id,
            "la": "zh-CN",
            "tz": "Asia/Shanghai",
            "wv": "4.10.7",
            "tq": "SD",
            "ve": "4.10.7",
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Cookie": self.cookies,
            "Origin": "https://www.qianwen.com",
            "Referer": "https://www.qianwen.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "x-deviceid": self.device_id,
            "x-platform": "pc_tongyi",
            "x-user-id": self.user_id,
        }
        if self.xsrf_token:
            headers["x-xsrf-token"] = self.xsrf_token
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _raise(self, response, context: str) -> None:
        if response.status_code in {401, 403}:
            raise QwenAuthError("千问 Cookie / XSRF 已失效")
        if response.status_code == 429:
            raise RateLimitError("千问 upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"千问 {context} HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"千问 {context} HTTP {response.status_code}: {response.text[:300]}"
            )

    def _load_session(self) -> None:
        response = self._client.post(
            SESSION_URL,
            params=self._query(),
            headers=self._headers(),
            json={"session_id": self.session_id},
        )
        self._raise(response, "session")
        data = response.json()
        topic_id = str(
            ((data.get("data") or {}).get("topic_id") or "")
        ).strip()
        if topic_id:
            self.topic_id = topic_id

    def check_health(self) -> bool:
        self._load_session()
        return True

    def _body(
        self,
        prompt: str,
        *,
        model: str,
        thinking: bool,
        search: bool,
    ) -> dict[str, Any]:
        req_id = uuid.uuid4().hex
        return {
            "req_id": req_id,
            "parent_req_id": "0",
            "messages": [
                {
                    "mime_type": "text/plain",
                    "content": prompt,
                    "meta_data": {"ori_query": prompt},
                    "status": "complete",
                }
            ],
            "scene": "chat",
            "sub_scene": "",
            "scene_param": "first_turn",
            "session_id": self.session_id,
            "biz_id": "ai_qwen",
            "topic_id": self.topic_id,
            "model": qwen_upstream_model(model or qwen_model_name()),
            "from": "pc",
            "protocol_version": "v2",
            "messages_merge": False,
            "chat_client": "h5",
            "deep_search": True if search else None,
            "temporary": False,
            "params_extra": {"supports_cowork": "true"},
            "biz_data": json.dumps({"is_preference_exp": "1"}),
            "chat_mode": "quick",
            "cms_test_data_ids": "",
            "bucket": {},
        }

    def _iter_sse(self, response):
        data_lines = []
        for raw_line in response.iter_lines():
            if isinstance(raw_line, (bytes, bytearray)):
                line = raw_line.decode("utf-8", errors="replace")
            else:
                line = str(raw_line)
            if not line:
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            try:
                event = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                yield event

    def _open(self, prompt: str, *, model: str, thinking: bool, search: bool):
        if not self.topic_id:
            self._load_session()
        params = {
            **self._query(),
            "nonce": uuid.uuid4().hex[:11],
            "timestamp": str(int(time.time() * 1000)),
        }
        response = self._client.post(
            CHAT_URL,
            params=params,
            headers=self._headers(),
            json=self._body(
                prompt,
                model=model,
                thinking=thinking,
                search=search,
            ),
            stream=True,
        )
        self._raise(response, "chat")
        return response

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
        response = self._open(
            prompt,
            model=upstream_model or qwen_model_name(),
            thinking=thinking_enabled,
            search=search_enabled,
        )
        try:
            answer = ""
            thinking = ""
            for event in self._iter_sse(response):
                data = event.get("data") or {}
                if not event.get("success", True):
                    raise UpstreamHintError(str(event.get("error_msg") or "千问错误"))
                for message in data.get("messages") or []:
                    content = message.get("content")
                    mime = str(message.get("mime_type") or "")
                    if not isinstance(content, str) or not content:
                        continue
                    if "think" in mime.lower() or "reason" in mime.lower():
                        delta = content[len(thinking):] if content.startswith(thinking) else content
                        thinking = content
                        if delta:
                            yield {"__type": "thinking", "content": delta}
                    else:
                        delta = content[len(answer):] if content.startswith(answer) else content
                        answer = content
                        if delta:
                            yield delta
                if data.get("status") == "complete":
                    break
            if not answer and not thinking:
                raise UpstreamTransientError("千问返回空响应")
        finally:
            response.close()

    def chat(self, *args, **kwargs) -> tuple[str, str]:
        answer = []
        reasoning = []
        for item in self.chat_stream(*args, **kwargs):
            if isinstance(item, dict) and item.get("__type") == "thinking":
                reasoning.append(str(item.get("content") or ""))
            else:
                answer.append(str(item))
        return "".join(answer), "".join(reasoning)
