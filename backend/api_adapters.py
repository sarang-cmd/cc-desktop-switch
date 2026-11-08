"""Third-party API format adapter layer."""

from __future__ import annotations

import json
import uuid
from typing import Any


def normalize_api_format(value: str) -> str:
    """Normalize legacy apiFormat values while keeping anthropic as the primary format."""
    normalized = str(value or "anthropic").strip().lower().replace("-", "_")
    if normalized in {"openai", "openai_chat", "chat_completions"}:
        return "openai_chat"
    if normalized in {"anthropic", "claude", "messages"}:
        return "anthropic"
    return normalized or "anthropic"


def content_to_text(content: Any) -> str:
    """Convert common content blocks into plain text accepted by OpenAI Chat."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("content"), list):
                    text = content_to_text(item["content"])
                    if text:
                        parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def _tool_result_content(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = content_to_text(content)
        return text if text else json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _anthropic_tools_to_openai(tools: Any) -> list[dict]:
    if not isinstance(tools, list):
        return []
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        name = tool.get("name")
        if not name:
            continue
        converted.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or tool.get("parameters") or {"type": "object"},
            },
        })
    return converted


def _anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice


def _anthropic_message_to_openai(message: dict) -> list[dict]:
    role = message.get("role", "user")
    content = message.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        role = "user"
    if not isinstance(content, list):
        return [{"role": role, "content": content_to_text(content)}]

    tool_messages = []
    text_blocks = []
    tool_calls = []
    for block in content:
        if not isinstance(block, dict):
            text_blocks.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or block.get("id") or "",
                "content": _tool_result_content(block),
            })
        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": block.get("name") or "tool",
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
        elif isinstance(block.get("text"), str):
            text_blocks.append(block["text"])

    messages = []
    if role == "assistant" and tool_calls:
        messages.append({
            "role": "assistant",
            "content": "\n".join(text_blocks) or None,
            "tool_calls": tool_calls,
        })
    elif role == "user" and tool_messages:
        messages.extend(tool_messages)
        text = "\n".join(text_blocks)
        if text:
            messages.append({"role": "user", "content": text})
    else:
        messages.append({"role": role, "content": "\n".join(text_blocks)})
    return messages


def anthropic_to_openai_chat_body(body: dict, stream: bool) -> dict:
    """Convert Claude Desktop Anthropic Messages requests into OpenAI Chat requests."""
    messages = [dict(message) for message in body.get("messages", [])]
    system_msg = body.get("system")
    if not system_msg and messages and messages[0].get("role") == "system":
        system_msg = messages.pop(0).get("content")

    openai_messages = []
    system_text = content_to_text(system_msg)
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    for message in messages:
        openai_messages.extend(_anthropic_message_to_openai(message))

    openai_body = {
        "model": body.get("model", ""),
        "messages": openai_messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": stream,
    }
    if "temperature" in body and body["temperature"] is not None:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        openai_body["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        openai_body["stop"] = body["stop_sequences"]
    tools = _anthropic_tools_to_openai(body.get("tools"))
    if tools:
        openai_body["tools"] = tools
    if body.get("tool_choice") is not None:
        openai_body["tool_choice"] = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
    return openai_body


def _normalize_usage(usage: Any) -> dict:
    def token_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    normalized = dict(usage) if isinstance(usage, dict) else {}
    return {
        "input_tokens": token_int(normalized.get("prompt_tokens") or normalized.get("input_tokens")),
        "output_tokens": token_int(normalized.get("completion_tokens") or normalized.get("output_tokens")),
    }


def _tool_call_to_anthropic_block(tool_call: dict) -> dict:
    function = tool_call.get("function") or {}
    raw_args = function.get("arguments") or "{}"
    try:
        parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        parsed_args = {"arguments": raw_args}
    return {
        "type": "tool_use",
        "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
        "name": function.get("name") or "tool",
        "input": parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args},
    }


def _openai_finish_reason_to_anthropic(reason: Any, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_use"
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    return mapping.get(reason, "end_turn")


def openai_chat_to_anthropic(openai_resp: dict, model: str) -> dict:
    """Convert an OpenAI Chat response into an Anthropic message response."""
    choice = (openai_resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks = []
    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict):
            content_blocks.append(_tool_call_to_anthropic_block(tool_call))
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]
    stop_reason = _openai_finish_reason_to_anthropic(choice.get("finish_reason"), bool(message.get("tool_calls")))
    return {
        "id": openai_resp.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": _normalize_usage(openai_resp.get("usage")),
    }


def openai_chat_chunk_to_anthropic(chunk: dict, model: str) -> dict:
    """Convert an OpenAI Chat streaming chunk into an Anthropic SSE event."""
    choices = chunk.get("choices", [])
    if not choices:
        return {"type": "message_stop"}

    delta = choices[0].get("delta", {})
    finish_reason = choices[0].get("finish_reason")

    if delta.get("tool_calls"):
        return {
            "type": "error",
            "error": {
                "type": "unsupported_streaming_tool_call",
                "message": "OpenAI Chat experimental adapter does not support streaming tool calls yet.",
            },
        }

    content = delta.get("content", "")
    if not content:
        if finish_reason:
            return {"type": "message_stop"}
        if delta.get("role"):
            return {
                "type": "message_start",
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        return {"type": "ping"}

    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": content},
    }
