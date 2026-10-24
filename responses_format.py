"""OpenAI Responses API <-> Chat Completions translation helpers."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Iterable


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for raw in content:
        if isinstance(raw, str):
            parts.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        nested = raw.get("content")
        if nested is not None:
            parts.append(_text_from_content(nested))
    return "".join(parts)


def instructions_text(instructions: Any) -> str:
    if isinstance(instructions, str):
        return instructions.strip()
    return _text_from_content(instructions).strip()


def _stringify_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _custom_input_from_arguments(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("input"), str):
        return value["input"]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, dict) and isinstance(parsed.get("input"), str):
            return parsed["input"]
        return value
    return "" if value is None else str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {"query": value}
        if isinstance(parsed, dict):
            return parsed
    return {"query": "" if value is None else str(value)}


class ResponsesToolMapping:
    def __init__(
        self,
        chat_name: str,
        kind: str,
        wire_name: str,
        *,
        namespace: str = "",
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.chat_name = chat_name
        self.kind = kind
        self.wire_name = wire_name
        self.namespace = namespace
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}


class ResponsesToolAdapter:
    """Lower Responses tools to Chat tools and restore Codex call items."""

    CORE_TOOL_NAMES = {
        "exec_command",
        "write_stdin",
        "view_image",
        "request_user_input",
        "apply_patch",
        "get_goal",
        "create_goal",
        "update_goal",
    }

    def __init__(
        self,
        raw_tools: Any,
        *,
        codex_mode: bool = False,
        tool_mode: str = "all",
        allowed_tools: set[str] | None = None,
    ) -> None:
        self.codex_mode = codex_mode
        self.tool_mode = str(tool_mode or "all").strip().lower()
        self.allowed_tools = {name.strip() for name in allowed_tools or set() if name.strip()}
        self.chat_tools: list[dict[str, Any]] = []
        self._by_chat_name: dict[str, ResponsesToolMapping] = {}
        self._register(raw_tools if isinstance(raw_tools, list) else [])

    @staticmethod
    def _safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", name.strip())

    @classmethod
    def _flatten_name(cls, namespace: str, name: str) -> str:
        full = cls._safe_name(f"{namespace}__{name}")
        if len(full) <= 64:
            return full
        digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:12]
        return f"{full[:51]}__{digest}"

    def _selected(self, mapping: ResponsesToolMapping) -> bool:
        if not self.codex_mode or self.tool_mode in {"", "all"}:
            return True
        if self.tool_mode == "none":
            return False
        candidates = {
            mapping.chat_name,
            mapping.wire_name,
            f"{mapping.namespace}.{mapping.wire_name}" if mapping.namespace else "",
        }
        if self.allowed_tools:
            return bool(candidates & self.allowed_tools)
        if self.tool_mode == "core":
            return mapping.kind != "namespace" and mapping.wire_name in self.CORE_TOOL_NAMES
        return True

    def _add_mapping(self, mapping: ResponsesToolMapping) -> None:
        existing = self._by_chat_name.get(mapping.chat_name)
        if existing is not None:
            if (
                existing.kind == mapping.kind
                and existing.wire_name == mapping.wire_name
                and existing.namespace == mapping.namespace
            ):
                return
            digest = hashlib.sha256(
                f"{mapping.kind}:{mapping.namespace}:{mapping.wire_name}".encode("utf-8")
            ).hexdigest()[:8]
            mapping.chat_name = f"{mapping.chat_name[:55]}_{digest}"
        self._by_chat_name[mapping.chat_name] = mapping
        if self._selected(mapping):
            self.chat_tools.append({
                "type": "function",
                "function": {
                    "name": mapping.chat_name,
                    "description": mapping.description,
                    "parameters": mapping.parameters,
                },
            })

    def _register_function(
        self,
        raw: dict[str, Any],
        *,
        kind: str = "function",
        namespace: str = "",
        wire_name: str = "",
    ) -> None:
        nested = raw.get("function")
        function = nested if isinstance(nested, dict) else raw
        name = str(wire_name or function.get("name") or "").strip()
        if not name:
            return
        chat_name = self._flatten_name(namespace, name) if namespace else self._safe_name(name)
        self._add_mapping(ResponsesToolMapping(
            chat_name,
            kind,
            name,
            namespace=namespace,
            description=str(function.get("description") or ""),
            parameters=function.get("parameters") or {"type": "object", "properties": {}},
        ))

    def _register(self, raw_tools: list[Any]) -> None:
        for raw in raw_tools:
            if not isinstance(raw, dict):
                continue
            tool_type = str(raw.get("type") or "")
            if tool_type == "function":
                self._register_function(raw)
            elif tool_type == "namespace":
                namespace = str(raw.get("name") or "").strip()
                children = raw.get("children") or raw.get("tools") or []
                if not namespace or not isinstance(children, list):
                    continue
                for child in children:
                    if isinstance(child, dict) and child.get("type") in {None, "", "function"}:
                        self._register_function(child, kind="namespace", namespace=namespace)
            elif tool_type == "custom":
                self._register_function(
                    {
                        "name": raw.get("name"),
                        "description": raw.get("description"),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "input": {
                                    "type": "string",
                                    "description": "Free-form input for this custom tool.",
                                },
                            },
                            "required": ["input"],
                            "additionalProperties": False,
                        },
                    },
                    kind="custom",
                )
            elif tool_type == "tool_search":
                self._register_function(
                    {
                        "name": "tool_search",
                        "description": "Search and load tools needed for the current task.",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                    kind="tool_search",
                )

    def mapping_for_input(self, item: dict[str, Any]) -> ResponsesToolMapping | None:
        item_type = str(item.get("type") or "")
        name = str(item.get("name") or "").strip()
        namespace = str(item.get("namespace") or "").strip()
        for mapping in self._by_chat_name.values():
            if mapping.wire_name != name:
                continue
            if namespace and mapping.namespace == namespace:
                return mapping
            if item_type == "custom_tool_call" and mapping.kind == "custom":
                return mapping
            if item_type == "tool_search_call" and mapping.kind == "tool_search":
                return mapping
            if item_type == "function_call" and mapping.kind in {"function", "namespace"}:
                if not namespace or mapping.namespace == namespace:
                    return mapping
        return self._by_chat_name.get(self._safe_name(name))

    def mapping_for_chat_name(self, chat_name: str) -> ResponsesToolMapping | None:
        return self._by_chat_name.get(chat_name)

    def lower_tool_call(self, item: dict[str, Any]) -> tuple[str, str, str]:
        item_type = str(item.get("type") or "")
        mapping = self.mapping_for_input(item)
        name = mapping.chat_name if mapping else str(item.get("name") or "")
        if item_type == "custom_tool_call":
            arguments = _stringify_arguments({"input": item.get("input", "")})
        elif item_type == "tool_search_call":
            arguments = _stringify_arguments(item.get("arguments", {}))
        else:
            arguments = _stringify_arguments(item.get("arguments", ""))
        call_id = str(item.get("call_id") or item.get("id") or _new_id("call"))
        return name, arguments, call_id

    def restore_tool_call(
        self,
        chat_name: str,
        arguments: Any,
        call_id: str,
        *,
        item_id: str = "",
        status: str = "completed",
    ) -> dict[str, Any]:
        mapping = self._by_chat_name.get(chat_name)
        arguments_text = _stringify_arguments(arguments)
        if mapping and mapping.kind == "custom":
            return {
                "id": item_id or _new_id("ctc"),
                "type": "custom_tool_call",
                "status": status,
                "call_id": call_id,
                "name": mapping.wire_name,
                "input": _custom_input_from_arguments(arguments),
            }
        if mapping and mapping.kind == "tool_search":
            return {
                "id": item_id or _new_id("tsc"),
                "type": "tool_search_call",
                "status": status,
                "call_id": call_id,
                "execution": "client",
                "arguments": _json_object(arguments),
            }
        item = {
            "id": item_id or _new_id("fc"),
            "type": "function_call",
            "status": status,
            "call_id": call_id,
            "name": mapping.wire_name if mapping else chat_name,
            "arguments": arguments_text,
        }
        if mapping and mapping.namespace:
            item["namespace"] = mapping.namespace
        return item

    def adapt_tool_choice(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        name = str(value.get("name") or (value.get("function") or {}).get("name") or "")
        if not name:
            return value
        for mapping in self._by_chat_name.values():
            if mapping.wire_name == name:
                if mapping.chat_name not in {
                    tool["function"]["name"] for tool in self.chat_tools
                }:
                    return "auto"
                return {**value, "name": mapping.chat_name}
        return value


def input_to_chat_messages(
    input_value: Any,
    instructions: Any = None,
    *,
    tool_adapter: ResponsesToolAdapter | None = None,
) -> list[dict[str, Any]]:
    """Convert Responses input items into Chat Completions message dicts."""
    messages: list[dict[str, Any]] = []
    system_text = instructions_text(instructions)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages
    if not isinstance(input_value, list):
        return messages

    for raw in input_value:
        if isinstance(raw, str):
            messages.append({"role": "user", "content": raw})
            continue
        if not isinstance(raw, dict):
            continue

        item_type = str(raw.get("type") or "")
        if item_type == "reasoning":
            continue

        if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
            if tool_adapter is not None:
                name, arguments, call_id = tool_adapter.lower_tool_call(raw)
            else:
                name = str(raw.get("name") or "")
                arguments = _stringify_arguments(raw.get("arguments", ""))
                call_id = str(raw.get("call_id") or raw.get("id") or _new_id("call"))
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            })
            continue

        if item_type in {
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_output",
        }:
            output = raw.get("output")
            if item_type == "tool_search_output" and isinstance(output, (dict, list)):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": str(raw.get("call_id") or raw.get("id") or ""),
                "content": _text_from_content(output),
            })
            continue

        if item_type and item_type != "message":
            # Built-in Responses tools such as web_search are not chat messages.
            continue

        role = str(raw.get("role") or "user").strip().lower()
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant"}:
            continue
        content = _text_from_content(raw.get("content"))
        message: dict[str, Any] = {"role": role, "content": content}
        raw_tool_calls = raw.get("tool_calls")
        if role == "assistant" and isinstance(raw_tool_calls, list):
            message["tool_calls"] = raw_tool_calls
        messages.append(message)

    return messages


def tools_to_chat_tools(
    raw_tools: Any,
    *,
    codex_mode: bool = False,
    tool_mode: str = "all",
    allowed_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert Responses function/namespace/custom tools into Chat tools."""
    return ResponsesToolAdapter(
        raw_tools,
        codex_mode=codex_mode,
        tool_mode=tool_mode,
        allowed_tools=allowed_tools,
    ).chat_tools


