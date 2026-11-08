"""Read-only diagnostics for Claude Desktop's Windows 3P configuration."""

import json
import os
import re
from pathlib import Path

POLICY_PATH = r"SOFTWARE\Policies\Claude"
_REDACTED = "<redacted>"
_SENSITIVE_NAME = re.compile(
    r"(?:api.?key|access.?token|token|secret|password|authorization|credential|headers)",
    re.IGNORECASE,
)
_SECRET_PATTERN = re.compile(
    r"(?i)(?:Bearer\s+\S+|sk-ant-[a-z0-9_-]{8,}|sk-[a-z0-9_-]{16,})"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^:/@\s]+:[^@/\s]+@")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret)=)[^&#\s]+")


def _redact(value, name=""):
    """Recursively redact credential-like fields and recognizable secret strings."""
    if _SENSITIVE_NAME.search(str(name)):
        return _REDACTED if value not in (None, "", [], {}) else ""
    if isinstance(value, dict):
        return {str(key): _redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, name) for item in value]
    if isinstance(value, str):
        value = _URL_CREDENTIALS.sub(r"\1<redacted>@", value)
        value = _QUERY_SECRET.sub(r"\1<redacted>", value)
        return _SECRET_PATTERN.sub(_REDACTED, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _read_json_snapshot(path: Path) -> dict:
    snapshot = {"path": str(path), "exists": path.is_file()}
    if not snapshot["exists"]:
        return snapshot
    try:
        with path.open("r", encoding="utf-8") as handle:
            content = json.load(handle)
        snapshot["contents"] = _redact(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        snapshot["readError"] = str(exc)
        snapshot["contents"] = "<not valid readable JSON; contents omitted>"
    return snapshot


def _registry_hive_snapshot(winreg, hive_name: str, hive) -> dict:
    snapshot = {"key": f"{hive_name}\\{POLICY_PATH}", "exists": False, "values": []}
    try:
        key = winreg.OpenKey(hive, POLICY_PATH, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return snapshot
    except OSError as exc:
        snapshot["readError"] = str(exc)
        return snapshot

    snapshot["exists"] = True
    try:
        index = 0
        while True:
            name, value, value_type = winreg.EnumValue(key, index)
            safe_value = _redact(value, name)
            snapshot["values"].append(
                {
                    "name": name,
                    "type": value_type,
                    "value": safe_value,
                }
            )
            index += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)
    return snapshot


def _config_root_snapshot(env_name: str) -> dict:
    base = os.environ.get(env_name, "").strip()
    root = Path(base) / "Claude-3p" if base else None
    result = {
        "environmentVariable": env_name,
        "basePath": base or None,
        "path": str(root) if root else None,
        "exists": bool(root and root.is_dir()),
    }
    if not root:
        result["note"] = f"{env_name} is not set"
        return result

    result["claudeDesktopConfig"] = _read_json_snapshot(root / "claude_desktop_config.json")
    library_dir = root / "configLibrary"
    library = {
        "path": str(library_dir),
        "exists": library_dir.is_dir(),
        "meta": _read_json_snapshot(library_dir / "_meta.json"),
        "entries": [],
    }
    if library_dir.is_dir():
        for entry_path in sorted(library_dir.glob("*.json")):
            if entry_path.name == "_meta.json":
                continue
            library["entries"].append(_read_json_snapshot(entry_path))
    result["configLibrary"] = library
    return result


def collect_windows_claude_diagnostics() -> dict:
    """Return redacted Windows policy and Claude-3p file diagnostics without modifying anything."""
    import sys

    report = {
        "format": "junction.windows-claude-diagnostics.v1",
        "readOnly": True,
        "platform": sys.platform,
        "registry": [],
        "configRoots": {
            "localAppData": _config_root_snapshot("LOCALAPPDATA"),
            "legacyAppData": _config_root_snapshot("APPDATA"),
        },
    }
    if sys.platform != "win32":
        report["registryNote"] = "Windows registry diagnostics are available only on Windows."
        return report

    try:
        import winreg
    except ImportError as exc:
        report["registryNote"] = f"Could not import winreg: {exc}"
        return report

    report["registry"] = [
        _registry_hive_snapshot(winreg, "HKCU", winreg.HKEY_CURRENT_USER),
        _registry_hive_snapshot(winreg, "HKLM", winreg.HKEY_LOCAL_MACHINE),
    ]
    return report
