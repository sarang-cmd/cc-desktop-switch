"""Windows registry / macOS plist operations for configuring Claude Desktop 3P mode."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Optional

from backend import i18n
from backend.model_alias import all_provider_model_entries, desktop_model_entries

REGISTRY_PATH = r"SOFTWARE\Policies\Claude"
CCDS_MARKER = "ccds_managed"
COWORK_EGRESS_ALLOWED_HOSTS = '["*"]'

# Expected configuration items (name -> (default_value, value_type))
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

def _win_get_key(read_only=False):
    import winreg
    try:
        if read_only:
            return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_READ)
        else:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH)
    except (PermissionError, FileNotFoundError, OSError):
        return None


def _b64_utf8(value: str) -> str:
    """Encode a string as Base64 to avoid PowerShell argument escaping issues."""
    return base64.b64encode(str(value or "").encode("utf-8")).decode("ascii")


def _ps_single_quote(value: str) -> str:
    """Escape a single-quoted PowerShell string."""
    return "'" + str(value).replace("'", "''") + "'"


def _current_user_sid() -> str:
    """Read the current logged-in user SID so elevated writes target the original user hive."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _run_elevated_powershell(script_text: str) -> tuple[bool, str]:
    """Run a temporary PowerShell script with UAC elevation."""
    fd, script_path = tempfile.mkstemp(prefix="ccds-desktop-config-", suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script_text)

        command = (
            "$p = Start-Process -FilePath 'powershell.exe' "
            "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"
            f"{_ps_single_quote(script_path)}) "
            "-WindowStyle Hidden -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired as exc:
        return False, i18n.t("registry.admin_timeout", error=exc)
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def _win_apply_config_elevated(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    """Write the current user's Claude Desktop policy via UAC elevation when direct access fails."""
    sid = _current_user_sid()
    target_path = f"Registry::HKEY_USERS\\{sid}\\{REGISTRY_PATH}" if sid else r"HKCU:\SOFTWARE\Policies\Claude"
    script = f"""
$ErrorActionPreference = 'Stop'
function DecodeUtf8([string]$Value) {{
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}}
$path = DecodeUtf8 '{_b64_utf8(target_path)}'
if (-not (Test-Path -LiteralPath $path)) {{
    New-Item -Path $path -Force | Out-Null
}}
$baseUrl = DecodeUtf8 '{_b64_utf8(base_url)}'
$gatewayApiKey = DecodeUtf8 '{_b64_utf8(gateway_api_key)}'
$inferenceModels = DecodeUtf8 '{_b64_utf8(inference_models or DESKTOP_CONFIG["inferenceModels"][0])}'
$authScheme = DecodeUtf8 '{_b64_utf8(auth_scheme or "bearer")}'
$gatewayHeaders = DecodeUtf8 '{_b64_utf8(gateway_headers or "[]")}'
$coworkEgressAllowedHosts = DecodeUtf8 '{_b64_utf8(COWORK_EGRESS_ALLOWED_HOSTS)}'
New-ItemProperty -LiteralPath $path -Name 'inferenceProvider' -Value 'gateway' -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'inferenceGatewayBaseUrl' -Value $baseUrl -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'inferenceGatewayApiKey' -Value $gatewayApiKey -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'inferenceGatewayAuthScheme' -Value $authScheme -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'inferenceGatewayHeaders' -Value $gatewayHeaders -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'inferenceModels' -Value $inferenceModels -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'isClaudeCodeForDesktopEnabled' -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name 'coworkEgressAllowedHosts' -Value $coworkEgressAllowedHosts -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $path -Name '{CCDS_MARKER}' -Value 'true' -PropertyType String -Force | Out-Null
"""
    ok, output = _run_elevated_powershell(script)
    if ok:
        return {"success": True, "message": i18n.t("registry.admin_applied")}
    detail = output or i18n.t("registry.admin_cancelled")
    return {"success": False, "message": i18n.t("registry.admin_required", detail=detail)}


def _win_get_config_status() -> dict:
    import winreg
    key = _win_get_key(read_only=True)
    if key is None:
        return {"configured": False, "keys": {}, "message": i18n.t("registry.key_missing")}
    result = {"configured": False, "keys": {}, "message": ""}
    try:
        i = 0
        while True:
            name, value, _ = winreg.EnumValue(key, i)
            result["keys"][name] = _safe_config_value(name, value)
            i += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)
    result["configured"] = (
        result["keys"].get("inferenceProvider") == "gateway"
        and result["keys"].get(CCDS_MARKER) == "true"
    )
    return result


def _win_apply_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    key = _win_get_key(read_only=False)
    if key is None:
        return _win_apply_config_elevated(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    import winreg
    try:
        inference_models = inference_models or DESKTOP_CONFIG["inferenceModels"][0]
        values = {
            "inferenceProvider": ("gateway", winreg.REG_SZ),
            "inferenceGatewayBaseUrl": (base_url, winreg.REG_SZ),
            "inferenceGatewayApiKey": (gateway_api_key, winreg.REG_SZ),
            "inferenceGatewayAuthScheme": (auth_scheme or "bearer", winreg.REG_SZ),
            "inferenceGatewayHeaders": (gateway_headers or "[]", winreg.REG_SZ),
            "inferenceModels": (inference_models, winreg.REG_SZ),
            "isClaudeCodeForDesktopEnabled": (1, winreg.REG_DWORD),
            "coworkEgressAllowedHosts": (COWORK_EGRESS_ALLOWED_HOSTS, winreg.REG_SZ),
            CCDS_MARKER: ("true", winreg.REG_SZ),
        }
        for name, (value, type_) in values.items():
            winreg.SetValueEx(key, name, 0, type_, value)
        return {"success": True, "message": i18n.t("registry.applied")}
    except PermissionError:
        return _win_apply_config_elevated(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    except Exception as e:
        return {"success": False, "message": i18n.t("registry.apply_failed", error=str(e))}
    finally:
        winreg.CloseKey(key)


def _win_clear_config() -> dict:
    import winreg
    # Read all existing value names
    key = _win_get_key(read_only=True)
    if key is None:
        return {"success": True, "message": i18n.t("registry.key_missing_no_clear")}
    names = []
    try:
        i = 0
        while True:
            name, _, _ = winreg.EnumValue(key, i)
            names.append(name)
            i += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)

    managed = _managed_policy_names(names)
    if not managed:
        return {"success": True, "message": i18n.t("registry.nothing_to_clear")}

    key = _win_get_key(read_only=False)
    if key is None:
        return {"success": False, "message": i18n.t("registry.open_failed")}
    try:
        for name in managed:
            winreg.DeleteValue(key, name)
        return {"success": True, "message": i18n.t("registry.cleared_count", count=len(managed))}
    except Exception as e:
        return {"success": False, "message": i18n.t("registry.clear_failed", error=str(e))}
    finally:
        winreg.CloseKey(key)


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


def _mac_config_json_path() -> str:
    return os.path.expanduser(MAC_3P_CONFIG)


def _mac_config_library_dir_path() -> str:
    return os.path.join(os.path.dirname(_mac_config_json_path()), MAC_3P_CONFIG_LIBRARY)


def _mac_config_library_meta_path() -> str:
    return os.path.join(_mac_config_library_dir_path(), "_meta.json")


def _mac_config_library_entry_path(entry_id: str) -> str:
    return os.path.join(_mac_config_library_dir_path(), f"{entry_id}.json")


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


def _mac_read_json_config() -> tuple[bool, dict, str]:
    return _mac_read_json_file(_mac_config_json_path())


def _mac_write_json_config(data: dict) -> tuple[bool, str]:
    return _mac_write_json_file(_mac_config_json_path(), data)


def _mac_json_inference_models(inference_models: str) -> list:
    try:
        parsed = json.loads(inference_models or DESKTOP_CONFIG["inferenceModels"][0])
    except (TypeError, ValueError):
        parsed = []
    result = []
    seen = set()
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                model_name = str(item.get("name") or "").strip()
                if not model_name or model_name in seen:
                    continue
                seen.add(model_name)
                cleaned = {"name": model_name}
                display_name = str(item.get("displayName") or "").strip()
                if display_name:
                    cleaned["displayName"] = display_name
                if item.get("supports1m") is True:
                    cleaned["supports1m"] = True
                result.append(cleaned)
            else:
                model_name = str(item or "").strip()
                if not model_name or model_name in seen:
                    continue
                seen.add(model_name)
                result.append(model_name)
    return result or ["sonnet", "haiku", "opus"]


def _mac_json_policy_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


def _mac_json_gateway_headers(gateway_headers: str) -> list[str]:
    try:
        parsed = json.loads(gateway_headers or DESKTOP_CONFIG["inferenceGatewayHeaders"][0])
    except (TypeError, ValueError):
        parsed = []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item or "").strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _mac_json_enterprise_config(
    base_url: str,
    gateway_api_key: str,
    inference_models: str,
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    return {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayApiKey": gateway_api_key,
        "inferenceGatewayAuthScheme": auth_scheme or DESKTOP_CONFIG["inferenceGatewayAuthScheme"][0],
        "inferenceGatewayHeaders": _mac_json_gateway_headers(gateway_headers),
        "inferenceModels": _mac_json_inference_models(inference_models),
        "isClaudeCodeForDesktopEnabled": True,
        "coworkEgressAllowedHosts": _mac_json_policy_list(COWORK_EGRESS_ALLOWED_HOSTS),
    }


def _mac_json_status_keys(enterprise_config: dict) -> dict:
    keys = {}
    for name in DESKTOP_CONFIG:
        if name not in enterprise_config:
            continue
        value = enterprise_config.get(name)
        if name == "inferenceModels" and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if name == "inferenceGatewayHeaders" and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if name == "coworkEgressAllowedHosts" and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if name == "isClaudeCodeForDesktopEnabled" and isinstance(value, bool):
            value = int(value)
        keys[name] = _safe_config_value(name, value)
    return keys


def _mac_flat_config_status_keys(config: dict) -> dict:
    keys = _mac_json_status_keys(config)
    aliases = {
        "provider": "inferenceProvider",
        "apiKey": "inferenceGatewayApiKey",
        "authScheme": "inferenceGatewayAuthScheme",
        "baseUrl": "inferenceGatewayBaseUrl",
        "models": "inferenceModels",
    }
    for source, target in aliases.items():
        if target in keys or source not in config:
            continue
        value = config.get(source)
        if source == "models" and isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        keys[target] = _safe_config_value(target, value)
    return keys


def _mac_get_json_config_status() -> dict:
    path = _mac_config_json_path()
    exists = os.path.exists(path)
    ok, data, message = _mac_read_json_config()
    if not ok:
        return {"configured": False, "keys": {}, "message": message, "exists": exists}
    enterprise_config = data.get("enterpriseConfig")
    if not isinstance(enterprise_config, dict):
        return {"configured": False, "keys": {}, "message": "", "exists": exists}
    keys = _mac_json_status_keys(enterprise_config)
    configured = data.get("deploymentMode") == "3p" and keys.get("inferenceProvider") == "gateway"
    return {"configured": configured, "keys": keys, "message": "", "exists": exists}


def _mac_config_library_entry_paths(include_missing_active: bool = False) -> tuple[bool, list[str], str]:
    library_dir = _mac_config_library_dir_path()
    meta_path = _mac_config_library_meta_path()
    ok, meta, message = _mac_read_json_file(meta_path)
    if not ok:
        return False, [], message

    paths = []
    applied_id = str(meta.get("appliedId") or "").strip()
    if applied_id and "/" not in applied_id and "\\" not in applied_id:
        active_path = _mac_config_library_entry_path(applied_id)
        if include_missing_active or os.path.exists(active_path):
            paths.append(active_path)

    if not paths and os.path.isdir(library_dir):
        for name in sorted(os.listdir(library_dir)):
            if not name.endswith(".json") or name == "_meta.json":
                continue
            paths.append(os.path.join(library_dir, name))

    return True, paths, ""


def _mac_get_library_config_status() -> dict:
    ok, paths, message = _mac_config_library_entry_paths()
    if not ok:
        return {"configured": False, "keys": {}, "message": message, "exists": False}
    if not paths:
        return {"configured": False, "keys": {}, "message": "", "exists": os.path.isdir(_mac_config_library_dir_path())}

    for path in paths:
        ok, data, message = _mac_read_json_file(path)
        if not ok:
            return {"configured": False, "keys": {}, "message": message, "exists": True}
        keys = _mac_flat_config_status_keys(data)
        if keys:
            return {
                "configured": keys.get("inferenceProvider") == "gateway",
                "keys": keys,
                "message": "",
                "exists": True,
            }
    return {"configured": False, "keys": {}, "message": "", "exists": True}


def _mac_apply_library_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    ok, paths, message = _mac_config_library_entry_paths(include_missing_active=True)
    if not ok:
        return {"success": False, "message": i18n.t("registry.config_lib_read_failed", error=message)}
    if not paths:
        library_dir = _mac_config_library_dir_path()
        os.makedirs(library_dir, exist_ok=True)
        entry_id = str(uuid.uuid4())
        meta = {"appliedId": entry_id}
        ok, message = _mac_write_json_file(_mac_config_library_meta_path(), meta)
        if not ok:
            return {"success": False, "message": i18n.t("registry.config_lib_write_failed", error=message)}
        paths = [_mac_config_library_entry_path(entry_id)]

    expected = _mac_json_enterprise_config(
        base_url,
        gateway_api_key,
        inference_models or DESKTOP_CONFIG["inferenceModels"][0],
        auth_scheme,
        gateway_headers,
    )
    failures = []
    for path in paths:
        ok, data, message = _mac_read_json_file(path)
        if not ok:
            failures.append(f"{os.path.basename(path)}: read failed: {message}")
            continue
        data.update(expected)
        ok, message = _mac_write_json_file(path, data)
        if not ok:
            failures.append(f"{os.path.basename(path)}: write failed: {message}")
            continue

        ok, saved, message = _mac_read_json_file(path)
        if not ok:
            failures.append(f"{os.path.basename(path)}: readback failed: {message}")
            continue
        for name, value in expected.items():
            if saved.get(name) != value:
                failures.append(f"{os.path.basename(path)}: {name}: readback mismatch")

    if failures:
        return {"success": False, "message": i18n.t("registry.config_lib_verify_failed", errors="; ".join(failures))}
    return {"success": True, "message": i18n.t("registry.config_lib_applied")}


def _mac_apply_json_config(
    base_url: str,
    gateway_api_key: str = "",
    inference_models: str = "",
    auth_scheme: str = "bearer",
    gateway_headers: str = "",
) -> dict:
    ok, data, message = _mac_read_json_config()
    if not ok:
        return {"success": False, "message": i18n.t("registry.json_read_failed", error=message)}

    expected = _mac_json_enterprise_config(
        base_url,
        gateway_api_key,
        inference_models or DESKTOP_CONFIG["inferenceModels"][0],
        auth_scheme,
        gateway_headers,
    )
    enterprise_config = data.get("enterpriseConfig")
    if not isinstance(enterprise_config, dict):
        enterprise_config = {}
    enterprise_config.update(expected)
    data["deploymentMode"] = "3p"
    data["enterpriseConfig"] = enterprise_config

    ok, message = _mac_write_json_config(data)
    if not ok:
        return {"success": False, "message": i18n.t("registry.json_write_failed", error=message)}

    ok, saved, message = _mac_read_json_config()
    if not ok:
        return {"success": False, "message": i18n.t("registry.json_readback_failed", error=message)}
    saved_enterprise = saved.get("enterpriseConfig")
    if not isinstance(saved_enterprise, dict) or saved.get("deploymentMode") != "3p":
        return {"success": False, "message": i18n.t("registry.json_verify_mode_failed")}
    failures = []
    for name, value in expected.items():
        if saved_enterprise.get(name) != value:
            failures.append(f"{name}: readback mismatch")
    if failures:
        return {"success": False, "message": i18n.t("registry.json_verify_failed", errors="; ".join(failures))}
    return {"success": True, "message": i18n.t("registry.json_applied")}


def _mac_get_config_status() -> dict:
    plist_status = _mac_get_plist_config_status()
    json_status = _mac_get_json_config_status()
    library_status = _mac_get_library_config_status()
    library_has_runtime_config = bool(library_status.get("keys"))
    json_has_runtime_config = bool(json_status.get("keys"))

    if library_has_runtime_config:
        keys = dict(library_status.get("keys") or {})
        configured = library_status.get("configured", False)
        active_source = "configLibrary"
        key_sources = {name: "configLibrary" for name in keys}
    else:
        keys = dict(plist_status.get("keys") or {})
        key_sources = {name: "plist" for name in keys}
        for name, value in (json_status.get("keys") or {}).items():
            keys[name] = value
            key_sources[name] = "json"
        configured = json_status.get("configured", False) if json_has_runtime_config else plist_status.get("configured", False)
        active_source = "json" if json_has_runtime_config else ("plist" if keys else "")

    return {
        "configured": configured,
        "keys": keys,
        "message": library_status.get("message") or json_status.get("message") or plist_status.get("message", ""),
        "activeSource": active_source,
        "keySources": key_sources,
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
) -> dict:
    plist_result = _mac_apply_plist_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    json_result = _mac_apply_json_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    library_result = _mac_apply_library_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    if plist_result.get("success") and json_result.get("success") and library_result.get("success"):
        return {"success": True, "message": i18n.t("registry.macos_applied")}

    failures = []
    write_failed = i18n.t("registry.write_failed_short")
    if not plist_result.get("success"):
        failures.append(f"plist: {plist_result.get('message', write_failed)}")
    if not json_result.get("success"):
        failures.append(f"json: {json_result.get('message', write_failed)}")
    if not library_result.get("success"):
        failures.append(f"configLibrary: {library_result.get('message', write_failed)}")
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
    ok, data, message = _mac_read_json_config()
    if not ok:
        return {"success": False, "message": i18n.t("registry.json_read_failed", error=message)}
    if not data:
        return {"success": True, "message": i18n.t("registry.json_missing_no_clear")}

    changed = False
    if "enterpriseConfig" in data:
        data.pop("enterpriseConfig", None)
        changed = True
    if data.get("deploymentMode") != "clear":
        data["deploymentMode"] = "clear"
        changed = True
    if not changed:
        return {"success": True, "message": i18n.t("registry.json_no_clear")}

    ok, message = _mac_write_json_config(data)
    if not ok:
        return {"success": False, "message": i18n.t("registry.json_write_failed", error=message)}
    return {"success": True, "message": i18n.t("registry.json_cleared")}


def _mac_clear_library_config() -> dict:
    ok, paths, message = _mac_config_library_entry_paths()
    if not ok:
        return {"success": False, "message": i18n.t("registry.config_lib_read_failed", error=message)}
    if not paths:
        return {"success": True, "message": i18n.t("registry.config_lib_missing_no_clear")}

    managed = set(DESKTOP_CONFIG.keys()) | {
        "provider",
        "apiKey",
        "authScheme",
        "baseUrl",
        "models",
    }
    failures = []
    for path in paths:
        ok, data, message = _mac_read_json_file(path)
        if not ok:
            failures.append(f"{os.path.basename(path)}: read failed: {message}")
            continue
        changed = False
        for name in managed:
            if name in data:
                data.pop(name, None)
                changed = True
        if not changed:
            continue
        ok, message = _mac_write_json_file(path, data)
        if not ok:
            failures.append(f"{os.path.basename(path)}: write failed: {message}")

    if failures:
        return {"success": False, "message": i18n.t("registry.config_lib_clear_failed", errors="; ".join(failures))}
    return {"success": True, "message": i18n.t("registry.config_lib_cleared")}


def _mac_clear_config() -> dict:
    plist_result = _mac_clear_plist_config()
    json_result = _mac_clear_json_config()
    library_result = _mac_clear_library_config()
    if plist_result.get("success") and json_result.get("success") and library_result.get("success"):
        return {"success": True, "message": i18n.t("registry.macos_cleared")}
    failures = []
    clear_failed = i18n.t("registry.clear_failed_short")
    if not plist_result.get("success"):
        failures.append(f"plist: {plist_result.get('message', clear_failed)}")
    if not json_result.get("success"):
        failures.append(f"json: {json_result.get('message', clear_failed)}")
    if not library_result.get("success"):
        failures.append(f"configLibrary: {library_result.get('message', clear_failed)}")
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
) -> dict:
    """Apply Claude Desktop 3P configuration."""
    inference_models = serialize_inference_models(provider, providers=providers, expose_all=expose_all)
    os_name = _os_name()
    if os_name == "win":
        return _win_apply_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    elif os_name == "mac":
        return _mac_apply_config(base_url, gateway_api_key, inference_models, auth_scheme, gateway_headers)
    return _not_supported()


def clear_config() -> dict:
    """Clear Claude Desktop 3P configuration."""
    os_name = _os_name()
    if os_name == "win":
        return _win_clear_config()
    elif os_name == "mac":
        return _mac_clear_config()
    return _not_supported()