def requested_search(raw_tools: Any) -> bool:
    if not isinstance(raw_tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and str(tool.get("type") or "") in {"web_search", "web_search_preview"}
        for tool in raw_tools
    )


def requested_thinking(reasoning: Any) -> bool:
    if not isinstance(reasoning, dict):
        return False
    effort = str(reasoning.get("effort") or "").strip().lower()
    return bool(effort and effort != "none")


def _usage_from_chat(usage: Any) -> dict[str, Any]:
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total_tokens,
    }


def _message_item(text: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": _new_id("msg"),
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "annotations": [],
            "logprobs": [],
            "text": text,
        }],
    }


def _function_call_item(
    tool_call: dict[str, Any],
    tool_adapter: ResponsesToolAdapter | None = None,
) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    if tool_adapter is not None:
        return tool_adapter.restore_tool_call(
            str(function.get("name") or ""),
            function.get("arguments") or "",
            str(tool_call.get("id") or _new_id("call")),
        )
    return {
        "id": _new_id("fc"),
        "type": "function_call",
        "status": "completed",
        "call_id": str(tool_call.get("id") or _new_id("call")),
        "name": str(function.get("name") or ""),
        "arguments": str(function.get("arguments") or ""),
    }


def _reasoning_item(text: str) -> dict[str, Any]:
    return {
        "id": _new_id("rs"),
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": text}],
    }


