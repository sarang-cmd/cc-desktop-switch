"""Provider helper utilities: model list fetching, mapping suggestions, balance/usage queries, and protocol detection."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend import i18n
from backend.api_adapters import normalize_api_format
from backend.model_alias import model_mappings_with_legacy_aliases
from backend.proxy import build_upstream_url, get_upstream_headers


MODEL_EXCLUDE_KEYWORDS = (
    "embedding",
    "rerank",
    "moderation",
    "whisper",
    "tts",
    "image",
    "vision",
    "audio",
)


def _clean_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _replace_path_suffix(url: str, suffixes: tuple[str, ...], replacement: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    lower = path.lower()
    for suffix in suffixes:
        if lower.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, f"{path.rstrip('/')}/{replacement.lstrip('/')}", "", ""))


def model_endpoint_candidates(provider: dict) -> list[str]:
    """Generate candidate model list endpoints for a provider.

    Different OpenAI/Anthropic-compatible APIs use varying path conventions,
    so we build a small ordered list of candidate endpoints to probe.
    """
    base_url = _clean_base_url(provider.get("baseUrl", ""))
    if not base_url:
        return []

    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))
    upstream = build_upstream_url(base_url, api_format)
    candidates: list[str] = []

    if api_format == "openai_chat":
        candidates.append(_replace_path_suffix(upstream, ("/chat/completions", "/completions"), "/models"))
        candidates.append(f"{base_url}/models")
    else:
        candidates.append(_replace_path_suffix(upstream, ("/v1/messages", "/messages"), "/v1/models"))
        if base_url.lower().endswith("/v1"):
            candidates.append(f"{base_url}/models")
        candidates.append(f"{base_url}/models")
        parts = urlsplit(base_url)
        stripped_path = parts.path.rstrip("/")
        if stripped_path.lower().endswith("/anthropic"):
            root_path = stripped_path[: -len("/anthropic")]
            root_url = urlunsplit((parts.scheme, parts.netloc, root_path.rstrip("/"), "", "")).rstrip("/")
            candidates.append(f"{root_url}/models")
            candidates.append(f"{root_url}/v1/models")

    seen = set()
    unique = []
    for item in candidates:
        if item and item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def _model_id_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("id", "name", "model", "model_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_model_ids(payload: Any) -> list[str]:
    """Extract model IDs from OpenAI, Anthropic, or generic model list responses."""
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if not candidates and isinstance(payload.get("data"), dict):
            data = payload["data"]
            for key in ("models", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    candidates = value
                    break

    model_ids = []
    seen = set()
    for item in candidates:
        model_id = _model_id_from_item(item)
        if not model_id:
            continue
        if model_id in seen:
            continue
        model_ids.append(model_id)
        seen.add(model_id)
    return model_ids


def _usable_model_ids(model_ids: list[str]) -> list[str]:
    usable = []
    for model_id in model_ids:
        lower = model_id.lower()
        if any(keyword in lower for keyword in MODEL_EXCLUDE_KEYWORDS):
            continue
        usable.append(model_id)
    return usable or model_ids


def _pick_model(model_ids: list[str], keywords: tuple[str, ...], fallback_index: int = 0) -> str:
    for keyword in keywords:
        for model_id in model_ids:
            if keyword in model_id.lower():
                return model_id
    if not model_ids:
        return ""
    return model_ids[min(fallback_index, len(model_ids) - 1)]


def suggest_model_mappings(model_ids: list[str]) -> dict:
    """Suggest default Claude slot mappings based on model names."""
    usable = _usable_model_ids(model_ids)
    sonnet = _pick_model(
        usable,
        ("sonnet", "claude", "k2", "glm-5.1", "qwen3-max", "max", "pro", "chat"),
    )
    haiku = _pick_model(
        usable,
        ("haiku", "flash", "lite", "mini", "turbo", "fast", "v3", "chat"),
        fallback_index=0,
    )
    opus = _pick_model(
        usable,
        ("opus", "reasoner", "thinking", "r1", "max", "pro", "plus"),
        fallback_index=0,
    )
    default = sonnet or opus or haiku or (usable[0] if usable else "")
    return model_mappings_with_legacy_aliases({
        "default": default,
        "opus_4_7": opus or default,
        "opus_4_6": "",
        "opus_3": "",
        "sonnet_4_6": sonnet or default,
        "sonnet_4_5": "",
        "haiku_4_5": haiku or default,
    })


async def fetch_provider_models(provider: dict) -> dict:
    """Query the provider's model list endpoint and return model IDs and suggested mappings."""
    endpoints = model_endpoint_candidates(provider)
    if not endpoints:
        return {
            "success": False,
            "code": "invalid_base_url",
            "message": i18n.t("provider_tools.invalid_base_url"),
            "models": [],
            "suggested": {},
        }

    headers = get_upstream_headers(provider)
    headers.pop("Content-Type", None)
    errors = []
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for endpoint in endpoints:
            try:
                response = await client.get(endpoint, headers=headers)
            except httpx.RequestError as exc:
                errors.append(f"{endpoint}: {exc.__class__.__name__}")
                continue
            if not response.is_success:
                errors.append(f"{endpoint}: HTTP {response.status_code}")
                continue
            try:
                payload = response.json()
            except ValueError:
                errors.append(i18n.t("provider_tools.non_json_response", endpoint=endpoint))
                continue
            model_ids = extract_model_ids(payload)
            if model_ids:
                return {
                    "success": True,
                    "endpoint": endpoint,
                    "models": model_ids,
                    "suggested": suggest_model_mappings(model_ids),
                }
            errors.append(i18n.t("provider_tools.no_models_found", endpoint=endpoint))

    return {
        "success": False,
        "code": "fetch_models_failed",
        "message": i18n.t("provider_tools.fetch_models_failed"),
        "models": [],
        "suggested": {},
        "errors": errors[-5:],
    }


