"""Windows registry / macOS plist operations for configuring Claude Desktop 3P mode."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Optional

from backend import config as app_config
from backend import i18n
from backend.model_alias import all_provider_model_entries, desktop_model_entries

REGISTRY_PATH = r"SOFTWARE\Policies\Claude"
CCDS_MARKER = "ccds_managed"
COWORK_EGRESS_ALLOWED_HOSTS = '["*"]'

# Legacy policy values retained for marker-gated migration and macOS preference compatibility.
DESKTOP_CONFIG = {
    "inferenceProvider": ("gateway", str),
    "inferenceGatewayApiKey": ("", str),
    "inferenceGatewayAuthScheme": ("bearer", str),
    "inferenceGatewayHeaders": ("[]", str),
    "inferenceModels": ("[]", str),
    "inferenceGatewayBaseUrl": ("http://127.0.0.1:18080", str),
    "isClaudeCodeForDesktopEnabled": (1, int),
    "coworkEgressAllowedHosts": (COWORK_EGRESS_ALLOWED_HOSTS, str),
}

# ── Helper Functions ──

def _managed_policy_names(names: list[str]) -> list[str]:
    """Return the Claude policy keys managed by this tool that should be deleted when clearing."""
    managed = set(DESKTOP_CONFIG.keys()) | {CCDS_MARKER}
    return [name for name in names if name in managed]


def _desktop_model_items(items: list) -> list:
    """Keep only model fields supported by the Claude Desktop policy."""
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        allowed = {
            "name": item.get("name"),
            "displayName": item.get("displayName"),
        }
        if item.get("supports1m") is True:
            allowed["supports1m"] = True
        cleaned.append({k: v for k, v in allowed.items() if v is not None})
    return cleaned


def _safe_config_value(name: str, value) -> str:
    """Return a display-safe configuration value without exposing secrets to the frontend."""
    lowered = name.lower()
    if "headers" in lowered and value in (None, "", "[]", []):
        return ""
    if any(token in lowered for token in ("key", "token", "secret", "authorization", "headers")):
        return "******" if value else ""
    return str(value)


def serialize_gateway_headers(extra_headers: Optional[dict], api_key: str = "") -> str:
    """Convert provider extraHeaders into extra request headers for the Claude 3P policy."""
    if not isinstance(extra_headers, dict) or not extra_headers:
        return ""
    headers = []
    for name, value in extra_headers.items():
        header_name = str(name or "").strip()
        if not header_name:
            continue
        header_value = str(value or "")
        if "{apiKey}" in header_value:
            header_value = header_value.replace("{apiKey}", api_key or "")
        headers.append(f"{header_name}: {header_value}")
    return json.dumps(headers, ensure_ascii=False, separators=(",", ":")) if headers else ""


def _os_name() -> str:
    """Return 'win', 'mac', or 'linux'."""
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _not_supported() -> dict:
    """Return a status message when running on neither Windows nor macOS."""
    return {
        "success": False,
        "code": "linux_unsupported",
        "message": i18n.t("registry.linux_unsupported"),
    }


def provider_inference_models(provider: Optional[dict]) -> list:
    """Generate the model list required by the Claude Desktop gateway policy.

    Claude Desktop only sees Claude-safe route names; raw upstream model IDs stay
    inside the local proxy mapping. 1M context still requires supports1m on the route.
    """
    if not provider:
        return []
    result = _desktop_model_items(desktop_model_entries(provider, use_alias=False))
    return result


def all_provider_inference_models(providers: list[dict]) -> list:
    """Generate the Claude Desktop model list across all providers."""
    result = _desktop_model_items(all_provider_model_entries(providers))
    return result


def serialize_inference_models(
    provider: Optional[dict],
    providers: Optional[list[dict]] = None,
    expose_all: bool = False,
) -> str:
    """Serialize inferenceModels for registry or plist writing."""
    models = all_provider_inference_models(providers or []) if expose_all else provider_inference_models(provider)
    return json.dumps(
        models,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ── Windows ──

def _win_policy_values(hive_name: str) -> dict:
    """Read values from a Claude policy hive. This function never creates or writes keys."""
    import winreg

    hive = winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    result = {"key": f"{hive_name}\\{REGISTRY_PATH}", "exists": False, "values": [], "error": ""}
    try:
        key = winreg.OpenKey(hive, REGISTRY_PATH, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["exists"] = True
    try:
        index = 0
        while True:
            name, value, value_type = winreg.EnumValue(key, index)
            result["values"].append({"name": name, "value": value, "type": value_type})
            index += 1
    except OSError as exc:
        if getattr(exc, "winerror", None) != 259 and "no more" not in str(exc).lower():
            result["error"] = str(exc)
    finally:
        winreg.CloseKey(key)
    return result


def _win_clean_marked_policy_values() -> dict:
    """Remove legacy HKCU values only when Junction's historical marker is present."""
    import winreg

    snapshot = _win_policy_values("HKCU")
    values = {item["name"]: item["value"] for item in snapshot["values"]}
    if str(values.get(CCDS_MARKER, "")).lower() != "true":
        return {"success": True, "removed": 0, "message": "No Junction-owned legacy policy marker was present."}

    managed = set(DESKTOP_CONFIG) | {CCDS_MARKER}
    names = [item["name"] for item in snapshot["values"] if item["name"] in managed]
    if not names:
        return {"success": True, "removed": 0, "message": "No legacy Junction policy values remained."}
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        )
    except OSError as exc:
        return {"success": False, "removed": 0, "message": f"Could not open HKCU\\{REGISTRY_PATH} for cleanup: {exc}"}

    failures = []
    removed = 0
    try:
        for name in names:
            try:
                winreg.DeleteValue(key, name)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(f"{name}: {exc}")
    finally:
        winreg.CloseKey(key)
    if failures:
        return {
            "success": False,
            "removed": removed,
            "message": "Could not remove some Junction-owned legacy policy values: " + "; ".join(failures),
        }
    return {"success": True, "removed": removed, "message": f"Removed {removed} Junction-owned legacy policy values."}


