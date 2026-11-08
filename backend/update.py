"""Auto-update checking and download protocol."""

from __future__ import annotations

import base64
import hashlib
import os
import platform as platform_module
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from backend import i18n


# ── Download Progress (in-memory state polled by frontend) ──
_download_progress: dict[str, Any] = {"active": False, "percent": 0, "message": ""}


def get_download_progress() -> dict[str, Any]:
    """Return the current download progress."""
    return dict(_download_progress)


def set_download_progress(active: bool = False, percent: int = 0, message: str = "") -> None:
    """Update the current download progress."""
    _download_progress.update({"active": active, "percent": percent, "message": message})


class UpdateCheckError(Exception):
    """Update check failure."""


def current_platform(sys_platform: str | None = None, machine: str | None = None) -> str:
    """Return the platform key used in latest.json."""
    raw_platform = sys_platform or sys.platform
    raw_machine = (machine or platform_module.machine() or "").lower()
    if raw_machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif raw_machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = raw_machine or "unknown"

    if raw_platform.startswith("win"):
        return f"windows-{arch}"
    if raw_platform == "darwin":
        return f"macos-{arch}"
    if raw_platform.startswith("linux"):
        return f"linux-{arch}"
    return f"{raw_platform}-{arch}"


def _version_parts(version: str) -> list[int]:
    text = (version or "").strip().lstrip("vV")
    parts = re.findall(r"\d+", text)
    return [int(part) for part in parts] or [0]


def is_newer_version(latest: str, current: str) -> bool:
    """Compare two semantic versions and return True when latest > current."""
    latest_parts = _version_parts(latest)
    current_parts = _version_parts(current)
    width = max(len(latest_parts), len(current_parts))
    latest_parts.extend([0] * (width - len(latest_parts)))
    current_parts.extend([0] * (width - len(current_parts)))
    return latest_parts > current_parts


def _validate_update_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UpdateCheckError(i18n.t("update.invalid_url_scheme"))
    return parsed.geturl()


def _safe_asset_name(name: str) -> str:
    """Keep only the base filename to prevent path traversal from latest.json."""
    filename = Path(str(name or "").strip()).name
    if not filename:
        raise UpdateCheckError(i18n.t("update.missing_filename"))
    return filename


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pick_platform(latest_json: dict[str, Any], platform: str) -> dict[str, Any]:
    platforms = latest_json.get("platforms") or {}
    data = platforms.get(platform)
    if not isinstance(data, dict):
        raise UpdateCheckError(i18n.t("update.no_platform_assets", platform=platform))
    return data


def pick_windows_installer(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the Windows setup installer over portable zip or single-file exe assets."""
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith("windows-setup.exe"):
            return asset
    raise UpdateCheckError(i18n.t("update.no_windows_installer"))


def pick_macos_installer(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the macOS PKG installer, with DMG as a fallback."""
    candidates = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".pkg"):
            return asset
        if name.endswith(".dmg"):
            candidates.append(asset)
    if candidates:
        return candidates[0]
    raise UpdateCheckError(i18n.t("update.no_macos_installer"))


def pick_platform_installer(assets: list[dict[str, Any]], platform: str) -> dict[str, Any]:
    """Select the launchable installer asset for the given platform."""
    if platform.startswith("windows-"):
        return pick_windows_installer(assets)
    if platform.startswith("macos-"):
        return pick_macos_installer(assets)
    raise UpdateCheckError(i18n.t("update.unsupported_platform", platform=platform))


def _allowed_install_extensions(platform: str) -> tuple[str, ...]:
    if platform.startswith("windows-"):
        return (".exe",)
    if platform.startswith("macos-"):
        return (".pkg", ".dmg")
    return ()


def install_command(path: str, platform: str) -> list[str]:
    """Return the command to launch a downloaded installer asset."""
    if platform.startswith("windows-"):
        return [path]
    if platform.startswith("macos-"):
        return ["open", path]
    raise UpdateCheckError(i18n.t("update.unsupported_platform", platform=platform))


def install_after_quit_command(path: str, platform: str, wait_for_pid: int) -> list[str]:
    """Return the helper command that waits for the current process to exit before launching the installer."""
    if wait_for_pid <= 0:
        raise UpdateCheckError(i18n.t("update.invalid_wait_pid"))
    if platform.startswith("windows-"):
        escaped_path = str(path).replace("'", "''")
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$pidToWait = {int(wait_for_pid)}; "
            f"$installer = '{escaped_path}'; "
            "$logDir = Join-Path $env:TEMP 'CC-Desktop-Switch\\updates'; "
            "New-Item -ItemType Directory -Force -Path $logDir | Out-Null; "
            "$log = Join-Path $logDir 'update-helper.log'; "
            "Add-Content -LiteralPath $log -Value (\"{0} helper started pid={1} installer={2}\" -f (Get-Date).ToString('o'), $pidToWait, $installer); "
            "try { "
            "Wait-Process -Id $pidToWait -Timeout 30 -ErrorAction SilentlyContinue; "
            "Add-Content -LiteralPath $log -Value (\"{0} app wait finished\" -f (Get-Date).ToString('o')); "
            "} catch { "
            "Add-Content -LiteralPath $log -Value (\"{0} app wait skipped: {1}\" -f (Get-Date).ToString('o'), $_.Exception.Message); "
            "}; "
            "try { "
            "Start-Process -FilePath $installer -WindowStyle Normal; "
            "Add-Content -LiteralPath $log -Value (\"{0} installer launched\" -f (Get-Date).ToString('o')); "
            "} catch { "
            "Add-Content -LiteralPath $log -Value (\"{0} installer launch failed: {1}\" -f (Get-Date).ToString('o'), $_.Exception.Message); "
            "}"
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded_script,
        ]
    if platform.startswith("macos-"):
        return [
            "/bin/sh",
            "-c",
            'pid="$1"; installer="$2"; '
            'while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done; '
            'exec open "$installer"',
            "ccds-update-installer",
            str(wait_for_pid),
            path,
        ]
    return install_command(path, platform)


