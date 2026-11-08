#!/usr/bin/env python3
"""CC Desktop Switch - Application entrypoint."""

import argparse
import ctypes
import json
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request as UrlRequest, urlopen

import uvicorn

from backend import config as cfg
from backend import i18n
from backend import registry
from backend.main import (
    _start_proxy_server,
    _stop_proxy_server,
    create_admin_app,
    desktop_config_target_for_provider,
    get_admin_token,
    register_app_activation_handler,
    register_update_quit_handler,
)


APP_NAME = "CC Desktop Switch"
APP_VERSION = "1.0.26"
TRAY_OPEN_LABEL = i18n.get_text("tray.open", "en")
TRAY_QUIT_LABEL = i18n.get_text("tray.quit", "en")
_macos_app_delegate = None
_macos_status_item = None
_macos_status_delegate = None
_single_instance_mutex = None
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND = 0x00010000
ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX_NAME = "Local\\CCDesktopSwitch.SingleInstance"


def safe_print(message: str):
    """Print safely without failing when running as a windowed executable with no console."""
    stream = getattr(sys, "stdout", None)
    if not stream:
        return
    try:
        print(message)
    except OSError:
        return


def show_message_box(title: str, message: str) -> bool:
    """Display a native message box on Windows; return False when unavailable."""
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            title,
            MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND,
        )
        return True
    except Exception as exc:
        safe_print(f"message box failed: {exc}")
        return False


def show_message_box_async(title: str, message: str):
    """Display a message box on a background thread so tray menu callbacks do not block."""
    threading.Thread(
        target=show_message_box,
        args=(title, message),
        daemon=True,
    ).start()


def acquire_single_instance_lock() -> bool:
    """Create a single-instance mutex on Windows; return False if an instance is already running."""
    global _single_instance_mutex
    if sys.platform != "win32":
        return True

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            safe_print("single instance mutex creation returned empty handle")
            return True
        already_running = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        if already_running:
            kernel32.CloseHandle(handle)
            return False
        _single_instance_mutex = handle
        return True
    except Exception as exc:
        safe_print(f"single instance mutex failed: {exc}")
        return True


def release_single_instance_lock():
    """Release the single-instance mutex handle."""
    global _single_instance_mutex
    if sys.platform != "win32" or not _single_instance_mutex:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(_single_instance_mutex)
    except Exception as exc:
        safe_print(f"single instance mutex release failed: {exc}")
    finally:
        _single_instance_mutex = None


def request_existing_instance_activate(admin_port: int, timeout: float = 3.0) -> bool:
    """Notify an existing running instance to show its window; return True if handled."""
    activate_url = f"http://127.0.0.1:{admin_port}/api/app/activate"
    deadline = time.time() + max(timeout, 0.0)

    while True:
        try:
            request = UrlRequest(
                activate_url,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "X-CCDS-Request": "1",
                },
                method="POST",
            )
            with urlopen(request, timeout=0.8) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                raw_body = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw_body or "{}")
                if 200 <= status < 300 and payload.get("success") and payload.get("handled"):
                    return True
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            pass

        if time.time() >= deadline:
            return False
        time.sleep(min(0.2, max(0.0, deadline - time.time())))


def write_crash_log():
    """Write crash tracebacks to a local log file when running as a windowed executable."""
    try:
        cfg.ensure_config_dir()
        log_path = Path(cfg.CONFIG_DIR) / "ccds-crash.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        return


def parse_args():
    """Parse CLI arguments. Defaults to the native desktop window, with browser mode as fallback."""
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open the system browser instead of the desktop window.",
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="Start the local admin server without opening any UI.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the admin server port.",
    )
    return parser.parse_args()