def _win_policy_lock_warnings() -> list[str]:
    """Explain any remaining policy values that can keep Claude's UI organization-managed."""
    warnings = []
    known = set(DESKTOP_CONFIG) | {CCDS_MARKER}
    user = _win_policy_values("HKCU")
    machine = _win_policy_values("HKLM")
    if user.get("error"):
        warnings.append(i18n.t("registry.policy_read_error", path=user["key"], error=user["error"]))
    if machine.get("error"):
        warnings.append(i18n.t("registry.policy_read_error", path=machine["key"], error=machine["error"]))

    marker = next((item["value"] for item in user["values"] if item["name"] == CCDS_MARKER), None)
    marker_is_ours = str(marker or "").lower() == "true"
    for item in user["values"]:
        name = item["name"]
        if name == CCDS_MARKER:
            continue
        if name in known:
            if not marker_is_ours:
                warnings.append(i18n.t("registry.policy_unmarked_value", path=f"{user['key']}\\{name}"))
            else:
                warnings.append(i18n.t("registry.policy_legacy_remained", path=f"{user['key']}\\{name}"))
        else:
            warnings.append(i18n.t("registry.policy_unknown_value", path=f"{user['key']}\\{name}"))
    for item in machine["values"]:
        warnings.append(i18n.t("registry.policy_hklm_value", path=f"{machine['key']}\\{item['name']}"))
    return warnings


def _win_get_config_status() -> dict:
    library_status = _get_library_config_status()
    user_policy = _win_policy_values("HKCU")
    policy_values = {item["name"]: item["value"] for item in user_policy["values"]}
    marked_legacy = str(policy_values.get(CCDS_MARKER, "")).lower() == "true"

    if library_status.get("keys"):
        keys = dict(library_status["keys"])
        configured = library_status.get("configured", False)
        active_source = "configLibrary"
    elif marked_legacy:
        keys = {
            item["name"]: _safe_config_value(item["name"], item["value"])
            for item in user_policy["values"]
            if item["name"] in DESKTOP_CONFIG
        }
        configured = keys.get("inferenceProvider") == "gateway"
        active_source = "legacyPolicy" if keys else ""
    else:
        keys = {}
        configured = False
        active_source = ""

    warnings = _win_policy_lock_warnings()
    message = "; ".join(warnings) or library_status.get("message", "")
    return {
        "configured": configured,
        "keys": keys,
        "message": message,
        "activeSource": active_source,
        "keySources": {name: active_source for name in keys},
        "sources": {
            "configLibrary": library_status.get("configured", False),
            "legacyPolicy": marked_legacy,
        },
        "lockedByOtherPolicy": bool(warnings),
        "policyWarnings": warnings,
    }


