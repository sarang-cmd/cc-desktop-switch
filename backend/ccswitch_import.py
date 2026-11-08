"""CC-Switch configuration import adapter.

Reads local CC-Switch configurations in read-only mode without modifying its database.
Only Anthropic-compatible providers are imported; OpenAI Chat / Responses entries are
shown as unsupported to avoid conversion boundary issues in the primary workflow.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from backend import config as cfg
from backend import i18n


CCSWITCH_DIR = Path.home() / ".cc-switch"
CCSWITCH_DB = CCSWITCH_DIR / "cc-switch.db"
CCSWITCH_LEGACY_CONFIG = CCSWITCH_DIR / "config.json"

SUPPORTED_API_FORMATS = {"anthropic", ""}
UNSUPPORTED_FORMAT_KEYS = {
    "openai_chat": "ccswitch.unsupported_openai_chat",
    "openai_responses": "ccswitch.unsupported_openai_responses",
}


def _safe_id(value: str) -> str:
    safe = "".join(ch for ch in str(value or "").lower() if ch.isalnum() or ch in {"-", "_"})
    return safe[:56] or secrets.token_hex(4)


def _mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "******"
    return f"{value[:4]}...{value[-4:]}"


def _load_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _is_local_proxy_url(url: str) -> bool:
    parsed = urlparse(_normalize_base_url(url))
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"} and parsed.port in {15721, 18080}


def _api_format(meta: dict, settings_config: dict) -> str:
    value = meta.get("apiFormat") or meta.get("api_format")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    env = settings_config.get("env") if isinstance(settings_config, dict) else {}
    if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL"):
        return "anthropic"
    return "anthropic"


def _builtin_defaults(name: str, base_url: str) -> dict:
    probe = f"{name} {base_url}".lower()
    if "deepseek" in probe:
        return {"authScheme": "bearer", "extraHeaders": {"x-api-key": "{apiKey}"}}
    if "bigmodel" in probe or "zhipu" in probe or "glm" in probe:
        return {"authScheme": "x-api-key", "extraHeaders": {}}
    if "dashscope" in probe or "bailian" in probe or "aliyun" in probe:
        return {"authScheme": "x-api-key", "extraHeaders": {}}
    return {"authScheme": "bearer", "extraHeaders": {}}


def _models_from_env(env: dict) -> dict:
    default_model = str(env.get("ANTHROPIC_MODEL") or "").strip()
    sonnet = str(env.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or default_model).strip()
    haiku = str(env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or default_model).strip()
    opus = str(env.get("ANTHROPIC_DEFAULT_OPUS_MODEL") or default_model).strip()
    default = default_model or sonnet or opus or haiku
    return {
        "sonnet": sonnet or default,
        "haiku": haiku or default,
        "opus": opus or default,
        "default": default,
    }


def _candidate_from_row(row: dict, include_secret: bool = False) -> dict:
    settings_config = _load_json(row.get("settings_config"), {})
    meta = _load_json(row.get("meta"), {})
    env = settings_config.get("env") if isinstance(settings_config, dict) else {}
    if not isinstance(env, dict):
        env = {}

    api_format = _api_format(meta if isinstance(meta, dict) else {}, settings_config)
    base_url = _normalize_base_url(env.get("ANTHROPIC_BASE_URL") or "")
    api_key = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")
    name = str(row.get("name") or row.get("id") or "CC-Switch Provider")

    supported = api_format in SUPPORTED_API_FORMATS
    reason = ""
    reason_code = ""
    if api_format not in SUPPORTED_API_FORMATS:
        reason_key = UNSUPPORTED_FORMAT_KEYS.get(api_format)
        if reason_key:
            reason_code = api_format
            reason = i18n.t(reason_key)
        else:
            reason_code = "unsupported_format"
            reason = i18n.t("ccswitch.unsupported_format", format=api_format)
    elif not base_url:
        supported = False
        reason_code = "missing_base_url"
        reason = i18n.t("ccswitch.missing_base_url")
    elif _is_local_proxy_url(base_url):
        supported = False
        reason_code = "local_proxy_url"
        reason = i18n.t("ccswitch.local_proxy_url")
    elif not api_key:
        supported = False
        reason_code = "missing_api_key"
        reason = i18n.t("ccswitch.missing_api_key")

    defaults = _builtin_defaults(name, base_url)
    provider = {
        "id": str(row.get("id") or ""),
        "name": name,
        "current": bool(row.get("is_current")),
        "apiFormat": api_format or "anthropic",
        "baseUrl": base_url,
        "hasApiKey": bool(api_key),
        "apiKeyPreview": _mask_secret(api_key),
        "models": _models_from_env(env),
        "authScheme": defaults["authScheme"],
        "extraHeaders": defaults["extraHeaders"],
        "supported": supported,
        "reason": reason,
        "reasonCode": reason_code,
    }
    if include_secret:
        provider["apiKey"] = api_key
    return provider


def _ccswitch_paths(root: Path | None = None) -> dict:
    base = root or CCSWITCH_DIR
    return {
        "dir": base,
        "db": base / "cc-switch.db",
        "legacy": base / "config.json",
    }


def status(root: Path | None = None) -> dict:
    paths = _ccswitch_paths(root)
    db_exists = paths["db"].exists()
    legacy_exists = paths["legacy"].exists()
    provider_count = 0
    supported_count = 0
    unsupported_count = 0
    if db_exists:
        try:
            providers = read_providers(root=root)
            provider_count = len(providers)
            supported_count = len([p for p in providers if p["supported"]])
            unsupported_count = provider_count - supported_count
        except CcSwitchImportError:
            pass
    elif legacy_exists:
        try:
            providers = read_providers(root=root)
            provider_count = len(providers)
            supported_count = len([p for p in providers if p["supported"]])
            unsupported_count = provider_count - supported_count
        except CcSwitchImportError:
            pass
    return {
        "found": db_exists or legacy_exists,
        "dir": str(paths["dir"]),
        "dbExists": db_exists,
        "legacyConfigExists": legacy_exists,
        "providerCount": provider_count,
        "supportedCount": supported_count,
        "unsupportedCount": unsupported_count,
    }


class CcSwitchImportError(ValueError):
    """CC-Switch import error."""


def _read_sqlite_rows(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, settings_config, meta, is_current, sort_index, created_at
            FROM providers
            WHERE app_type = 'claude'
            ORDER BY COALESCE(sort_index, 999999), COALESCE(created_at, 0), id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise CcSwitchImportError(i18n.t("ccswitch.db_read_failed", error=exc)) from exc
    finally:
        conn.close()


def _read_legacy_rows(config_path: Path) -> list[dict]:
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CcSwitchImportError(i18n.t("ccswitch.legacy_read_failed", error=exc)) from exc

    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return []
    current = data.get("current")
    rows = []
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        rows.append({
            "id": provider_id,
            "name": provider.get("name") or provider_id,
            "settings_config": provider.get("settingsConfig") or provider.get("settings_config") or provider,
            "meta": provider.get("meta") or {},
            "is_current": provider_id == current,
            "sort_index": provider.get("sortIndex"),
            "created_at": provider.get("createdAt"),
        })
    return rows


def _raw_rows(root: Path | None = None) -> list[dict]:
    paths = _ccswitch_paths(root)
    if paths["db"].exists():
        return _read_sqlite_rows(paths["db"])
    if paths["legacy"].exists():
        return _read_legacy_rows(paths["legacy"])
    return []


def read_providers(root: Path | None = None, include_secret: bool = False) -> list[dict]:
    rows = _raw_rows(root)
    return [_candidate_from_row(row, include_secret=include_secret) for row in rows]


def _existing_keys(providers: Iterable[dict]) -> dict[str, set]:
    keys = {"provider": set(), "source": set(), "names": set()}
    for provider in providers:
        name = str(provider.get("name", "")).strip()
        keys["provider"].add((name.lower(), _normalize_base_url(provider.get("baseUrl", "")).lower()))
        if name:
            keys["names"].add(name.lower())
        source = provider.get("source") if isinstance(provider.get("source"), dict) else {}
        if source.get("type") == "cc-switch" and source.get("id"):
            keys["source"].add(str(source["id"]))
    return keys


def _dedupe_import_name(name: str, existing_names: set[str]) -> str:
    base = i18n.t("ccswitch.imported_suffix", name=name)
    candidate = base
    index = 2
    while candidate.lower() in existing_names:
        candidate = f"{base} {index}"
        index += 1
    existing_names.add(candidate.lower())
    return candidate


def _to_ccds_provider(candidate: dict, name: str | None = None) -> dict:
    provider_id = candidate["id"]
    return {
        "id": f"ccswitch-{_safe_id(provider_id)}",
        "name": name or candidate["name"],
        "baseUrl": candidate["baseUrl"],
        "apiKey": candidate.get("apiKey", ""),
        "authScheme": candidate.get("authScheme") or "bearer",
        "apiFormat": "anthropic",
        "models": candidate.get("models") or {},
        "extraHeaders": candidate.get("extraHeaders") or {},
        "isBuiltin": False,
        "source": {
            "type": "cc-switch",
            "id": provider_id,
        },
    }


def import_providers(ids: list[str] | None = None, set_default: bool = False, root: Path | None = None) -> dict:
    candidates = read_providers(root=root, include_secret=True)
    selected_ids = set(ids or [item["id"] for item in candidates if item["supported"]])
    existing = _existing_keys(cfg.get_providers())
    imported = []
    skipped = []
    unsupported = []

    supported_to_import = []
    for candidate in candidates:
        if candidate["id"] not in selected_ids:
            continue
        if not candidate["supported"]:
            unsupported.append({
                "id": candidate["id"],
                "name": candidate["name"],
                "reason": candidate["reason"],
                "reasonCode": candidate.get("reasonCode", ""),
            })
            continue
        source_key = str(candidate["id"])
        if source_key in existing["source"]:
            skipped.append({
                "id": candidate["id"],
                "name": candidate["name"],
                "reason": i18n.t("ccswitch.already_imported"),
                "reasonCode": "already_imported",
            })
            continue
        key = (candidate["name"].strip().lower(), _normalize_base_url(candidate["baseUrl"]).lower())
        if key in existing["provider"]:
            candidate = dict(candidate)
            candidate["importName"] = _dedupe_import_name(candidate["name"], existing["names"])
        supported_to_import.append(candidate)

    backup = None
    if supported_to_import:
        backup = cfg.create_backup("before-ccswitch-import")
        for candidate in supported_to_import:
            provider = cfg.add_provider(_to_ccds_provider(candidate, candidate.get("importName")))
            imported.append({
                "id": provider["id"],
                "name": provider["name"],
                "baseUrl": provider["baseUrl"],
            })
            existing["provider"].add((provider["name"].strip().lower(), _normalize_base_url(provider["baseUrl"]).lower()))
            existing["names"].add(provider["name"].strip().lower())
            existing["source"].add(str(candidate["id"]))
        if set_default and imported:
            cfg.set_active_provider(imported[0]["id"])

    return {
        "imported": imported,
        "skipped": skipped,
        "unsupported": unsupported,
        "backup": backup,
    }
