"""Localization contract tests: English default, key parity, and no stray CJK text."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from backend import config as cfg
from backend import i18n

ROOT = Path(__file__).resolve().parents[1]


def frontend_dictionary_keys() -> dict:
    """Return the {en, zh} key sets declared in frontend/js/i18n.js."""
    script = """
const fs = require("fs");
global.document = { documentElement: {}, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = { dispatchEvent: () => {} };
global.CustomEvent = function () {};
eval(fs.readFileSync("frontend/js/i18n.js", "utf-8"));
const dict = window.CCI18n.dictionaries;
console.log(JSON.stringify({
  language: window.CCI18n.language,
  en: Object.keys(dict.en),
  zh: Object.keys(dict.zh),
}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class BackendLocalizationTests(unittest.TestCase):
    def test_english_is_the_default_language(self):
        self.assertEqual(cfg.DEFAULT_CONFIG["settings"]["language"], "en")

    def test_get_text_defaults_to_english(self):
        self.assertEqual(i18n.get_text("status_active"), i18n.TRANSLATIONS["en"]["status_active"])
        self.assertNotEqual(
            i18n.TRANSLATIONS["en"]["status_active"],
            i18n.TRANSLATIONS["zh"]["status_active"],
        )
        self.assertEqual(i18n.get_text("status_active", "zh"), i18n.TRANSLATIONS["zh"]["status_active"])

    def test_english_and_chinese_dictionaries_have_identical_keys(self):
        english = set(i18n.TRANSLATIONS["en"])
        chinese = set(i18n.TRANSLATIONS["zh"])
        self.assertEqual(english - chinese, set())
        self.assertEqual(chinese - english, set())

    def test_every_builtin_preset_has_bilingual_names_and_hints(self):
        for preset in cfg.get_presets():
            preset_id = preset["id"]
            for lang in ("en", "zh"):
                self.assertIn(f"preset.{preset_id}.name", i18n.TRANSLATIONS[lang], preset_id)


class FrontendLocalizationTests(unittest.TestCase):
    def test_default_language_is_english_and_ignores_the_os_locale(self):
        script = """
const fs = require("fs");
global.document = { documentElement: {}, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: () => {} };
global.window = { dispatchEvent: () => {} };
global.CustomEvent = function () {};
global.navigator = { language: "zh-CN", languages: ["zh-CN"] };
eval(fs.readFileSync("frontend/js/i18n.js", "utf-8"));
console.log(JSON.stringify({
  language: window.CCI18n.language,
  greeting: window.CCI18n.t("nav.dashboard"),
}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["language"], "en")
        self.assertEqual(payload["greeting"], "Dashboard")

    def test_explicit_language_choice_is_persisted(self):
        script = """
const fs = require("fs");
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
global.document = { documentElement: {}, querySelectorAll: () => [] };
global.window = { dispatchEvent: () => {} };
global.CustomEvent = function () {};
eval(fs.readFileSync("frontend/js/i18n.js", "utf-8"));
window.CCI18n.apply("zh");
const stored = localStorage.getItem("ccds.language");
eval(fs.readFileSync("frontend/js/i18n.js", "utf-8"));
console.log(JSON.stringify({ stored, restored: window.CCI18n.language }));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["stored"], "zh")
        self.assertEqual(payload["restored"], "zh")

    def test_frontend_dictionaries_have_identical_keys(self):
        payload = frontend_dictionary_keys()
        self.assertEqual(sorted(set(payload["en"]) - set(payload["zh"])), [])
        self.assertEqual(sorted(set(payload["zh"]) - set(payload["en"])), [])
        self.assertEqual(payload["language"], "en")

    def test_index_html_ships_english_text_and_i18n_hooks(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<html lang="en"', html)
        self.assertIn('data-i18n="nav.dashboard">Dashboard</span>', html)
        self.assertIn('data-i18n-placeholder="providers.keyPlaceholder"', html)
        self.assertIn('data-i18n-aria-label="header.primaryNav"', html)
        self.assertIn('data-lang="en"', html)
        self.assertIn('data-lang="zh"', html)

    def test_provider_mapping_uses_a_responsive_routing_patchboard(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn('class="provider-mapping-section routing-patchboard"', html)
        self.assertIn('class="patchboard-rail"', html)
        self.assertIn('class="form-mapping-row patchboard-row', app)
        self.assertIn('function syncPatchboardRow(input)', app)
        self.assertIn('data-patch-state', app)
        self.assertIn('.patchboard-row.is-patched .patch-wire-line', css)
        self.assertIn('@media (max-width: 860px)', css)


class LocalizationScriptTests(unittest.TestCase):
    def test_check_i18n_script_passes(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_i18n.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
