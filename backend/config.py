"""Configuration management - JSON config file read and write."""

import copy
import json
import os
import secrets
import shutil
from datetime import datetime
from typing import Optional

from backend import i18n
from backend.model_alias import model_mappings_with_legacy_aliases, normalize_model_mappings
from backend.preset_loader import load_builtin_presets


def _resolve_config_paths() -> tuple[str, str, str]:
    override = os.environ.get("CCDS_CONFIG_DIR", "").strip()
    config_dir = os.path.abspath(os.path.expanduser(override or "~/.cc-desktop-switch"))
    return (
        config_dir,
        os.path.join(config_dir, "config.json"),
        os.path.join(config_dir, "backups"),
    )


CONFIG_DIR, CONFIG_FILE, BACKUP_DIR = _resolve_config_paths()
DEFAULT_UPDATE_URL = "https://github.com/lonr-6/cc-desktop-switch/releases/latest/download/latest.json"

DEFAULT_CONFIG = {
    "version": "1.0.27",
    "activeProvider": None,
    "gatewayApiKey": None,
    "providers": [],
    "settings": {
        "theme": "default",
        "language": "en",
        "proxyPort": 18080,
        "adminPort": 18081,
        "autoStart": False,
        "exposeAllProviderModels": False,
        "updateUrl": DEFAULT_UPDATE_URL,
        "upstreamProxy": "",
        "upstreamProxyEnabled": False,
        "enableBillingHeaderRectifier": True,
    },
}


def ensure_config_dir():
    """Ensure the configuration directory exists."""
    os.makedirs(CONFIG_DIR, exist_ok=True)


def ensure_backup_dir():
    """Ensure the configuration backup directory exists."""
    ensure_config_dir()
    os.makedirs(BACKUP_DIR, exist_ok=True)


