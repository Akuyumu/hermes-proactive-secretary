"""Explicit legacy-state import: preservation, refusal, idempotency, dry-run.

Every legacy tree is synthetic and lives under the workspace; no live/private
state is read. The new contract paths are the plugin's own ``paths`` values.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state_import  # noqa: E402


def _load_plugin_package():
    name = "hermes_proactive_portal_state_import"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
paths = importlib.import_module(f"{_PKG.__name__}.paths")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(self.root / "data")
        os.environ["HERMES_HOME"] = str(self.root / "home")
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_packaged_defaults_without_top_level_plugin_path(self):
        """Hermes loads a package; root is not necessarily on sys.path."""
        module = importlib.import_module(f"{_PKG.__name__}.state_import")
        saved = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path if p != str(ROOT)]
            targets = module._default_targets()
        finally:
            sys.path[:] = saved
        self.assertEqual(targets[0], paths.state_dir())
        self.assertEqual(targets[3], paths.data_root())

    def _legacy(self) -> Path:
        legacy = self.root / "legacy"
        (legacy / "var" / "registries" / "nested").mkdir(parents=True, exist_ok=True)
        (legacy / "policy").mkdir(parents=True, exist_ok=True)
        (legacy / "var" / "reminders.json").write_text(json.dumps({
            "schema": 1,
            "reminders": {"r1": {"status": "pending", "text": "Ship it",
                                 "due": "2026-10-05T20:00:00+08:00"}},
        }), encoding="utf-8")
        (legacy / "var" / "registries" / "adaptive.json").write_text(
            json.dumps({"registries": {"public": {"v": 3}}}), encoding="utf-8")
        (legacy / "var" / "registries" / "nested" / "deep.json").write_text(
            json.dumps({"deep": True}), encoding="utf-8")
        (legacy / "policy" / "policy.json").write_text(
            json.dumps({"persona": "Legacy", "daily_normal_cap": 7}), encoding="utf-8")
        (legacy / "policy" / "preferences.json").write_text(json.dumps({
            "schema": 1, "explicit": [{"topic": "x"}], "inferred": [], "snoozes": [],
            "paused": {"active": True, "provenance": "legacy"},
        }), encoding="utf-8")
        return legacy

    def _with_transient(self, legacy: Path) -> None:
        (legacy / "var" / "run.lock").write_text("lock", encoding="utf-8")
        (legacy / "var" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (legacy / "var" / "__pycache__" / "x.pyc").write_text("bytecode", encoding="utf-8")
        (legacy / "var" / "shadow").mkdir(parents=True, exist_ok=True)
        (legacy / "var" / "shadow" / "shadow_state.json").write_text("{}", encoding="utf-8")
        (legacy / "README.txt").write_text("not part of the contract", encoding="utf-8")


class PreservationTests(ImportTestCase):
    def test_nested_state_registries_reminders_preferences_policy_preserved(self):
        legacy = self._legacy()
        manifest = state_import.import_legacy(legacy)
        self.assertTrue(manifest["ok"])
        expected = [
            ("var/reminders.json", paths.state_dir() / "reminders.json"),
            ("var/registries/adaptive.json", paths.state_dir() / "registries" / "adaptive.json"),
            ("var/registries/nested/deep.json",
             paths.state_dir() / "registries" / "nested" / "deep.json"),
            ("policy/policy.json", paths.policy_path()),
            ("policy/preferences.json", paths.preferences_path()),
        ]
        for rel, target in expected:
            self.assertTrue(target.is_file(), rel)
            self.assertEqual(target.read_bytes(), (legacy / rel).read_bytes(), rel)
        # Reminders are copied verbatim, never rebuilt.
        reg = json.loads((paths.state_dir() / "reminders.json").read_text(encoding="utf-8"))
        self.assertEqual(reg["reminders"]["r1"]["status"], "pending")
        self.assertEqual(reg["reminders"]["r1"]["text"], "Ship it")
        prefs = json.loads(paths.preferences_path().read_text(encoding="utf-8"))
        self.assertTrue(prefs["paused"]["active"])

    def test_private_permissions(self):
        legacy = self._legacy()
        state_import.import_legacy(legacy)
        self.assertEqual(_mode(paths.data_root()), 0o700)
        self.assertEqual(_mode(paths.state_dir()), 0o700)
        self.assertEqual(_mode(paths.state_dir() / "registries"), 0o700)
        self.assertEqual(_mode(paths.state_dir() / "registries" / "nested"), 0o700)
        for target in (paths.state_dir() / "reminders.json", paths.policy_path(),
                       paths.preferences_path()):
            self.assertEqual(_mode(target), 0o600, target)

    def test_manifest_has_hashes_and_counts(self):
        legacy = self._legacy()
        manifest = state_import.import_legacy(legacy)
        self.assertEqual(manifest["counts"]["copied"], 5)
        self.assertEqual(manifest["counts"]["conflicts"], 0)
        self.assertTrue(manifest["manifest_sha256"])
        for entry in manifest["files"]:
            self.assertTrue(entry["sha256"])

    def test_excluded_filenames_reported_not_contents(self):
        legacy = self._legacy()
        self._with_transient(legacy)
        manifest = state_import.preview(legacy)
        excluded = set(manifest["excluded"])
        self.assertIn("var/run.lock", excluded)
        self.assertIn("var/__pycache__/x.pyc", excluded)
        self.assertIn("var/shadow/shadow_state.json", excluded)
        self.assertIn("README.txt", excluded)
        # Only names/reasons, never file contents.
        self.assertNotIn("lock", manifest["excluded"])
        state_import.import_legacy(legacy)
        self.assertFalse((paths.state_dir() / "run.lock").exists())
        self.assertFalse((paths.state_dir() / "shadow").exists())
        self.assertFalse((paths.state_dir() / "__pycache__").exists())


class RefusalTests(ImportTestCase):
    def test_symlink_in_legacy_tree_is_rejected(self):
        legacy = self._legacy()
        os.symlink(legacy / "var" / "reminders.json", legacy / "var" / "linked.json")
        with self.assertRaises(state_import.LegacyTreeError):
            state_import.import_legacy(legacy)
        self.assertFalse((paths.state_dir() / "reminders.json").exists(),
                         "a rejected import must copy nothing")

    def test_conflicting_target_refuses_without_partial_copy(self):
        legacy = self._legacy()
        (legacy / "var" / "extra.json").write_text("new", encoding="utf-8")
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        (paths.state_dir() / "reminders.json").write_text("different", encoding="utf-8")
        with self.assertRaises(state_import.ImportConflictError) as ctx:
            state_import.import_legacy(legacy)
        manifest = ctx.exception.manifest
        self.assertEqual([c["rel"] for c in manifest["conflicts"]], ["var/reminders.json"])
        self.assertFalse((paths.state_dir() / "extra.json").exists(),
                         "no partial copy when a conflict is present")
        self.assertEqual((paths.state_dir() / "reminders.json").read_text(encoding="utf-8"),
                         "different", "an existing target must never be overwritten")

    def test_matching_hashes_are_idempotent(self):
        legacy = self._legacy()
        first = state_import.import_legacy(legacy)
        self.assertEqual(first["counts"]["copied"], 5)
        second = state_import.import_legacy(legacy)
        self.assertTrue(second["ok"])
        self.assertEqual(second["counts"]["copied"], 0)
        self.assertEqual(second["counts"]["identical"], 5)
        self.assertEqual(second["counts"]["conflicts"], 0)
        self.assertEqual(second["manifest_sha256"], first["manifest_sha256"])

    def test_missing_legacy_tree_is_rejected(self):
        with self.assertRaises(state_import.LegacyTreeError):
            state_import.import_legacy(self.root / "does-not-exist")


class DryRunTests(ImportTestCase):
    def test_preview_makes_no_changes(self):
        legacy = self._legacy()
        self._with_transient(legacy)
        manifest = state_import.preview(legacy)
        self.assertTrue(manifest["dry_run"])
        self.assertTrue(manifest["ok"])
        self.assertGreaterEqual(manifest["counts"]["total"], 8)
        self.assertFalse(paths.data_root().exists(),
                         "dry-run preview must not create the target data root")
        self.assertFalse(paths.state_dir().exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
