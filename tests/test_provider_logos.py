import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = ROOT / "frontend/js/api.js"


class ProviderLogoTests(unittest.TestCase):
    def test_mapped_logo_assets_exist_and_are_local(self):
        source = API_SOURCE.read_text(encoding="utf-8")
        logo_paths = re.findall(r"logo:\s*'([^']+)'", source)

        self.assertTrue(logo_paths)
        for relative_path in logo_paths:
            with self.subTest(asset=relative_path):
                asset_path = ROOT / "frontend" / relative_path
                self.assertTrue(asset_path.is_file(), f"Missing provider logo: {asset_path}")
                self.assertNotRegex(relative_path, r"^https?://", "Provider logos must remain bundled locally")
                if asset_path.suffix.lower() == ".svg":
                    ET.parse(asset_path)


if __name__ == "__main__":
    unittest.main()
