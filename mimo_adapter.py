"""Adapter for Xiaomi MiMo Studio's Cookie-authenticated chat SSE."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException
from upstream_http import stream_timeout

from adapter import (
    RateLimitError,
    UpstreamContextLimitError,
    UpstreamHintError,
    UpstreamTransientError,
)
from provider_registry import mimo_model_name, mimo_upstream_model

BASE_URL = "https://aistudio.xiaomimimo.com"
COOKIE_DOMAIN = "aistudio.xiaomimimo.com"
HEALTH_URL = f"{BASE_URL}/open-apis/chat/conversation/list"
CREATE_URL = f"{BASE_URL}/open-apis/chat/conversation/save"
COMPLETION_URL = f"{BASE_URL}/open-apis/react/chat"
FASTCHAT_COMPLETION_URL = f"{BASE_URL}/fastchat/open-apis/bot/chat"
FASTCHAT_MODELS = frozenset({"mimo-v2.6-pro-ultraspeed-studio"})
ULTRASPEED_FALLBACK_MODEL = os.environ.get(
    "MIMO_ULTRASPEED_FALLBACK_MODEL",
    "mimo-v2.6-pro",
).strip()
IMPERSONATE = os.environ.get("MIMO_IMPERSONATE", "chrome150")
REQUIRED_COOKIES = ("userId", "xiaomichatbot_serviceToken", "xiaomichatbot_ph")


class MimoAuthError(RuntimeError):
    """MiMo rejected the browser login cookies."""


def _cookie_items(cookies: str):
    for part in str(cookies or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if name:
            yield name, value


def mimo_cookie_value(cookies: str, name: str) -> str:
    # Cookie values are not form data: literal '+' and '=' must survive.
    return dict(_cookie_items(cookies)).get(name, "")


def mimo_user_id_from_cookies(cookies: str) -> str:
    return mimo_cookie_value(cookies, "userId")


def mimo_validate_cookies(cookies: str) -> None:
    if any(character in str(cookies or "") for character in ("\r", "\n", "\0")):
        raise ValueError("MiMo Cookie 不能包含换行或控制字符")
    missing = [name for name in REQUIRED_COOKIES if not mimo_cookie_value(cookies, name)]
    if missing:
        raise ValueError(f"MiMo 账号 Cookie 缺少：{', '.join(missing)}")


class _MimoThinkTagParser:
    """Split UltraSpeed's inline <think> stream without leaking tag bytes."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._mode = "prefix"
        self._buffer = ""

    def feed(self, text: Any) -> list[tuple[str, str]]:
        if not isinstance(text, str) or not text:
            return []
        # UltraSpeed emits NUL after opening and closing think tags.
        self._buffer += text.replace("\x00", "")
        output: list[tuple[str, str]] = []
        while self._buffer:
            if self._mode == "prefix":
                if self._buffer.startswith(self.OPEN):
                    self._buffer = self._buffer[len(self.OPEN):]
                    self._mode = "reasoning"
                    continue
                if self.OPEN.startswith(self._buffer):
                    break
                self._mode = "answer"
                continue
            if self._mode == "reasoning":
                index = self._buffer.find(self.CLOSE)
                if index < 0:
                    held = 0
                    for length in range(len(self.CLOSE) - 1, 0, -1):
                        if self._buffer.endswith(self.CLOSE[:length]):
                            held = length
                            break
                    emit = len(self._buffer) - held
                    if emit > 0:
                        output.append(("thinking", self._buffer[:emit]))
                        self._buffer = self._buffer[emit:]
                    break
                if index > 0:
                    output.append(("thinking", self._buffer[:index]))
                self._buffer = self._buffer[index + len(self.CLOSE):]
                self._mode = "answer"
                continue
            if self._buffer:
                output.append(("answer", self._buffer))
                self._buffer = ""
        return output

    def flush(self) -> list[tuple[str, str]]:
        if not self._buffer:
            return []
        kind = "thinking" if self._mode in {"prefix", "reasoning"} else "answer"
        output = [(kind, self._buffer)]
        self._buffer = ""
        return output


