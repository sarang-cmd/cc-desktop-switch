import copy
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config as cfg
from backend import registry


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(self):
        super().__init__("winreg")
        self.values = {"HKCU": {}, "HKLM": {}}
        self.opens = []
        self.deleted = []

    def OpenKey(self, hive, path, _reserved, access):
        self.opens.append((hive, path, access))
        if path != registry.REGISTRY_PATH or not self.values[hive]:
            raise FileNotFoundError(path)
        return hive

    def EnumValue(self, key, index):
        try:
            name = list(self.values[key])[index]
        except IndexError as exc:
            raise OSError("no more values") from exc
        value = self.values[key][name]
        return name, value, self.REG_SZ if isinstance(value, str) else self.REG_DWORD

    def DeleteValue(self, key, name):
        if name not in self.values[key]:
            raise FileNotFoundError(name)
        del self.values[key][name]
        self.deleted.append((key, name))

    @staticmethod
    def CloseKey(_key):
        return None


class WindowsConfigLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_config_dir = cfg.CONFIG_DIR
        self.old_config_file = cfg.CONFIG_FILE
        self.old_backup_dir = cfg.BACKUP_DIR
        cfg.CONFIG_DIR = str(self.root / "junction-config")
        cfg.CONFIG_FILE = str(self.root / "junction-config" / "config.json")
        cfg.BACKUP_DIR = str(self.root / "junction-config" / "backups")
        cfg.save_config(copy.deepcopy(cfg.DEFAULT_CONFIG))
        self.local_base = self.root / "local"
        self.roaming_base = self.root / "roaming"
        self.local_root = self.local_base / "Claude-3p"
        self.legacy_root = self.roaming_base / "Claude-3p"
        self.reg = FakeWinreg()

    def tearDown(self):
        cfg.CONFIG_DIR = self.old_config_dir
        cfg.CONFIG_FILE = self.old_config_file
        cfg.BACKUP_DIR = self.old_backup_dir
        self.temp.cleanup()

    def windows(self, legacy=False):
        self.local_base.mkdir(exist_ok=True)
        env = {
            "LOCALAPPDATA": str(self.local_base),
            "APPDATA": str(self.roaming_base),
        }
        if legacy:
            self.local_root.mkdir(parents=True, exist_ok=True)
            self.local_root.rmdir()
            self.legacy_root.mkdir(parents=True, exist_ok=True)
        return (
            patch("backend.registry._os_name", return_value="win"),
            patch.dict(os.environ, env, clear=False),
            patch.dict(sys.modules, {"winreg": self.reg}),
        )

    def apply(self, chat_tab_enabled=True):
        os_patch, env_patch, winreg_patch = self.windows()
        with os_patch, env_patch, winreg_patch:
            return registry._win_apply_config(
                "http://127.0.0.1:18080",
                "gateway-secret",
                '[{"name":"sonnet","displayName":"Claude Sonnet"},{"name":"haiku"}]',
                "bearer",
                "[]",
                chat_tab_enabled,
            )

    def test_apply_reapply_migrates_marked_policy_and_preserves_foreign_entry(self):
        self.local_root.mkdir(parents=True)
        library = self.local_root / "configLibrary"
        library.mkdir()
        meta = {
            "appliedId": "router-entry",
            "entries": [
                {"id": "router-entry", "name": "Claude Code Router", "owner": "other-tool"},
                {"id": "unrelated-entry", "name": "Other Tool"},
            ],
            "extraMeta": "preserve",
        }
        (library / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        foreign_entry = {
            "inferenceProvider": "gateway",
            "inferenceGatewayBaseUrl": "https://foreign.example/v1",
            "inferenceGatewayApiKey": "foreign-secret",
            "chatTabEnabled": False,
            "modelDiscoveryEnabled": False,
            "inferenceModels": [{"name": "foreign-model", "labelOverride": "Keep this"}],
            "banner": {"enabled": True, "text": "foreign", "backgroundColor": "#fff", "textColor": "#111"},
            "inferenceCredentialKind": "router-owned",
        }
        foreign_bytes = json.dumps(foreign_entry, sort_keys=True)
        (library / "router-entry.json").write_text(foreign_bytes, encoding="utf-8")

        config_path = self.local_root / "claude_desktop_config.json"
        original_config = {
            "deploymentMode": "1p",
            "preferences": {"sidebarMode": "task", "launchAtLogin": False},
            "customPreference": {"value": 9},
        }
        config_path.write_text(json.dumps(original_config, indent=2), encoding="utf-8")

        self.reg.values["HKCU"] = {
            **{name: value for name, (value, _type) in registry.DESKTOP_CONFIG.items()},
            registry.CCDS_MARKER: "true",
            "organizationOwnedValue": "leave-me",
        }
        first = self.apply(chat_tab_enabled=True)
        self.assertTrue(first["success"], first)

        saved_meta = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
        junction_item = next(item for item in saved_meta["entries"] if item["name"] == "Junction")
        junction_id = junction_item["id"]
        self.assertEqual(saved_meta["appliedId"], junction_id)
        self.assertEqual(saved_meta["extraMeta"], "preserve")
        self.assertEqual(saved_meta["entries"][:2], meta["entries"])
        self.assertEqual(
            json.dumps(json.loads((library / "router-entry.json").read_text(encoding="utf-8")), sort_keys=True),
            foreign_bytes,
        )

        entry = json.loads((library / f"{junction_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["inferenceProvider"], "gateway")
        self.assertEqual(entry["inferenceGatewayBaseUrl"], "http://127.0.0.1:18080")
        self.assertEqual(entry["inferenceGatewayApiKey"], "gateway-secret")
        self.assertTrue(entry["chatTabEnabled"])
        self.assertTrue(entry["modelDiscoveryEnabled"])
        self.assertEqual(entry["inferenceModels"], [
            {"name": "sonnet", "labelOverride": "Claude Sonnet"},
            {"name": "haiku"},
        ])
        for unverified_or_unsupported in (
            "inferenceCredentialKind",
            "isClaudeCodeForDesktopEnabled",
            "coworkEgressAllowedHosts",
        ):
            self.assertNotIn(unverified_or_unsupported, entry)

        updated_config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_config["deploymentMode"], "3p")
        self.assertEqual(updated_config["preferences"], original_config["preferences"])
        self.assertEqual(updated_config["customPreference"], original_config["customPreference"])
        self.assertEqual(json.loads(Path(str(config_path) + ".junction.bak").read_text())["deploymentMode"], "1p")
        self.assertNotIn("inferenceGatewayApiKey", updated_config)
        self.assertEqual(self.reg.values["HKCU"], {"organizationOwnedValue": "leave-me"})
        self.assertNotIn("HKLM", [hive for hive, _name in self.reg.deleted])
        self.assertTrue(first["lockedByOtherPolicy"])
        self.assertIn("organizationOwnedValue", first["message"])

        saved_app_settings = cfg.get_settings()
        self.assertEqual(saved_app_settings[registry.JUNCTION_LIBRARY_ENTRY_SETTING], junction_id)
        self.assertEqual(saved_app_settings[registry.JUNCTION_PREVIOUS_APPLIED_SETTING], "router-entry")

        second = self.apply(chat_tab_enabled=False)
        self.assertTrue(second["success"], second)
        meta_after_reapply = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta_after_reapply["appliedId"], junction_id)
        self.assertEqual(sum(item.get("name") == "Junction" for item in meta_after_reapply["entries"]), 1)
        reapplied_entry = json.loads((library / f"{junction_id}.json").read_text(encoding="utf-8"))
        self.assertFalse(reapplied_entry["chatTabEnabled"])
        self.assertEqual(
            json.loads((library / "router-entry.json").read_text(encoding="utf-8")),
            foreign_entry,
        )

    def test_clear_removes_only_junction_and_restores_previous_applied_id(self):
        self.local_root.mkdir(parents=True)
        library = self.local_root / "configLibrary"
        library.mkdir()
        meta = {"appliedId": "router-entry", "entries": [{"id": "router-entry", "name": "Claude Code Router"}]}
        (library / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (library / "router-entry.json").write_text(json.dumps({"note": "foreign"}), encoding="utf-8")
        config_path = self.local_root / "claude_desktop_config.json"
        config_path.write_text(json.dumps({"deploymentMode": "1p", "preferences": {"keep": True}}), encoding="utf-8")
        self.reg.values["HKCU"] = {
            "inferenceProvider": "gateway",
            "inferenceGatewayApiKey": "old-key",
            registry.CCDS_MARKER: "true",
            "orgValue": "leave-me",
        }
        self.reg.values["HKLM"] = {"ManagedByAdmin": "yes"}

        applied = self.apply()
        self.assertTrue(applied["success"], applied)
        junction_id = cfg.get_settings()[registry.JUNCTION_LIBRARY_ENTRY_SETTING]
        os_patch, env_patch, winreg_patch = self.windows()
        with os_patch, env_patch, winreg_patch:
            result = registry._win_clear_config()

        self.assertTrue(result["success"], result)
        self.assertIn("HKLM policy values require an administrator", result["message"])
        saved_meta = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_meta["entries"], [{"id": "router-entry", "name": "Claude Code Router"}])
        self.assertEqual(saved_meta["appliedId"], "router-entry")
        self.assertFalse((library / f"{junction_id}.json").exists())
        self.assertEqual(json.loads((library / "router-entry.json").read_text()), {"note": "foreign"})
        self.assertEqual(json.loads(config_path.read_text())["deploymentMode"], "3p")
        self.assertEqual(json.loads(config_path.read_text())["preferences"], {"keep": True})
        self.assertEqual(self.reg.values["HKCU"], {"orgValue": "leave-me"})
        self.assertEqual(self.reg.values["HKLM"], {"ManagedByAdmin": "yes"})
        self.assertFalse(cfg.get_settings()[registry.JUNCTION_LIBRARY_ENTRY_SETTING])

    def test_clear_preserves_unmarked_legacy_policy_values_and_reports_exact_keys(self):
        self.local_root.mkdir(parents=True)
        library = self.local_root / "configLibrary"
        library.mkdir()
        (library / "_meta.json").write_text(json.dumps({"entries": []}), encoding="utf-8")
        config_path = self.local_root / "claude_desktop_config.json"
        config_path.write_text(json.dumps({"deploymentMode": "1p", "keep": True}), encoding="utf-8")
        self.reg.values["HKCU"] = {
            "inferenceProvider": "gateway",
            "inferenceGatewayApiKey": "user-or-org-key",
            "organizationPolicy": "managed",
        }

        os_patch, env_patch, winreg_patch = self.windows()
        with os_patch, env_patch, winreg_patch:
            result = registry._win_clear_config()

        self.assertTrue(result["success"], result)
        self.assertEqual(self.reg.values["HKCU"], {
            "inferenceProvider": "gateway",
            "inferenceGatewayApiKey": "user-or-org-key",
            "organizationPolicy": "managed",
        })
        self.assertIn(
            "HKCU\\SOFTWARE\\Policies\\Claude\\inferenceProvider",
            result["message"],
        )
        self.assertIn(
            "HKCU\\SOFTWARE\\Policies\\Claude\\inferenceGatewayApiKey",
            result["message"],
        )
        self.assertIn(
            "HKCU\\SOFTWARE\\Policies\\Claude\\organizationPolicy",
            result["message"],
        )
        self.assertEqual(self.reg.deleted, [])
        self.assertEqual(json.loads(config_path.read_text())["deploymentMode"], "1p")

    def test_uses_legacy_appdata_root_only_when_local_root_is_absent(self):
        self.legacy_root.mkdir(parents=True)
        local_config = self.local_root / "claude_desktop_config.json"
        legacy_config = self.legacy_root / "claude_desktop_config.json"
        legacy_config.write_text(json.dumps({"deploymentMode": "1p", "keep": "legacy"}), encoding="utf-8")

        os_patch, env_patch, winreg_patch = self.windows(legacy=True)
        with os_patch, env_patch, winreg_patch:
            self.assertEqual(registry._config_root_path(), str(self.legacy_root))
            result = registry._win_apply_config("http://127.0.0.1:18080", "secret", "[]")

        self.assertTrue(result["success"], result)
        self.assertFalse(local_config.exists())
        self.assertEqual(json.loads(legacy_config.read_text())["deploymentMode"], "3p")
        self.assertTrue((self.legacy_root / "configLibrary" / "_meta.json").exists())

    def test_status_does_not_report_an_inactive_junction_entry_as_applied(self):
        self.local_root.mkdir(parents=True)
        library = self.local_root / "configLibrary"
        library.mkdir()
        (library / "_meta.json").write_text(json.dumps({
            "appliedId": "router-entry",
            "entries": [
                {"id": "router-entry", "name": "Claude Code Router"},
                {"id": "junction-entry", "name": "Junction"},
            ],
        }), encoding="utf-8")
        (library / "junction-entry.json").write_text(json.dumps({
            "inferenceProvider": "gateway",
            "inferenceGatewayBaseUrl": "https://junction.example",
            "inferenceGatewayApiKey": "junction-secret",
            "inferenceModels": [{"name": "sonnet"}],
        }), encoding="utf-8")
        cfg.update_settings({registry.JUNCTION_LIBRARY_ENTRY_SETTING: "junction-entry"})

        os_patch, env_patch, winreg_patch = self.windows()
        with os_patch, env_patch, winreg_patch:
            status = registry._win_get_config_status()

        self.assertFalse(status["configured"])
        self.assertEqual(status["activeSource"], "configLibrary")
        self.assertEqual(status["keys"]["inferenceGatewayBaseUrl"], "https://junction.example")
        self.assertFalse(self.reg.deleted)

    def test_status_warns_for_unknown_hkcu_and_hklm_values_without_writing(self):
        self.reg.values["HKCU"] = {"organizationManaged": "true"}
        self.reg.values["HKLM"] = {"MachinePolicy": "true"}
        os_patch, env_patch, winreg_patch = self.windows()
        with os_patch, env_patch, winreg_patch:
            status = registry._win_get_config_status()

        self.assertFalse(status["configured"])
        self.assertTrue(status["lockedByOtherPolicy"])
        self.assertIn("HKCU\\SOFTWARE\\Policies\\Claude\\organizationManaged", status["message"])
        self.assertIn("HKLM policy values require an administrator", status["message"])
        self.assertEqual(self.reg.deleted, [])


if __name__ == "__main__":
    unittest.main()