def load_config() -> dict:
    """Load the configuration file from disk."""
    ensure_config_dir()
    if not os.path.exists(CONFIG_FILE):
        return _config_with_legacy_model_aliases(copy.deepcopy(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError):
        return _config_with_legacy_model_aliases(copy.deepcopy(DEFAULT_CONFIG))
    return _config_with_legacy_model_aliases(normalize_config(raw))


def save_config(config: dict):
    """Save the configuration file to disk atomically."""
    ensure_config_dir()
    normalized = normalize_config(config)
    # Atomic write: write to a temporary file first, then rename.
    tmp_file = CONFIG_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    shutil.move(tmp_file, CONFIG_FILE)


def _normalize_provider(provider: dict) -> dict:
    """Fill in required provider fields for backward compatibility with older configs."""
    normalized = dict(provider)
    provider_id = str(normalized.get("id") or "")
    safe_id = "".join(ch for ch in provider_id if ch.isalnum() or ch in {"-", "_"})[:64]
    normalized["id"] = safe_id or secrets.token_hex(4)
    normalized.setdefault("name", "Unnamed Provider")
    normalized.setdefault("baseUrl", "")
    normalized.setdefault("authScheme", "bearer")
    normalized.setdefault("apiFormat", "anthropic")
    normalized.setdefault("apiKey", "")
    normalized.setdefault("extraHeaders", {})
    normalized.setdefault("modelCapabilities", {})
    normalized.setdefault("requestOptions", {})
    normalized.setdefault("isBuiltin", False)
    normalized.setdefault("sortIndex", 0)
    normalized["models"] = normalize_model_mappings(normalized.get("models"))
    return normalized


def _provider_with_legacy_model_aliases(provider: dict) -> dict:
    compat = copy.deepcopy(provider)
    compat["models"] = model_mappings_with_legacy_aliases(compat.get("models"))
    return compat


def _config_with_legacy_model_aliases(config: dict) -> dict:
    compat = copy.deepcopy(config)
    providers = compat.get("providers", [])
    if isinstance(providers, list):
        compat["providers"] = [
            _provider_with_legacy_model_aliases(provider)
            if isinstance(provider, dict) else provider
            for provider in providers
        ]
    return compat


def _preset_with_legacy_model_aliases(preset: dict) -> dict:
    compat = _provider_with_legacy_model_aliases(_normalize_provider(copy.deepcopy(preset)))
    model_options = compat.get("modelOptions")
    if isinstance(model_options, dict):
        for option in model_options.values():
            if isinstance(option, dict) and isinstance(option.get("models"), dict):
                option["models"] = model_mappings_with_legacy_aliases(option.get("models"))
    return compat


def normalize_config(config: dict) -> dict:
    """Normalize an imported or raw config dictionary into the current schema."""
    if not isinstance(config, dict):
        raise ValueError(i18n.get_text("config.must_be_object", "en"))

    source = config.get("config") if isinstance(config.get("config"), dict) else config
    normalized = copy.deepcopy(DEFAULT_CONFIG)
    normalized.update({k: v for k, v in source.items() if k in normalized})
    normalized["version"] = source.get("version", DEFAULT_CONFIG["version"])

    settings = dict(DEFAULT_CONFIG["settings"])
    imported_settings = source.get("settings", {})
    if isinstance(imported_settings, dict):
        settings.update(imported_settings)
    normalized["settings"] = settings

    providers = source.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError(i18n.get_text("config.providers_must_be_array", "en"))
    normalized_providers = []
    seen_ids = set()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        normalized_provider = _normalize_provider(provider)
        if normalized_provider["id"] in seen_ids:
            normalized_provider["id"] = f"{normalized_provider['id']}-{secrets.token_hex(2)}"
        seen_ids.add(normalized_provider["id"])
        normalized_providers.append(normalized_provider)
    normalized["providers"] = normalized_providers

    provider_ids = {p["id"] for p in normalized["providers"]}
    active_provider = source.get("activeProvider")
    if active_provider in provider_ids:
        normalized["activeProvider"] = active_provider
    else:
        normalized["activeProvider"] = normalized["providers"][0]["id"] if normalized["providers"] else None

    if source.get("gatewayApiKey"):
        normalized["gatewayApiKey"] = source["gatewayApiKey"]

    return normalized


def create_backup(reason: str = "manual") -> dict:
    """Back up the current config file and return backup metadata."""
    ensure_backup_dir()
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())

    safe_reason = "".join(ch for ch in str(reason or "manual").lower() if ch.isalnum() or ch in {"-", "_"})[:32]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    filename = f"config-{timestamp}-{safe_reason or 'manual'}-{secrets.token_hex(2)}.json"
    target = os.path.join(BACKUP_DIR, filename)
    shutil.copy2(CONFIG_FILE, target)
    stat = os.stat(target)
    return {
        "name": filename,
        "size": stat.st_size,
        "createdAt": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def list_backups() -> list:
    """List saved configuration backups."""
    ensure_backup_dir()
    backups = []
    for name in os.listdir(BACKUP_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        backups.append({
            "name": name,
            "size": stat.st_size,
            "createdAt": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    return sorted(backups, key=lambda item: item["createdAt"], reverse=True)


def export_config() -> dict:
    """Export the full configuration including API keys for local user backup."""
    return {
        "format": "cc-desktop-switch.config",
        "exportedAt": datetime.now().isoformat(timespec="seconds"),
        "config": load_config(),
    }


def import_config(data: dict) -> dict:
    """Import configuration after automatically backing up the current config."""
    backup = create_backup("before-import")
    normalized = normalize_config(data)
    save_config(normalized)
    return {"config": normalized, "backup": backup}


def get_or_create_gateway_api_key() -> str:
    """Get the local gateway auth key, generating one if it does not exist yet.

    This key is written into Claude Desktop's managed policy to satisfy gateway mode's
    required credential check. It is not the upstream provider API key.
    """
    config = load_config()
    key = config.get("gatewayApiKey")
    if not key:
        key = "ccds_" + secrets.token_urlsafe(32)
        config["gatewayApiKey"] = key
        save_config(config)
    return key


def get_gateway_api_key() -> Optional[str]:
    """Read the local gateway auth key without auto-creating it."""
    return load_config().get("gatewayApiKey")


def get_providers() -> list:
    """Return all configured providers."""
    config = load_config()
    return config.get("providers", [])


def get_provider(provider_id: str) -> Optional[dict]:
    """Return a provider by ID."""
    for provider in get_providers():
        if provider.get("id") == provider_id:
            return provider
    return None


def get_active_provider() -> Optional[dict]:
    """Return the currently active provider."""
    config = load_config()
    active_id = config.get("activeProvider")
    if not active_id:
        providers = config.get("providers", [])
        return providers[0] if providers else None
    for p in config.get("providers", []):
        if p["id"] == active_id:
            return p
    return None


def add_provider(provider: dict) -> dict:
    """Add a new provider."""
    config = load_config()
    providers = config.get("providers", [])

    # Generate a unique ID
    import uuid
    provider = _normalize_provider(provider)
    existing_ids = {p.get("id") for p in providers}
    candidate_id = provider.get("id") or str(uuid.uuid4())[:8]
    while candidate_id in existing_ids:
        candidate_id = f"{provider.get('id') or 'provider'}-{secrets.token_hex(2)}"
    provider["id"] = candidate_id
    provider["sortIndex"] = len(providers)

    providers.append(provider)
    config["providers"] = providers

    # Automatically set as default if this is the first provider
    if len(providers) == 1:
        config["activeProvider"] = provider["id"]

    save_config(config)
    return _provider_with_legacy_model_aliases(provider)


def update_provider(provider_id: str, data: dict) -> Optional[dict]:
    """Update an existing provider."""
    config = load_config()
    for i, p in enumerate(config.get("providers", [])):
        if p["id"] == provider_id:
            updated = dict(p)
            updated.update(data)
            updated["id"] = provider_id
            updated["isBuiltin"] = p.get("isBuiltin", False)

            # Leaving API Key blank in the edit form means "keep existing key".
            if not data.get("apiKey"):
                updated["apiKey"] = p.get("apiKey", "")

            # Preserve preset extra auth headers such as DeepSeek's x-api-key.
            if "extraHeaders" not in data or data.get("extraHeaders") in (None, {}):
                updated["extraHeaders"] = p.get("extraHeaders", {})

            if "modelCapabilities" not in data:
                updated["modelCapabilities"] = p.get("modelCapabilities", {})

            if "requestOptions" not in data:
                updated["requestOptions"] = p.get("requestOptions", {})

            if "models" in data and isinstance(data["models"], dict):
                # Strip legacy compatibility aliases before merging so explicitly cleared slots stay cleared.
                merged_models = normalize_model_mappings(p.get("models", {}))
                merged_models.update(data["models"])
                updated["models"] = normalize_model_mappings(merged_models)

            config["providers"][i] = updated
            save_config(config)
            return _provider_with_legacy_model_aliases(updated)
    return None


def delete_provider(provider_id: str) -> bool:
    """Delete a provider by ID."""
    config = load_config()
    original_len = len(config.get("providers", []))
    config["providers"] = [p for p in config.get("providers", []) if p["id"] != provider_id]

    if len(config["providers"]) == original_len:
        return False

    # If the active provider was deleted, fall back to the first remaining provider.
    if config.get("activeProvider") == provider_id:
        config["activeProvider"] = config["providers"][0]["id"] if config["providers"] else None

    for index, provider in enumerate(config["providers"]):
        provider["sortIndex"] = index

    save_config(config)
    return True


def set_active_provider(provider_id: str) -> bool:
    """Set the default active provider."""
    config = load_config()
    for p in config.get("providers", []):
        if p["id"] == provider_id:
            config["activeProvider"] = provider_id
            save_config(config)
            return True
    return False


def update_models(provider_id: str, models: dict) -> bool:
    """Update model mappings for a provider."""
    config = load_config()
    for p in config.get("providers", []):
        if p["id"] == provider_id:
            p["models"] = normalize_model_mappings(models)
            save_config(config)
            return True
    return False


def reorder_providers(provider_ids: list[str]) -> bool:
    """Save providers in the order specified by the frontend drag-and-drop list."""
    config = load_config()
    providers = config.get("providers", [])
    by_id = {provider.get("id"): provider for provider in providers}
    ordered = []
    seen = set()
    for provider_id in provider_ids:
        provider = by_id.get(provider_id)
        if provider and provider_id not in seen:
            ordered.append(provider)
            seen.add(provider_id)
    ordered.extend(provider for provider in providers if provider.get("id") not in seen)
    if len(ordered) != len(providers):
        return False
    for index, provider in enumerate(ordered):
        provider["sortIndex"] = index
    config["providers"] = ordered
    save_config(config)
    return True


def get_settings() -> dict:
    """Return current application settings."""
    config = load_config()
    settings = dict(DEFAULT_CONFIG["settings"])
    settings.update(config.get("settings", {}))
    # Unified model menu remains disabled to prevent cross-provider capability conflicts.
    settings["exposeAllProviderModels"] = False
    if not settings.get("updateUrl"):
        settings["updateUrl"] = DEFAULT_UPDATE_URL
    return settings


def update_settings(settings: dict) -> dict:
    """Update application settings."""
    config = load_config()
    current = dict(DEFAULT_CONFIG["settings"])
    current.update(config.get("settings", {}))
    current.update(settings)
    current["exposeAllProviderModels"] = False
    if not current.get("updateUrl"):
        current["updateUrl"] = DEFAULT_UPDATE_URL
    config["settings"] = current
    save_config(config)
    return current


def get_presets() -> list:
    """Return the list of builtin provider presets."""
    return [_preset_with_legacy_model_aliases(preset) for preset in load_builtin_presets()]