def wait_for_admin(url: str, timeout: float = 12.0) -> bool:
    """Wait until the admin server is reachable before opening the UI window."""
    deadline = time.time() + timeout
    base_url = url.split("#", 1)[0].rstrip("/")
    status_url = f"{base_url}/api/ready"

    while time.time() < deadline:
        try:
            with urlopen(status_url, timeout=0.6) as response:
                if response.status < 500:
                    return True
        except (OSError, URLError):
            time.sleep(0.2)

    return False


def admin_ui_url(admin_port: int) -> str:
    """Build the frontend entrypoint URL containing the local admin token."""
    return f"http://127.0.0.1:{admin_port}/#token={get_admin_token()}"


def admin_public_url(admin_port: int) -> str:
    """Build the token-free display URL for the admin server."""
    return f"http://127.0.0.1:{admin_port}"


def build_admin_server(admin_app, port: int) -> uvicorn.Server:
    """Create the admin uvicorn server controlled by the desktop window lifecycle."""
    server_config = uvicorn.Config(
        admin_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    return uvicorn.Server(server_config)


def start_admin_server(admin_app, port: int):
    """Start the admin server on a daemon thread for WebView or browser access."""
    server = build_admin_server(admin_app, port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def open_browser_when_ready(url: str):
    if wait_for_admin(url):
        webbrowser.open(url)


def _macos_should_quit_from_close_event() -> bool:
    """Best-effort distinction between closing the window and quitting the app."""
    if sys.platform != "darwin":
        return False

    try:
        import AppKit
    except Exception:
        return False

    try:
        event = AppKit.NSApp.currentEvent()
    except Exception:
        return False
    if event is None:
        return True

    try:
        event_window = event.window()
    except Exception:
        event_window = None
    if event_window is None:
        return True

    try:
        if event.type() == AppKit.NSKeyDown:
            chars = str(event.charactersIgnoringModifiers() or "").lower()
            return chars == "q"
    except Exception:
        return False

    return False


def _macos_hide_dock_icon(AppKit) -> bool:
    """Keep the macOS app in accessory activation policy so it stays out of the Dock."""
    try:
        policy = getattr(AppKit, "NSApplicationActivationPolicyAccessory")
        AppKit.NSApp.setActivationPolicy_(policy)
        return True
    except Exception as exc:
        safe_print(f"macOS dock hide unavailable: {exc}")
        return False


def _macos_status_bar_image(AppKit, Foundation):
    """Use a macOS template symbol image so the menu bar icon adapts to light and dark mode."""
    try:
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "arrow.triangle.2.circlepath",
            APP_NAME,
        )
        if image is None:
            return None
        image.setTemplate_(True)
        image.setSize_(Foundation.NSMakeSize(18, 18))
        return image
    except Exception as exc:
        safe_print(f"macOS status item template image unavailable: {exc}")
        return None


def _install_macos_reopen_handler(window, controller):
    """Install Cocoa hooks for reopen and menu-bar status item behavior."""
    if sys.platform != "darwin":
        return

    try:
        window.events.shown.wait(10)
        import AppKit
        import Foundation
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
    except Exception as exc:
        safe_print(f"macOS reopen handler unavailable: {exc}")
        return

    class CCDesktopSwitchAppDelegate(AppKit.NSObject):
        def applicationShouldTerminate_(self, app):
            should_close = True
            try:
                for instance in list(BrowserView.instances.values()):
                    should_close = should_close and BrowserView.should_close(instance.pywebview_window)
            except Exception:
                return Foundation.YES
            return Foundation.YES if should_close else Foundation.NO

        def applicationSupportsSecureRestorableState_(self, app):
            return Foundation.YES

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible_windows):
            if controller.window_hidden or not bool(has_visible_windows):
                controller.show_window()
            return Foundation.YES

    class CCDesktopSwitchStatusDelegate(AppKit.NSObject):
        def showApp_(self, sender):
            controller.show_window()

        def quitApp_(self, sender):
            controller.quit_app()

    delegate = CCDesktopSwitchAppDelegate.alloc().init().retain()
    status_delegate = CCDesktopSwitchStatusDelegate.alloc().init().retain()

    def set_delegate():
        global _macos_app_delegate, _macos_status_item, _macos_status_delegate
        _macos_app_delegate = delegate
        _macos_status_delegate = status_delegate
        AppKit.NSApplication.sharedApplication().setDelegate_(delegate)
        _macos_hide_dock_icon(AppKit)
        try:
            status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength
            )
            button = status_item.button()
            if button is not None:
                image = _macos_status_bar_image(AppKit, Foundation)
                if image is not None:
                    button.setImage_(image)
                else:
                    button.setTitle_("CCDS")
                button.setToolTip_(APP_NAME)

            menu = AppKit.NSMenu.alloc().init()
            show_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Show CC Desktop Switch",
                "showApp:",
                "",
            )
            show_item.setTarget_(status_delegate)
            menu.addItem_(show_item)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit CC Desktop Switch",
                "quitApp:",
                "",
            )
            quit_item.setTarget_(status_delegate)
            menu.addItem_(quit_item)
            status_item.setMenu_(menu)
            _macos_status_item = status_item
        except Exception as exc:
            safe_print(f"macOS status item unavailable: {exc}")

    AppHelper.callAfter(set_delegate)


