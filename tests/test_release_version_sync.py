import unittest
from pathlib import Path

import main as app_main
from backend import config as cfg
from backend.main import create_admin_app
from backend.proxy import create_proxy_app


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.0.29"
EXPECTED_REPOSITORY = "sarang-cmd/junction"


class ReleaseVersionSyncTests(unittest.TestCase):
    def test_runtime_and_packaging_versions_are_synchronized(self):
        self.assertEqual(app_main.APP_VERSION, EXPECTED_VERSION)
        self.assertEqual(cfg.DEFAULT_CONFIG["version"], EXPECTED_VERSION)
        self.assertEqual(create_admin_app().version, EXPECTED_VERSION)
        self.assertEqual(create_proxy_app().version, EXPECTED_VERSION)

        checks = {
            "frontend/index.html": f"<strong>{EXPECTED_VERSION}</strong>",
            "installer.nsi": f'!define PRODUCT_VERSION "{EXPECTED_VERSION}"',
            "scripts/New-Release.ps1": f'[string]$Version = "{EXPECTED_VERSION}"',
            "start.bat": f"Junction v{EXPECTED_VERSION}",
            "build.bat": f"Junction v{EXPECTED_VERSION} - Build Tool",
            "docs/CHANGELOG.md": f"## v{EXPECTED_VERSION}",
            f"docs/release-notes-v{EXPECTED_VERSION}.md": f"# Junction v{EXPECTED_VERSION}",
        }
        for relative_path, expected in checks.items():
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(expected, text, relative_path)

    def test_update_checker_and_public_links_use_current_repository(self):
        self.assertEqual(
            cfg.DEFAULT_UPDATE_URL,
            f"https://github.com/{EXPECTED_REPOSITORY}/releases/latest/download/latest.json",
        )
        for relative_path in ("frontend/index.html", "site/index.html", "site/app.js"):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(EXPECTED_REPOSITORY, text, relative_path)
            self.assertNotIn("lonr-6/cc-desktop-switch", text, relative_path)

    def test_legacy_project_update_urls_migrate_but_custom_feed_is_preserved(self):
        old_urls = (
            "https://github.com/lonr-6/cc-desktop-switch/releases/latest/download/latest.json",
            "https://github.com/sarang-cmd/cc-desktop-switch/releases/latest/download/latest.json",
        )
        for old_url in old_urls:
            migrated = cfg.normalize_config({"settings": {"updateUrl": old_url}})
            self.assertEqual(migrated["settings"]["updateUrl"], cfg.DEFAULT_UPDATE_URL)

        custom_url = "https://updates.example.test/junction/latest.json"
        custom = cfg.normalize_config({"settings": {"updateUrl": custom_url}})
        self.assertEqual(custom["settings"]["updateUrl"], custom_url)

    def test_release_packaging_and_site_downloads_use_junction_assets(self):
        release_script = (ROOT / "scripts" / "New-Release.ps1").read_text(encoding="utf-8")
        manifest_script = (ROOT / "scripts" / "New-ReleaseManifest.ps1").read_text(encoding="utf-8")
        site_script = (ROOT / "site" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"Junction-v$Version-Windows-Setup.exe"', release_script)
        self.assertIn('"Junction-v$Version-Windows-Portable.zip"', release_script)
        self.assertIn('"Junction-v$Version-macOS-$Arch.$extension"', release_script)
        self.assertIn('"Junction-v$Version-Windows-Setup.exe"', manifest_script)
        self.assertIn('"windows-installer": /windows.*(?:setup|installer).*\\.exe$/i', site_script)
        self.assertIn('"mac-arm-pkg": /macos-arm64\\.pkg$/i', site_script)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("published v1.0.28 release", readme)
        self.assertIn('[string]$Version = "1.0.29"', release_script)

    def test_installer_syntax_workflow_uses_the_rebranded_payload_directory(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        installer = (ROOT / "installer.nsi").read_text(encoding="utf-8")
        self.assertIn("mkdir -p dist/Junction", workflow)
        self.assertIn('File /r "dist\\Junction\\*.*"', installer)

    def test_upgrade_compatibility_identifiers_remain_stable(self):
        config = (ROOT / "backend/config.py").read_text(encoding="utf-8")
        installer = (ROOT / "installer.nsi").read_text(encoding="utf-8")
        self.assertIn("~/.cc-desktop-switch", config)
        self.assertIn('"cc-desktop-switch.config"', config)
        self.assertIn("LEGACY_UNINST_KEY", installer)
        self.assertIn('"CC-Desktop-Switch.exe"', installer)


if __name__ == "__main__":
    unittest.main()