def _win_apply_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    library_result = _apply_library_config(
        base_url,
        gateway_api_key,
        inference_models,
        auth_scheme,
        gateway_headers,
        chat_tab_enabled,
    )
    if not library_result.get("success"):
        return library_result

    mode_result = _set_deployment_mode("3p")
    if not mode_result.get("success"):
        return mode_result

    legacy_result = _win_clean_marked_policy_values()
    warnings = _win_policy_lock_warnings()
    messages = []
    if not legacy_result.get("success"):
        messages.append(legacy_result.get("message", "Legacy policy cleanup failed."))
    messages.extend(warnings)
    result = {"success": True, "message": "Junction configLibrary entry applied."}
    if messages:
        result["message"] += " " + " ".join(messages)
    if warnings:
        result["lockedByOtherPolicy"] = True
        result["policyWarnings"] = warnings
    return result


def _win_clear_config() -> dict:
    library_result = _clear_library_config()
    if not library_result.get("success"):
        return library_result

    mode_result = {"success": True, "message": ""}
    if library_result.get("wasActive") and not library_result.get("remainingEntries"):
        mode_result = _set_deployment_mode("clear")

    legacy_result = _win_clean_marked_policy_values()
    warnings = _win_policy_lock_warnings()
    failures = [
        item.get("message", "Clear failed.")
        for item in (mode_result, legacy_result)
        if not item.get("success")
    ]
    if failures:
        return {"success": False, "message": " ".join(failures)}

    message = "Junction's configLibrary entry and marked legacy policy values were cleared."
    if warnings:
        message += " " + " ".join(warnings)
    return {
        "success": True,
        "message": message,
        "lockedByOtherPolicy": bool(warnings),
        "policyWarnings": warnings,
    }


# ── macOS ──

MAC_BUNDLE = "com.anthropic.claudefordesktop"
MAC_PLIST = f"~/Library/Preferences/{MAC_BUNDLE}.plist"
MAC_3P_CONFIG = "~/Library/Application Support/Claude-3p/claude_desktop_config.json"
MAC_3P_CONFIG_LIBRARY = "configLibrary"


