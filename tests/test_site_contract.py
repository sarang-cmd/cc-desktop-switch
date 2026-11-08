import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
REPOSITORY = "sarang-cmd/cc-desktop-switch"


class SiteContractTests(unittest.TestCase):
    def test_homepage_exposes_functional_downloads_and_project_links(self):
        html = (SITE / "index.html").read_text(encoding="utf-8")
        script = (SITE / "app.js").read_text(encoding="utf-8")
        for section in ("features", "how-it-works", "downloads"):
            self.assertIn(f'id="{section}"', html)
        self.assertIn(f'const repo = "{REPOSITORY}";', script)
        self.assertIn("https://api.github.com/repos/${repo}/releases/latest", script)
        self.assertIn("https://github.com/${repo}/releases/latest", script)
        self.assertIn("browser_download_url", script)
        self.assertIn('data-asset="windows-installer"', html)
        self.assertIn('data-asset="windows-portable"', html)
        self.assertIn('data-asset="mac-arm-pkg"', html)
        self.assertIn('data-asset="mac-intel-pkg"', html)

    def test_legal_pages_are_linked_and_marked_as_drafts(self):
        homepage = (SITE / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="terms.html"', homepage)
        self.assertIn('href="privacy.html"', homepage)
        for page in ("terms.html", "privacy.html"):
            html = (SITE / page).read_text(encoding="utf-8")
            self.assertIn("Draft status", html)
            self.assertTrue(
                "not a finalized" in html.lower() or "not a binding agreement" in html.lower()
            )

    def test_accessibility_motion_and_responsive_styles_are_present(self):
        html = (SITE / "index.html").read_text(encoding="utf-8")
        css = (SITE / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="skip-link"', html)
        self.assertIn('aria-label="Main navigation"', html)
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertNotIn("Inter", css)
        for page in ("index.html", "terms.html", "privacy.html"):
            page_html = (SITE / page).read_text(encoding="utf-8")
            self.assertIn('aria-controls="primary-nav"', page_html)
            self.assertIn('id="primary-nav"', page_html)
        self.assertIn(".menu-toggle", (SITE / "nav.js").read_text(encoding="utf-8"))

    def test_real_screenshot_is_shipped_with_the_static_site(self):
        source = (ROOT / "docs/screenshots/provider-routing.png").read_bytes()
        shipped = (SITE / "assets/provider-routing.png").read_bytes()
        self.assertEqual(source, shipped)
        self.assertIn('alt="Junction\'s model-routing patchboard', (SITE / "index.html").read_text(encoding="utf-8"))

    def test_pages_workflow_deploys_the_static_site_directory(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("actions/upload-pages-artifact@v3", workflow)
        self.assertRegex(workflow, re.compile(r"path:\s*site\b"))
        self.assertIn("actions/deploy-pages@v4", workflow)


if __name__ == "__main__":
    unittest.main()
