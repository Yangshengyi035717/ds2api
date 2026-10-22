"""Adapter for the Tencent Hunyuan AI Studio web chat SSE protocol."""
from __future__ import annotations

import json
import os
from typing import Any

from curl_cffi import requests as cffi_requests

from adapter import RateLimitError, UpstreamHintError, UpstreamTransientError
from provider_registry import hunyuan_model_name, hunyuan_upstream_model

BASE_URL = "https://api.hunyuan.tencent.com"
GENERATE_ID_PATH = "/api/new-portal/generate/id"
CONVERSATION_LIST_PATH = "/api/new-portal/chat/conversation/list"
CHAT_PATH = "/api/new-portal/chat/{conversation_id}"
AGENT_ID = os.environ.get("HUNYUAN_AGENT_ID", "HunyuanDefault").strip() or "HunyuanDefault"
BASE_MODEL = os.environ.get("HUNYUAN_BASE_MODEL", "gpt_175B_0404").strip() or "gpt_175B_0404"
DEFAULT_EFFORT = (
    os.environ.get("HUNYUAN_DEFAULT_EFFORT", "no_think").strip() or "no_think"
)
IMPERSONATE = os.environ.get("HUNYUAN_IMPERSONATE", "chrome150")


class HunyuanAuthError(RuntimeError):
    """Hunyuan web cookies or account identity were rejected."""


