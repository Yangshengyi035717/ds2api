"""Adapter for Baidu Wenxin (chat.baidu.com) web SSE chat."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException
from upstream_http import stream_timeout

from adapter import RateLimitError, UpstreamHintError, UpstreamTransientError
from provider_registry import wenxin_model_name, wenxin_upstream_model

CHAT_URL = "https://chat.baidu.com/aichat/api/conversation"
FRAME_URL = "https://wenxin.baidu.com/"
IMPERSONATE = os.environ.get("WENXIN_IMPERSONATE", "chrome150")
CHAT_SAMPLES = "WISE_NEW_CSAITAB"


class WenxinAuthError(RuntimeError):
    """Wenxin token/lid credentials were rejected."""


def wenxin_cookie_value(cookies: str, name: str) -> str:
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _base64_text(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def wenxin_baiduid(cookies: str) -> str:
    return wenxin_cookie_value(cookies, "BAIDUID").split(":", 1)[0].strip()


def wenxin_tk(token: str, lid: str) -> str:
    token = str(token or "").strip()
    lid = str(lid or "").strip()
    if not token or not lid:
        raise ValueError("Wenxin 账号需要 token 和 lid")
    return f"{_base64_text(f'{token}|{lid}')}-{lid}-3"


def wenxin_chat_token(token: str, lid: str, query: str = "") -> str:
    token = str(token or "").strip()
    lid = str(lid or "").strip()
    if not token or not lid:
        raise ValueError("Wenxin 账号需要 token 和 lid")
    # Mirrors the browser's btoa(`${token}|${hash(query)}|${Date.now()}|${lid}`)-${lid}-3.
    query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()
    return (
        f"{_base64_text(f'{token}|{query_hash}|{int(time.time() * 1000)}|{lid}')}"
        f"-{lid}-3"
    )


class WenxinAdapter:
    def __init__(
        self,
        token: str = "",
        lid: str = "",
        cookies: str = "",
        device_id: str = "",
        user_id: str = "",
        session_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.token = (token or "").strip()
        self.lid = (lid or "").strip()
        self.cookies = (cookies or "").strip()
        self.device_id = (device_id or "").strip()
        self.user_id = (user_id or "").strip()
        self.session_id = (session_id or "").strip()
        if not self.cookies:
            raise ValueError("Wenxin 账号需要登录 Cookie（至少包含 BDUSS）")
        self.proxy = proxy
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )
        if not self.token or not self.lid:
            self.refresh_frame_identity()

    def refresh_frame_identity(self) -> None:
        response = self._client.get(
            FRAME_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": self.cookies,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/155.0.0.0 Safari/537.36"
                ),
            },
        )
        if response.status_code in {401, 403}:
            raise WenxinAuthError("Wenxin 登录 Cookie 已失效")
        if response.status_code >= 400:
            raise UpstreamTransientError(
                f"Wenxin frame HTTP {response.status_code}"
            )
        match = re.search(
            r'<script type="application/json" name="aiTabFrameBaseData">(.*?)</script>',
            response.text,
            re.S,
        )
        if not match:
            raise UpstreamTransientError("Wenxin frame identity is missing")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise UpstreamTransientError(
                "Wenxin frame identity is invalid"
            ) from exc
        token = str(payload.get("token") or "").strip()
        lid = str(payload.get("lid") or "").strip()
        if not token or not lid:
            raise WenxinAuthError("Wenxin 页面未返回 token/lid，请重新登录")
        self.token = token
        self.lid = lid
        user_info = payload.get("userInfo") or {}
        if isinstance(user_info, dict):
            self.user_id = str(user_info.get("baiduid") or self.user_id).strip()
        self.user_id = self.user_id or wenxin_baiduid(self.cookies)

    def create_session(self) -> str:
        if not self.token or not self.lid:
            self.refresh_frame_identity()
        return self.session_id or self.lid

    def _headers(self, prompt: str = "", *, stream: bool = True) -> dict[str, str]:
        if not self.token or not self.lid:
            self.refresh_frame_identity()
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://wenxin.baidu.com",
            "Referer": "https://wenxin.baidu.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "isDeepseek": "1",
            "source": "pc_csaitab",
            "personifiedSwitch": "0",
        }
        if self.cookies:
            headers["Cookie"] = self.cookies
        if prompt:
            encoded_prompt = quote(prompt, safe="")
            headers["X-Chat-Message"] = (
                f"query:{encoded_prompt},"
                "anti_ext:%7B%22inputT%22%3Anull%2C%22ck1%22%3A84%2C"
                "%22ck9%22%3A467%2C%22ck10%22%3A307%7D,"
                "enter_type:sidebar_dialog,re_rank:1,modelName:smartMode,sa:bkb"
            )
        return headers

    @staticmethod
    def _json_body(response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _raise_response(self, response, context: str) -> None:
        if response.status_code in {401, 403}:
            raise WenxinAuthError("Wenxin token/lid 已失效，请重新导入")
        if response.status_code == 429:
            raise RateLimitError("Wenxin upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"Wenxin {context} HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise UpstreamHintError(
                f"Wenxin {context} HTTP {response.status_code}: {response.text[:300]}"
            )
        content_type = str(
            getattr(response, "headers", {}).get("content-type") or ""
        ).lower()
        if "text/event-stream" in content_type:
            return
        body = self._json_body(response)
        status = body.get("status")
        if status not in (None, 0, "0"):
            raise UpstreamHintError(
                f"Wenxin {context}: {body.get('message') or status}"
            )

    def check_health(self) -> bool:
        response = self._client.post(
            CHAT_URL,
            headers=self._headers(),
            json=self._body("hi", thinking=False, search=False),
            stream=True,
        )
        self._raise_response(response, "health")
        try:
            for event_name, payload in self._iter_sse(response):
                if event_name == "basedata":
                    event = self._parse_json(payload)
                    user = event.get("user") if isinstance(event, dict) else None
                    if isinstance(user, dict) and user.get("isUserLogin") is False:
                        raise WenxinAuthError("Wenxin 登录态已失效")
                    return True
        finally:
            response.close()
        return True

    def _body(self, prompt: str, *, thinking: bool, search: bool) -> dict[str, Any]:
        if not self.token or not self.lid:
            self.refresh_frame_identity()
        return {
            "message": {
                "inputMethod": "chat_search",
                "isRebuild": False,
                "content": {
                    "query": "",
                    "agentInfo": {
                        "agent_id": [""],
                        "params": '{"agt_rk":1,"agt_sess_cnt":0}',
                    },
                    "agentInfoList": [],
                    "qtype": 0,
                    "extData": {},
                },
                "searchInfo": {
                    "srcid": "",
                    "order": "",
                    "tplname": "",
                    "dqaKey": "",
                    "re_rank": "1",
                    "ori_lid": "",
                    "sa": "bkb",
                    "enter_type": "sidebar_dialog",
                    "chatParams": {
                        "setype": "csaitab",
                        "chat_samples": CHAT_SAMPLES,
                        "chat_token": wenxin_chat_token(
                            self.token,
                            self.lid,
                            prompt,
                        ),
                        "scene": "",
                    },
                    "isPrivateChat": False,
                    "usedModel": {
                        "modelName": wenxin_upstream_model(
                            wenxin_model_name()
                        ),
                        "modelFunction": {
                            "deepSearch": "1" if search else "0",
                            "thinkMode": "1" if thinking else "0",
                        },
                        "showModelName": wenxin_upstream_model(
                            wenxin_model_name()
                        ),
                    },
                    "landingPageSwitch": "",
                    "landingPage": "aitab",
                    "ecomFrom": "",
                    "hasLocPermission": "",
                    "isInnovate": 2,
                    "applid": "",
                    "a_lid": "",
                    "capsuleSelectMode": "fast",
                    "showMindMap": False,
                    "deepDecisionInfo": {"isDeepDecision": 0},
                },
                "from": "",
                "source": "pc_csaitab",
                "query": [
                    {
                        "type": "TEXT",
                        "data": {
                            "text": {
                                "query": prompt,
                                "extData": "{}",
                                "text_type": "",
                            }
                        },
                    }
                ],
                "anti_ext": {
                    "inputT": None,
                    "ck1": 84,
                    "ck9": 467,
                    "ck10": 307,
                },
            },
            "sa": "bkb",
            "setype": "csaitab",
            "rank": 1,
        }

    @staticmethod
    def _parse_json(payload: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

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

    def _open(self, prompt: str, *, thinking: bool, search: bool):
        response = self._client.post(
            CHAT_URL,
            headers=self._headers(prompt),
            json=self._body(prompt, thinking=thinking, search=search),
            stream=True,
            timeout=stream_timeout(),
        )
        self._raise_response(response, "chat")
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
            thinking=thinking_enabled,
            search=search_enabled,
        )
        answer_seen = False
        thinking_seen = False
        done = False
        try:
            for event_name, payload in self._iter_sse(response):
                event = self._parse_json(payload)
                if not isinstance(event, dict):
                    continue
                if event_name == "basedata":
                    user = event.get("user")
                    if isinstance(user, dict) and user.get("isUserLogin") is False:
                        raise WenxinAuthError("Wenxin 登录态已失效")
                    continue
                status = event.get("status")
                if status not in (None, 0, "0"):
                    message = str(event.get("message") or "").strip()
                    if any(
                        token in message
                        for token in ("频繁", "限流", "上限", "稍后重试")
                    ):
                        raise RateLimitError(
                            message or "Wenxin upstream rate limit",
                            "rate_limit_reached",
                        )
                    raise UpstreamHintError(message or f"Wenxin status {status}")
                data = event.get("data") or {}
                message_obj = data.get("message") or {}
                # A component finishing (e.g. reasoning) is not necessarily
                # the end of the whole answer. Prefer message-level state.
                state = (message_obj.get("metaData") or {}).get("state")
                content = message_obj.get("content") or {}
                generator = content.get("generator")
                if not isinstance(generator, dict):
                    if state == "generate-complete":
                        done = True
                        break
                    continue
                data_obj = generator.get("data") or {}
                value = data_obj.get("value")
                if isinstance(value, str) and value:
                    if generator.get("component") in {
                        "deep_think",
                        "thought",
                        "reasoning",
                    }:
                        thinking_seen = True
                        yield {"__type": "thinking", "content": value}
                    else:
                        answer_seen = True
                        yield value
                hints = content.get("hints")
                if isinstance(hints, dict):
                    for part in hints.get("parts") or []:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            if "抱歉" in text or "错误" in text:
                                raise UpstreamHintError(text)
                if state == "generate-complete" or (
                    not state and generator.get("component") == "markdown-yiyan"
                    and generator.get("isFinished") is True
                ):
                    done = True
                    break
            if not answer_seen and not thinking_seen:
                raise UpstreamTransientError("Wenxin returned an empty stream")
            if not done:
                raise UpstreamTransientError("Wenxin stream ended before its generate-complete event")
        except RequestException:
            raise UpstreamTransientError("Wenxin stream was interrupted before completion") from None
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
