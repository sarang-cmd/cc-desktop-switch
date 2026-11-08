"""Local proxy service: model name translation, request forwarding, and SSE stream processing."""

import json
import os
import re
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend import i18n
from backend.api_adapters import (
    anthropic_to_openai_chat_body,
    content_to_text,
    normalize_api_format,
    openai_chat_chunk_to_anthropic,
    openai_chat_to_anthropic,
)
from backend.model_alias import (
    all_provider_model_entries,
    custom_model_mappings,
    desktop_model_entries,
    desktop_route_ids,
    model_mappings_with_legacy_aliases,
    provider_model_ids as alias_provider_model_ids,
    resolve_model_alias,
    resolve_requested_model_slot,
)


def provider_model_ids(provider: Optional[dict]) -> list:
    """Return the raw upstream model IDs configured for the given provider."""
    return alias_provider_model_ids(provider)


def gateway_models_response(
    provider: Optional[dict],
    providers: Optional[list[dict]] = None,
    expose_all: bool = False,
) -> dict:
    """Build an Anthropic /v1/models style model list response."""
    if expose_all:
        entries = all_provider_model_entries(providers or [])
    else:
        entries = desktop_model_entries(provider)
    data = []
    for item in entries:
        model_id = item["name"]
        row = {
            "type": "model",
            "id": model_id,
            "display_name": item.get("displayName") or model_id,
            "created_at": "2024-01-01T00:00:00Z",
        }
        if item.get("supports1m") is True:
            row["supports1m"] = True
        data.append(row)
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


def _configured_desktop_route_names(provider: Optional[dict]) -> set[str]:
    return {
        str(item.get("name"))
        for item in desktop_model_entries(provider)
        if item.get("name")
    }


def _is_unmapped_desktop_route(model_id: str, provider: Optional[dict]) -> bool:
    """Return True if the request hit a Claude-safe route that is not mapped on this provider."""
    requested = str(model_id or "").strip()
    if not requested:
        return False
    if requested in provider_model_ids(provider):
        return False
    mapped_slot = resolve_requested_model_slot(requested)
    if requested not in set(desktop_route_ids()) and not mapped_slot:
        return False
    if mapped_slot:
        models_config = model_mappings_with_legacy_aliases((provider or {}).get("models", {}))
        if models_config.get(mapped_slot):
            return False
    return requested not in _configured_desktop_route_names(provider)


def unmapped_desktop_route_error(model_id: str) -> dict:
    return {
        "error": {
            "type": "invalid_request_error",
            "message": i18n.t("proxy.unmapped_model_error", model=model_id),
        }
    }


class ProxyStats:
    """Proxy request statistics."""

    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.today = 0
        self._date = datetime.now().strftime("%Y-%m-%d")

    def record(self, success: bool):
        self.total += 1
        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_str != self._date:
            self.today = 0
            self._date = today_str
        self.today += 1
        if success:
            self.success += 1
        else:
            self.failed += 1

    def to_dict(self):
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "today": self.today,
        }


class LogBuffer:
    """Ring buffer for proxy logs."""

    def __init__(self, max_size=200):
        self._logs = []
        self._max_size = max_size

    def add(self, level: str, message: str):
        self._logs.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })
        if len(self._logs) > self._max_size:
            self._logs = self._logs[-self._max_size:]

    def get_all(self):
        return list(self._logs)

    def clear(self):
        self._logs = []


# Global singletons
stats = ProxyStats()
log_buffer = LogBuffer()


def map_model(original_model: str, provider: Optional[dict]) -> str:
    """Map a standard Claude model name to the provider's configured upstream model ID."""
    if not provider or not original_model:
        return original_model

    models_config = model_mappings_with_legacy_aliases(provider.get("models", {}))
    if not models_config:
        return original_model

    custom_models = custom_model_mappings(provider.get("models", {}))
    if original_model in custom_models:
        return custom_models[original_model]

    # Claude Desktop in gateway mode may directly send raw model IDs returned by /v1/models.
    # Pass them through unchanged so e.g. deepseek-v4-pro[1m] is not overwritten by default.
    if original_model in provider_model_ids(provider):
        return original_model

    mapped_slot = resolve_requested_model_slot(original_model)
    if mapped_slot:
        return models_config.get(mapped_slot) or original_model

    return models_config.get("default") or original_model