class DesktopTrayController:
    """System tray controller: hide window on close and quit explicitly from the tray menu."""

    def __init__(self, window, icon_path: Path):
        self.window = window
        self.icon_path = Path(icon_path)
        self.icon = None
        self.thread = None
        self.pystray = None
        self.exit_requested = False
        self._notified = False
        self.window_hidden = False

    def start(self) -> bool:
        """Start the system tray icon; return False when dependencies are unavailable."""
        if sys.platform == "darwin":
            safe_print("system tray disabled on macOS: AppKit must run on the main thread")
            return False

        try:
            import pystray
            from PIL import Image
        except Exception as exc:
            safe_print(f"system tray unavailable: {exc}")
            return False

        try:
            self.pystray = pystray
            image = Image.open(self.icon_path)
            self.icon = pystray.Icon(APP_NAME, image, APP_NAME, self.build_menu())
            self.thread = threading.Thread(target=self.icon.run, daemon=True)
            self.thread.start()
            return True
        except Exception as exc:
            safe_print(f"system tray failed: {exc}")
            return False

    def build_menu(self):
        """Build the tray menu including quick provider switching items."""
        if not self.pystray:
            return None
        items = [
            self.pystray.MenuItem(i18n.t("tray.open"), self.show_window, default=True),
            self.pystray.Menu.SEPARATOR,
        ]
        items.extend(self.provider_menu_items())
        items.extend([
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem(i18n.t("tray.quit"), self.quit_app),
        ])
        return self.pystray.Menu(*items)

    def provider_menu_items(self):
        """Return provider switch menu items."""
        if not self.pystray:
            return []
        config = cfg.load_config()
        providers = config.get("providers", [])
        if not providers:
            return [self.pystray.MenuItem(i18n.t("tray.no_providers"), None, enabled=False)]

        items = [self.pystray.MenuItem(i18n.t("tray.switch_provider"), None, enabled=False)]
        for provider in providers:
            provider_id = provider.get("id")
            name = provider.get("name", "Unnamed Provider")
            items.append(self.pystray.MenuItem(
                name,
                self._make_provider_switcher(provider_id),
                checked=lambda item, pid=provider_id: cfg.load_config().get("activeProvider") == pid,
            ))
        return items

    def _make_provider_switcher(self, provider_id: str):
        def switch(icon=None, item=None):
            self.switch_provider(provider_id)
        return switch

    def switch_provider(self, provider_id: str) -> bool:
        """Switch the active default provider from the system tray menu."""
        if not provider_id:
            return False
        if not cfg.set_active_provider(provider_id):
            return False
        provider = cfg.get_provider(provider_id)
        desktop_message = ""
        try:
            if provider:
                settings = cfg.get_settings()
                target = desktop_config_target_for_provider(provider, settings)
                result = registry.apply_config(
                    target["baseUrl"],
                    gateway_api_key=target["apiKey"],
                    provider=target["provider"],
                    providers=target["providers"],
                    expose_all=target["exposeAll"],
                    auth_scheme=target["authScheme"],
                    gateway_headers=target["gatewayHeaders"],
                )
                if target.get("requiresProxy"):
                    _start_proxy_server(settings.get("proxyPort", 18080), restart=True)
                desktop_message = (
                    i18n.t("tray.desktop_synced_suffix")
                    if result.get("success")
                    else i18n.t("tray.desktop_reapply_suffix")
                )
        except Exception as exc:
            safe_print(f"sync desktop config failed: {exc}")
            desktop_message = i18n.t("tray.desktop_reapply_suffix")
        self.refresh_menu()
        try:
            if self.icon and provider:
                self.icon.notify(
                    i18n.t(
                        "tray.switched_notify",
                        name=provider.get("name", provider_id),
                        suffix=desktop_message,
                    ),
                    APP_NAME,
                )
        except Exception:
            pass
        return True

    def show_desktop_restart_dialog(self, provider: dict, desktop_synced: bool = False):
        """Show a restart reminder after switching providers from the tray."""
        provider_name = provider.get("name") or i18n.t("tray.current_provider")
        sync_line = (
            i18n.t("tray.restart_synced_detail")
            if desktop_synced
            else i18n.t("tray.restart_pending_detail")
        )
        message = i18n.t("tray.restart_body", name=provider_name, detail=sync_line)
        show_message_box_async(i18n.t("tray.restart_title"), message)

    def refresh_menu(self):
        """Refresh the tray menu after provider changes."""
        if not self.icon or not self.pystray:
            return
        try:
            self.icon.menu = self.build_menu()
            if hasattr(self.icon, "update_menu"):
                self.icon.update_menu()
        except Exception as exc:
            safe_print(f"refresh tray menu failed: {exc}")

    def handle_window_closing(self):
        """Handle pywebview closing event; return False to cancel closing and hide instead."""
        if self.exit_requested or _macos_should_quit_from_close_event():
            return None

        self.hide_window()
        self.notify_hidden()
        return False

    def hide_window(self):
        self.window.hide()
        self.window_hidden = True

    def show_window(self, icon=None, item=None):
        try:
            self.window.show()
            self.window.restore()
            if sys.platform == "darwin":
                try:
                    import AppKit
                    _macos_hide_dock_icon(AppKit)
                    AppKit.NSApp.activateIgnoringOtherApps_(True)
                except Exception:
                    pass
            self.window_hidden = False
        except Exception as exc:
            safe_print(f"show window failed: {exc}")

    def notify_hidden(self):
        if self._notified or not self.icon:
            return
        self._notified = True
        try:
            self.icon.notify(
                i18n.t("tray.minimized_notify"),
                APP_NAME,
            )
        except Exception:
            return

    def quit_app(self, icon=None, item=None):
        self.exit_requested = True
        try:
            self.window.destroy()
        except Exception as exc:
            safe_print(f"quit failed: {exc}")

    def stop(self):
        if not self.icon:
            return
        try:
            self.icon.stop()
        except Exception:
            return