def _mac_run(args: list) -> tuple:
    """Run a macOS defaults command and return (ok, output)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        output = "\n".join(part.strip() for part in (r.stdout, r.stderr) if part.strip())
        return r.returncode == 0, output
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _mac_get_plist_config_status() -> dict:
    keys = {}
    for name in DESKTOP_CONFIG:
        ok, out = _mac_run(["defaults", "read", MAC_BUNDLE, name])
        if ok:
            keys[name] = _safe_config_value(name, out)
    # Check marker
    ok, marker = _mac_run(["defaults", "read", MAC_BUNDLE, CCDS_MARKER])
    marked = ok and marker == "true"
    configured = keys.get("inferenceProvider") == "gateway" and marked
    return {"configured": configured, "keys": keys, "message": ""}


def _mac_apply_plist_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    try:
        inference_models = inference_models or DESKTOP_CONFIG["inferenceModels"][0]
        expected = {}
        failures = []
        for name in DESKTOP_CONFIG:
            val, typ = DESKTOP_CONFIG[name]
            if name == "inferenceGatewayBaseUrl":
                val = base_url
            if name == "inferenceGatewayApiKey":
                val = gateway_api_key
            if name == "inferenceGatewayAuthScheme":
                val = auth_scheme or DESKTOP_CONFIG["inferenceGatewayAuthScheme"][0]
            if name == "inferenceGatewayHeaders":
                val = gateway_headers or DESKTOP_CONFIG["inferenceGatewayHeaders"][0]
            if name == "inferenceModels":
                val = inference_models
            expected[name] = val
            # Choose defaults -type flag based on Python value type
            if typ == int:
                ok, out = _mac_run(["defaults", "write", MAC_BUNDLE, name, "-int", str(val)])
            else:
                ok, out = _mac_run(["defaults", "write", MAC_BUNDLE, name, "-string", str(val)])
            if not ok:
                detail = out if "key" not in name.lower() else "defaults write failed"
                failures.append(f"{name}: {detail or 'defaults write failed'}")

        ok, out = _mac_run(["defaults", "write", MAC_BUNDLE, CCDS_MARKER, "-string", "true"])
        if not ok:
            failures.append(f"{CCDS_MARKER}: {out or 'defaults write failed'}")
        expected[CCDS_MARKER] = "true"

        if failures:
            return {"success": False, "message": i18n.t("registry.macos_write_failed", errors="; ".join(failures))}

        for name, val in expected.items():
            ok, out = _mac_run(["defaults", "read", MAC_BUNDLE, name])
            if not ok:
                failures.append(f"{name}: readback failed")
                continue
            if str(out) != str(val):
                failures.append(f"{name}: readback mismatch")

        if failures:
            return {"success": False, "message": i18n.t("registry.macos_verify_failed", errors="; ".join(failures))}
        return {"success": True, "message": i18n.t("registry.macos_applied")}
    except Exception as e:
        return {"success": False, "message": i18n.t("registry.macos_failed", error=str(e))}


def _windows_config_root_path() -> str:
    """Prefer LOCALAPPDATA; fall back to APPDATA only when it is the existing legacy root."""
    local_base = os.environ.get("LOCALAPPDATA", "").strip()
    legacy_base = os.environ.get("APPDATA", "").strip()
    local_root = os.path.join(local_base, "Claude-3p") if local_base else ""
    legacy_root = os.path.join(legacy_base, "Claude-3p") if legacy_base else ""
    if local_root and os.path.isdir(local_root):
        return local_root
    if legacy_root and os.path.isdir(legacy_root):
        return legacy_root
    if local_root:
        return local_root
    if legacy_root:
        return legacy_root
    return os.path.join(os.path.expanduser("~"), "AppData", "Local", "Claude-3p")


def _mac_config_json_path() -> str:
    return os.path.expanduser(MAC_3P_CONFIG)


def _config_root_path() -> str:
    if _os_name() == "win":
        return _windows_config_root_path()
    return os.path.dirname(_mac_config_json_path())


def _config_json_path() -> str:
    return os.path.join(_config_root_path(), "claude_desktop_config.json")


def _config_library_dir_path() -> str:
    return os.path.join(_config_root_path(), MAC_3P_CONFIG_LIBRARY)


def _config_library_meta_path() -> str:
    return os.path.join(_config_library_dir_path(), "_meta.json")


def _config_library_entry_path(entry_id: str) -> str:
    return os.path.join(_config_library_dir_path(), f"{entry_id}.json")


def _mac_read_json_file(path: str) -> tuple[bool, dict, str]:
    if not os.path.exists(path):
        return True, {}, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return False, {}, "JSON root is not an object"
        return True, data, ""
    except Exception as exc:
        return False, {}, str(exc)


def _mac_write_json_file(path: str, data: dict) -> tuple[bool, str]:
    directory = os.path.dirname(path)
    temp_path = ""
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".ccds-", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
        return True, ""
    except Exception as exc:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False, str(exc)


def _set_deployment_mode(mode: str) -> dict:
    """Change only deploymentMode, preserve all other Claude preferences, and keep a first-change backup."""
    path = _config_json_path()
    ok, data, message = _mac_read_json_file(path)
    if not ok:
        return {"success": False, "message": f"Could not read Claude Desktop config: {message}"}
    if data.get("deploymentMode") == mode:
        return {"success": True, "message": ""}

    if os.path.isfile(path):
        backup_path = path + ".junction.bak"
        try:
            if not os.path.exists(backup_path):
                shutil.copy2(path, backup_path)
        except OSError as exc:
            return {"success": False, "message": f"Could not back up Claude Desktop config: {exc}"}

    data["deploymentMode"] = mode
    ok, message = _mac_write_json_file(path, data)
    if not ok:
        return {"success": False, "message": f"Could not update Claude Desktop deploymentMode: {message}"}
    return {"success": True, "message": ""}


def _mac_get_json_config_status() -> dict:
    path = _config_json_path()
    exists = os.path.exists(path)
    ok, data, message = _mac_read_json_file(path)
    if not ok:
        return {"configured": False, "keys": {}, "message": message, "exists": exists}
    return {
        "configured": data.get("deploymentMode") == "3p",
        "keys": {},
        "message": "",
        "exists": exists,
    }


JUNCTION_LIBRARY_ENTRY_SETTING = "claudeConfigLibraryEntryId"
JUNCTION_PREVIOUS_APPLIED_SETTING = "claudeConfigLibraryPreviousAppliedId"


def _library_entry_info() -> tuple[bool, dict, str]:
    """Find only Junction's entry, using its saved ID or its exact display name."""
    ok, meta, message = _mac_read_json_file(_config_library_meta_path())
    if not ok:
        return False, {}, message
    entries = meta.get("entries", [])
    if not isinstance(entries, list):
        return False, {}, "configLibrary metadata entries is not a list"

    saved_id = str(app_config.get_settings().get(JUNCTION_LIBRARY_ENTRY_SETTING) or "").strip()
    candidate = None
    if saved_id:
        candidate = next(
            (
                entry for entry in entries
                if isinstance(entry, dict)
                and str(entry.get("id") or "") == saved_id
                and entry.get("name") == "Junction"
            ),
            None,
        )
    if candidate is None:
        candidate = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("name") == "Junction"),
            None,
        )
    entry_id = str(candidate.get("id") or "").strip() if candidate else ""
    if entry_id and (os.path.basename(entry_id) != entry_id or entry_id in {".", ".."}):
        return False, {}, "Junction configLibrary entry ID is not a safe filename"
    path = _config_library_entry_path(entry_id) if entry_id else ""
    return True, {"meta": meta, "entries": entries, "entry": candidate, "id": entry_id, "path": path}, ""