class MimoAdapter:
    def __init__(
        self,
        token: str = "",
        cookies: str = "",
        device_id: str = "",
        user_id: str = "",
        session_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        mimo_validate_cookies(cookies)
        self.token = token or ""
        self.cookies = cookies.strip()
        self.device_id = device_id or ""
        self.user_id = mimo_user_id_from_cookies(cookies)
        self.session_id = ""
        self.proxy = proxy
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )
        for name, value in _cookie_items(cookies):
            self._client.cookies.set(name, value, domain=COOKIE_DOMAIN, path="/")

    def _sync_cookies(self) -> None:
        values = {
            cookie.name: cookie.value
            for cookie in self._client.cookies.jar
            if cookie.domain.lstrip(".") in {COOKIE_DOMAIN, "xiaomimimo.com"}
            and not cookie.is_expired()
        }
        # Normalize rotated parent-domain cookies so old seeded values do not
        # produce duplicate Cookie names on the next request.
        self._client.cookies.clear()
        for name, value in values.items():
            self._client.cookies.set(name, value, domain=COOKIE_DOMAIN, path="/")
        self.cookies = "; ".join(f"{name}={value}" for name, value in values.items())
        self.user_id = mimo_user_id_from_cookies(self.cookies)

    def _query(self) -> dict[str, str]:
        return {"xiaomichatbot_ph": mimo_cookie_value(self.cookies, "xiaomichatbot_ph")}

    @staticmethod
    def _headers(*, stream: bool = False) -> dict[str, str]:
        return {
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "x-timezone": "Asia/Shanghai",
        }

    def _safe_message(self, value: Any) -> str:
        message = str(value or "")
        for name, secret in _cookie_items(self.cookies):
            if secret and (name in REQUIRED_COOKIES or len(secret) >= 8):
                message = message.replace(secret, "[redacted]")
                message = message.replace(quote(secret, safe=""), "[redacted]")
        return message[:300]

    def _raise_business_error(self, payload: dict, context: str) -> None:
        code = payload.get("code")
        if code in (None, 0, "0"):
            return
        self._raise_event_error(
            payload.get("msg") or payload.get("message") or "",
            code=code,
            context=context,
        )

    def _raise_event_error(
        self, message: Any, *, code: Any = None, context: str = "chat"
    ) -> None:
        text = self._safe_message(message)
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "query is too long",
                "input is too long",
                "context length",
                "maximum context",
                "prompt too long",
                "请求过长",
                "上下文过长",
            )
        ):
            raise UpstreamContextLimitError(text or "MiMo context limit exceeded")
        if str(code) in {"401", "403"} or any(
            marker in lowered for marker in ("未登录", "登录过期", "登录失效", "unauthorized")
        ):
            raise MimoAuthError("MiMo 登录 Cookie 已失效，请重新登录并导入")
        if str(code) == "429" or any(
            marker in lowered
            for marker in ("频繁", "限流", "配额", "额度", "too many", "rate limit", "quota")
        ):
            raise RateLimitError(
                text or "MiMo upstream rate limit", "rate_limit_reached"
            )
        if any(
            marker in lowered
            for marker in ("繁忙", "暂时不可用", "busy", "unavailable")
        ):
            raise UpstreamTransientError(text or "MiMo upstream unavailable")
        raise UpstreamHintError(f"MiMo {context}: {text or 'upstream rejected the request'}")

    def _request(self, url: str, body: dict, context: str, *, stream: bool = False):
        try:
            response = self._client.post(
                url,
                params=self._query(),
                headers=self._headers(stream=stream),
                json=body,
                stream=stream,
                timeout=stream_timeout() if stream else 120,
                allow_redirects=False,
            )
        except RequestException:
            # curl errors may include the credential-bearing query URL.
            raise UpstreamTransientError(f"MiMo {context} network request failed") from None
        self._sync_cookies()
        try:
            if response.status_code in {401, 403} or 300 <= response.status_code < 400:
                raise MimoAuthError("MiMo 登录 Cookie 已失效，请重新登录并导入")
            if response.status_code == 429:
                raise RateLimitError("MiMo upstream rate limit", "rate_limit_reached")
            if response.status_code >= 500:
                raise UpstreamTransientError(f"MiMo {context} HTTP {response.status_code}")
            if response.status_code >= 400:
                raise UpstreamHintError(f"MiMo {context} HTTP {response.status_code}")
            content_type = str(response.headers.get("content-type") or "").lower()
            if stream and "text/event-stream" not in content_type:
                payload = self._json_body(response)
                self._raise_business_error(payload, context)
                raise UpstreamTransientError("MiMo returned a non-SSE chat response")
            return response
        except Exception:
            response.close()
            raise

    @staticmethod
    def _json_body(response) -> dict:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise UpstreamTransientError("MiMo returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise UpstreamTransientError("MiMo returned an invalid response object")
        return payload

    def _request_json(self, url: str, body: dict, context: str) -> dict:
        response = self._request(url, body, context)
        try:
            payload = self._json_body(response)
            self._raise_business_error(payload, context)
            if payload.get("code") not in (0, "0"):
                raise UpstreamTransientError("MiMo response is missing its status code")
            return payload
        finally:
            response.close()

    def check_health(self) -> bool:
        # 只读探活：不创建会话、不生成内容，不把匿名 HTML 当作成功。
        payload = self._request_json(
            HEALTH_URL, {"pageInfo": {"pageNum": 1, "pageSize": 1}}, "health"
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("dataList"), list):
            raise UpstreamTransientError("MiMo health response is missing the conversation list")
        return True

    def create_session(self) -> str:
        # 每轮创建独立会话并发送完整上下文，不复用抓包中的会话 ID。
        conversation_id = uuid.uuid4().hex
        payload = self._request_json(
            CREATE_URL,
            {"conversationId": conversation_id, "title": "API conversation", "type": "chat"},
            "create conversation",
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("conversationId") != conversation_id:
            raise UpstreamTransientError("MiMo did not confirm the new conversation id")
        self.session_id = conversation_id
        return conversation_id

    @staticmethod
    def _iter_sse(response):
        event_name = ""
        data_lines: list[str] = []
        for raw_line in response.iter_lines():
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, bytes)
                else str(raw_line)
            ).rstrip("\r")
            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name, data_lines = "", []
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if data_lines:
            yield event_name, "\n".join(data_lines)

    def _stream_completion(
        self,
        session_id: str,
        prompt: str,
        completion_url: str,
        upstream_model_name: str,
        tagged_content: bool,
        *,
        model_type: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        parent_message_id: int | None = None,
        ready_out: dict | None = None,
    ):
        response = self._request(
            completion_url,
            {
                "msgId": uuid.uuid4().hex,
                "conversationId": session_id,
                "query": prompt,
                "isEditedQuery": False,
                "modelConfig": {
                    "enableThinking": bool(thinking_enabled),
                    "webSearchStatus": "enabled" if search_enabled else "disabled",
                    "model": upstream_model_name,
                },
                "multiMedias": [],
            },
            "chat",
            stream=True,
        )
        seen_content = False
        finished = False
        content_parser = _MimoThinkTagParser() if tagged_content else None
        try:
            for event_name, data in self._iter_sse(response):
                if data == "[DONE]":
                    finished = True
                    break
                if event_name not in {
                    "dialogId", "message", "usage", "finish",
                    "error", "sensitive_query", "sensitive_title", "tip_truncate",
                }:
                    continue
                try:
                    payload = json.loads(data)
                except (ValueError, TypeError):
                    if event_name == "error":
                        self._raise_event_error(data)
                    raise UpstreamTransientError("MiMo returned an invalid SSE event") from None
                if not isinstance(payload, dict):
                    raise UpstreamTransientError("MiMo returned an invalid SSE object")
                self._raise_business_error(payload, "chat")
                message_type = payload.get("messageType", "")
                if event_name in {"error", "sensitive_query", "sensitive_title", "tip_truncate"} or message_type == "error":
                    self._raise_event_error(
                        payload.get("content") or payload.get("message") or event_name,
                        code=payload.get("code"),
                    )
                if event_name == "finish":
                    finished = True
                    break
                if event_name != "message" or message_type not in ("", "assistant"):
                    continue
                reasoning = payload.get("reasoningContent")
                content = payload.get("content")
                if isinstance(reasoning, str) and reasoning:
                    seen_content = True
                    yield {"__type": "thinking", "content": reasoning}
                if isinstance(content, str) and content:
                    if content_parser is None:
                        seen_content = True
                        yield content
                    else:
                        for kind, text in content_parser.feed(content):
                            if not text:
                                continue
                            seen_content = True
                            if kind == "thinking":
                                yield {"__type": "thinking", "content": text}
                            else:
                                yield text
            if content_parser is not None:
                for kind, text in content_parser.flush():
                    if not text:
                        continue
                    seen_content = True
                    if kind == "thinking":
                        yield {"__type": "thinking", "content": text}
                    else:
                        yield text
            if not finished:
                raise UpstreamTransientError("MiMo stream ended before its finish event")
            if not seen_content:
                raise UpstreamTransientError("MiMo returned an empty stream")
        except RequestException:
            raise UpstreamTransientError("MiMo chat stream was interrupted") from None
        finally:
            response.close()
            self._sync_cookies()

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
        requested_model = mimo_upstream_model(
            upstream_model or mimo_model_name()
        )
        tagged_content = requested_model.lower() in FASTCHAT_MODELS
        attempts = [(
            FASTCHAT_COMPLETION_URL if tagged_content else COMPLETION_URL,
            requested_model,
            tagged_content,
        )]
        if (
            tagged_content
            and ULTRASPEED_FALLBACK_MODEL
            and ULTRASPEED_FALLBACK_MODEL.lower() != requested_model.lower()
        ):
            attempts.append((
                COMPLETION_URL,
                mimo_upstream_model(ULTRASPEED_FALLBACK_MODEL),
                False,
            ))

        for index, (url, model, tagged) in enumerate(attempts):
            buffered_reasoning: list[dict] = []
            emitted_to_client = False
            try:
                for item in self._stream_completion(
                    session_id,
                    prompt,
                    url,
                    model,
                    tagged,
                    model_type=model_type,
                    thinking_enabled=thinking_enabled,
                    search_enabled=search_enabled,
                    parent_message_id=parent_message_id,
                    ready_out=ready_out,
                ):
                    if (
                        not emitted_to_client
                        and isinstance(item, dict)
                        and item.get("__type") == "thinking"
                    ):
                        buffered_reasoning.append(item)
                        continue
                    if not emitted_to_client:
                        for reasoning in buffered_reasoning:
                            yield reasoning
                        buffered_reasoning.clear()
                        emitted_to_client = True
                    yield item
                if buffered_reasoning:
                    for reasoning in buffered_reasoning:
                        yield reasoning
                return
            except UpstreamTransientError:
                if emitted_to_client or index == len(attempts) - 1:
                    raise

    def chat(self, *args, **kwargs) -> tuple[str, str]:
        answer: list[str] = []
        reasoning: list[str] = []
        for item in self.chat_stream(*args, **kwargs):
            if isinstance(item, dict) and item.get("__type") == "thinking":
                reasoning.append(str(item.get("content") or ""))
            else:
                answer.append(str(item))
        return "".join(answer), "".join(reasoning)

    def close(self) -> None:
        self._client.close()