def open_desktop_window(url: str) -> bool:
    """Open the native desktop window; return False on failure so caller can fall back to browser mode."""
    try:
        import webview
    except Exception as exc:
        safe_print(f"pywebview unavailable, fallback to browser: {exc}")
        return False

    try:
        window = webview.create_window(
            APP_NAME,
            url,
            width=1240,
            height=820,
            min_size=(980, 680),
            text_select=True,
        )
        tray = DesktopTrayController(
            window,
            Path(__file__).resolve().parent / "frontend" / "assets" / "app-icon.png",
        )

        def request_quit_for_update():
            if sys.platform == "darwin":
                try:
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(tray.quit_app)
                    return
                except Exception:
                    pass
            tray.quit_app()

        def request_window_activation() -> bool:
            if sys.platform == "darwin":
                try:
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(tray.show_window)
                    return True
                except Exception:
                    pass
            tray.show_window()
            return True

        tray_started = tray.start()
        if tray_started or sys.platform == "darwin":
            window.events.closing += tray.handle_window_closing
        if tray_started:
            window.events.closed += tray.stop

        menu = []
        if sys.platform == "darwin":
            from webview.menu import Menu, MenuAction, MenuSeparator

            menu = [
                Menu("Window", [
                    MenuAction("Show CC Desktop Switch", tray.show_window),
                    MenuSeparator(),
                    MenuAction("Quit CC Desktop Switch", tray.quit_app),
                ]),
            ]

        register_update_quit_handler(request_quit_for_update)
        register_app_activation_handler(request_window_activation)
        try:
            if sys.platform == "darwin":
                webview.start(
                    func=_install_macos_reopen_handler,
                    args=(window, tray),
                    debug=False,
                    menu=menu,
                )
            else:
                webview.start(debug=False, menu=menu)
        finally:
            register_update_quit_handler(None)
            register_app_activation_handler(None)
        return True
    except Exception as exc:
        safe_print(f"desktop window failed, fallback to browser: {exc}")
        return False