def _provider_kind(provider: dict) -> str:
    host = urlsplit(str(provider.get("baseUrl") or "")).netloc.lower()
    if host == "api.deepseek.com":
        return "deepseek"
    probe = f"{provider.get('name', '')} {provider.get('baseUrl', '')}".lower()
    if "siliconflow" in probe:
        return "siliconflow"
    if "openrouter" in probe:
        return "openrouter"
    if "novita" in probe:
        return "novita"
    if "stepfun" in probe or "step" in probe:
        return "stepfun"
    return "unknown"


def balance_endpoint(provider: dict) -> tuple[str, str] | None:
    kind = _provider_kind(provider)
    base = _clean_base_url(provider.get("baseUrl", "")).lower()
    if kind == "deepseek":
        return kind, "https://api.deepseek.com/user/balance"
    if kind == "siliconflow":
        host = "https://api.siliconflow.cn"
        if ".com" in base:
            host = "https://api.siliconflow.com"
        return kind, f"{host}/v1/user/info"
    if kind == "openrouter":
        return kind, "https://openrouter.ai/api/v1/credits"
    if kind == "novita":
        return kind, "https://api.novita.ai/v3/user/balance"
    if kind == "stepfun":
        return kind, "https://api.stepfun.com/v1/accounts"
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _money_item(label: str, remaining=None, total=None, used=None, unit: str = "") -> dict:
    return {
        "label": label,
        "remaining": _float_or_none(remaining),
        "total": _float_or_none(total),
        "used": _float_or_none(used),
        "unit": unit,
    }


def normalize_balance_payload(kind: str, payload: dict) -> list[dict]:
    """Normalize vendor-specific balance/usage responses into a unified list."""
    if kind == "deepseek":
        items = []
        for item in payload.get("balance_infos", []) or []:
            if not isinstance(item, dict):
                continue
            currency = item.get("currency", "CNY")
            items.append(_money_item(
                label=str(currency),
                remaining=item.get("total_balance"),
                total=item.get("granted_balance"),
                used=item.get("topped_up_balance"),
                unit=str(currency),
            ))
        return items

    if kind == "openrouter":
        data = payload.get("data", payload)
        total = _float_or_none(data.get("total_credits"))
        used = _float_or_none(data.get("total_usage"))
        remaining = total - used if total is not None and used is not None else None
        return [_money_item("credits", remaining=remaining, total=total, used=used, unit="USD")]

    data = payload.get("data", payload)
    if isinstance(data, dict):
        for remaining_key in ("balance", "remaining", "available_balance", "availableBalance", "credit"):
            if remaining_key in data:
                return [_money_item(
                    "balance",
                    remaining=data.get(remaining_key),
                    total=data.get("total") or data.get("totalBalance") or data.get("total_credits"),
                    used=data.get("used") or data.get("usage") or data.get("usedBalance"),
                    unit=str(data.get("currency") or data.get("unit") or ""),
                )]
    return []


