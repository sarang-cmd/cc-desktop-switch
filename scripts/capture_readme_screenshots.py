#!/usr/bin/env python3
"""Capture repeatable README screenshots from the real app using demo-only data.

Requires the optional ``playwright`` Python package and its Chromium browser.
This helper uses an isolated temporary CCDS_CONFIG_DIR and never calls a provider.
"""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TEMP_CONFIG = tempfile.TemporaryDirectory(prefix="ccds-readme-screenshots-")
os.environ["CCDS_CONFIG_DIR"] = TEMP_CONFIG.name

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from backend import config as cfg  # noqa: E402
from backend.main import create_admin_app, get_admin_token  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "screenshots"
PORT = int(os.environ.get("CCDS_SCREENSHOT_PORT", "18791"))
BASE_URL = f"http://127.0.0.1:{PORT}"


def seed_demo_config() -> None:
    presets = {preset["id"]: preset for preset in cfg.get_presets()}
    deepseek = copy.deepcopy(presets["deepseek"])
    deepseek.update({
        "id": "screenshot-deepseek",
        "name": "DeepSeek",
        "apiKey": "demo-key-not-valid",
        "sortIndex": 0,
    })
    openrouter = copy.deepcopy(presets["openrouter"])
    openrouter.update({
        "id": "screenshot-openrouter",
        "name": "OpenRouter",
        "apiKey": "demo-key-not-valid",
        "sortIndex": 1,
    })
    config = copy.deepcopy(cfg.DEFAULT_CONFIG)
    config["providers"] = [deepseek, openrouter]
    config["activeProvider"] = deepseek["id"]
    config["gatewayApiKey"] = "screenshot-gateway-key-not-valid"
    config["settings"].update({"language": "en", "theme": "default", "autoStart": False})
    cfg.save_config(config)


def wait_for_server() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/ready", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("The local app server did not become ready.")


def assert_no_horizontal_overflow(page, label: str) -> None:
    metrics = page.evaluate("""() => ({
      viewport: document.documentElement.clientWidth,
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      mainClient: document.querySelector('.app-main')?.clientWidth || 0,
      mainScroll: document.querySelector('.app-main')?.scrollWidth || 0
    })""")
    if metrics["document"] > metrics["viewport"] + 1 or metrics["body"] > metrics["viewport"] + 1:
        raise AssertionError(f"{label} has horizontal overflow: {metrics}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    seed_demo_config()

    server = uvicorn.Server(uvicorn.Config(
        create_admin_app(),
        host="127.0.0.1",
        port=PORT,
        log_level="error",
        access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        wait_for_server()
        token = get_admin_token()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1600, "height": 1280}, device_scale_factor=1)
            page.goto(f"{BASE_URL}/#token={token}", wait_until="networkidle")
            page.locator("#dashboardProviderCards .provider-switch-card[data-provider-id]").first.wait_for()
            page.evaluate("document.fonts.ready")
            assert_no_horizontal_overflow(page, "desktop dashboard")
            page.screenshot(path=str(OUTPUT_DIR / "dashboard.png"), full_page=False)

            page.locator('a[href="#providers/add"]').first.click()
            page.locator("#presetList [data-preset='openrouter']").wait_for()
            page.locator("#presetList [data-preset='openrouter']").click()
            page.locator("#providerMappingStack .patchboard-row").first.wait_for()
            page.locator("#providerName").wait_for()
            page.evaluate("document.fonts.ready")
            page.evaluate("document.querySelector('.app-main').scrollTop = 0")
            assert_no_horizontal_overflow(page, "desktop provider editor")
            page.screenshot(path=str(OUTPUT_DIR / "provider-routing.png"), full_page=False)

            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.dispatchEvent(new Event('resize'))")
            page.wait_for_timeout(150)
            page.evaluate("""() => {
              const main = document.querySelector('.app-main');
              const patchboard = document.querySelector('.routing-patchboard');
              main.scrollTop = Math.max(0, patchboard.offsetTop - 10);
            }""")
            assert_no_horizontal_overflow(page, "mobile provider editor")
            page.screenshot(path=str(OUTPUT_DIR / "provider-routing-mobile.png"), full_page=False)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        TEMP_CONFIG.cleanup()

    for path in sorted(OUTPUT_DIR.glob("*.png")):
        print(f"Captured {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SCREENSHOT_ERROR={type(exc).__name__}: {exc}", flush=True)
        raise
