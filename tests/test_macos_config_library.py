import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config as cfg
from backend import registry


class MacosConfigLibraryTests(unittest.TestCase):
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

    def tearDown(self):
        cfg.CONFIG_DIR = self.old_config_dir
        cfg.CONFIG_FILE = self.old_config_file
        cfg.BACKUP_DIR = self.old_backup_dir
        self.temp.cleanup()

    def test_apply_adds_junction_entry_and_preserves_foreign_applied_entry(self):
        json_path = self.root / "Claude-3p" / "claude_desktop_config.json"
        library = json_path.parent / "configLibrary"
        library.mkdir(parents=True)
        foreign_id = "router-entry"
        foreign = {
            "inferenceProvider": "gateway",
            "inferenceGatewayBaseUrl": "https://router.example/v1",
            "inferenceGatewayApiKey": "router-secret",
            "chatTabEnabled": False,
            "modelDiscoveryEnabled": False,
            "inferenceModels": [{"name": "router-model", "labelOverride": "Router"}],
            "banner": {"enabled": True, "text": "Router banner"},
        }
        meta = {
            "appliedId": foreign_id,
            "entries": [{"id": foreign_id, "name": "Claude Code Router"}],
            "custom": "keep",
        }
        (library / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        foreign_path = library / f"{foreign_id}.json"
        foreign_path.write_text(json.dumps(foreign), encoding="utf-8")
        original_config = {
            "deploymentMode": "1p",
            "preferences": {"sidebarMode": "task"},
            "otherSetting": {"enabled": True},
        }
        json_path.write_text(json.dumps(original_config, indent=2), encoding="utf-8")

        with patch.object(registry, "MAC_3P_CONFIG", str(json_path)):
            with patch("backend.registry._mac_apply_plist_config", return_value={"success": True}):
                result = registry._mac_apply_config(
                    "http://127.0.0.1:18080",
                    gateway_api_key="junction-secret",
                    inference_models='[{"name":"model-a","displayName":"Model A"},{"name":"model-b"}]',
                    auth_scheme="x-api-key",
                    gateway_headers='["x-api-key: junction-secret"]',
                    chat_tab_enabled=True,
                )

        self.assertTrue(result["success"], result)
        saved_meta = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_meta["custom"], "keep")
        self.assertEqual(saved_meta["entries"][0], meta["entries"][0])
        self.assertEqual(saved_meta["appliedId"], cfg.get_settings()[registry.JUNCTION_LIBRARY_ENTRY_SETTING])
        junction_meta = next(item for item in saved_meta["entries"] if item["name"] == "Junction")
        self.assertEqual(junction_meta["id"], saved_meta["appliedId"])
        junction = json.loads((library / f"{junction_meta['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(junction["inferenceProvider"], "gateway")
        self.assertEqual(junction["inferenceGatewayBaseUrl"], "http://127.0.0.1:18080")
        self.assertEqual(junction["inferenceGatewayApiKey"], "junction-secret")
        self.assertTrue(junction["chatTabEnabled"])
        self.assertEqual(junction["inferenceModels"], [
            {"name": "model-a", "labelOverride": "Model A"},
            {"name": "model-b"},
        ])
        self.assertNotIn("inferenceGatewayAuthScheme", junction)
        self.assertNotIn("inferenceCredentialKind", junction)
        self.assertEqual(json.loads(foreign_path.read_text(encoding="utf-8")), foreign)
        self.assertEqual(saved_meta["appliedId"], junction_meta["id"])

        saved_config = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_config["deploymentMode"], "3p")
        self.assertEqual(saved_config["preferences"], original_config["preferences"])
        self.assertEqual(saved_config["otherSetting"], original_config["otherSetting"])
        self.assertEqual(saved_config.keys(), original_config.keys())
        backup = json.loads(Path(str(json_path) + ".junction.bak").read_text(encoding="utf-8"))
        self.assertEqual(backup, original_config)

    def test_clear_removes_only_junction_entry_and_preserves_foreign_selection(self):
        json_path = self.root / "Claude-3p" / "claude_desktop_config.json"
        library = json_path.parent / "configLibrary"
        library.mkdir(parents=True)
        foreign_id = "router-entry"
        junction_id = "junction-entry"
        entries = [
            {"id": foreign_id, "name": "Claude Code Router"},
            {"id": junction_id, "name": "Junction"},
        ]
        (library / "_meta.json").write_text(
            json.dumps({"appliedId": junction_id, "entries": entries}), encoding="utf-8"
        )
        (library / f"{foreign_id}.json").write_text(json.dumps({"note": "leave untouched"}), encoding="utf-8")
        (library / f"{junction_id}.json").write_text(json.dumps({"inferenceProvider": "gateway"}), encoding="utf-8")
        json_path.write_text(json.dumps({"deploymentMode": "3p", "preferences": {"keep": True}}), encoding="utf-8")
        cfg.update_settings({
            registry.JUNCTION_LIBRARY_ENTRY_SETTING: junction_id,
            registry.JUNCTION_PREVIOUS_APPLIED_SETTING: foreign_id,
        })

        with patch.object(registry, "MAC_3P_CONFIG", str(json_path)):
            with patch("backend.registry._mac_clear_plist_config", return_value={"success": True}):
                result = registry._mac_clear_config()

        self.assertTrue(result["success"], result)
        saved_meta = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_meta["entries"], [{"id": foreign_id, "name": "Claude Code Router"}])
        self.assertEqual(saved_meta["appliedId"], foreign_id)
        self.assertEqual(json.loads((library / f"{foreign_id}.json").read_text()), {"note": "leave untouched"})
        self.assertFalse((library / f"{junction_id}.json").exists())
        saved_config = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_config["deploymentMode"], "3p")
        self.assertEqual(saved_config["preferences"], {"keep": True})


if __name__ == "__main__":
    unittest.main()
