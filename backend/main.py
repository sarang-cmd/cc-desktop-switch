"""FastAPI application: local management API and static file server."""

import asyncio
import json
import os
import platform as platform_module
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import ccswitch_import
from backend import config as cfg
from backend import i18n
from backend import provider_tools
from backend import registry
from backend import update as updater
from backend.api_adapters import normalize_api_format
from backend.model_alias import desktop_route_ids, provider_model_ids
from backend.proxy import (
    build_upstream_url,
    create_proxy_app,
    get_upstream_headers,
    log_buffer as proxy_logs,
    stats as proxy_stats,
)

# ── Path Configuration ──
# Frontend directory: frontend/ under the repository root
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_update_quit_handler: Optional[Callable[[], None]] = None
_app_activation_handler: Optional[Callable[[], bool]] = None
_admin_token = secrets.token_urlsafe(32)
ADMIN_TOKEN_HEADER = "x-ccds-admin-token"
STATIC_REQUEST_HEADER = "x-ccds-request"
DIAGNOSTICS_FORMAT = "ccds.diagnostics.v1"
REDACTED = "******"
_SENSITIVE_FIELD_RE = re.compile(
    r"(api[-_]?key|gatewayapikey|token|secret|password|authorization|cookie|headers)",
    re.IGNORECASE,
)


def get_admin_token() -> str:
    """Return the local management API token for the current process."""
    return _admin_token


def verify_admin_token(value: str) -> bool:
    """Verify the local management API token."""
    return bool(value) and secrets.compare_digest(str(value), _admin_token)


def _popen_hidden(command: list[str], *, detached: bool = False, show_window: bool = False):
    """Launch an external process without popping up a black console window on Windows."""
    kwargs = {"close_fds": True}
    if detached:
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        creationflags = 0
        if not show_window:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = creationflags
    elif detached:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def register_update_quit_handler(handler: Optional[Callable[[], None]]):
    """Register a callback to gracefully quit the main application before launching an update installer."""
    global _update_quit_handler
    _update_quit_handler = handler


def register_app_activation_handler(handler: Optional[Callable[[], bool]]):
    """Register a callback to bring the existing window to the foreground on a second launch."""
    global _app_activation_handler
    _app_activation_handler = handler


def _get_update_quit_handler() -> Optional[Callable[[], None]]:
    handler = _update_quit_handler
    return handler if callable(handler) else None


def _activate_existing_app() -> bool:
    handler = _app_activation_handler
    if not callable(handler):
        return False
    try:
        return bool(handler())
    except Exception:
        return False


def _schedule_update_quit_for_install(handler: Callable[[], None], delay: float = 0.8) -> bool:
    """Delay quitting slightly so the frontend receives the installation response first."""
    if not callable(handler):
        return False

    def _invoke_handler():
        try:
            handler()
        except Exception:
            return

    timer = threading.Timer(delay, _invoke_handler)
    timer.daemon = True
    timer.start()
    return True


def _launch_update_installer(installer_path: str, platform: str) -> bool:
    """Launch the installer; in desktop mode, quit the main app first and let the helper open the installer."""
    should_wait_for_quit = platform.startswith(("windows-", "macos-"))
    quit_handler = _get_update_quit_handler() if should_wait_for_quit else None
    if should_wait_for_quit and quit_handler:
        command = updater.install_after_quit_command(installer_path, platform, os.getpid())
        # Windows PowerShell can skip the encoded helper when launched with
        # DETACHED_PROCESS from the packaged app. Keep the window hidden, but
        # do not detach here; the helper waits until this app exits before
        # launching the installer.
        _popen_hidden(command, detached=not platform.startswith("windows-"))
        return _schedule_update_quit_for_install(quit_handler)

    command = updater.install_command(installer_path, platform)
    if platform.startswith("windows-"):
        _popen_hidden(command, detached=True, show_window=True)
    else:
        _popen_hidden(command, detached=True)
    return False


def _public_provider(provider: Optional[dict]) -> Optional[dict]:
    """Return a display-safe provider dict without exposing the raw API Key."""
    if not provider:
        return None
    public = dict(provider)
    if "apiKey" in public:
        public["hasApiKey"] = bool(public.get("apiKey"))
        public.pop("apiKey", None)
    public.pop("extraHeaders", None)
    return public


def _redact_text(value: str, limit: int = 500) -> str:
    """Redact sensitive tokens from text for diagnostic bundles and support output."""
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
    text = re.sub(r"(?i)\b(sk-[a-z0-9_-]{8,}|ccds_[a-z0-9_-]{8,})\b", REDACTED, text)
    text = re.sub(r"([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@", r"\1******:******@", text, flags=re.IGNORECASE)
    return text[:limit]


def _is_sensitive_field(key: str) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(str(key or "")))


def _redact_value(value, field_name: str = ""):
    """Recursively redact diagnostic output so configuration keys or log tokens never leak."""
    if _is_sensitive_field(field_name):
        if isinstance(value, dict):
            return {str(key): REDACTED if item else "" for key, item in value.items()}
        if isinstance(value, list):
            return [REDACTED if item else "" for item in value]
        return REDACTED if value else ""
    if isinstance(value, dict):
        return {str(key): _redact_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, field_name) for item in value]
    if isinstance(value, str):
        return _redact_text(value, 2000)
    return value


def _safe_url_parts(url: str) -> dict:
    """Keep only credential-free and query-free URL diagnostic components."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return {"scheme": "", "host": "", "path": ""}
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "port": parsed.port,
        "path": parsed.path or "",
        "base": urlunparse((parsed.scheme, parsed.netloc.split("@")[-1], parsed.path, "", "", "")),
    }


def _normalize_proxy_url(value: str) -> str:
    """Normalize a user-supplied upstream proxy URL so urlparse can parse it."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return f"http://{text}"
    return text


def _is_local_proxy_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    return normalized == "localhost" or normalized == "::1" or normalized.startswith("127.")


def _check_local_proxy_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.35):
            return True
    except OSError:
        return False


