import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.windows_claude_diagnostics import collect_windows_claude_diagnostics


class ReadOnlyDiagnosticsTests(unittest.TestCase):
    def test_collects_policy_value_names_and_redacts_secrets(self):
        values = {
            "HKCU": [
                ("inferenceProvider", "gateway", 1),
                ("inferenceGatewayApiKey", "example-private-key", 1),
                ("organizationMode", "managed", 1),
            ],
            "HKLM": [("inferenceModels", "[]", 1)],
        }

        class FakeWinreg:
            HKEY_CURRENT_USER = "HKCU"
            HKEY_LOCAL_MACHINE = "HKLM"
            KEY_READ = 1

            @staticmethod
            def OpenKey(hive, path, _reserved, _access):
                if hive not in values:
                    raise FileNotFoundError
                return hive

            @staticmethod
            def EnumValue(key, index):
                try:
                    return values[key][index]
                except IndexError as exc:
                    raise OSError from exc

            @staticmethod
            def CloseKey(_key):
                return None

        with patch("sys.platform", "win32"):
            with patch.dict("sys.modules", {"winreg": FakeWinreg}):
                report = collect_windows_claude_diagnostics()

        self.assertTrue(report["readOnly"])
        self.assertEqual([item["name"] for item in report["registry"][0]["values"]], [
            "inferenceProvider", "inferenceGatewayApiKey", "organizationMode",
        ])
        self.assertEqual(report["registry"][0]["values"][1]["value"], "<redacted>")
        self.assertEqual(report["registry"][1]["values"][0]["name"], "inferenceModels")
        self.assertFalse(hasattr(FakeWinreg, "SetValueEx"))

    def test_reads_local_and_legacy_config_files_with_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            local = base / "local"
            legacy = base / "roaming"
            for root in (local / "Claude-3p", legacy / "Claude-3p"):
                library = root / "configLibrary"
                library.mkdir(parents=True)
                (root / "claude_desktop_config.json").write_text(
                    json.dumps({
                        "deploymentMode": "3p",
                        "enterpriseConfig": {
                            "inferenceProvider": "gateway",
                            "inferenceGatewayApiKey": "do-not-print",
                        },
                    }),
                    encoding="utf-8",
                )
                (library / "_meta.json").write_text(
                    json.dumps({"appliedId": "active-entry"}), encoding="utf-8"
                )
                (library / "active-entry.json").write_text(
                    json.dumps({
                        "inferenceProvider": "gateway",
                        "inferenceGatewayApiKey": "also-do-not-print",
                    }),
                    encoding="utf-8",
                )

            env = {"LOCALAPPDATA": str(local), "APPDATA": str(legacy)}
            with patch.dict(os.environ, env, clear=True):
                with patch("sys.platform", "linux"):
                    report = collect_windows_claude_diagnostics()

        local_snapshot = report["configRoots"]["localAppData"]
        legacy_snapshot = report["configRoots"]["legacyAppData"]
        self.assertTrue(local_snapshot["exists"])
        self.assertTrue(legacy_snapshot["exists"])
        for snapshot in (local_snapshot, legacy_snapshot):
            config = snapshot["claudeDesktopConfig"]["contents"]
            entry = snapshot["configLibrary"]["entries"][0]["contents"]
            self.assertEqual(config["enterpriseConfig"]["inferenceGatewayApiKey"], "<redacted>")
            self.assertEqual(entry["inferenceGatewayApiKey"], "<redacted>")
            self.assertEqual(snapshot["configLibrary"]["meta"]["contents"]["appliedId"], "active-entry")

    def test_redacts_secrets_in_arbitrary_text_values(self):
        with patch("sys.platform", "linux"):
            with patch.dict(os.environ, {}, clear=True):
                report = collect_windows_claude_diagnostics()

        self.assertTrue(report["readOnly"])
        self.assertIn("LOCALAPPDATA", report["configRoots"]["localAppData"]["note"])


if __name__ == "__main__":
    unittest.main()
