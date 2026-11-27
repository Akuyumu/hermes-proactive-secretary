"""Built-in identifier guard coverage for the release export scan."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_export  # noqa: E402


def _synthetic_tree(*, email: str = "", zone: str = "", phone: str = "",
                    home: str = "") -> Path:
    root = Path(tempfile.mkdtemp(prefix="release-guard-"))
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "plugin.yaml").write_text(
        f"version: {release_export.VERSION}\n", encoding="utf-8")
    pkg = root / "src" / "proactive_pilot"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'__version__ = "{release_export.VERSION}"\n', encoding="utf-8")
    lines = ["SAMPLE = 1"]
    if email:
        lines.append(f'CONTACT = "{email}"')
    if zone:
        lines.append(f'ZONE = "{zone}"')
    if phone:
        lines.append(f'PHONE = "{phone}"')
    if home:
        lines.append(f'PATH = "{home}"')
    (pkg / "sample.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


class ReleaseIdentifierGuardTests(unittest.TestCase):
    def setUp(self):
        self._roots: list[Path] = []

    def tearDown(self):
        import shutil
        for root in self._roots:
            shutil.rmtree(root, ignore_errors=True)

    def _scan(self, **kwargs) -> dict:
        root = _synthetic_tree(**kwargs)
        self._roots.append(root)
        return release_export.scan(root)

    def test_literal_email_and_regional_timezone_are_rejected(self):
        report = self._scan(email="person@corp-mail.example",
                            zone="Asia/Singapore")
        self.assertFalse(report["ok"])
        blob = "\n".join(report["problems"])
        self.assertIn("src/proactive_pilot/sample.py:2: [email]", blob)
        self.assertIn("src/proactive_pilot/sample.py:3: [timezone]", blob)
        self.assertIn("Asia/Singapore", blob)

    def test_phone_and_home_path_are_rejected(self):
        report = self._scan(phone="+14155552671", home="/home/realuser")
        self.assertFalse(report["ok"])
        blob = "\n".join(report["problems"])
        self.assertIn("[phone]", blob)
        self.assertIn("[home_path]", blob)

    def test_allowed_placeholders_are_accepted(self):
        report = self._scan(email="noreply@example.org")
        self.assertTrue(report["ok"], report["problems"])

    def test_shipped_tree_passes_the_built_in_guard(self):
        report = release_export.scan(ROOT)
        self.assertTrue(report["ok"], report["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