def _upstream_proxy_diagnostics(settings: dict) -> dict:
    """Return credential-free upstream proxy diagnostics."""
    raw_value = str(settings.get("upstreamProxy") or "").strip()
    result = {
        "enabled": bool(settings.get("upstreamProxyEnabled")),
        "configured": bool(raw_value),
        "value": REDACTED if raw_value else "",
        "scheme": "",
        "host": "",
        "port": None,
        "isLocal": False,
        "reachable": None,
    }
    if not raw_value:
        return result

    try:
        parsed = urlparse(_normalize_proxy_url(raw_value))
        port = parsed.port
    except ValueError:
        result["parseError"] = "invalid_proxy_url"
        return result

    host = parsed.hostname or ""
    result.update({
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "isLocal": _is_local_proxy_host(host),
    })
    if result["enabled"] and result["isLocal"] and port:
        result["reachable"] = _check_local_proxy_reachable(host, port)
    return result


def _diagnostics_provider(provider: dict) -> dict:
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "apiFormat": provider.get("apiFormat") or "anthropic",
        "authScheme": provider.get("authScheme") or "bearer",
        "hasApiKey": bool(provider.get("apiKey")),
        "baseUrl": _safe_url_parts(provider.get("baseUrl", "")),
        "mappedSlots": sorted([key for key, value in models.items() if value]),
    }


def _diagnostics_recent_logs(limit: int = 50) -> list[dict]:
    logs = proxy_logs.get_all()[-limit:]
    return [
        {
            "time": item.get("time"),
            "level": item.get("level"),
            "message": _redact_text(item.get("message", ""), 800),
        }
        for item in logs
    ]


def _diagnostics_payload() -> dict:
    settings = cfg.get_settings()
    providers = cfg.get_providers()
    active = cfg.get_active_provider()
    desktop_status = registry.get_config_status()
    proxy_port = settings.get("proxyPort", 18080)
    desktop_health = _desktop_health(desktop_status, proxy_port, active, providers, False)
    return {
        "format": DIAGNOSTICS_FORMAT,
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app": {
            "version": cfg.DEFAULT_CONFIG.get("version", "1.0.0"),
            "platform": sys.platform,
            "platformDetail": platform_module.platform(),
            "adminPort": settings.get("adminPort", 18081),
            "proxyPort": proxy_port,
        },
        "desktop": {
            "configured": bool(desktop_status.get("configured")),
            "health": _redact_value(desktop_health),
            "keys": _redact_value(desktop_status.get("keys") or {}),
        },
        "providers": [_diagnostics_provider(provider) for provider in providers],
        "activeProviderId": active.get("id") if active else None,
        "settings": {
            "autoStart": bool(settings.get("autoStart")),
            "upstreamProxy": _upstream_proxy_diagnostics(settings),
        },
        "proxy": {
            "running": bool(_proxy_running),
            "port": _proxy_port or proxy_port,
            "stats": proxy_stats.to_dict(),
            "recentLogs": _diagnostics_recent_logs(),
        },
        "redactions": [
            "apiKey",
            "gatewayApiKey",
            "authorization",
            "x-api-key",
            "url.userinfo",
            "token-like-query",
        ],
    }


def _is_non_json_log_message(message: str) -> bool:
    text = str(message or "")
    en_prefix = re.sub(r"[:\s]+$", "", i18n.get_text("proxy.log_non_json_prefix", "en"))
    zh_prefix = re.sub(r"[\W_]+$", "", i18n.get_text("proxy.log_non_json_prefix", "zh"))
    return (
        "invalid_upstream_response" in text
        or (en_prefix and en_prefix in text)
        or (zh_prefix and zh_prefix in text)
    )


def _diagnostics_checks(payload: dict) -> list[dict]:
    checks = []
    active_id = payload.get("activeProviderId")
    providers = payload.get("providers") or []
    active = next((provider for provider in providers if provider.get("id") == active_id), None)
    recent_logs = payload.get("proxy", {}).get("recentLogs") or []
    invalid_upstream_logs = [
        item
        for item in recent_logs
        if _is_non_json_log_message(str(item.get("message") or ""))
    ]
    recent_error_logs = [
        item
        for item in recent_logs
        if str(item.get("level") or "").upper() == "ERROR"
    ]
    checks.append({
        "code": "active_provider",
        "ok": bool(active),
        "message": i18n.t("diag.active_provider_ok") if active else i18n.t("diag.active_provider_missing"),
    })
    checks.append({
        "code": "active_provider_api_key",
        "ok": bool(active and active.get("hasApiKey")),
        "message": i18n.t("diag.api_key_ok") if active and active.get("hasApiKey") else i18n.t("diag.api_key_missing"),
    })
    checks.append({
        "code": "desktop_config",
        "ok": bool(payload.get("desktop", {}).get("configured")),
        "message": i18n.t("diag.desktop_managed_ok") if payload.get("desktop", {}).get("configured") else i18n.t("diag.desktop_managed_no"),
    })
    health = payload.get("desktop", {}).get("health") or {}
    checks.append({
        "code": "desktop_health",
        "ok": not bool(health.get("needsApply")),
        "message": i18n.t("diag.desktop_sync_ok") if not health.get("needsApply") else i18n.t("diag.desktop_sync_needs_apply"),
        "issues": health.get("issues") or [],
    })
    helper_warnings = [
        item
        for item in health.get("warnings") or []
        if item.get("code") == "claude_code_helper_missing"
    ]
    if helper_warnings:
        checks.append({
            "code": "claude_code_helper",
            "ok": True,
            "level": "info",
            "message": helper_warnings[0].get("message") or i18n.t("diag.helper_unavailable"),
        })
    checks.append({
        "code": "local_gateway",
        "ok": bool(payload.get("proxy", {}).get("running")),
        "message": i18n.t("diag.gateway_running") if payload.get("proxy", {}).get("running") else i18n.t("diag.gateway_stopped"),
    })
    upstream_proxy = payload.get("settings", {}).get("upstreamProxy") or {}
    if (
        upstream_proxy.get("enabled")
        and upstream_proxy.get("configured")
        and upstream_proxy.get("isLocal")
        and upstream_proxy.get("reachable") is False
    ):
        host = upstream_proxy.get("host") or "127.0.0.1"
        port = upstream_proxy.get("port") or ""
        checks.append({
            "code": "upstream_proxy_unreachable",
            "ok": False,
            "message": i18n.t("diag.upstream_proxy_unreachable", host=host, port=port),
            "host": host,
            "port": port,
        })
    if invalid_upstream_logs:
        checks.append({
            "code": "upstream_response",
            "ok": False,
            "message": i18n.t("diag.upstream_non_json"),
            "count": len(invalid_upstream_logs),
        })
    elif recent_error_logs:
        checks.append({
            "code": "proxy_recent_errors",
            "ok": False,
            "message": i18n.t("diag.recent_error_logs"),
            "count": len(recent_error_logs),
        })
    return checks


