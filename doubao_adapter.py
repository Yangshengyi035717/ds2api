"""Adapter for Doubao (doubao.com) web chat SSE."""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException
from upstream_http import stream_timeout

from adapter import RateLimitError, UpstreamHintError, UpstreamTransientError
from provider_registry import doubao_model_name, doubao_upstream_model

COMPLETION_URL = "https://www.doubao.com/chat/completion"
HEALTH_URL = "https://www.doubao.com/samantha/notice/info"
IMPERSONATE = os.environ.get("DOUBAO_IMPERSONATE", "chrome150")
DEFAULT_BOT_ID = os.environ.get("DOUBAO_BOT_ID", "7338286299411103781").strip()
DEFAULT_DEVICE_ID = os.environ.get("DOUBAO_DEVICE_ID", "").strip()
DEFAULT_WEB_ID = os.environ.get("DOUBAO_WEB_ID", "").strip()
DEFAULT_TEA_UUID = os.environ.get("DOUBAO_TEA_UUID", "").strip()
DEFAULT_FP = os.environ.get(
    "DOUBAO_FP",
    "verify_mu157itq_2RQBTvvC_4XG2_4LX4_8JYa_16kt14UwtkUC",
).strip()
DEFAULT_WEB_TAB_ID = os.environ.get(
    "DOUBAO_WEB_TAB_ID",
    "21f3115b-2403-469e-b0b9-873ac55709a2",
).strip()


class DoubaoAuthError(RuntimeError):
    """Doubao login cookie or anti-bot signature was rejected."""


def doubao_cookie_value(cookies: str, name: str) -> str:
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