def _native_inference_models(inference_models: str) -> list[dict]:
    """Convert internal model labels to Claude's observed configLibrary field names."""
    try:
        parsed = json.loads(inference_models or "[]")
    except (TypeError, ValueError):
        parsed = []
    result = []
    seen = set()
    if not isinstance(parsed, list):
        return result
    for item in parsed:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            label = str(item.get("labelOverride") or item.get("displayName") or "").strip()
        else:
            name = str(item or "").strip()
            label = ""
        if not name or name in seen:
            continue
        seen.add(name)
        native = {"name": name}
        if label:
            native["labelOverride"] = label
        result.append(native)
    return result


def _library_entry_expected_config(
    base_url: str,
    gateway_api_key: str,
    inference_models: str,
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    """Build Junction's library entry from fields observed in Claude's native entry format."""
    return {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayApiKey": gateway_api_key,
        "chatTabEnabled": bool(chat_tab_enabled),
        "modelDiscoveryEnabled": True,
        "inferenceModels": _native_inference_models(inference_models),
    }


def _get_library_config_status() -> dict:
    ok, info, message = _library_entry_info()
    if not ok:
        return {"configured": False, "keys": {}, "message": message, "exists": False}
    if not info["id"]:
        return {
            "configured": False,
            "keys": {},
            "message": "",
            "exists": os.path.isdir(_config_library_dir_path()),
        }
    ok, data, message = _mac_read_json_file(info["path"])
    if not ok:
        return {"configured": False, "keys": {}, "message": message, "exists": True}
    keys = {}
    for name in (
        "inferenceProvider",
        "inferenceGatewayBaseUrl",
        "inferenceGatewayApiKey",
        "chatTabEnabled",
        "modelDiscoveryEnabled",
        "inferenceModels",
    ):
        if name not in data:
            continue
        value = data[name]
        if name == "inferenceModels" and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        keys[name] = _safe_config_value(name, value)
    active = info["meta"].get("appliedId") == info["id"]
    return {
        "configured": keys.get("inferenceProvider") == "gateway" and active,
        "keys": keys,
        "message": "",
        "exists": True,
        "entryId": info["id"],
        "active": active,
    }


def _apply_library_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    ok, info, message = _library_entry_info()
    if not ok:
        return {"success": False, "message": f"Could not read configLibrary metadata: {message}"}

    meta = info["meta"]
    entries = list(info["entries"])
    entry = info["entry"]
    entry_id = info["id"] or str(uuid.uuid4())
    previous_applied = str(meta.get("appliedId") or "").strip()
    saved_settings = app_config.get_settings()
    saved_previous = str(saved_settings.get(JUNCTION_PREVIOUS_APPLIED_SETTING) or "").strip()
    if previous_applied == entry_id:
        previous_applied = saved_previous
    expected = _library_entry_expected_config(
        base_url,
        gateway_api_key,
        inference_models or DESKTOP_CONFIG["inferenceModels"][0],
        auth_scheme,
        gateway_headers,
        chat_tab_enabled,
    )

    entry_path = _config_library_entry_path(entry_id)
    ok, data, message = _mac_read_json_file(entry_path)
    if not ok:
        return {"success": False, "message": f"Could not read Junction configLibrary entry: {message}"}
    data.update(expected)
    ok, message = _mac_write_json_file(entry_path, data)
    if not ok:
        return {"success": False, "message": f"Could not write Junction configLibrary entry: {message}"}
    ok, saved, message = _mac_read_json_file(entry_path)
    if not ok or any(saved.get(name) != value for name, value in expected.items()):
        return {"success": False, "message": f"Junction configLibrary entry verification failed: {message or 'readback mismatch'}"}

    if entry is None:
        entries.append({"id": entry_id, "name": "Junction"})
    else:
        for item in entries:
            if isinstance(item, dict) and str(item.get("id") or "") == entry_id and item.get("name") == "Junction":
                item.setdefault("name", "Junction")
                break
    meta["entries"] = entries
    meta["appliedId"] = entry_id
    ok, message = _mac_write_json_file(_config_library_meta_path(), meta)
    if not ok:
        return {"success": False, "message": f"Could not update configLibrary metadata: {message}"}

    app_config.update_settings({
        JUNCTION_LIBRARY_ENTRY_SETTING: entry_id,
        JUNCTION_PREVIOUS_APPLIED_SETTING: previous_applied,
    })
    return {"success": True, "message": "Junction configLibrary entry applied.", "entryId": entry_id}


def _clear_library_config() -> dict:
    ok, info, message = _library_entry_info()
    if not ok:
        return {"success": False, "message": f"Could not read configLibrary metadata: {message}"}
    meta = info["meta"]
    entries = list(info["entries"])
    entry_id = info["id"]
    entry = info["entry"]
    saved_settings = app_config.get_settings()
    saved_id = str(saved_settings.get(JUNCTION_LIBRARY_ENTRY_SETTING) or "").strip()
    was_active = bool(entry is not None and entry_id and meta.get("appliedId") == entry_id)

    removed = False
    if entry is not None and entry_id:
        entries = [
            item for item in entries
            if not (
                isinstance(item, dict)
                and str(item.get("id") or "") == entry_id
                and item.get("name") == "Junction"
            )
        ]
        removed = len(entries) != len(info["entries"])
    if was_active:
        previous = str(saved_settings.get(JUNCTION_PREVIOUS_APPLIED_SETTING) or "").strip()
        remaining_ids = {
            str(item.get("id") or "") for item in entries if isinstance(item, dict)
        }
        if previous and previous in remaining_ids:
            meta["appliedId"] = previous
        else:
            meta.pop("appliedId", None)

    changed = removed or (was_active and meta != info["meta"])
    if changed:
        meta["entries"] = entries
        ok, message = _mac_write_json_file(_config_library_meta_path(), meta)
        if not ok:
            return {"success": False, "message": f"Could not update configLibrary metadata: {message}"}

    cleanup_error = ""
    if removed:
        try:
            os.remove(_config_library_entry_path(entry_id))
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = f"Could not remove orphaned Junction configLibrary file: {exc}"

    if saved_id or saved_settings.get(JUNCTION_PREVIOUS_APPLIED_SETTING):
        app_config.update_settings({
            JUNCTION_LIBRARY_ENTRY_SETTING: "",
            JUNCTION_PREVIOUS_APPLIED_SETTING: "",
        })
    if cleanup_error:
        return {"success": False, "message": cleanup_error}
    return {
        "success": True,
        "message": "Junction configLibrary entry cleared." if removed else "No Junction configLibrary entry was present.",
        "wasActive": was_active,
        "remainingEntries": [item for item in entries if isinstance(item, dict)],
        "removed": removed,
    }


# Compatibility aliases retain the old macOS function names while sharing one safe implementation.
def _mac_get_library_config_status() -> dict:
    return _get_library_config_status()


def _mac_apply_library_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    return _apply_library_config(
        base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers, chat_tab_enabled
    )


def _mac_clear_library_config() -> dict:
    return _clear_library_config()


def _mac_apply_json_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    """Retain the legacy helper name; Claude's JSON config now changes only deploymentMode."""
    return _set_deployment_mode("3p")


def _mac_get_config_status() -> dict:
    plist_status = _mac_get_plist_config_status()
    json_status = _mac_get_json_config_status()
    library_status = _mac_get_library_config_status()
    if library_status.get("keys"):
        keys = dict(library_status["keys"])
        active_source = "configLibrary"
        configured = library_status.get("configured", False)
    else:
        keys = dict(plist_status.get("keys") or {})
        active_source = "plist" if keys else ""
        configured = plist_status.get("configured", False)

    return {
        "configured": configured,
        "keys": keys,
        "message": library_status.get("message") or json_status.get("message") or plist_status.get("message", ""),
        "activeSource": active_source,
        "keySources": {name: active_source for name in keys},
        "sources": {
            "plist": plist_status.get("configured", False),
            "json": json_status.get("configured", False),
            "configLibrary": library_status.get("configured", False),
        },
    }


def _mac_apply_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    library_result = _mac_apply_library_config(
        base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers, chat_tab_enabled
    )
    json_result = _mac_apply_json_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    plist_result = _mac_apply_plist_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    if library_result.get("success") and json_result.get("success") and plist_result.get("success"):
        return {"success": True, "message": i18n.t("registry.macos_applied")}

    failures = []
    write_failed = i18n.t("registry.write_failed_short")
    if not library_result.get("success"):
        failures.append(f"configLibrary: {library_result.get('message', write_failed)}")
    if not json_result.get("success"):
        failures.append(f"json: {json_result.get('message', write_failed)}")
    if not plist_result.get("success"):
        failures.append(f"plist: {plist_result.get('message', write_failed)}")
    return {"success": False, "message": i18n.t("registry.macos_partial_write_failed", errors="; ".join(failures))}


def _mac_clear_plist_config() -> dict:
    managed = list(DESKTOP_CONFIG.keys()) + [CCDS_MARKER]
    count = 0
    for name in managed:
        ok, _ = _mac_run(["defaults", "delete", MAC_BUNDLE, name])
        if ok:
            count += 1
    if count:
        return {"success": True, "message": i18n.t("registry.cleared_count", count=count)}
    return {"success": True, "message": i18n.t("registry.nothing_to_clear")}


def _mac_clear_json_config() -> dict:
    """Retain the legacy helper name; only deploymentMode is changed."""
    return _set_deployment_mode("clear")


def _mac_clear_library_config() -> dict:
    return _clear_library_config()


def _mac_clear_config() -> dict:
    library_result = _mac_clear_library_config()
    if not library_result.get("success"):
        return library_result
    json_result = {"success": True, "message": ""}
    if library_result.get("wasActive") and not library_result.get("remainingEntries"):
        json_result = _mac_clear_json_config()
    plist_result = _mac_clear_plist_config()
    if plist_result.get("success") and json_result.get("success"):
        return {"success": True, "message": i18n.t("registry.macos_cleared")}
    failures = []
    clear_failed = i18n.t("registry.clear_failed_short")
    if not plist_result.get("success"):
        failures.append(f"plist: {plist_result.get('message', clear_failed)}")
    if not json_result.get("success"):
        failures.append(f"json: {json_result.get('message', clear_failed)}")
    return {"success": False, "message": i18n.t("registry.macos_partial_clear_failed", errors="; ".join(failures))}


# ── Unified Entrypoint ──

def is_configured() -> bool:
    """Return True if Claude Desktop is currently configured by this tool."""
    status = get_config_status()
    return status.get("configured", False)


def get_config_status() -> dict:
    """Return the current Claude Desktop configuration status."""
    os_name = _os_name()
    if os_name == "win":
        return _win_get_config_status()
    elif os_name == "mac":
        return _mac_get_config_status()
    return {"configured": False, "keys": {}, "message": i18n.t("registry.win_mac_only")}


def apply_config(
    base_url: str = "http://127.0.0.1:18080",
    gateway_api_key: str = "",
    provider: Optional[dict] = None,
    providers: Optional[list[dict]] = None,
    expose_all: bool = False,
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
    chat_tab_enabled: bool = True,
) -> dict:
    """Apply Claude Desktop 3P configuration."""
    inference_models = serialize_inference_models(provider, providers=providers, expose_all=expose_all)
    os_name = _os_name()
    if os_name == "win":
        return _win_apply_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers, chat_tab_enabled)
    elif os_name == "mac":
        return _mac_apply_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers, chat_tab_enabled)
    return _not_supported()


def clear_config() -> dict:
    """Clear Claude Desktop 3P configuration."""
    os_name = _os_name()
    if os_name == "win":
        return _win_clear_config()
    elif os_name == "mac":
        return _mac_clear_config()
    return _not_supported()
