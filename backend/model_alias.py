"""Model alias and multi-provider routing utilities."""

from __future__ import annotations

import copy
import re
from typing import Optional


MODEL_SLOTS = (
    {
        "key": "default",
        "legacy": ("default",),
        "claude_ids": (),
    },
    {
        "key": "opus_4_7",
        "legacy": ("opus",),
        "claude_ids": ("claude-opus-4-7",),
    },
    {
        "key": "opus_4_6",
        "legacy": (),
        "claude_ids": ("claude-opus-4-6",),
    },
    {
        "key": "opus_3",
        "legacy": (),
        "claude_ids": ("claude-3-opus",),
    },
    {
        "key": "sonnet_4_6",
        "legacy": ("sonnet",),
        "claude_ids": ("claude-sonnet-4-6",),
    },
    {
        "key": "sonnet_4_5",
        "legacy": (),
        "claude_ids": ("claude-sonnet-4-5",),
    },
    {
        "key": "haiku_4_5",
        "legacy": ("haiku",),
        "claude_ids": ("claude-haiku-4-5",),
    },
)
MODEL_ORDER = tuple(item["key"] for item in MODEL_SLOTS)
DEFAULT_MODEL_KEY = "default"
LEGACY_MODEL_KEYS = ("default", "sonnet", "opus", "haiku")
MODEL_MAPPING_KEYS = set(MODEL_ORDER) | set(LEGACY_MODEL_KEYS)
CUSTOM_ROUTE_RE = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]*$")
CLAUDE_ID_TO_SLOT = {
    claude_id.lower(): slot["key"]
    for slot in MODEL_SLOTS
    for claude_id in slot["claude_ids"]
}
INTERNAL_CLAUDE_ID_TO_SLOT = {
    # Claude Desktop / Code WebFetch can request the dated Haiku ID even when
    # the picker only shows the stable claude-haiku-4-5 route.
    "claude-haiku-4-5-20251001": "haiku_4_5",
}
DESKTOP_ROUTE_IDS = tuple(
    claude_id
    for slot in MODEL_SLOTS
    for claude_id in slot["claude_ids"]
)


def empty_model_mappings() -> dict:
    return {key: "" for key in MODEL_ORDER}


def is_safe_custom_route_id(route_id: str) -> bool:
    """Only claude-* route names are safe to expose to Claude Desktop."""
    route_id = str(route_id or "").strip()
    return bool(CUSTOM_ROUTE_RE.fullmatch(route_id))


def normalize_model_mappings(models: Optional[dict]) -> dict:
    """Normalize legacy 4-slot and current multi-slot structures while preserving safe custom Claude routes."""
    normalized = empty_model_mappings()
    if not isinstance(models, dict):
        return normalized

    source = copy.deepcopy(models)
    default = str(source.get("default") or "").strip()
    normalized["default"] = default

    for slot in MODEL_SLOTS:
        key = slot["key"]
        if key == DEFAULT_MODEL_KEY:
            continue
        for candidate in (key, *slot["legacy"]):
            value = str(source.get(candidate) or "").strip()
            if value:
                normalized[key] = value
                break

    for key, value in source.items():
        route_id = str(key or "").strip()
        source_model = str(value or "").strip()
        if route_id not in MODEL_MAPPING_KEYS and is_safe_custom_route_id(route_id) and source_model:
            normalized[route_id] = source_model
    return normalized


def custom_model_mappings(models: Optional[dict]) -> dict[str, str]:
    """Return safe custom Claude route -> upstream model mappings."""
    normalized = normalize_model_mappings(models)
    return {
        key: value
        for key, value in normalized.items()
        if key not in MODEL_MAPPING_KEYS and is_safe_custom_route_id(key) and value
    }


def model_mappings_with_legacy_aliases(models: Optional[dict]) -> dict:
    """Add legacy 4-slot aliases onto the normalized slot structure for backward-compatible reads."""
    normalized = normalize_model_mappings(models)
    compat = dict(normalized)
    compat["default"] = normalized.get("default", "")
    compat["sonnet"] = (
        normalized.get("sonnet_4_6")
        or normalized.get("sonnet_4_5")
        or ""
    )
    compat["opus"] = (
        normalized.get("opus_4_7")
        or normalized.get("opus_4_6")
        or normalized.get("opus_3")
        or ""
    )
    compat["haiku"] = (
        normalized.get("haiku_4_5")
        or ""
    )
    return compat


def provider_model_ids(provider: Optional[dict]) -> list[str]:
    """Return the deduplicated upstream model IDs exposed by a provider in stable order."""
    if not provider:
        return []
    models = normalize_model_mappings(provider.get("models") or {})
    ordered: list[str] = []
    for key in MODEL_ORDER:
        model_id = str(models.get(key) or "").strip()
        if model_id and model_id not in ordered:
            ordered.append(model_id)
    for model_id in custom_model_mappings(provider.get("models") or {}).values():
        model_id = str(model_id or "").strip()
        if model_id and model_id not in ordered:
            ordered.append(model_id)
    return ordered