class DoubaoAdapter:
    def __init__(
        self,
        cookies: str = "",
        device_id: str = "",
        web_id: str = "",
        tea_uuid: str = "",
        fp: str = "",
        web_tab_id: str = "",
        bot_id: str = "",
        session_id: str = "",
        user_id: str = "",
        proxy: str | None = None,
        impersonate: str = IMPERSONATE,
    ) -> None:
        self.cookies = (cookies or "").strip()
        if not doubao_cookie_value(self.cookies, "sessionid"):
            raise ValueError("Doubao 账号需要包含 sessionid 的登录 Cookie")
        self.device_id = (
            (device_id or "").strip()
            or DEFAULT_DEVICE_ID
            or doubao_cookie_value(self.cookies, "device_id")
            or "7685346952515520051"
        )
        self.web_id = (
            (web_id or "").strip()
            or DEFAULT_WEB_ID
            or doubao_cookie_value(self.cookies, "web_id")
            or self.device_id
        )
        self.tea_uuid = (
            (tea_uuid or "").strip()
            or DEFAULT_TEA_UUID
            or doubao_cookie_value(self.cookies, "tea_uuid")
            or self.web_id
        )
        self.fp = (fp or "").strip() or DEFAULT_FP
        self.web_tab_id = (web_tab_id or "").strip() or DEFAULT_WEB_TAB_ID
        self.bot_id = (bot_id or "").strip() or DEFAULT_BOT_ID
        self.session_id = (session_id or "").strip()
        self.user_id = (user_id or "").strip()
        self.proxy = proxy
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=120,
            proxies={"all": proxy} if proxy else None,
        )

    def create_session(self) -> str:
        # Doubao creates the server-side conversation on the first completion
        # request; the local id is only a client-side correlation key.
        return self.session_id or f"local_{int(time.time() * 1000)}"

    def _query(self) -> dict[str, str]:
        return {
            "aid": "497858",
            "channel": "YR6cn",
            "device_id": self.device_id,
            "device_platform": "web",
            "doubao_device_platform": "web",
            "doubao_pc_version": "3.42.2",
            "fp": self.fp,
            "language": "zh",
            "pc_version": "3.42.2",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "CN",
            "samantha_web": "1",
            "sys_region": "CN",
            "tea_uuid": self.tea_uuid,
            "tz_name": "Asia/Shanghai",
            "use-olympus-account": "1",
            "version_code": "20800",
            "web_id": self.web_id,
            "web_platform": "browser",
            "web_tab_id": self.web_tab_id,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Cookie": self.cookies,
            "Origin": "https://www.doubao.com",
            "Referer": f"https://www.doubao.com/chat/{self.session_id or 'local'}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
            "agw-js-conv": "str, str",
        }

    @staticmethod
    def _json_body(response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _raise_response(self, response, context: str) -> None:
        if response.status_code in {401, 403}:
            raise DoubaoAuthError("Doubao 登录 Cookie 已失效")
        if response.status_code == 429:
            raise RateLimitError("Doubao upstream rate limit", "rate_limit_reached")
        if response.status_code >= 500:
            raise UpstreamTransientError(
                f"Doubao {context} HTTP {response.status_code}: {response.text[:200]}"
            )
        content_type = str(
            getattr(response, "headers", {}).get("content-type") or ""
        ).lower()
        if "text/event-stream" in content_type:
            return
        body = self._json_body(response)
        code = body.get("code")
        message = str(
            body.get("msg")
            or body.get("message")
            or (body.get("error") or {}).get("message")
            or ""
        ).strip()
        if str(code or "") in {"710012001", "401", "403"}:
            raise DoubaoAuthError(message or "Doubao 登录已过期")
        if response.status_code >= 400 or code not in (None, 0, "0"):
            raise UpstreamHintError(
                f"Doubao {context}: {message or code or response.status_code}"
            )

    def check_health(self) -> bool:
        # 只读接口：不创建会话、不消耗生成额度。
        response = self._client.get(
            HEALTH_URL,
            params=self._query(),
            headers=self._headers(),
        )
        self._raise_response(response, "health")
        return True

    def _body(self, prompt: str, *, thinking: bool, search: bool) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        local_id = self.session_id or f"local_{now_ms}"
        local_message_id = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        return {
            "client_meta": {
                "local_conversation_id": local_id,
                "conversation_id": self.session_id if self.session_id.startswith("3") else "",
                "bot_id": self.bot_id,
                "last_section_id": "",
                "last_message_index": None,
                "local_permissions": [
                    {"permission_name": "ACCESS_COARSE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_FINE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_BACKGROUND_LOCATION", "status": 3},
                ],
            },
            "messages": [
                {
                    "local_message_id": local_message_id,
                    "content_block": [
                        {
                            "block_type": 10000,
                            "content": {
                                "text_block": {
                                    "text": prompt,
                                    "icon_url": "",
                                    "icon_url_dark": "",
                                    "summary": "",
                                },
                                "pc_event_block": "",
                            },
                            "block_id": block_id,
                            "parent_id": "",
                            "meta_info": [],
                            "extra_info": {},
                            "append_fields": [],
                        }
                    ],
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "agent_mode": 2,
                "tts_switch": False,
                "need_deep_think": 3 if thinking else 0,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": not bool(self.session_id),
                "conversation_init_option": {"need_ack_conversation": True},
                "conversation_init_ext": {
                    "model_item_key": "0",
                    "reasoning_effort": "3",
                    "mode_id": "1",
                },
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "support_lazy_fetch_stream": True,
                "is_ai_playground": False,
                "is_old_user": False,
                "user_ug_source_info": {
                    "channel": "YR6cn",
                    "landing_page_url": f"https://www.doubao.com/chat/{local_id}?channel=YR6cn",
                    "source": "",
                    "channel_v2": "YR6cn",
                    "landing_page_url_v2": f"https://www.doubao.com/chat/{local_id}?channel=YR6cn",
                    "source_v2": "",
                },
                "general_task_param": {"server_do_compare": True},
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": now_ms // 1000,
                    "append_sse_event_scene": 0,
                },
                "message_storage_type": 0,
                "related_deleted_message_ids": {},
                "connector_info_list": [],
                "model_config": {
                    "model_item_key": doubao_upstream_model(
                        doubao_model_name()
                    ),
                    "model_extra_params": {},
                    "reasoning_effort": 3,
                },
                "aggregate_params": {
                    "mention_skill_list": "[]",
                    "mention_plugin_list": "[]",
                    "mention_ext": "[{}]",
                    "conversation_mode": "1",
                    "mode_id": "1",
                    "model_item_key": doubao_upstream_model(
                        doubao_model_name()
                    ),
                    "agent_mode": "2",
                    "reasoning_effort": "3",
                    "provider_id": "",
                },
                "conversation_mode": 1,
            },
            "user_context": [],
            "ext": {
                "agent_mode": "2",
                "use_deep_think": "3" if thinking else "0",
                "sub_conv_firstmet_type": "1",
                "general_task_param": '{"server_do_compare":true}',
                "collection_id": "",
                "is_finish": "1",
                "conversation_init_option": '{"need_ack_conversation":true}',
                "commerce_credit_config_enable": "0",
            },
        }

    @staticmethod
    def _iter_sse(response):
        event = ""
        data_lines: list[str] = []
        for raw_line in response.iter_lines():
            line = (
                raw_line.decode("utf-8", errors="replace")
                if isinstance(raw_line, (bytes, bytearray))
                else str(raw_line)
            )
            if not line:
                if data_lines:
                    yield event, "\n".join(data_lines)
                event = ""
                data_lines = []
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield event, "\n".join(data_lines)

    def _open(self, prompt: str, *, thinking: bool, search: bool):
        response = self._client.post(
            COMPLETION_URL,
            params=self._query(),
            headers=self._headers(),
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
        thinking_blocks: set[str] = set()
        done = False
        try:
            for event, payload in self._iter_sse(response):
                try:
                    data = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    continue
                if event == "SSE_REPLY_END":
                    if data.get("end_type") != 1:
                        raise UpstreamTransientError("Doubao stream ended abnormally")
                    done = True
                    break
                if event == "SSE_ACK":
                    ack = (data.get("ack_client_meta") or {})
                    if ready_out is not None and ack.get("conversation_id"):
                        ready_out["session_id"] = ack.get("conversation_id")
                    continue
                if event == "STREAM_MSG_NOTIFY":
                    blocks = (data.get("content") or {}).get("content_block") or []
                elif event == "CHUNK_DELTA":
                    text = data.get("text")
                    if isinstance(text, str) and text:
                        answer_seen = True
                        yield text
                    continue
                elif event == "STREAM_CHUNK":
                    blocks = [
                        block
                        for op in data.get("patch_op") or []
                        if op.get("patch_object") == 1
                        for block in (op.get("patch_value") or {}).get("content_block") or []
                    ]
                else:
                    continue
                for block in blocks:
                    block_type = block.get("block_type")
                    if block_type == 10040 and block.get("block_id"):
                        thinking_blocks.add(str(block["block_id"]))
                    text_block = (block.get("content") or {}).get("text_block") or {}
                    text = text_block.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    if block.get("parent_id") in thinking_blocks or block_type == 10040:
                        thinking_seen = True
                        yield {"__type": "thinking", "content": text}
                    elif not block.get("parent_id"):
                        answer_seen = True
                        yield text
            if not answer_seen and not thinking_seen:
                raise UpstreamTransientError("Doubao returned an empty stream")
            if not done:
                raise UpstreamTransientError("Doubao stream ended before its SSE_REPLY_END event")
        except RequestException:
            raise UpstreamTransientError("Doubao stream was interrupted before completion") from None
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