def build_upstream_url(base_url: str, api_format: str) -> str:
    """Build the final upstream request URL from the configured Base URL.

    Users may enter either a root Base URL or a full endpoint path; normalize it
    here to avoid duplicating /v1/messages or /chat/completions.
    """
    clean = str(base_url or "").strip().rstrip("/")
    api_format = normalize_api_format(api_format)
    lower = clean.lower()
    if api_format == "openai_chat":
        if lower.endswith("/chat/completions"):
            return clean
        return f"{clean}/chat/completions"
    if lower.endswith("/v1/messages"):
        return clean
    if lower.endswith("/v1"):
        return f"{clean}/messages"
    return f"{clean}/v1/messages"


def _content_to_text(content) -> str:
    """Convert Anthropic content blocks to a plain string for OpenAI-compatible endpoints."""
    return content_to_text(content)


def _anthropic_to_openai_body(body: dict, stream: bool) -> dict:
    """Convert an Anthropic Messages request from Claude Desktop into OpenAI Chat format."""
    return anthropic_to_openai_chat_body(body, stream)


def get_upstream_headers(provider: dict) -> dict:
    """Build upstream authentication and protocol headers."""
    auth_scheme = provider.get("authScheme", "bearer")
    api_key = provider.get("apiKey", "")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if normalize_api_format(provider.get("apiFormat", "anthropic")) == "anthropic":
        headers["anthropic-version"] = "2023-06-01"

    if api_key:
        if auth_scheme == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    # Merge custom extra headers from the provider (supporting {apiKey} interpolation)
    extra = provider.get("extraHeaders", {})
    if isinstance(extra, dict):
        for k, v in extra.items():
            headers[k] = v.replace("{apiKey}", api_key) if isinstance(v, str) else v

    return headers


def _provider_kind(provider: dict) -> str:
    """Infer provider family from name and URL for vendor-specific request handling."""
    host = urlsplit(str(provider.get("baseUrl") or "")).netloc.lower()
    if provider.get("supportsDeepSeekMax") is True or host == "api.deepseek.com":
        return "deepseek"
    probe = f"{provider.get('name', '')} {provider.get('baseUrl', '')}".lower()
    if "moonshot" in probe or "kimi" in probe:
        return "kimi"
    if "bigmodel" in probe or "zhipu" in probe or "glm" in probe:
        return "zhipu"
    if "dashscope" in probe or "bailian" in probe or "aliyun" in probe:
        return "bailian"
    if "siliconflow" in probe:
        return "siliconflow"
    if "qnaigc" in probe or "qiniu" in probe:
        return "qiniu"
    return "unknown"


def _deep_merge(target: dict, source: dict) -> dict:
    """Recursively merge request option dictionaries while preserving existing fields."""
    merged = dict(target)
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _anthropic_request_options(provider: dict) -> dict:
    options = provider.get("requestOptions") or {}
    if not isinstance(options, dict):
        return {}
    anthropic_options = options.get("anthropic", options)
    return anthropic_options if isinstance(anthropic_options, dict) else {}


def apply_anthropic_request_options(upstream_body: dict, provider: dict) -> dict:
    """Handle vendor-specific thinking parameters in Anthropic requests.

    DeepSeek's Anthropic-compatible endpoint supports thinking and output_config.effort=max.
    Other vendors' Anthropic compatibility layers vary, so we strip request-level thinking
    by default to prevent upstream HTTP 400 errors.
    """
    kind = _provider_kind(provider)
    options = _anthropic_request_options(provider)

    if kind != "deepseek":
        upstream_body.pop("thinking", None)
        return upstream_body

    if options:
        upstream_body = _deep_merge(upstream_body, options)

    return upstream_body


_BILLING_HEADER_PREFIXES = (
    "x-anthropic-billing-header:",
    "x-ahthropic-billing-header:",
)


def _is_billing_header_text(value: str) -> bool:
    text = str(value or "").lstrip().lower()
    return any(text.startswith(prefix) for prefix in _BILLING_HEADER_PREFIXES)


def _strip_billing_header_from_text(value: str) -> tuple[str, int]:
    """Strip dynamic billing headers injected by Claude Code while preserving remaining system text."""
    text = str(value or "")
    lines = text.splitlines()
    removed = 0
    while lines and _is_billing_header_text(lines[0]):
        lines.pop(0)
        removed += 1
    while removed and lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines), removed