def provider_slug(provider: dict) -> str:
    """Generate a stable provider slug prefix for the Claude model menu."""
    source = str(provider.get("id") or provider.get("name") or "provider").lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", source).strip("-_")
    return slug[:56] or "provider"


def model_alias(provider: dict, model_id: str) -> str:
    """Combine a provider slug and model ID into a menu alias."""
    return f"{provider_slug(provider)}/{model_id}"


def model_supports_1m(provider: dict, model_id: str) -> bool:
    """Return True if the model should declare supports1m."""
    capabilities = provider.get("modelCapabilities") or {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    model_capability = capabilities.get(model_id)
    return "[1m]" in model_id.lower() or (
        isinstance(model_capability, dict)
        and model_capability.get("supports1m") is True
    )


def desktop_model_entries(provider: Optional[dict], use_alias: bool = False) -> list[dict]:
    """Generate explicit safe-route model entries visible to Claude Desktop and the gateway."""
    if not provider:
        return []
    raw_models = provider.get("models") or {}
    if not isinstance(raw_models, dict):
        raw_models = {}
    normalized = normalize_model_mappings(raw_models)
    provider_name = str(provider.get("name") or provider.get("id") or "Provider")
    entries: list[dict] = []
    seen: set[str] = set()

    def add_entry(route_id: str, source_model: str) -> None:
        route_id = str(route_id or "").strip()
        source_model = str(source_model or "").strip()
        if not route_id or not source_model:
            return
        name = model_alias(provider, route_id) if use_alias else route_id
        if name in seen:
            return
        seen.add(name)
        item = {
            "name": name,
            "displayName": f"{provider_name} / {route_id}" if use_alias else route_id,
            "sourceModel": source_model,
            "providerId": provider.get("id"),
        }
        if model_supports_1m(provider, source_model):
            item["supports1m"] = True
        entries.append(item)

    for slot in MODEL_SLOTS:
        if slot["key"] == DEFAULT_MODEL_KEY or not slot["claude_ids"]:
            continue
        source_model = normalized.get(slot["key"])
        if source_model:
            add_entry(slot["claude_ids"][0], source_model)

    for route_id, source_model in custom_model_mappings(raw_models).items():
        add_entry(route_id, source_model)

    return entries


def provider_model_entries(provider: Optional[dict], use_alias: bool = False) -> list[dict]:
    """Generate raw upstream model entries; do not expose directly to Claude Desktop."""
    if not provider:
        return []
    entries: list[dict] = []
    provider_name = str(provider.get("name") or provider.get("id") or "Provider")
    for model_id in provider_model_ids(provider):
        name = model_alias(provider, model_id) if use_alias else model_id
        item = {
            "name": name,
            "displayName": f"{provider_name} / {model_id}" if use_alias else model_id,
            "sourceModel": model_id,
            "providerId": provider.get("id"),
        }
        if model_supports_1m(provider, model_id):
            item["supports1m"] = True
        entries.append(item)
    return entries


def all_provider_model_entries(providers: list[dict]) -> list[dict]:
    """Generate deduplicated safe-route model entries across all providers."""
    entries: list[dict] = []
    seen: set[str] = set()
    for provider in providers:
        if not provider:
            continue
        for item in desktop_model_entries(provider, use_alias=True):
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            entries.append(item)
    return entries


def resolve_model_alias(providers: list[dict], requested_model: str) -> tuple[Optional[dict], str, bool]:
    """Resolve a provider/model alias into the target provider and upstream model ID."""
    requested = str(requested_model or "")
    if "/" not in requested:
        return None, requested, False
    slug, model_id = requested.split("/", 1)
    if not slug or not model_id:
        return None, requested, False
    for provider in providers:
        if provider_slug(provider) == slug:
            for item in desktop_model_entries(provider):
                if model_id == item.get("name"):
                    return provider, model_id, True
            if model_id in provider_model_ids(provider):
                return provider, model_id, True
            # Allow manually specified model IDs even if not listed in mappings.
            return provider, model_id, True
    return None, requested, False


def resolve_requested_model_slot(requested_model: str) -> Optional[str]:
    """Resolve a requested Claude model name into the configured mapping slot key."""
    requested = str(requested_model or "").strip().lower()
    if not requested:
        return None
    mapped = CLAUDE_ID_TO_SLOT.get(requested)
    if mapped:
        return mapped
    mapped = INTERNAL_CLAUDE_ID_TO_SLOT.get(requested)
    if mapped:
        return mapped

    # Support legacy Claude model IDs and family names not explicitly listed.
    if "haiku" in requested:
        return "haiku"
    if "sonnet" in requested:
        if "4-6" in requested:
            return "sonnet_4_6"
        if "4-5" in requested:
            return "sonnet_4_5"
        return "sonnet"
    if "opus" in requested:
        if "4-7" in requested:
            return "opus_4_7"
        if "4-6" in requested:
            return "opus_4_6"
        if requested.startswith("claude-3") or "-3-" in requested or requested.endswith("-3"):
            return "opus_3"
        return "opus"
    return None


def desktop_route_ids() -> tuple[str, ...]:
    """Return the set of safe route IDs visible to Claude Desktop."""
    return DESKTOP_ROUTE_IDS