def _cookie_map(cookies: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def hunyuan_cookie_value(cookies: str, name: str) -> str:
    return _cookie_map(cookies).get(name, "").strip()


def hunyuan_token_from_cookies(cookies: str) -> str:
    return hunyuan_cookie_value(cookies, "hunyuan_token")


def hunyuan_user_id_from_cookies(cookies: str) -> str:
    return hunyuan_cookie_value(cookies, "hunyuan_user")


class HunyuanAdapter:
    def __init__(
        self,
        token: str = "",
        cookies: str = "",
        user_id: str = "",
        device_id: str = "",
        session_id: str = "",
        topic_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.token = (token or "").strip()
        self.user_id = (user_id or "").strip()
        self.device_id = (device_id or "").strip()
        self.session_id = (session_id or "").strip()
        self.topic_id = (topic_id or "").strip()

        cookie_values = _cookie_map(cookies)
        self.token = self.token or cookie_values.get("hunyuan_token", "").strip()
        self.user_id = self.user_id or cookie_values.get("hunyuan_user", "").strip()
        if self.token:
            cookie_values["hunyuan_token"] = self.token
        if self.user_id:
            cookie_values["hunyuan_user"] = self.user_id
        cookie_values.setdefault("hunyuan_source", "web")
        self.cookies = _cookie_header(cookie_values)
        if not self.token or not self.user_id:
            raise ValueError("Hunyuan 账号需要包含 hunyuan_token 和 hunyuan_user 的 Cookie")
        if not self.cookies:
            raise ValueError("Hunyuan 账号需要 Cookie")

        self.proxy = proxy
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )

    def _headers(
        self,
        *,
        content_type: str = "application/json",
        conversation_id: str = "",
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh",
            "Content-Type": content_type,
            "Cookie": self.cookies,
            "Origin": "https://aistudio.tencent.com",
            "Referer": "https://aistudio.tencent.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "X-Source": "web",
        }
        if conversation_id:
            headers["X-AgentID"] = f"{AGENT_ID}/{conversation_id}"
            headers["chat_version"] = "v1"
        return headers

    @staticmethod
    def _json_body(response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _raise_response(
        self,
        response,
        context: str,
        *,
        parse_json: bool = True,
    ) -> None:
        if response.status_code in {401, 403}:
            raise HunyuanAuthError("Hunyuan Cookie 已失效，请重新导入")
        if response.status_code == 429:
            raise RateLimitError("Hunyuan upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"Hunyuan {context} HTTP {response.status_code}: {response.text[:200]}"
            )
        if not parse_json and response.status_code < 400:
            return
        body = self._json_body(response)
        error = body.get("error")
        code = body.get("code")
        message = str(
            body.get("message")
            or body.get("msg")
            or (error.get("message") if isinstance(error, dict) else "")
            or ""
        ).strip()
        if (
            isinstance(error, dict)
            and str(error.get("code") or "").strip() in {"401", "403", "999"}
        ) or str(code or "").strip() in {"401", "403", "999"}:
            raise HunyuanAuthError(message or "Hunyuan Cookie 已失效")
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"Hunyuan {context} HTTP {response.status_code}: "
                f"{(message or response.text)[:300]}"
            )
        if code not in (None, 0, 200, "0", "200"):
            raise UpstreamHintError(f"Hunyuan {context}: {message or code}")

    def create_session(self) -> str:
        response = self._client.post(
            BASE_URL + GENERATE_ID_PATH,
            headers=self._headers(),
            json={},
        )
        self._raise_response(response, "generate-id")
        data = self._json_body(response)
        conversation_id = str(data.get("id") or "").strip()
        if not conversation_id:
            raise UpstreamTransientError("Hunyuan did not return a conversation id")
        return conversation_id

    def check_health(self) -> bool:
        response = self._client.post(
            BASE_URL + CONVERSATION_LIST_PATH,
            headers=self._headers(),
            json={"limit": 1, "offset": 0, "modelTag": "all"},
        )
        self._raise_response(response, "conversation-list")
        return True

    def _request_body(
        self,
        prompt: str,
        *,
        model: str,
        thinking: bool,
        search: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": BASE_MODEL,
            "prompt": prompt,
            "plugin": "Adaptive",
            "displayPrompt": prompt,
            "displayPromptType": 1,
            "options": {
                "imageIntention": {
                    "needIntentionModel": True,
                    "backendUpdateFlag": 2,
                }
            },
            "targetLang": None,
            "targetLangLabel": None,
            "sourceLang": None,
            "sourceLangLabel": None,
            "translateModelList": [],
            "podcast": {"voices": []},
            "effort": "high" if thinking else DEFAULT_EFFORT,
            "displayImageIntentionLabels": [],
            "multimedia": [],
            "agentId": AGENT_ID,
            "customParams": {
                "temperature": 0.9,
                "top_p": 1,
                "repetition_penalty": 1,
                "max_output_tokens": 65536,
            },
            "supportHint": 1,
            "version": "v2",
            "chatModelId": hunyuan_upstream_model(model or hunyuan_model_name()),
        }
        if search:
            body["supportFunctions"] = ["supportInternetSearch"]
        return body

    def _open(
        self,
        conversation_id: str,
        prompt: str,
        *,
        model: str,
        thinking: bool,
        search: bool,
    ):
        response = self._client.post(
            BASE_URL + CHAT_PATH.format(conversation_id=conversation_id),
            headers=self._headers(
                content_type="text/plain;charset=UTF-8",
                conversation_id=conversation_id,
            ),
            data=json.dumps(
                self._request_body(
                    prompt,
                    model=model,
                    thinking=thinking,
                    search=search,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            stream=True,
        )
        response_headers = getattr(response, "headers", {}) or {}
        content_type = str(response_headers.get("content-type") or "").lower()
        self._raise_response(
            response,
            "chat",
            parse_json="application/json" in content_type,
        )
        return response

    @staticmethod
    def _iter_sse(response):
        event_name = ""
        data_lines: list[str] = []
        for raw_line in response.iter_lines():
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line)
            )
            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = ""
                data_lines = []
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield event_name, "\n".join(data_lines)

    @staticmethod
    def _raise_stream_error(message: str) -> None:
        text = str(message or "").strip() or "Hunyuan upstream error"
        lowered = text.lower()
        if any(token in text for token in ("限额", "频繁", "请求过多")) or any(
            token in lowered for token in ("rate limit", "quota", "too many")
        ):
            raise RateLimitError(text, "rate_limit_reached")
        if any(token in lowered for token in ("cookie", "unauthorized", "forbidden")):
            raise HunyuanAuthError(text)
        raise UpstreamHintError(text)

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
            session_id,
            prompt,
            model=upstream_model or hunyuan_model_name(),
            thinking=thinking_enabled,
            search=search_enabled,
        )
        answer_seen = False
        thinking_seen = False
        channel = "answer"
        try:
            for event_name, payload in self._iter_sse(response):
                if event_name == "error":
                    self._raise_stream_error(payload)
                if event_name == "speech_type":
                    value = payload.strip().lower()
                    if value.startswith("thought"):
                        channel = "thinking"
                    elif value.startswith("text"):
                        channel = "answer"
                    continue
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or "").strip().lower()
                if event_type == "error":
                    self._raise_stream_error(
                        str(event.get("msg") or event.get("message") or "")
                    )
                if event_type in {"meta", "hint", "usage", "replace"}:
                    continue
                if event_type in {"thought", "think", "reasoning"}:
                    text = event.get("msg")
                    if isinstance(text, str) and text:
                        thinking_seen = True
                        yield {"__type": "thinking", "content": text}
                    continue
                if event_type != "text":
                    continue
                text = event.get("msg")
                if not isinstance(text, str) or not text:
                    continue
                if channel == "thinking":
                    thinking_seen = True
                    yield {"__type": "thinking", "content": text}
                else:
                    answer_seen = True
                    yield text
            if not answer_seen and not thinking_seen:
                raise UpstreamTransientError("Hunyuan returned an empty stream")
        finally:
            response.close()

    def chat(self, *args, **kwargs) -> tuple[str, str]:
        answer: list[str] = []
        reasoning: list[str] = []
        for item in self.chat_stream(*args, **kwargs):
            if isinstance(item, dict) and item.get("__type") == "thinking":
                reasoning.append(str(item.get("content") or ""))
            else:
                answer.append(str(item))
        return "".join(answer), "".join(reasoning)