async def fetch_latest_json(url: str) -> dict[str, Any]:
    safe_url = _validate_update_url(url)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(safe_url)
            response.raise_for_status()
            # Support UTF-8 BOM written by some release tools while keeping standard JSON parsing.
            data = response.json()
    except httpx.HTTPError as exc:
        raise UpdateCheckError(i18n.t("update.request_failed", error=exc)) from exc
    except ValueError as exc:
        try:
            import json

            data = json.loads(response.content.decode("utf-8-sig"))
        except Exception as sig_exc:
            raise UpdateCheckError(i18n.t("update.invalid_json")) from sig_exc

    if not isinstance(data, dict):
        raise UpdateCheckError(i18n.t("update.invalid_manifest"))
    return data


async def check_update(
    url: str,
    current_version: str,
    platform: str = "windows-x64",
) -> dict[str, Any]:
    latest_json = await fetch_latest_json(url)
    latest_version = str(latest_json.get("version") or "")
    if not latest_version:
        raise UpdateCheckError(i18n.t("update.missing_version"))

    platform_data = _pick_platform(latest_json, platform)
    assets = platform_data.get("assets") or []
    if not isinstance(assets, list):
        raise UpdateCheckError(i18n.t("update.invalid_assets"))

    return {
        "success": True,
        "updateAvailable": is_newer_version(latest_version, current_version),
        "currentVersion": current_version,
        "latestVersion": latest_version,
        "platform": platform,
        "pubDate": latest_json.get("pub_date"),
        "notes": latest_json.get("notes", ""),
        "assets": assets,
        "minimumSupportedVersion": latest_json.get("minimum_supported_version"),
        "updateProtocol": latest_json.get("update_protocol", 1),
    }


async def download_asset(
    asset: dict[str, Any],
    target_dir: str | Path | None = None,
    platform: str = "windows-x64",
) -> dict[str, Any]:
    """Download an installer asset and verify its sha256 against latest.json."""
    url = _validate_update_url(str(asset.get("url") or ""))
    filename = _safe_asset_name(str(asset.get("name") or Path(urlparse(url).path).name))
    allowed_extensions = _allowed_install_extensions(platform)
    if not allowed_extensions:
        raise UpdateCheckError(i18n.t("update.unsupported_platform", platform=platform))
    if not filename.lower().endswith(allowed_extensions):
        allowed = " / ".join(allowed_extensions)
        raise UpdateCheckError(i18n.t("update.only_installer_allowed", allowed=allowed))

    updates_dir = Path(target_dir or Path(tempfile.gettempdir()) / "CC-Desktop-Switch" / "updates")
    updates_dir.mkdir(parents=True, exist_ok=True)
    target = updates_dir / filename
    partial = target.with_name(f"{target.name}.download")

    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                set_download_progress(
                    active=True,
                    percent=0,
                    message=i18n.t("update.download_starting"),
                )
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = min(100, int(downloaded / total_size * 100))
                                set_download_progress(
                                    active=True,
                                    percent=percent,
                                    message=i18n.t("update.downloading_percent", percent=percent),
                                )
                set_download_progress(
                    active=True,
                    percent=100,
                    message=i18n.t("update.verifying"),
                )
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateCheckError(i18n.t("update.download_failed_detail", error=exc)) from exc
    except OSError as exc:
        set_download_progress(active=False, percent=0, message="")
        partial.unlink(missing_ok=True)
        raise UpdateCheckError(i18n.t("update.write_failed_detail", error=exc)) from exc
    finally:
        set_download_progress(active=False, percent=0, message="")

    actual_sha = _file_sha256(partial)
    expected_sha = str(asset.get("sha256") or "").strip().lower()
    if expected_sha and actual_sha.lower() != expected_sha:
        partial.unlink(missing_ok=True)
        raise UpdateCheckError(i18n.t("update.checksum_failed"))

    os.replace(partial, target)
    return {
        "asset": asset,
        "path": str(target),
        "sha256": actual_sha,
        "size": target.stat().st_size,
    }


async def download_update(
    url: str,
    current_version: str,
    platform: str = "windows-x64",
    target_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Check for updates and download the platform installer when a newer release exists."""
    result = await check_update(url, current_version, platform)
    if not result.get("updateAvailable"):
        return {
            **result,
            "downloaded": False,
            "code": "up_to_date",
            "message": i18n.t("update.up_to_date"),
        }

    installer_asset = pick_platform_installer(result.get("assets") or [], platform)
    downloaded = await download_asset(installer_asset, target_dir=target_dir, platform=platform)
    return {
        **result,
        "downloaded": True,
        "installerAsset": installer_asset,
        "installerPath": downloaded["path"],
        "installerSha256": downloaded["sha256"],
        "installerSize": downloaded["size"],
    }