def rectify_claude_code_billing_header(body: dict, enabled: bool = True) -> int:
    """Remove dynamic billing headers sent by Claude Code through the LLM gateway.

    Only system strings or system text blocks are modified; user messages are untouched.
    """
    if not enabled or not isinstance(body, dict):
        return 0

    system = body.get("system")
    if isinstance(system, str):
        cleaned, removed = _strip_billing_header_from_text(system)
        if removed:
            if cleaned:
                body["system"] = cleaned
            else:
                body.pop("system", None)
        return removed

    if not isinstance(system, list):
        return 0

    cleaned_blocks = []
    removed = 0
    for block in system:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and _is_billing_header_text(block.get("text", ""))
        ):
            removed += 1
            continue
        cleaned_blocks.append(block)

    if removed:
        if cleaned_blocks:
            body["system"] = cleaned_blocks
        else:
            body.pop("system", None)
    return removed


def _is_max_unsupported_error(status_code: int, error_text: str) -> bool:
    """Identify 400/422 errors that explicitly reject thinking/effort fields as Max-unsupported."""
    if status_code not in (400, 422):
        return False
    text = (error_text or "").lower()
    parameter_markers = (
        "output_config",
        "output config",
        "reasoning_effort",
        "reasoning effort",
        "thinking",
        "effort",
    )
    rejection_markers = (
        "not supported",
        "unsupported",
        "invalid parameter",
        "invalid value",
        "unknown parameter",
        "unknown field",
        "unrecognized",
        "not allowed",
        "unexpected",
        "extra inputs are not permitted",
    )
    return (
        any(marker in text for marker in parameter_markers)
        and any(marker in text for marker in rejection_markers)
    )