def _base_response(
    response_id: str,
    model: str,
    request_data: dict[str, Any],
    *,
    status: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": request_data.get("instructions"),
        "max_output_tokens": request_data.get("max_output_tokens"),
        "model": model,
        "output": output,
        "parallel_tool_calls": bool(request_data.get("parallel_tool_calls", True)),
        "previous_response_id": request_data.get("previous_response_id"),
        "reasoning": request_data.get("reasoning"),
        "service_tier": request_data.get("service_tier"),
        "store": bool(request_data.get("store", False)),
        "temperature": request_data.get("temperature"),
        "text": request_data.get("text") or {"format": {"type": "text"}},
        "tool_choice": request_data.get("tool_choice", "auto"),
        "tools": request_data.get("tools") or [],
        "top_p": request_data.get("top_p"),
        "truncation": request_data.get("truncation", "disabled"),
        "usage": usage,
        "user": request_data.get("user"),
        "metadata": request_data.get("metadata") or {},
    }
    return response


def build_nonstream_response(
    chat_payload: dict[str, Any],
    response_id: str,
    request_data: dict[str, Any],
    tool_adapter: ResponsesToolAdapter | None = None,
) -> dict[str, Any]:
    choice = (chat_payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = str(message.get("content") or "")
    reasoning = str(message.get("reasoning_content") or "")
    tool_calls = message.get("tool_calls") or []

    output: list[dict[str, Any]] = []
    if reasoning:
        output.append(_reasoning_item(reasoning))
    if text or not tool_calls:
        output.append(_message_item(text))
    for tool_call in tool_calls:
        if isinstance(tool_call, dict):
            output.append(_function_call_item(tool_call, tool_adapter))

    return _base_response(
        response_id,
        str(chat_payload.get("model") or request_data.get("model") or ""),
        request_data,
        status="completed",
        output=output,
        usage=_usage_from_chat(chat_payload.get("usage")),
    )


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ResponsesStreamTranslator:
    """Translate OpenAI Chat Completions SSE chunks into Responses SSE events."""

    def __init__(
        self,
        response_id: str,
        model: str,
        request_data: dict[str, Any],
        tool_adapter: ResponsesToolAdapter | None = None,
    ):
        self.response_id = response_id
        self.model = model
        self.request_data = request_data
        self.tool_adapter = tool_adapter
        self.created_at = int(time.time())
        self.sequence = 0
        self.next_output_index = 0
        self.output_items: list[dict[str, Any]] = []
        self.message_id = ""
        self.message_index: int | None = None
        self.message_open = False
        self.message_done = False
        self.reasoning_id = ""
        self.reasoning_index: int | None = None
        self.reasoning_open = False
        self.reasoning_done = False
        self.text = ""
        self.reasoning = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.tool_order: list[int] = []
        self.usage = _usage_from_chat(None)
        self.started = False
        self.finished = False

    def _next_sequence(self) -> int:
        value = self.sequence
        self.sequence += 1
        return value

    def _event(self, name: str, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("response"), dict):
            payload["response"]["created_at"] = self.created_at
        return format_sse(name, {"type": name, **payload, "sequence_number": self._next_sequence()})

    def _alloc_output_index(self) -> int:
        index = self.next_output_index
        self.next_output_index += 1
        return index

    def initial_events(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        response = _base_response(
            self.response_id,
            self.model,
            self.request_data,
            status="in_progress",
            output=[],
        )
        return [
            self._event("response.created", {"response": response}),
            self._event("response.in_progress", {"response": response}),
        ]

    def _ensure_reasoning_item(self, events: list[str]) -> None:
        if self.reasoning_open or self.reasoning_done:
            return
        self.reasoning_id = _new_id("rs")
        self.reasoning_index = self._alloc_output_index()
        self.reasoning_open = True
        events.append(self._event(
            "response.output_item.added",
            {
                "output_index": self.reasoning_index,
                "item": {
                    "id": self.reasoning_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            },
        ))
        events.append(self._event(
            "response.reasoning_summary_part.added",
            {
                "item_id": self.reasoning_id,
                "output_index": self.reasoning_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
        ))

    def _close_reasoning_item(self, events: list[str]) -> None:
        if not self.reasoning_open or self.reasoning_index is None:
            return
        self.reasoning_open = False
        self.reasoning_done = True
        item = {
            "id": self.reasoning_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": self.reasoning}],
        }
        events.append(self._event(
            "response.reasoning_summary_text.done",
            {
                "item_id": self.reasoning_id,
                "output_index": self.reasoning_index,
                "summary_index": 0,
                "text": self.reasoning,
            },
        ))
        events.append(self._event(
            "response.reasoning_summary_part.done",
            {
                "item_id": self.reasoning_id,
                "output_index": self.reasoning_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": self.reasoning},
            },
        ))
        events.append(self._event(
            "response.output_item.done",
            {"output_index": self.reasoning_index, "item": item},
        ))
        self.output_items.append(item)

    def _ensure_message_item(self, events: list[str]) -> None:
        if self.message_open or self.message_done:
            return
        self.message_id = _new_id("msg")
        self.message_index = self._alloc_output_index()
        self.message_open = True
        events.append(self._event(
            "response.output_item.added",
            {
                "output_index": self.message_index,
                "item": {
                    "id": self.message_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
        ))
        events.append(self._event(
            "response.content_part.added",
            {
                "item_id": self.message_id,
                "output_index": self.message_index,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": "",
                    "annotations": [],
                    "logprobs": [],
                },
            },
        ))

    def _close_message_item(self, events: list[str]) -> None:
        if not self.message_open or self.message_index is None:
            return
        self.message_open = False
        self.message_done = True
        part = {
            "type": "output_text",
            "text": self.text,
            "annotations": [],
            "logprobs": [],
        }
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        events.append(self._event(
            "response.output_text.done",
            {
                "item_id": self.message_id,
                "output_index": self.message_index,
                "content_index": 0,
                "text": self.text,
                "logprobs": [],
            },
        ))
        events.append(self._event(
            "response.content_part.done",
            {
                "item_id": self.message_id,
                "output_index": self.message_index,
                "content_index": 0,
                "part": part,
            },
        ))
        events.append(self._event(
            "response.output_item.done",
            {"output_index": self.message_index, "item": item},
        ))
        self.output_items.append(item)

    def _tool_item(self, stored: dict[str, Any], *, status: str) -> dict[str, Any]:
        if self.tool_adapter is not None:
            return self.tool_adapter.restore_tool_call(
                stored["chat_name"],
                stored["arguments"],
                stored["id"],
                item_id=stored["item_id"],
                status=status,
            )
        return {
            "id": stored["item_id"],
            "type": "function_call",
            "status": status,
            "call_id": stored["id"],
            "name": stored["chat_name"],
            "arguments": "" if status == "in_progress" else stored["arguments"],
        }

    def _announce_tool(self, stored: dict[str, Any], events: list[str]) -> None:
        if stored["announced"] or not stored["chat_name"]:
            return
        stored["announced"] = True
        item = self._tool_item(stored, status="in_progress")
        events.append(self._event(
            "response.output_item.added",
            {"output_index": stored["output_index"], "item": item},
        ))
        mapping = (
            self.tool_adapter.mapping_for_chat_name(stored["chat_name"])
            if self.tool_adapter is not None
            else None
        )
        if (
            stored["arguments"]
            and (mapping is None or mapping.kind in {"function", "namespace"})
        ):
            events.append(self._event(
                "response.function_call_arguments.delta",
                {
                    "item_id": stored["item_id"],
                    "output_index": stored["output_index"],
                    "delta": stored["arguments"],
                    "call_id": stored["id"],
                    "name": mapping.wire_name if mapping else stored["chat_name"],
                },
            ))
            stored["emitted_arguments"] = len(stored["arguments"])

    def _close_tool(self, stored: dict[str, Any], events: list[str]) -> None:
        if stored["done"]:
            return
        stored["done"] = True
        self._announce_tool(stored, events)
        if not stored["announced"]:
            return
        item = self._tool_item(stored, status="completed")
        mapping = (
            self.tool_adapter.mapping_for_chat_name(stored["chat_name"])
            if self.tool_adapter is not None
            else None
        )
        kind = mapping.kind if mapping else "function"
        wire_name = mapping.wire_name if mapping else stored["chat_name"]
        if kind == "custom":
            if item["input"]:
                events.append(self._event(
                    "response.custom_tool_call_input.delta",
                    {
                        "item_id": stored["item_id"],
                        "output_index": stored["output_index"],
                        "delta": item["input"],
                    },
                ))
            events.append(self._event(
                "response.custom_tool_call_input.done",
                {
                    "item_id": stored["item_id"],
                    "output_index": stored["output_index"],
                    "call_id": stored["id"],
                    "name": wire_name,
                    "input": item["input"],
                },
            ))
        elif kind != "tool_search":
            events.append(self._event(
                "response.function_call_arguments.done",
                {
                    "item_id": stored["item_id"],
                    "output_index": stored["output_index"],
                    "arguments": item["arguments"],
                    "call_id": stored["id"],
                    "name": wire_name,
                },
            ))
        events.append(self._event(
            "response.output_item.done",
            {"output_index": stored["output_index"], "item": item},
        ))
        self.output_items.append(item)

    def feed_chat_chunk(self, chunk: dict[str, Any]) -> list[str]:
        events: list[str] = []
        if self.finished:
            return events
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = _usage_from_chat(usage)

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._ensure_reasoning_item(events)
            self.reasoning += reasoning
            events.append(self._event(
                "response.reasoning_summary_text.delta",
                {
                    "item_id": self.reasoning_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "delta": reasoning,
                },
            ))

        content = delta.get("content")
        if isinstance(content, str) and content:
            self._close_reasoning_item(events)
            self._ensure_message_item(events)
            self.text += content
            events.append(self._event(
                "response.output_text.delta",
                {
                    "item_id": self.message_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "delta": content,
                    "logprobs": [],
                },
            ))

        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for raw in raw_tool_calls:
                if not isinstance(raw, dict):
                    continue
                index = int(raw.get("index") or 0)
                current = self.tool_calls.get(index)
                if current is None:
                    self._close_reasoning_item(events)
                    self._close_message_item(events)
                    current = {
                        "id": "",
                        "chat_name": "",
                        "arguments": "",
                        "item_id": _new_id("fc"),
                        "output_index": self._alloc_output_index(),
                        "announced": False,
                        "emitted_arguments": 0,
                        "done": False,
                    }
                    self.tool_calls[index] = current
                    self.tool_order.append(index)
                if raw.get("id"):
                    current["id"] = str(raw["id"])
                function = raw.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        current["chat_name"] += str(function["name"])
                    if function.get("arguments"):
                        current["arguments"] += str(function["arguments"])
                self._announce_tool(current, events)
                mapping = (
                    self.tool_adapter.mapping_for_chat_name(current["chat_name"])
                    if self.tool_adapter is not None and current["chat_name"]
                    else None
                )
                if (
                    current["announced"]
                    and (mapping is None or mapping.kind in {"function", "namespace"})
                    and len(current["arguments"]) > current["emitted_arguments"]
                ):
                    delta_text = current["arguments"][current["emitted_arguments"]:]
                    current["emitted_arguments"] = len(current["arguments"])
                    events.append(self._event(
                        "response.function_call_arguments.delta",
                        {
                            "item_id": current["item_id"],
                            "output_index": current["output_index"],
                            "delta": delta_text,
                            "call_id": current["id"],
                            "name": mapping.wire_name if mapping else current["chat_name"],
                        },
                    ))
        return events

    def final_events(self) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        events: list[str] = []
        self._close_reasoning_item(events)
        if (
            not self.message_done
            and not self.tool_calls
            and self.reasoning
            and not self.text
        ):
            self.reasoning_done = True
            self._ensure_message_item(events)
            self.text = self.reasoning
            events.append(self._event(
                "response.output_text.delta",
                {
                    "item_id": self.message_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "delta": self.text,
                    "logprobs": [],
                },
            ))
        if not self.message_done and not self.tool_calls:
            self._ensure_message_item(events)
        self._close_message_item(events)
        for index in self.tool_order:
            self._close_tool(self.tool_calls[index], events)

        response = _base_response(
            self.response_id,
            self.model,
            self.request_data,
            status="completed",
            output=self.output_items,
            usage=self.usage,
        )
        events.append(self._event("response.completed", {"response": response}))
        return events

    def progress_event(self) -> str:
        """Keep event-based clients alive without inventing text or usage."""
        return self._event("response.in_progress", {"response": _base_response(
            self.response_id, self.model, self.request_data,
            status="in_progress", output=[],
        )})

    def error_event(self, message: str, code: str = "upstream_error") -> str:
        self.finished = True
        return self._event(
            "error",
            {
                "code": code,
                "message": message,
                "param": None,
            },
        )

    def failed_event(self, message: str, code: str = "upstream_error") -> str:
        self.finished = True
        response = _base_response(
            self.response_id, self.model, self.request_data,
            status="failed", output=[], usage=self.usage,
        )
        response["error"] = {"code": code, "message": message}
        return self._event("response.failed", {"response": response})


def parse_chat_sse_event(raw_event: str) -> Iterable[str]:
    """Yield `data:` payloads from one Chat Completions SSE event block."""
    data_lines = []
    for line in raw_event.replace("\r\n", "\n").split("\n"):
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)
