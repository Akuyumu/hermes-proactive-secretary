"""CLI surface for validated configuration (show/validate/preview/init/migrate).

Uses the real registered ``hermes secretary configure`` handler in an isolated
data directory; no network, no live profile.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package():
    name = "hermes_proactive_secretary_cfg_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


def _json_out(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    try:
        payload = json.loads(buf.getvalue())
    except ValueError:
        payload = {"raw": buf.getvalue()}
    return rc, payload


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(paths.DATA_ENV)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        os.environ[paths.DATA_ENV] = str(Path(self._tmp.name) / "data")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop(paths.DATA_ENV, None)
        else:
            os.environ[paths.DATA_ENV] = self._old


def _ns(action, **kw):
    defaults = {"action": action, "file": None, "from_policy": None,
                "force": False, "dry_run": False, "json": True, "data_dir": None}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class CliSurfaceTests(unittest.TestCase):
    def test_configure_subcommands_parse(self):
        parser = argparse.ArgumentParser(prog="hermes secretary")
        sc.register_cli(parser)
        for action in ("show", "validate", "preview", "init", "migrate"):
            ns = parser.parse_args(["configure", action])
            self.assertEqual(ns.secretary_command, "configure")
            self.assertEqual(ns.action, action)

    def test_dispatch_has_configure(self):
        self.assertIn("configure", sc._DISPATCH)


class ConfigureCommandTests(_DataDirMixin):
    def test_init_is_idempotent(self):
        rc, payload = _json_out(sc.configure_command, _ns("init", file=False))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "written")
        rc, payload = _json_out(sc.configure_command, _ns("init"))
        self.assertEqual(payload["status"], "kept")
        self.assertTrue(paths.user_config_path().is_file())

    def test_show_reports_schema_and_defaults_when_absent(self):
        rc, payload = _json_out(sc.configure_command, _ns("show"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["config"]["timezone"], "UTC")
        self.assertIn("native_settings", payload["schema"])

    def test_validate_rejects_corrupt_file(self):
        path = paths.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        rc, payload = _json_out(sc.configure_command, _ns("validate"))
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])

    def test_validate_rejects_credential_value(self):
        path = paths.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "version": 1, "mail": {"token": "x"}}),
                        encoding="utf-8")
        rc, payload = _json_out(sc.configure_command, _ns("validate"))
        self.assertEqual(rc, 1)
        self.assertIn("credential", payload["error"])

    def test_preview_reports_policy_changes(self):
        path = paths.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "version": 1, "timezone": "Europe/Paris"}),
                        encoding="utf-8")
        rc, payload = _json_out(sc.configure_command, _ns("preview"))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["changes"]["timezone"]["to"], "Europe/Paris")

    def test_migrate_from_legacy_policy(self):
        legacy = Path(self._tmp.name) / "legacy_policy.json"
        legacy.write_text(json.dumps({"timezone": "Asia/Singapore", "feeds_enabled": False,
                                      "obligations_enabled": True}), encoding="utf-8")
        rc, payload = _json_out(sc.configure_command,
                                _ns("migrate", from_policy=str(legacy)))
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "written")
        written = json.loads(paths.user_config_path().read_text(encoding="utf-8"))
        self.assertEqual(written["timezone"], "Asia/Singapore")
        self.assertEqual(written["mail"]["accounts"], ["formal", "informal"])
        # A second migrate must not overwrite without --force.
        rc, payload = _json_out(sc.configure_command,
                                _ns("migrate", from_policy=str(legacy)))
        self.assertEqual(payload["status"], "kept")

    def test_migrate_dry_run_writes_nothing(self):
        legacy = Path(self._tmp.name) / "legacy_policy.json"
        legacy.write_text(json.dumps({"timezone": "UTC"}), encoding="utf-8")
        rc, payload = _json_out(sc.configure_command,
                                _ns("migrate", from_policy=str(legacy), dry_run=True))
        self.assertEqual(rc, 0)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(paths.user_config_path().exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