def _redact_sensitive_text(value: str, limit: int = 500) -> str:
    """Return a redacted summary string safe for logs and diagnostic bundles."""
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{12,}", r"\1******", text)
    text = re.sub(
        r"(?i)(\b(?:authorization|x-api-key|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|client[-_]?secret|token|secret|password|key)\b\s*[:=]\s*[\"']?)[^\"'\s,&<>]+",
        r"\1******",
        text,
    )
    text = re.sub(
        r"(?i)([?&][^=&#\s]*(?:api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|client[-_]?secret|token|secret|password|key)[^=&#\s]*=)[^&#\s]+",
        r"\1******",
        text,
    )
    text = re.sub(
        r"(?i)(\b[\w.-]*(?:api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|client[-_]?secret|token|secret|password|key)[\w.-]*\s*=\s*)[^<>\s,&]+",
        r"\1******",
        text,
    )
    text = re.sub(r"(?i)\b(sk-[a-z0-9_-]{8,}|ccds_[a-z0-9_-]{8,})\b", "******", text)
    text = re.sub(r"([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@", r"\1******:******@", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    return text[:limit]


def _content_type_header(response: httpx.Response) -> str:
    return str(response.headers.get("content-type") or "").strip()


def _content_type(response: httpx.Response) -> str:
    return _content_type_header(response).split(";")[0].strip().lower()


def _upstream_host(upstream_url: str) -> str:
    try:
        return urlsplit(upstream_url).netloc
    except Exception:
        return ""


def _invalid_upstream_response_error(
    response: httpx.Response,
    upstream_url: str,
    api_format: str,
    body_preview: str,
) -> dict:
    return {
        "error": {
            "type": "invalid_upstream_response",
            "status": response.status_code,
            "contentType": _content_type_header(response) or "unknown",
            "bodyPreview": body_preview,
            "upstreamHost": _upstream_host(upstream_url),
            "apiFormat": api_format,
            "message": i18n.t("proxy.non_json_error"),
        }
    }


def _log_invalid_upstream_response(response: httpx.Response, preview: str):
    log_buffer.add(
        "ERROR",
        (
            f"{i18n.t('proxy.log_non_json_prefix')}"
            f"HTTP {response.status_code}, content-type={_content_type_header(response) or 'unknown'}, "
            f"preview={preview or '(empty)'}"
        ),
    )


def _is_socks_dependency_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return "socks" in text and ("socksio" in text or "httpx[socks]" in text)


def _socks_dependency_error_message() -> str:
    return i18n.t("proxy.socks_missing_error")


def _proxy_log_value(proxy: str) -> str:
    return _redact_sensitive_text(proxy, 300)


def _connection_error_message(proxy: Optional[str], message: str) -> str:
    if proxy:
        return i18n.t(
            "proxy.connect_error_via_proxy",
            proxy=_proxy_log_value(proxy),
            error=message,
        )
    return i18n.t("proxy.connect_error_direct", error=message)


def _stream_content_type_compatible(response: httpx.Response) -> bool:
    content_type = _content_type(response)
    if not content_type:
        return True
    return (
        content_type == "text/event-stream"
        or content_type == "application/json"
        or content_type.endswith("+json")
        or content_type == "application/x-ndjson"
    )


def _normalize_usage(usage) -> dict:
    """Ensure Anthropic usage objects always include integer input_tokens and output_tokens."""
    def token_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    normalized = dict(usage) if isinstance(usage, dict) else {}
    input_tokens = (
        normalized.get("input_tokens")
        if normalized.get("input_tokens") is not None
        else normalized.get("prompt_tokens")
    )
    output_tokens = (
        normalized.get("output_tokens")
        if normalized.get("output_tokens") is not None
        else normalized.get("completion_tokens")
    )
    normalized["input_tokens"] = token_int(input_tokens)
    normalized["output_tokens"] = token_int(output_tokens)
    return normalized


def _normalize_content(content) -> list:
    """Normalize common upstream content variants into Anthropic content blocks."""
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _normalize_anthropic_message(message: dict, model: str) -> dict:
    """Populate fields expected by Claude Desktop on Anthropic message responses."""
    normalized = dict(message) if isinstance(message, dict) else {}
    normalized.setdefault("id", f"msg_{uuid.uuid4().hex[:12]}")
    normalized.setdefault("type", "message")
    normalized.setdefault("role", "assistant")
    normalized["model"] = normalized.get("model") or model
    normalized["content"] = _normalize_content(normalized.get("content"))
    normalized["usage"] = _normalize_usage(normalized.get("usage"))
    return normalized


def _normalize_anthropic_response(upstream_data: dict, model: str) -> dict:
    """Normalize Anthropic-compatible responses so Claude Desktop can safely read usage.input_tokens."""
    if not isinstance(upstream_data, dict) or upstream_data.get("error"):
        return upstream_data
    if upstream_data.get("type") == "message" or "content" in upstream_data:
        return _normalize_anthropic_message(upstream_data, model)
    return upstream_data


def _normalize_anthropic_sse_event(event: dict, model: str) -> dict:
    """Normalize usage fields inside Anthropic-compatible SSE events."""
    if not isinstance(event, dict):
        return event
    normalized = dict(event)
    event_type = normalized.get("type")
    if event_type == "message_start":
        normalized["message"] = _normalize_anthropic_message(
            normalized.get("message") or {},
            model,
        )
    elif event_type == "message_delta":
        normalized["usage"] = _normalize_usage(normalized.get("usage"))
    elif "usage" in normalized:
        normalized["usage"] = _normalize_usage(normalized.get("usage"))
    return normalized


async def forward_request(
    body: dict,
    provider: dict,
    request_id: str,
) -> dict:
    """Forward a non-streaming request to the upstream API."""
    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))

    if api_format == "openai_chat":
        upstream_url = build_upstream_url(provider.get("baseUrl", ""), api_format)
        upstream_body = _anthropic_to_openai_body(body, stream=False)
    else:
        upstream_url = build_upstream_url(provider.get("baseUrl", ""), api_format)
        upstream_body = dict(body)
        upstream_body.pop("stream", None)
        upstream_body = apply_anthropic_request_options(upstream_body, provider)

    headers = get_upstream_headers(provider)

    log_buffer.add("INFO", i18n.t("proxy.log_forward", url=upstream_url))
    log_buffer.add(
        "INFO",
        i18n.t(
            "proxy.log_model",
            src=body.get("model", ""),
            dst=upstream_body.get("model", ""),
        ),
    )

    proxy = _get_http_proxy()
    if proxy:
        log_buffer.add("INFO", i18n.t("proxy.log_upstream_proxy", proxy=_proxy_log_value(proxy)))

    try:
        async with httpx.AsyncClient(timeout=120.0, proxy=proxy) as client:
            resp = await client.post(
                upstream_url,
                json=upstream_body,
                headers=headers,
            )

        stats.record(resp.is_success)
        log_buffer.add(
            "SUCCESS" if resp.is_success else "ERROR",
            i18n.t(
                "proxy.log_response",
                status=resp.status_code,
                seconds=round(resp.elapsed.total_seconds(), 2),
            ),
        )

        if not resp.is_success:
            raw_error_text = resp.text or i18n.t("proxy.upstream_api_error")
            error_text = _redact_sensitive_text(raw_error_text)
            if _is_max_unsupported_error(resp.status_code, raw_error_text):
                return {
                    "id": "msg_hint",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", ""),
                    "content": [{"type": "text", "text": i18n.t("proxy.max_unsupported_hint")}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            return {
                "error": {
                    "type": "upstream_error",
                    "status": resp.status_code,
                    "message": error_text,
                }
            }

        try:
            upstream_data = resp.json()
        except json.JSONDecodeError:
            stats.failed += 1
            stats.success = max(0, stats.success - 1)
            preview = _redact_sensitive_text(resp.text)
            _log_invalid_upstream_response(resp, preview)
            return _invalid_upstream_response_error(resp, upstream_url, api_format, preview)

        if api_format == "openai_chat":
            return _openai_to_anthropic(upstream_data, body.get("model", ""))
        return _normalize_anthropic_response(upstream_data, body.get("model", ""))

    except httpx.TimeoutException:
        stats.record(False)
        log_buffer.add("ERROR", i18n.t("proxy.log_timeout"))
        return {
            "error": {
                "type": "timeout",
                "message": i18n.t("proxy.timeout_error"),
            }
        }
    except Exception as e:
        stats.record(False)
        message = f"{e.__class__.__name__}: {str(e)}".rstrip()
        if _is_socks_dependency_error(e):
            log_buffer.add("ERROR", i18n.t("proxy.log_socks_missing", error=message))
            return {
                "error": {
                    "type": "socks_proxy_dependency_missing",
                    "message": _socks_dependency_error_message(),
                }
            }
        proxy = _get_http_proxy()
        log_buffer.add("ERROR", i18n.t("proxy.log_request_failed", error=message))
        return {
            "error": {
                "type": "connection_error",
                "message": _connection_error_message(proxy, message),
            }
        }


async def forward_request_stream(
    body: dict,
    provider: dict,
    request_id: str,
):
    """Forward a streaming request to the upstream API (SSE)."""
    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))

    if api_format == "openai_chat":
        upstream_url = build_upstream_url(provider.get("baseUrl", ""), api_format)
        upstream_body = _anthropic_to_openai_body(body, stream=True)
    else:
        upstream_url = build_upstream_url(provider.get("baseUrl", ""), api_format)
        upstream_body = dict(body)
        upstream_body = apply_anthropic_request_options(upstream_body, provider)
        upstream_body["stream"] = True

    headers = get_upstream_headers(provider)

    log_buffer.add("INFO", i18n.t("proxy.log_stream_forward", url=upstream_url))

    proxy = _get_http_proxy()
    if proxy:
        log_buffer.add("INFO", i18n.t("proxy.log_upstream_proxy", proxy=_proxy_log_value(proxy)))

    try:
        async with httpx.AsyncClient(timeout=300.0, proxy=proxy) as client:
            async with client.stream(
                "POST",
                upstream_url,
                json=upstream_body,
                headers=headers,
            ) as resp:

                log_buffer.add(
                    "SUCCESS" if resp.is_success else "ERROR",
                    i18n.t("proxy.log_stream_connected", status=resp.status_code),
                )

                if not resp.is_success:
                    stats.record(False)
                    raw_error_text = (await resp.aread()).decode("utf-8", errors="replace")
                    error_text = _redact_sensitive_text(raw_error_text)
                    if _is_max_unsupported_error(resp.status_code, raw_error_text):
                        model = body.get("model", "")
                        hint = i18n.t("proxy.max_unsupported_hint")
                        yield f'event: message_start\ndata: {{"type":"message_start","message":{{"id":"msg_hint","type":"message","role":"assistant","content":[],"model":"{model}","stop_reason":null,"stop_sequence":null,"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
                        yield f'event: content_block_start\ndata: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n'
                        yield f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{hint}"}}}}\n\n'
                        yield f'event: content_block_stop\ndata: {{"type":"content_block_stop","index":0}}\n\n'
                        yield f'event: message_delta\ndata: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","stop_sequence":null}},"usage":{{"output_tokens":0}}}}\n\n'
                        yield f'event: message_stop\ndata: {{"type":"message_stop"}}\n\n'
                        return
                    error_event = {
                        "type": "error",
                        "error": {
                            "type": "upstream_error",
                            "status": resp.status_code,
                            "message": error_text or i18n.t("proxy.upstream_api_error"),
                        },
                    }
                    yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                    return

                if not _stream_content_type_compatible(resp):
                    stats.record(False)
                    raw_text = (await resp.aread()).decode("utf-8", errors="replace")
                    preview = _redact_sensitive_text(raw_text)
                    _log_invalid_upstream_response(resp, preview)
                    error_event = {
                        "type": "error",
                        "error": _invalid_upstream_response_error(
                            resp,
                            upstream_url,
                            api_format,
                            preview,
                        )["error"],
                    }
                    yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                    return

                if api_format == "openai_chat":
                    prefix = "data: "
                    emitted_event = False
                    preview_lines = []
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        data_str = ""
                        if line.startswith(prefix):
                            data_str = line[len(prefix):]
                        else:
                            data_str = line.strip()

                        if data_str.strip() == "[DONE]":
                            emitted_event = True
                            yield "event: done\ndata: {}\n\n"
                            continue
                        try:
                            openai_chunk = json.loads(data_str)
                            anthropic_chunk = _openai_chunk_to_anthropic(openai_chunk, body.get("model", ""))
                            emitted_event = True
                            yield f"data: {json.dumps(anthropic_chunk)}\n\n"
                        except json.JSONDecodeError:
                            if len(preview_lines) < 8:
                                preview_lines.append(line)
                            continue
                    if not emitted_event:
                        stats.record(False)
                        preview = _redact_sensitive_text("\n".join(preview_lines))
                        _log_invalid_upstream_response(resp, preview)
                        error_event = {
                            "type": "error",
                            "error": _invalid_upstream_response_error(
                                resp,
                                upstream_url,
                                api_format,
                                preview,
                            )["error"],
                        }
                        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                        return
                else:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[len("data:"):].strip()
                            if data_str and data_str != "[DONE]":
                                try:
                                    event = json.loads(data_str)
                                    event = _normalize_anthropic_sse_event(event, body.get("model", ""))
                                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n"
                                    continue
                                except json.JSONDecodeError:
                                    pass
                        yield line + "\n"

                stats.record(True)
                log_buffer.add("SUCCESS", i18n.t("proxy.log_stream_done"))

    except httpx.TimeoutException:
        stats.record(False)
        log_buffer.add("ERROR", i18n.t("proxy.log_stream_timeout"))
        error_event = {
            "type": "error",
            "error": {
                "type": "timeout",
                "message": i18n.t("proxy.stream_timeout_error"),
            },
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
    except Exception as e:
        stats.record(False)
        message = f"{e.__class__.__name__}: {str(e)}".rstrip()
        if _is_socks_dependency_error(e):
            log_buffer.add("ERROR", i18n.t("proxy.log_socks_missing", error=message))
            error_event = {
                "type": "error",
                "error": {
                    "type": "socks_proxy_dependency_missing",
                    "message": _socks_dependency_error_message(),
                },
            }
            yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            return
        proxy = _get_http_proxy()
        log_buffer.add("ERROR", i18n.t("proxy.log_stream_failed", error=message))
        error_event = {
            "type": "error",
            "error": {
                "type": "connection_error",
                "message": _connection_error_message(proxy, message),
            },
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"


def _openai_to_anthropic(openai_resp: dict, model: str) -> dict:
    """Convert an OpenAI Chat response into Anthropic Messages format."""
    return openai_chat_to_anthropic(openai_resp, model)


def _openai_chunk_to_anthropic(chunk: dict, model: str) -> dict:
    """Convert an OpenAI streaming chunk into Anthropic SSE format."""
    return openai_chat_chunk_to_anthropic(chunk, model)


# ========== FastAPI Application ==========

from backend.config import get_active_provider, get_gateway_api_key, get_providers, get_settings


def _get_http_proxy() -> Optional[str]:
    """Return the configured upstream proxy URL, falling back to HTTP_PROXY / HTTPS_PROXY / ALL_PROXY."""
    settings = get_settings()
    if not settings.get("upstreamProxyEnabled", True):
        return None
    configured = str(settings.get("upstreamProxy") or "").strip()
    if configured:
        if "://" not in configured:
            configured = f"http://{configured}"
        return configured
    for env in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        val = os.environ.get(env, os.environ.get(env.lower(), "")).strip()
        if val:
            return val
    return None


def create_proxy_app() -> FastAPI:
    """Create the local proxy FastAPI application."""
    app = FastAPI(title="Junction Proxy", version="1.0.28")

    def upstream_error_status(result: dict) -> int:
        """Convert upstream errors into HTTP error status codes so Claude Desktop does not treat them as 200 OK."""
        error = result.get("error") if isinstance(result, dict) else None
        status = error.get("status") if isinstance(error, dict) else None
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            return 502
        return status_code if 400 <= status_code <= 599 else 502

    def gateway_auth_failed(request: Request) -> bool:
        gateway_api_key = get_gateway_api_key()
        if not gateway_api_key:
            return True
        auth_header = request.headers.get("authorization", "")
        bearer_token = auth_header.removeprefix("Bearer ").strip()
        x_api_key = request.headers.get("x-api-key", "").strip()
        return gateway_api_key not in {bearer_token, x_api_key}

    def gateway_auth_error() -> JSONResponse:
        log_buffer.add("ERROR", i18n.t("proxy.log_gateway_auth_failed"))
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid gateway API key"}},
        )

    @app.get("/health")
    @app.get("/status")
    async def health():
        return {"status": "ok", "stats": stats.to_dict()}

    @app.api_route("/v1/models", methods=["GET", "OPTIONS"])
    @app.api_route("/claude/v1/models", methods=["GET", "OPTIONS"])
    async def handle_models(request: Request):
        if request.method == "OPTIONS":
            return JSONResponse(
                content={},
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        if gateway_auth_failed(request):
            return gateway_auth_error()
        settings = get_settings()
        expose_all = bool(settings.get("exposeAllProviderModels"))
        provider = get_active_provider()
        return gateway_models_response(
            provider,
            providers=get_providers() if expose_all else None,
            expose_all=expose_all,
        )

    @app.api_route("/v1/messages", methods=["POST", "OPTIONS"])
    @app.api_route("/claude/v1/messages", methods=["POST", "OPTIONS"])
    async def handle_messages(request: Request):
        if request.method == "OPTIONS":
            return JSONResponse(
                content={},
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                },
            )

        request_id = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
        body = await request.json()

        if gateway_auth_failed(request):
            return gateway_auth_error()

        settings = get_settings()
        removed_billing_headers = rectify_claude_code_billing_header(
            body,
            enabled=settings.get("enableBillingHeaderRectifier") is not False,
        )
        if removed_billing_headers:
            log_buffer.add("INFO", i18n.t("proxy.log_billing_header_stripped", count=removed_billing_headers))
        expose_all = bool(settings.get("exposeAllProviderModels"))
        providers = get_providers() if expose_all else []
        # Get the active provider; when exposeAllProviderModels is enabled, allow model aliases to route to other providers.
        provider = get_active_provider()
        alias_provider, alias_model, alias_hit = resolve_model_alias(providers, body.get("model", "")) if expose_all else (None, "", False)
        if alias_hit and alias_provider:
            provider = alias_provider
        if not provider or not provider.get("apiKey"):
            log_buffer.add("ERROR", i18n.t("proxy.log_no_active_provider"))
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "No active provider configured"}},
            )

        # Model name translation
        original_model = body.get("model", "")
        route_model = alias_model if alias_hit else original_model
        if _is_unmapped_desktop_route(route_model, provider):
            log_buffer.add("ERROR", i18n.t("proxy.log_unmapped_model", model=route_model))
            return JSONResponse(
                status_code=400,
                content=unmapped_desktop_route_error(route_model),
            )
        mapped_model = map_model(route_model, provider)
        body["model"] = mapped_model

        log_buffer.add("INFO", i18n.t("proxy.log_request_post"))
        log_buffer.add("INFO", i18n.t("proxy.log_model_mapping", src=original_model, dst=mapped_model))

        # Warn if tools are sent to DeepSeek's Anthropic-compatible endpoint
        if body.get("tools") and _provider_kind(provider) == "deepseek":
            log_buffer.add("WARNING", i18n.t("proxy.log_deepseek_tools_warn"))

        is_stream = body.get("stream", False)

        if is_stream:
            return StreamingResponse(
                forward_request_stream(body, provider, request_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": "*",
                },
            )
        else:
            result = await forward_request(body, provider, request_id)
            if isinstance(result, dict) and result.get("error"):
                return JSONResponse(
                    status_code=upstream_error_status(result),
                    content=result,
                )
            return JSONResponse(content=result)

    return app
