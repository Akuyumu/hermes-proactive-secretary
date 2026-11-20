"""Offline regressions for the legacy root-module shadow guard."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = tempfile.mkdtemp(prefix="secretary-layout-shadow-")
_ENV_KEYS = ("HOME", "HERMES_HOME", "HERMES_PROACTIVE_SECRETARY_DATA", "TMPDIR")
_OLD_ENV: dict[str, str | None] = {}


def setUpModule():
    for key in _ENV_KEYS:
        _OLD_ENV[key] = os.environ.get(key)
    os.environ["HOME"] = os.path.join(SCRATCH, "home")
    os.environ["HERMES_HOME"] = os.path.join(SCRATCH, "profile")
    os.environ["HERMES_PROACTIVE_SECRETARY_DATA"] = os.path.join(SCRATCH, "data")
    os.environ["TMPDIR"] = SCRATCH
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["HERMES_HOME"]).mkdir(parents=True, exist_ok=True)


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    for key, value in _OLD_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import release_export  # noqa: E402
from proactive_pilot import layout  # noqa: E402


def _load_package():
    name = "hermes_proactive_secretary_layout_shadow"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
sc = importlib.import_module(f"{PKG.__name__}.secretary.secretary_cli")


def _tree(*, subpackage: bool = True, shadow: bool = False, orphan: bool = False) -> Path:
    root = Path(tempfile.mkdtemp(prefix="tree-", dir=SCRATCH))
    if subpackage:
        (root / "secretary").mkdir()
        (root / "secretary" / "secretary_cli.py").write_text("# real\n", encoding="utf-8")
    if shadow:
        (root / "secretary_cli.py").write_text("# stale root module\n", encoding="utf-8")
    if orphan:
        (root / "secretary_orphan.py").write_text("# no subpackage counterpart\n",
                                                  encoding="utf-8")
    return root


def _extract_package() -> Path:
    out = Path(tempfile.mkdtemp(prefix="package-", dir=SCRATCH)) / "plugin.tar.gz"
    result = release_export.build_package(out, root=ROOT)
    if not result["ok"]:
        raise AssertionError(result)
    installed = out.parent / "installed"
    installed.mkdir()
    with tarfile.open(out, "r:gz") as archive:
        archive.extractall(installed, filter="data")
    return installed


class LayoutDetectionTests(unittest.TestCase):
    def test_clean_tree_has_no_shadows(self):
        root = _tree()
        self.assertEqual(layout.legacy_shadows(root), [])
        self.assertFalse(layout.is_shadowed(root))

    def test_root_module_shadowing_subpackage_is_reported_once(self):
        root = _tree(shadow=True)
        self.assertEqual(layout.legacy_shadows(root), ["secretary_cli.py"])
        self.assertTrue(layout.is_shadowed(root))

    def test_root_module_without_subpackage_counterpart_is_not_a_shadow(self):
        root = _tree(orphan=True)
        self.assertEqual(layout.legacy_shadows(root), [])

    def test_missing_subpackage_is_not_a_shadow_problem(self):
        root = _tree(subpackage=False, shadow=True)
        self.assertEqual(layout.legacy_shadows(root), [])

    def test_missing_or_erroring_path_returns_empty(self):
        self.assertEqual(layout.legacy_shadows(Path(SCRATCH) / "absent"), [])
        root = _tree()
        with mock.patch("pathlib.Path.iterdir", side_effect=OSError("denied")):
            self.assertEqual(layout.legacy_shadows(root), [])


class DoctorShadowTests(unittest.TestCase):
    def _doctor(self, installed: Path) -> dict:
        with mock.patch.object(release_export, "_import_registration",
                               return_value={"ok": True}), \
                mock.patch.object(release_export, "_native_host_doctor",
                                  return_value={"ok": True}):
            return release_export.doctor(installed)

    def test_clean_extracted_package_is_not_blocked_by_layout(self):
        installed = _extract_package()
        report = self._doctor(installed)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["layout_shadows"], [])
        self.assertEqual(report["layout_problems"], [])

    def test_shadowed_extracted_package_fails_doctor(self):
        installed = _extract_package()
        shadowed = installed.parent / "shadowed"
        shutil.copytree(installed, shadowed)
        (shadowed / "secretary_cli.py").write_text("# stale root module\n", encoding="utf-8")
        report = self._doctor(shadowed)
        self.assertFalse(report["ok"])
        self.assertEqual(report["layout_shadows"], ["secretary_cli.py"])
        self.assertEqual(len(report["layout_problems"]), 1)
        self.assertIn("secretary_cli.py", report["layout_problems"][0])
        self.assertIn("reinstall", report["layout_problems"][0])


class StatusLayoutTests(unittest.TestCase):
    def _status_human(self, root: Path) -> str:
        backend = sc.cron_mod.InMemoryCronBackend()
        buf = io.StringIO()
        with mock.patch.object(sc.paths, "plugin_root", return_value=root), \
                mock.patch.object(sc, "_backend", return_value=backend), \
                contextlib.redirect_stdout(buf):
            rc = sc.status_command(argparse.Namespace(json=False, data_dir=None))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_status_line_only_when_shadows_exist(self):
        shadowed = self._status_human(_tree(shadow=True))
        self.assertIn("layout        : 1 legacy root module(s) shadow secretary/", shadowed)
        clean = self._status_human(_tree())
        self.assertNotIn("layout        :", clean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