def _provider_not_found():
    return JSONResponse(
        status_code=404,
        content={"success": False, "message": i18n.t("admin.provider_not_found")},
    )


def _parse_inference_models(raw_value: str) -> list:
    """Parse inferenceModels from the Claude Desktop managed policy."""
    try:
        parsed = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_policy_list(value) -> list[str]:
    """Parse a JSON string or JSON array policy list."""
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            parsed = []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def _policy_enabled(value) -> bool:
    """Return True if a bool/int/string policy value represents an enabled flag."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "enabled"}


def _claude_code_helper_candidates() -> list[Path]:
    """Return candidate paths where the Claude Desktop 3P helper binary may reside."""
    override = os.environ.get("CCDS_CLAUDE_CODE_HELPER_PATHS", "").strip()
    if override:
        return [Path(item) for item in override.split(os.pathsep) if item.strip()]

    home = Path.home()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return []
        return list(Path(local_app_data).glob("Claude-3p/claude-code/*/claude.exe"))
    if sys.platform == "darwin":
        return list((home / "Library/Application Support/Claude-3p/claude-code").glob("*/claude"))
    return []


def _claude_code_helper_status(keys: dict) -> dict:
    enabled = _policy_enabled(keys.get("isClaudeCodeForDesktopEnabled"))
    if not enabled:
        return {"enabled": False, "available": True, "checked": 0}
    candidates = _claude_code_helper_candidates()
    return {
        "enabled": True,
        "available": any(path.is_file() for path in candidates),
        "checked": len(candidates),
    }


def _inference_model_names(items: list) -> list[str]:
    """Extract model names from Desktop inferenceModels items."""
    names = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("name")
        else:
            value = item
        name = str(value or "").strip()
        if name:
            names.append(name)
    return names


def _raw_desktop_model_names(
    names: list[str],
    provider: Optional[dict],
    providers: Optional[list[dict]],
    target_models: Optional[list] = None,
) -> list[str]:
    """Identify raw upstream model names written by older versions."""
    suspicious_tokens = (
        "deepseek",
        "kimi",
        "moonshot",
        "glm",
        "qwen",
        "dashscope",
        "aliyun",
        "siliconflow",
        "mimo",
    )
    safe_route_names = {
        str(item.get("name"))
        for item in target_models or []
        if isinstance(item, dict) and item.get("name")
    }
    upstream_ids = set(provider_model_ids(provider))
    for item in providers or []:
        upstream_ids.update(provider_model_ids(item))

    raw_names = []
    for name in names:
        if name in safe_route_names:
            continue
        lowered = name.lower()
        if name in upstream_ids or any(token in lowered for token in suspicious_tokens):
            raw_names.append(name)
    return raw_names


def _stale_desktop_route_names(names: list[str], target_models: list) -> list[str]:
    """Identify unmapped Claude-safe routes left over from previous configurations."""
    allowed = {
        str(item.get("name"))
        for item in target_models
        if isinstance(item, dict) and item.get("name")
    }
    known_routes = set(desktop_route_ids())
    return [
        name
        for name in names
        if name in known_routes and name not in allowed
    ]


def _no_desktop_model_routes_response(target: dict, proxy_port: int, *, attempted: bool = False) -> dict:
    """Build the failure response when no explicit model slots are mapped."""
    response = {
        "success": False,
        "message": i18n.t("admin.map_at_least_one_slot"),
        "mode": target["mode"],
        "requiresProxy": target["requiresProxy"],
        "proxyStarted": False,
        "proxyPort": proxy_port,
        "configSuccess": False,
    }
    if attempted:
        response["attempted"] = True
    return response


def desktop_config_target_for_provider(provider: Optional[dict], settings: Optional[dict] = None) -> dict:
    """Build the target configuration to write into Claude Desktop.

    Since v1.0.18, Claude Desktop always points to the local forwarding address so that
    model mapping, extra headers, and protocol conversion are handled uniformly.
    """
    settings = settings or cfg.get_settings()
    proxy_port = settings.get("proxyPort", 18080)
    return {
        "baseUrl": f"http://127.0.0.1:{proxy_port}",
        "apiKey": cfg.get_or_create_gateway_api_key(),
        "authScheme": "bearer",
        "gatewayHeaders": "",
        "provider": provider,
        "providers": None,
        "exposeAll": False,
        "requiresProxy": True,
        "mode": "local_proxy",
    }


def _desktop_health(
    desktop_status: dict,
    proxy_port: int,
    provider: Optional[dict],
    providers: Optional[list[dict]] = None,
    expose_all: bool = False,
) -> dict:
    """Evaluate whether the Claude Desktop configuration matches the current provider."""
    keys = desktop_status.get("keys") or {}
    settings = dict(cfg.get_settings())
    settings["proxyPort"] = proxy_port
    target = desktop_config_target_for_provider(provider, settings)
    expected_base_url = str(target.get("baseUrl") or "").rstrip("/")
    actual_base_url = str(keys.get("inferenceGatewayBaseUrl") or "").rstrip("/")
    issues = []
    warnings = []

    if actual_base_url and actual_base_url != expected_base_url:
        issues.append({
            "code": "gateway_base_url_mismatch",
            "message": i18n.t("health.gateway_url_mismatch"),
        })

    if desktop_status.get("configured") is False:
        if keys:
            issues.append({
                "code": "not_managed_by_ccds",
                "message": i18n.t("health.not_managed_by_latest"),
            })
        else:
            issues.append({
                "code": "desktop_not_configured",
                "message": i18n.t("health.not_configured"),
            })

    cowork_egress_hosts = _parse_policy_list(keys.get("coworkEgressAllowedHosts"))
    if keys and "*" not in cowork_egress_hosts:
        issues.append({
            "code": "cowork_egress_hosts_missing",
            "message": i18n.t("health.cowork_egress_missing"),
        })

    helper_status = _claude_code_helper_status(keys)
    if helper_status["enabled"] and not helper_status["available"]:
        warnings.append({
            "code": "claude_code_helper_missing",
            "severity": "info",
            "diagnosticOnly": True,
            "checked": helper_status.get("checked", 0),
            "message": i18n.t("health.claude_code_helper_missing"),
        })

    inference_models = _parse_inference_models(str(keys.get("inferenceModels") or ""))
    inference_model_names = _inference_model_names(inference_models)
    target_models = registry.provider_inference_models(provider)
    raw_model_names = _raw_desktop_model_names(inference_model_names, provider, providers, target_models)
    if raw_model_names:
        issues.append({
            "code": "invalid_inference_model_names",
            "message": i18n.t("health.legacy_real_models_exposed"),
            "models": raw_model_names,
        })
    stale_route_names = _stale_desktop_route_names(inference_model_names, target_models)
    if stale_route_names:
        issues.append({
            "code": "stale_inference_model_routes",
            "message": i18n.t("health.unmapped_routes_exposed"),
            "models": stale_route_names,
        })
    one_million_models = [
        str(item.get("name"))
        for item in target_models
        if isinstance(item, dict) and item.get("supports1m") is True and item.get("name")
    ]
    one_million_ready = True
    if one_million_models:
        written_one_million = {
            str(item.get("name"))
            for item in inference_models
            if (
                isinstance(item, dict)
                and item.get("name")
                and item.get("supports1m") is True
            )
        }
        one_million_ready = all(model in written_one_million for model in one_million_models)
        if not one_million_ready:
            issues.append({
                "code": "one_million_not_written",
                "message": i18n.t("health.model_1m_capability_mismatch"),
                "models": one_million_models,
                "writtenModels": sorted(written_one_million),
            })

    return {
        "needsApply": bool(issues),
        "oneMillionReady": one_million_ready,
        "expectedBaseUrl": expected_base_url,
        "actualBaseUrl": actual_base_url,
        "mode": target.get("mode"),
        "requiresProxy": bool(target.get("requiresProxy")),
        "issues": issues,
        "warnings": warnings,
    }


def _sync_desktop_for_active_provider() -> dict:
    """Sync the Claude Desktop model list managed by this tool when the active provider changes."""
    provider = cfg.get_active_provider()
    if not provider:
        return {"attempted": False, "success": False, "message": i18n.t("admin.no_default_provider")}

    settings = cfg.get_settings()
    target = desktop_config_target_for_provider(provider, settings)
    proxy_port = int(settings.get("proxyPort", 18080))
    if not registry.provider_inference_models(provider):
        return _no_desktop_model_routes_response(target, proxy_port, attempted=True)
    result = registry.apply_config(
        target["baseUrl"],
        gateway_api_key=target["apiKey"],
        provider=target["provider"],
        providers=target["providers"],
        expose_all=target["exposeAll"],
        auth_scheme=target["authScheme"],
        gateway_headers=target["gatewayHeaders"],
    )
    response = {
        "attempted": True,
        "mode": target["mode"],
        "requiresProxy": target["requiresProxy"],
        "configSuccess": bool(result.get("success")),
        "proxyStarted": False,
        "proxyPort": proxy_port,
        **result,
    }
    if result.get("success") and target["requiresProxy"]:
        proxy_started = _start_proxy_server(proxy_port, restart=True)
        response["proxyStarted"] = bool(proxy_started)
        if not proxy_started:
            response["success"] = False
            response["message"] = i18n.t("admin.desktop_written_proxy_failed")
    elif result.get("success"):
        response["proxyStarted"] = bool(_proxy_running)
    return response


def _provider_test_model(provider: dict) -> str:
    for model in provider_model_ids(provider):
        if model:
            return model
    return "claude-sonnet-4-6"


def _provider_test_body(provider: dict, api_format: str) -> dict:
    model = _provider_test_model(provider)
    if api_format == "openai_chat":
        return {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
    return {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }


def _is_kimi_provider(provider: dict) -> bool:
    """Roughly identify a Kimi provider to give specific troubleshooting guidance."""
    probe = f"{provider.get('name', '')} {provider.get('baseUrl', '')}".lower()
    return "kimi" in probe or "moonshot" in probe


async def _test_provider_connection(provider: dict) -> dict:
    """Test whether a provider can reach its upstream endpoint."""
    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))
    base_url = build_upstream_url(provider.get("baseUrl", ""), api_format)
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "success": False,
            "message": i18n.t("provider_tools.invalid_base_url"),
        }

    headers = get_upstream_headers(provider)
    headers.pop("Content-Type", None)
    started = time.perf_counter()

    try:
        timeout = httpx.Timeout(8.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.head(base_url, headers=headers)
            if response.status_code in {404, 405}:
                response = await client.get(base_url, headers=headers)
            if response.status_code in {404, 405} and provider.get("apiKey"):
                response = await client.post(
                    base_url,
                    headers=get_upstream_headers(provider),
                    json=_provider_test_body(provider, api_format),
                )
    except httpx.RequestError as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {
            "success": True,
            "ok": False,
            "latencyMs": latency_ms,
            "message": i18n.t("conn.connect_failed", error=exc.__class__.__name__),
        }

    latency_ms = round((time.perf_counter() - started) * 1000)
    status_code = response.status_code
    reachable = status_code < 500
    if 200 <= status_code < 300:
        message = i18n.t("conn.ok", latency=latency_ms)
    elif status_code in {401, 403}:
        reachable = False
        if _is_kimi_provider(provider):
            message = i18n.t("conn.kimi_auth_failed", status=status_code, latency=latency_ms)
        else:
            message = i18n.t("conn.auth_failed", status=status_code, latency=latency_ms)
    elif status_code in {404, 405}:
        reachable = False
        message = i18n.t("conn.endpoint_unavailable", status=status_code, latency=latency_ms)
    else:
        message = i18n.t("conn.reachable", status=status_code, latency=latency_ms)

    return {
        "success": True,
        "ok": reachable,
        "latencyMs": latency_ms,
        "statusCode": status_code,
        "message": message,
    }


def _provider_compatibility(provider: dict) -> dict:
    """Return a third-party protocol compatibility summary without making network requests."""
    api_format = normalize_api_format(provider.get("apiFormat", "anthropic"))
    if api_format == "anthropic":
        return {
            "id": provider.get("id"),
            "name": provider.get("name"),
            "apiFormat": api_format,
            "level": "stable",
            "message": i18n.t("compat.anthropic_summary"),
            "checks": {
                "models": True,
                "text": True,
                "stream": True,
                "tools": True,
                "streamingTools": True,
            },
        }
    if api_format == "openai_chat":
        return {
            "id": provider.get("id"),
            "name": provider.get("name"),
            "apiFormat": api_format,
            "level": "experimental",
            "message": i18n.t("compat.openai_summary"),
            "checks": {
                "models": True,
                "text": True,
                "stream": True,
                "tools": True,
                "streamingTools": False,
            },
        }
    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "apiFormat": api_format,
        "level": "unsupported",
        "message": i18n.t("compat.unsupported_summary", format=api_format),
        "checks": {
            "models": False,
            "text": False,
            "stream": False,
            "tools": False,
            "streamingTools": False,
        },
    }


async def _detect_local_proxy() -> Optional[str]:
    """Auto-detect a local proxy port by checking environment variables first, then common ports."""
    for env in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        val = os.environ.get(env, os.environ.get(env.lower(), "")).strip()
        if val:
            return val

    common_ports = [
        7890,   # Clash / ClashX / Clash Verge (HTTP)
        7897,   # Clash Verge Rev
        7891,   # Clash (SOCKS / HTTP)
        6152,   # Surge (HTTP)
        6153,   # Surge (SOCKS)
        1080,   # Shadowsocks / SSR / v2rayN (SOCKS)
        10808,  # v2rayN (SOCKS)
        10809,  # v2rayN (HTTP)
        1082,   # Shadowrocket
        8118,   # Privoxy
        8888,   # Fiddler / Charles
        8889,   # Surge Mac
    ]

    for port in common_ports:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return f"http://127.0.0.1:{port}"
        except (OSError, asyncio.TimeoutError):
            continue
    return None


def create_admin_app() -> FastAPI:
    """Create the admin FastAPI application."""
    app = FastAPI(title="CC Desktop Switch Admin", version="1.0.26")

    @app.middleware("http")
    async def require_local_admin_auth(request: Request, call_next):
        """Protect the local management API from cross-site browser access."""
        path = request.url.path
        if path == "/api/ready":
            return await call_next(request)

        if path == "/api/app/activate":
            if request.headers.get(STATIC_REQUEST_HEADER) != "1":
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "message": "Invalid local request"},
                )
            return await call_next(request)

        if path.startswith("/api/"):
            token = request.headers.get(ADMIN_TOKEN_HEADER, "")
            if not verify_admin_token(token):
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "message": "Invalid admin token"},
                )

        return await call_next(request)

    @app.get("/api/ready")
    async def ready():
        """Public liveness endpoint that returns no local configuration."""
        return {"success": True, "ready": True}

    # ── Status API ──
    @app.get("/api/status")
    async def get_status():
        """Get global application status."""
        providers = cfg.get_providers()
        active = cfg.get_active_provider()
        desktop_status = registry.get_config_status()
        settings = cfg.get_settings()
        proxy_port = settings.get("proxyPort", 18080)
        expose_all = False
        target = desktop_config_target_for_provider(active, settings)

        return {
            "desktopConfigured": desktop_status.get("configured", False),
            "proxyRunning": _proxy_running,
            "proxyPort": proxy_port,
            "desktopMode": target["mode"],
            "desktopRequiresProxy": target["requiresProxy"],
            "activeProvider": _public_provider(active),
            "activeProviderId": active["id"] if active else None,
            "providerCount": len(providers),
            "desktopHealth": _desktop_health(desktop_status, proxy_port, active, providers, expose_all),
            "exposeAllProviderModels": expose_all,
        }

    @app.post("/api/app/activate")
    async def activate_app():
        """Called by a second process launch to bring the existing window to the foreground."""
        handled = _activate_existing_app()
        return {"success": True, "handled": handled}

    # ── Providers API ──
    @app.get("/api/providers")
    async def list_providers():
        """Get all configured providers."""
        providers = [_public_provider(p) for p in cfg.get_providers()]
        active_id = cfg.load_config().get("activeProvider")
        return {
            "providers": providers,
            "activeId": active_id,
        }

    @app.put("/api/providers/reorder")
    async def reorder_providers(request: Request):
        """Save the reordered list of provider IDs."""
        data = await request.json()
        provider_ids = data.get("providerIds", [])
        if not isinstance(provider_ids, list) or not all(isinstance(item, str) for item in provider_ids):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": i18n.t("admin.provider_ids_must_be_array")},
            )
        if cfg.reorder_providers(provider_ids):
            return {"success": True, "providers": [_public_provider(p) for p in cfg.get_providers()]}
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": i18n.t("admin.reorder_failed")},
        )

    @app.get("/api/providers/{provider_id}/secret")
    async def get_provider_secret(provider_id: str):
        """Read the saved API Key for a provider (local frontend only)."""
        provider = cfg.get_provider(provider_id)
        if not provider:
            return _provider_not_found()
        return {"apiKey": provider.get("apiKey", "")}

    @app.post("/api/providers")
    async def create_provider(request: Request):
        """Add a new provider."""
        data = await request.json()
        provider = cfg.add_provider(data)
        return {"success": True, "provider": _public_provider(provider)}

    @app.put("/api/providers/{provider_id}")
    async def edit_provider(provider_id: str, request: Request):
        """Edit an existing provider."""
        data = await request.json()
        result = cfg.update_provider(provider_id, data)
        if result:
            return {"success": True, "provider": _public_provider(result)}
        return _provider_not_found()

    @app.post("/api/providers/{provider_id}/models/{model}/check")
    async def check_provider_model(provider_id: str, model: str):
        """Check whether a specific model is available via a minimal chat completion request."""
        provider = cfg.get_provider(provider_id)
        if not provider:
            return _provider_not_found()
        result = await provider_tools.check_model_available(provider, model)
        return {"success": True, **result}

    @app.delete("/api/providers/{provider_id}")
    async def remove_provider(provider_id: str):
        """Delete a provider."""
        if cfg.delete_provider(provider_id):
            return {"success": True, "message": i18n.t("admin.deleted")}
        return _provider_not_found()

    @app.put("/api/providers/{provider_id}/default")
    async def set_default_provider(provider_id: str):
        """Set the active default provider."""
        if cfg.set_active_provider(provider_id):
            try:
                desktop_sync = _sync_desktop_for_active_provider()
            except Exception as exc:
                desktop_sync = {
                    "attempted": True,
                    "success": False,
                    "message": i18n.t("admin.desktop_sync_failed", error=exc),
                }
            return {
                "success": True,
                "message": i18n.t("admin.default_updated"),
                "desktopSync": desktop_sync,
            }
        return _provider_not_found()

    @app.post("/api/providers/test")
    async def test_provider_payload(request: Request):
        """Test connectivity for an unsaved provider payload from the form."""
        data = await request.json()
        return await _test_provider_connection(data)

    @app.post("/api/providers/detect-format")
    async def detect_provider_format(request: Request):
        """Detect the upstream API protocol format."""
        data = await request.json()
        base_url = str(data.get("baseUrl") or "").strip()
        api_key = str(data.get("apiKey") or "").strip()
        if not base_url:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": i18n.t("provider_tools.enter_base_url")},
            )
        result = await provider_tools.detect_api_format(base_url, api_key)
        if result.get("success"):
            return result
        return JSONResponse(
            status_code=400,
            content=result,
        )

    @app.post("/api/providers/models/available")
    async def get_available_models_from_payload(request: Request):
        """Fetch available models for an unsaved provider payload from the form."""
        data = await request.json()
        result = await provider_tools.fetch_provider_models(data)
        if result.get("success"):
            return result
        return JSONResponse(status_code=400, content=result)

    @app.post("/api/providers/{provider_id}/test")
    async def test_saved_provider(provider_id: str):
        """Test connectivity and latency for a saved provider."""
        for provider in cfg.get_providers():
            if provider["id"] == provider_id:
                return await _test_provider_connection(provider)
        return _provider_not_found()

    @app.post("/api/providers/{provider_id}/usage")
    async def query_provider_usage(provider_id: str):
        """Query balance/usage for a saved provider."""
        provider = cfg.get_provider(provider_id)
        if not provider:
            return _provider_not_found()
        return await provider_tools.query_provider_usage(provider)

    @app.get("/api/providers/compatibility")
    async def provider_compatibility_report():
        """Return third-party API compatibility summaries for all saved providers."""
        providers = [_provider_compatibility(provider) for provider in cfg.get_providers()]
        return {
            "success": True,
            "providers": providers,
            "experimentalCount": len([item for item in providers if item["level"] == "experimental"]),
        }

    # ── Model Mapping API ──
    @app.get("/api/providers/{provider_id}/models")
    async def get_models(provider_id: str):
        """Get model mappings for a provider."""
        providers = cfg.get_providers()
        for p in providers:
            if p["id"] == provider_id:
                return {"models": p.get("models", {})}
        return _provider_not_found()

    @app.get("/api/providers/{provider_id}/models/available")
    async def get_available_models(provider_id: str):
        """Fetch available models supported by a saved provider."""
        provider = cfg.get_provider(provider_id)
        if not provider:
            return _provider_not_found()
        result = await provider_tools.fetch_provider_models(provider)
        if result.get("success"):
            return result
        return JSONResponse(status_code=400, content=result)

    @app.post("/api/providers/{provider_id}/models/autofill")
    async def autofill_models(provider_id: str):
        """Fetch available models and save suggested slot mappings."""
        provider = cfg.get_provider(provider_id)
        if not provider:
            return _provider_not_found()
        result = await provider_tools.fetch_provider_models(provider)
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        if cfg.update_models(provider_id, result.get("suggested", {})):
            return {
                "success": True,
                "models": result.get("models", []),
                "suggested": result.get("suggested", {}),
                "endpoint": result.get("endpoint"),
                "message": i18n.t("admin.models_autofilled"),
            }
        return _provider_not_found()

    @app.put("/api/providers/{provider_id}/models")
    async def save_models(provider_id: str, request: Request):
        """Save model mappings for a provider."""
        data = await request.json()
        if cfg.update_models(provider_id, data.get("models", {})):
            return {"success": True, "message": i18n.t("admin.models_saved")}
        return _provider_not_found()

    # ── Backup / Import / Export API ──
    @app.post("/api/config/backup")
    async def create_config_backup():
        """Manually create a configuration backup."""
        return {"success": True, "backup": cfg.create_backup("manual")}

    @app.get("/api/config/backups")
    async def list_config_backups():
        """List configuration backups."""
        return {"backups": cfg.list_backups()}

    @app.get("/api/config/export")
    async def export_config():
        """Export full configuration (includes API Keys, for local backup only)."""
        return cfg.export_config()

    @app.post("/api/config/import")
    async def import_config(request: Request):
        """Import full configuration after automatically backing up current configuration."""
        try:
            data = await request.json()
            result = cfg.import_config(data)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc)},
            )
        return {
            "success": True,
            "message": i18n.t("admin.config_imported"),
            "backup": result["backup"],
        }

    # ── Diagnostics API ──
    @app.get("/api/diagnostics/summary")
    async def diagnostics_summary():
        """Return a redacted diagnostic summary."""
        payload = _diagnostics_payload()
        return {
            "success": True,
            "format": payload["format"],
            "summary": {
                "format": payload["format"],
                "app": payload["app"],
                "desktop": payload["desktop"],
                "activeProviderId": payload["activeProviderId"],
                "proxy": {
                    "running": payload["proxy"]["running"],
                    "port": payload["proxy"]["port"],
                    "stats": payload["proxy"]["stats"],
                },
            },
        }

    @app.post("/api/diagnostics/export")
    async def diagnostics_export():
        """Export a redacted diagnostic bundle safe to share with maintainers."""
        payload = _diagnostics_payload()
        return {"success": True, "format": payload["format"], "diagnostics": payload}

    @app.post("/api/diagnostics/check")
    async def diagnostics_check():
        """Run local configuration and gateway health checks."""
        payload = _diagnostics_payload()
        checks = _diagnostics_checks(payload)
        return {
            "success": True,
            "format": payload["format"],
            "ok": all(item.get("ok") for item in checks),
            "checks": checks,
        }

    # ── CC-Switch Import API ──
    @app.get("/api/ccswitch/status")
    async def get_ccswitch_status():
        """Detect local CC-Switch configuration without returning API Keys."""
        return {"success": True, **ccswitch_import.status()}

    @app.get("/api/ccswitch/providers")
    async def get_ccswitch_providers():
        """Preview Claude providers importable from CC-Switch (API Keys are masked)."""
        try:
            providers = ccswitch_import.read_providers()
        except ccswitch_import.CcSwitchImportError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc)},
            )
        return {
            "success": True,
            "providers": providers,
            "supportedCount": len([item for item in providers if item.get("supported")]),
            "unsupportedCount": len([item for item in providers if not item.get("supported")]),
        }

    @app.post("/api/ccswitch/import")
    async def import_ccswitch_providers(request: Request):
        """Import Anthropic-compatible providers from CC-Switch."""
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        try:
            result = ccswitch_import.import_providers(
                ids=data.get("ids"),
                set_default=bool(data.get("setDefault")),
            )
        except ccswitch_import.CcSwitchImportError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc)},
            )
        return {
            "success": True,
            "message": i18n.t("admin.ccswitch_imported", count=len(result["imported"])),
            **result,
        }

    # ── Desktop Integration API ──
    @app.get("/api/desktop/status")
    async def get_desktop_status():
        """Get Claude Desktop registry/plist configuration status."""
        status = registry.get_config_status()
        settings = cfg.get_settings()
        proxy_port = settings.get("proxyPort", 18080)
        status["health"] = _desktop_health(
            status,
            proxy_port,
            cfg.get_active_provider(),
            cfg.get_providers(),
            False,
        )
        return status

    @app.post("/api/desktop/configure")
    async def apply_desktop_config(request: Request):
        """Apply Claude Desktop configuration to registry/plist."""
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        active_provider = cfg.get_active_provider()
        settings = cfg.get_settings()
        if data.get("port"):
            settings = dict(settings)
            settings["proxyPort"] = int(data["port"])
        target = desktop_config_target_for_provider(active_provider, settings)
        proxy_port = int(settings.get("proxyPort", 18080))
        if not registry.provider_inference_models(active_provider):
            return _no_desktop_model_routes_response(target, proxy_port)
        result = registry.apply_config(
            target["baseUrl"],
            gateway_api_key=target["apiKey"],
            provider=target["provider"],
            providers=target["providers"],
            expose_all=target["exposeAll"],
            auth_scheme=target["authScheme"],
            gateway_headers=target["gatewayHeaders"],
        )
        response = {
            **result,
            "configSuccess": bool(result.get("success")),
            "mode": target["mode"],
            "requiresProxy": target["requiresProxy"],
            "proxyStarted": False,
            "proxyPort": proxy_port,
        }
        if result.get("success") and target["requiresProxy"]:
            proxy_started = _start_proxy_server(proxy_port, restart=True)
            response["proxyStarted"] = bool(proxy_started)
            if not proxy_started:
                response["success"] = False
                response["message"] = i18n.t("admin.desktop_written_proxy_failed")
        elif result.get("success"):
            response["proxyStarted"] = bool(_proxy_running)
        return response

    @app.post("/api/desktop/clear")
    async def clear_desktop_config():
        """Clear Claude Desktop registry/plist configuration."""
        return registry.clear_config()

    # ── Proxy API ──
    @app.get("/api/proxy/status")
    async def get_proxy_status():
        """Get forwarding service status."""
        return {
            "running": _proxy_running,
            "port": _proxy_port or cfg.get_settings().get("proxyPort", 18080),
            "stats": proxy_stats.to_dict(),
        }

    @app.post("/api/proxy/start")
    async def start_proxy(request: Request):
        """Start forwarding service."""
        global _proxy_running
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        requested_port = data.get("port")
        if requested_port:
            cfg.update_settings({"proxyPort": int(requested_port)})

        port = cfg.get_settings().get("proxyPort", 18080)
        healthy_before = _proxy_server_accepting(port)
        success = _start_proxy_server(port)
        if success:
            message = (
                i18n.t("admin.proxy_already_running")
                if healthy_before
                else i18n.t("admin.proxy_started_on_port", port=port)
            )
            return {"success": True, "message": message, "port": port}
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": i18n.t("admin.proxy_start_failed")},
        )

    @app.post("/api/proxy/stop")
    async def stop_proxy():
        """Stop forwarding service."""
        global _proxy_running
        if not _proxy_running:
            return {"success": True, "message": i18n.t("admin.proxy_not_running")}

        _stop_proxy_server()
        return {"success": True, "message": i18n.t("admin.proxy_stopped")}

    @app.get("/api/proxy/logs")
    async def get_proxy_logs():
        """Get forwarding service logs."""
        return {"logs": proxy_logs.get_all()}

    @app.post("/api/proxy/logs/clear")
    async def clear_proxy_logs():
        """Clear forwarding service logs."""
        proxy_logs.clear()
        return {"success": True}

    # ── Settings API ──
    @app.get("/api/settings")
    async def get_settings():
        """Get application settings."""
        return cfg.get_settings()

    @app.put("/api/settings")
    async def save_settings(request: Request):
        """Save application settings."""
        data = await request.json()
        settings = cfg.update_settings(data)
        return {"success": True, "settings": settings}

    @app.get("/api/proxy/detect")
    async def detect_local_proxy():
        """Auto-detect local proxy port."""
        detected = await _detect_local_proxy()
        return {"detected": detected or ""}

    @app.get("/api/update/check")
    async def check_update(url: Optional[str] = None, current: Optional[str] = None, platform: Optional[str] = None):
        """Check for the latest version without downloading or installing automatically."""
        settings = cfg.get_settings()
        update_url = url or settings.get("updateUrl") or cfg.DEFAULT_UPDATE_URL
        if not update_url:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": i18n.t("admin.configure_update_url_first")},
            )
        try:
            return await updater.check_update(
                url=update_url,
                current_version=current or cfg.DEFAULT_CONFIG.get("version", "1.0.0"),
                platform=platform or updater.current_platform(),
            )
        except updater.UpdateCheckError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc)},
            )

    @app.post("/api/update/install")
    async def download_and_install_update(request: Request):
        """Download the latest installer package and launch it."""
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        settings = cfg.get_settings()
        update_url = data.get("url") or settings.get("updateUrl") or cfg.DEFAULT_UPDATE_URL
        platform = data.get("platform") or updater.current_platform()
        try:
            result = await updater.download_update(
                url=update_url,
                current_version=data.get("current") or cfg.DEFAULT_CONFIG.get("version", "1.0.0"),
                platform=platform,
            )
            if not result.get("updateAvailable"):
                return result
            installer_path = result.get("installerPath")
            if not installer_path:
                raise updater.UpdateCheckError(i18n.t("admin.download_installer_failed"))
            resolved_platform = result.get("platform") or platform
            quit_requested = _launch_update_installer(installer_path, resolved_platform)
            is_macos = resolved_platform.startswith("macos-")
            is_windows = resolved_platform.startswith("windows-")
            if quit_requested:
                install_message = i18n.t("admin.install_quit_starting")
            elif is_macos:
                install_message = i18n.t("admin.install_macos_opened")
            elif is_windows:
                install_message = i18n.t("admin.install_windows_started")
            else:
                install_message = i18n.t("admin.install_opened")
            return {
                **result,
                "success": True,
                "installerStarted": True,
                "quitRequested": quit_requested,
                "message": install_message,
            }
        except updater.UpdateCheckError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc)},
            )
        except OSError as exc:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": i18n.t("admin.install_launch_failed", error=exc)},
            )

    @app.get("/api/update/progress")
    async def get_update_progress():
        """Return the current update download progress for frontend polling."""
        return updater.get_download_progress()

    # ── Presets API ──
    @app.get("/api/presets")
    async def get_presets():
        """Get builtin provider presets."""
        return {"presets": cfg.get_presets()}

    # ── Mount Frontend Static Files ──
    # Must be mounted after all /api/* routes so "/" does not shadow API endpoints.
    if FRONTEND_DIR.exists():
        frontend_static = StaticFiles(directory=str(FRONTEND_DIR), html=True)
        app.mount("/", frontend_static, name="frontend")

    return app


# ── Proxy Server Management ──
_proxy_running = False
_proxy_thread: Optional[threading.Thread] = None
_proxy_server = None
_proxy_port: Optional[int] = None
_proxy_lock = threading.RLock()


def _proxy_server_accepting(port: int) -> bool:
    """Confirm that the currently recorded proxy thread is still listening on the given port."""
    with _proxy_lock:
        thread = _proxy_thread
        server = _proxy_server
        running = _proxy_running
        current_port = _proxy_port
    if not running or current_port != int(port):
        return False
    if not thread or not thread.is_alive():
        return False
    if not server or getattr(server, "should_exit", False):
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_proxy_server_start(server, thread: threading.Thread, port: int, timeout: float = 2.0) -> bool:
    """Wait for uvicorn to finish binding so port conflicts are not misreported as started."""
    deadline = time.perf_counter() + timeout
    has_started_flag = hasattr(server, "started")
    while time.perf_counter() < deadline:
        if has_started_flag and getattr(server, "started", False):
            return True
        if not thread.is_alive():
            return False
        if not has_started_flag:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=0.1):
                    return True
            except OSError:
                pass
        time.sleep(0.05)
    if has_started_flag:
        return bool(getattr(server, "started", False))
    return bool(thread.is_alive())


def _start_proxy_server(port: int, restart: bool = False) -> bool:
    """Start the proxy server in a background thread."""
    global _proxy_running, _proxy_thread, _proxy_server, _proxy_port

    requested_port = int(port)
    with _proxy_lock:
        already_running = bool(_proxy_running)
        current_port = _proxy_port
        current_thread = _proxy_thread
    if already_running:
        if current_port == requested_port and not restart and _proxy_server_accepting(requested_port):
            return True
        _stop_proxy_server()
    elif current_thread and current_thread.is_alive():
        _stop_proxy_server()

    proxy_app = create_proxy_app()

    config = uvicorn.Config(
        proxy_app,
        host="127.0.0.1",
        port=requested_port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    with _proxy_lock:
        _proxy_server = server

    def run():
        global _proxy_running, _proxy_port
        try:
            server.run()
        finally:
            with _proxy_lock:
                if _proxy_server is server:
                    _proxy_running = False
                    _proxy_port = None

    _proxy_thread = threading.Thread(target=run, daemon=True)
    _proxy_thread.start()
    started = _wait_for_proxy_server_start(server, _proxy_thread, requested_port)
    with _proxy_lock:
        _proxy_running = bool(started)
    if not started:
        server.should_exit = True
        if _proxy_thread.is_alive():
            _proxy_thread.join(timeout=1.0)
        with _proxy_lock:
            if _proxy_server is server:
                _proxy_server = None
            _proxy_port = None
    else:
        with _proxy_lock:
            _proxy_port = requested_port
    return bool(started)


def _stop_proxy_server(wait: bool = True):
    """Stop the proxy server."""
    global _proxy_running, _proxy_thread, _proxy_server, _proxy_port
    with _proxy_lock:
        server = _proxy_server
        thread = _proxy_thread
        if server:
            server.should_exit = True
        _proxy_running = False
        _proxy_port = None
    if wait and thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    with _proxy_lock:
        if _proxy_thread is thread and (not thread or not thread.is_alive()):
            _proxy_thread = None
        if _proxy_server is server and (not thread or not thread.is_alive()):
            _proxy_server = None
