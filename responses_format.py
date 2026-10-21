"""OpenAI Responses API <-> Chat Completions translation helpers."""
from __future__ import annotations

import json
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


def input_to_chat_messages(input_value: Any, instructions: Any = None) -> list[dict[str, Any]]:
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

        if item_type == "function_call":
            name = str(raw.get("name") or "")
            arguments = raw.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
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

        if item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": str(raw.get("call_id") or raw.get("id") or ""),
                "content": _text_from_content(raw.get("output")),
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


def tools_to_chat_tools(raw_tools: Any) -> list[dict[str, Any]]:
    """Convert flat Responses function tools into Chat Completions tools."""
    result: list[dict[str, Any]] = []
    if not isinstance(raw_tools, list):
        return result
    for raw in raw_tools:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            continue
        nested = raw.get("function")
        function = nested if isinstance(nested, dict) else raw
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return result


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
            "text": text,
        }],
    }


def _function_call_item(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
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
            output.append(_function_call_item(tool_call))

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

    def __init__(self, response_id: str, model: str, request_data: dict[str, Any]):
        self.response_id = response_id
        self.model = model
        self.request_data = request_data
        self.sequence = 0
        self.message_id = _new_id("msg")
        self.text = ""
        self.reasoning = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage = _usage_from_chat(None)
        self.started = False

    def _next_sequence(self) -> int:
        value = self.sequence
        self.sequence += 1
        return value

    def _event(self, name: str, payload: dict[str, Any]) -> str:
        return format_sse(name, {"type": name, **payload, "sequence_number": self._next_sequence()})

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
        message_item = {
            "id": self.message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        return [
            self._event("response.created", {"response": response}),
            self._event("response.in_progress", {"response": response}),
            self._event(
                "response.output_item.added",
                {"output_index": 0, "item": message_item},
            ),
            self._event(
                "response.content_part.added",
                {
                    "item_id": self.message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def feed_chat_chunk(self, chunk: dict[str, Any]) -> list[str]:
        events: list[str] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = _usage_from_chat(usage)

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

        content = delta.get("content")
        if isinstance(content, str) and content:
            self.text += content
            events.append(self._event(
                "response.output_text.delta",
                {
                    "item_id": self.message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": content,
                },
            ))
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning += reasoning

        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for raw in raw_tool_calls:
                if not isinstance(raw, dict):
                    continue
                index = int(raw.get("index") or 0)
                current = self.tool_calls.setdefault(
                    index,
                    {"id": "", "function": {"name": "", "arguments": ""}},
                )
                if raw.get("id"):
                    current["id"] = str(raw["id"])
                function = raw.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        current["function"]["name"] += str(function["name"])
                    if function.get("arguments"):
                        current["function"]["arguments"] += str(function["arguments"])
        return events

    def final_events(self) -> list[str]:
        events: list[str] = []
        if self.text:
            events.append(self._event(
                "response.output_text.done",
                {
                    "item_id": self.message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": self.text,
                },
            ))
        events.append(self._event(
            "response.content_part.done",
            {
                "item_id": self.message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            },
        ))
        message_item = _message_item(self.text)
        message_item["id"] = self.message_id
        events.append(self._event(
            "response.output_item.done",
            {"output_index": 0, "item": message_item},
        ))

        output = [message_item]
        next_index = 1
        if self.reasoning:
            reasoning_item = _reasoning_item(self.reasoning)
            events.append(self._event(
                "response.output_item.added",
                {
                    "output_index": next_index,
                    "item": {**reasoning_item, "status": "in_progress", "summary": []},
                },
            ))
            events.append(self._event(
                "response.output_item.done",
                {"output_index": next_index, "item": reasoning_item},
            ))
            output.append(reasoning_item)
            next_index += 1
        for index in sorted(self.tool_calls):
            tool_call = self.tool_calls[index]
            item = _function_call_item(tool_call)
            events.append(self._event(
                "response.output_item.added",
                {"output_index": next_index, "item": {**item, "status": "in_progress", "arguments": ""}},
            ))
            if item["arguments"]:
                events.append(self._event(
                    "response.function_call_arguments.delta",
                    {"item_id": item["id"], "output_index": next_index, "delta": item["arguments"]},
                ))
            events.append(self._event(
                "response.function_call_arguments.done",
                {
                    "item_id": item["id"],
                    "output_index": next_index,
                    "arguments": item["arguments"],
                },
            ))
            events.append(self._event(
                "response.output_item.done",
                {"output_index": next_index, "item": item},
            ))
            output.append(item)
            next_index += 1

        response = _base_response(
            self.response_id,
            self.model,
            self.request_data,
            status="completed",
            output=output,
            usage=self.usage,
        )
        events.append(self._event("response.completed", {"response": response}))
        return events

    def error_event(self, message: str) -> str:
        return self._event(
            "error",
            {
                "code": "upstream_error",
                "message": message,
                "param": None,
            },
        )


def parse_chat_sse_event(raw_event: str) -> Iterable[str]:
    """Yield `data:` payloads from one Chat Completions SSE event block."""
    data_lines = []
    for line in raw_event.replace("\r\n", "\n").split("\n"):
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)