async def query_provider_usage(provider: dict) -> dict:
    """Query provider balance/usage for vendors with known public balance endpoints."""
    if not provider.get("apiKey"):
        return {
            "success": False,
            "code": "save_api_key_first",
            "message": i18n.t("provider_tools.save_api_key_first"),
        }

    endpoint_info = balance_endpoint(provider)
    if not endpoint_info:
        return {
            "success": True,
            "supported": False,
            "items": [],
            "code": "usage_unsupported",
            "message": i18n.t("provider_tools.usage_unsupported"),
        }

    kind, endpoint = endpoint_info
    headers = get_upstream_headers(provider)
    headers.pop("Content-Type", None)
    timeout = httpx.Timeout(12.0, connect=6.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(endpoint, headers=headers)
    except httpx.RequestError as exc:
        return {
            "success": True,
            "supported": True,
            "ok": False,
            "code": "usage_query_failed",
            "message": i18n.t("provider_tools.usage_query_failed", error=exc.__class__.__name__),
            "items": [],
        }

    if not response.is_success:
        return {
            "success": True,
            "supported": True,
            "ok": False,
            "statusCode": response.status_code,
            "code": "usage_http_error",
            "message": i18n.t("provider_tools.usage_http_error", status=response.status_code),
            "items": [],
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "success": True,
            "supported": True,
            "ok": False,
            "code": "usage_non_json",
            "message": i18n.t("provider_tools.usage_non_json"),
            "items": [],
        }

    items = normalize_balance_payload(kind, payload)
    return {
        "success": True,
        "supported": True,
        "ok": bool(items),
        "endpoint": endpoint,
        "items": items,
        "code": "usage_ok" if items else "usage_no_fields",
        "message": i18n.t("provider_tools.usage_ok") if items else i18n.t("provider_tools.usage_no_fields"),
    }


async def check_model_available(provider: dict, model: str) -> dict:
    """Check whether a specific model is available via a minimal chat completion request."""
    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))
    headers = get_upstream_headers(provider)
    url = build_upstream_url(provider.get("baseUrl", ""), api_format)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.is_success:
            return {
                "available": True,
                "code": "model_ok",
                "message": i18n.t("provider_tools.model_ok"),
            }

        try:
            error_data = resp.json()
            error_msg = (
                error_data.get("error", {}).get("message", "")
                or error_data.get("message", "")
                or resp.text[:200]
            )
        except ValueError:
            error_msg = resp.text[:200] or f"HTTP {resp.status_code}"

        return {"available": False, "message": error_msg or f"HTTP {resp.status_code}"}

    except httpx.TimeoutException:
        return {
            "available": False,
            "code": "timeout",
            "message": i18n.t("provider_tools.timeout"),
        }
    except httpx.ConnectError:
        return {
            "available": False,
            "code": "connect_failed",
            "message": i18n.t("provider_tools.connect_failed"),
        }
    except Exception as e:
        return {"available": False, "message": f"{e.__class__.__name__}: {str(e)[:200]}"}


# ── Protocol Format Auto-Detection ──

STANDARD_ENDPOINTS = [
    ("/v1/messages", "anthropic"),
    ("/messages", "anthropic"),
    ("/v1/chat/completions", "openai_chat"),
    ("/chat/completions", "openai_chat"),
    ("/v1/responses", "openai_responses"),
    ("/responses", "openai_responses"),
]


def _detect_format_from_response(data: dict, status_code: int, expected_format: str) -> tuple[bool, str]:
    """Identify protocol format from response body and status code."""
    if not isinstance(data, dict):
        return False, ""

    # Success response check
    if status_code == 200:
        if expected_format == "anthropic":
            if data.get("type") == "message" and isinstance(data.get("content"), list):
                return True, "high"
        elif expected_format == "openai_chat":
            if "choices" in data and isinstance(data.get("choices"), list):
                return True, "high"
        elif expected_format == "openai_responses":
            if "output" in data and isinstance(data.get("output"), list):
                return True, "high"

    # Error response check
    if status_code in {400, 422}:
        if expected_format == "anthropic":
            if data.get("type") == "error" and isinstance(data.get("error"), dict):
                return True, "high"
        elif expected_format in {"openai_chat", "openai_responses"}:
            error = data.get("error")
            if isinstance(error, dict) and "message" in error:
                return True, "high"

    return False, ""


async def _probe_single_endpoint(url: str, expected_format: str, api_key: str) -> dict[str, Any]:
    """Send a probe request to a single endpoint."""
    body: dict[str, Any] = {}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if expected_format == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": "___probe_test___",
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 1,
        }
    elif expected_format == "openai_chat":
        body = {
            "model": "___probe_test___",
            "messages": [{"role": "user", "content": "test"}],
        }
    elif expected_format == "openai_responses":
        body = {
            "model": "___probe_test___",
            "input": "test",
        }

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.post(url, json=body, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}

        detected, confidence = _detect_format_from_response(data, resp.status_code, expected_format)
        if detected:
            return {"detected": True, "confidence": confidence}

        if resp.status_code in {401, 403}:
            return {"detected": False, "exists": True}

    except Exception:
        pass

    return {"detected": False, "exists": False}


async def detect_api_format(base_url: str, api_key: str = "") -> dict[str, Any]:
    """Probe standard endpoints to detect the upstream API protocol format."""
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return {
            "success": False,
            "code": "enter_base_url",
            "message": i18n.t("provider_tools.enter_base_url"),
        }

    for path, fmt in STANDARD_ENDPOINTS:
        url = f"{clean}{path}"
        result = await _probe_single_endpoint(url, fmt, api_key)
        if result.get("detected"):
            return {
                "success": True,
                "apiFormat": fmt,
                "endpoint": url,
                "confidence": result.get("confidence", "medium"),
            }

    return {
        "success": False,
        "code": "detect_failed",
        "message": i18n.t("provider_tools.detect_failed"),
    }