def run_browser_mode(admin_app, admin_port: int, open_ui: bool = True):
    url = admin_ui_url(admin_port)
    public_url = admin_public_url(admin_port)
    if open_ui:
        threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()

    panel_label = i18n.t("startup.admin_panel")
    stop_hint = i18n.t("startup.press_ctrl_c")
    safe_print(f"""
╔══════════════════════════════════════════╗
║       {APP_NAME} v{APP_VERSION}          ║
║                                          ║
║  {panel_label}: {public_url}
║                                          ║
║  {stop_hint}
╚══════════════════════════════════════════╝
    """)

    uvicorn.run(
        admin_app,
        host="127.0.0.1",
        port=admin_port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )


def run_desktop_mode(admin_app, admin_port: int):
    base_url = f"http://127.0.0.1:{admin_port}"
    url = admin_ui_url(admin_port)
    server, server_thread = start_admin_server(admin_app, admin_port)

    try:
        if not wait_for_admin(base_url):
            safe_print(f"admin server is not ready, fallback to browser: {base_url}")
            webbrowser.open(url)
            while server_thread.is_alive() and not server.should_exit:
                time.sleep(0.5)
            return

        if not open_desktop_window(url):
            webbrowser.open(url)
            while server_thread.is_alive() and not server.should_exit:
                time.sleep(0.5)
    finally:
        server.should_exit = True
        _stop_proxy_server()


def main():
    args = parse_args()

    cfg.ensure_config_dir()

    settings = cfg.get_settings()
    admin_port = args.port or settings.get("adminPort", 18081)
    proxy_port = settings.get("proxyPort", 18080)
    auto_start_proxy = settings.get("autoStart", False)

    if not acquire_single_instance_lock():
        safe_print(i18n.t("single_instance.activating"))
        if request_existing_instance_activate(admin_port):
            safe_print(i18n.t("single_instance.activated"))
        else:
            safe_print(i18n.t("single_instance.activate_failed"))
            show_message_box(
                APP_NAME,
                i18n.t("single_instance.already_running_box"),
            )
        return

    try:
        if auto_start_proxy:
            safe_print(i18n.t("startup.auto_proxy", port=proxy_port))
            _start_proxy_server(proxy_port)

        admin_app = create_admin_app()

        if args.server_only:
            run_browser_mode(admin_app, admin_port, open_ui=False)
        elif args.browser:
            run_browser_mode(admin_app, admin_port, open_ui=True)
        else:
            run_desktop_mode(admin_app, admin_port)
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        write_crash_log()
        raise
