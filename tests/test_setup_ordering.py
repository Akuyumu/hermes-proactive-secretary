"""Setup ordering + legacy-import integration regressions (offline).

Verifies that setup runs the offline self-test + no-send check BEFORE installing
runners or creating any cron job, that a failing check creates zero jobs, that an
imported global pause is preserved, and that the import dry-run makes no changes.
No network, no live profile, no live scheduler.
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


def _load_plugin_package():
    name = "hermes_proactive_portal_setup_order"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.portal.portal_cli")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
runner_install = importlib.import_module(f"{_PKG.__name__}.runner_install")

PILOT = cron_mod.PILOT_JOB_NAME
REMINDER = cron_mod.REMINDER_JOB_NAME


def _ok_checks(_fixture: str) -> dict:
    return {"self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}


def _fail_checks(_fixture: str) -> dict:
    return {"self_test_rc": 1, "self_test": {"total": 1, "passed": 0, "failed": 1},
            "dry_run_rc": 0}


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        (root / "home").mkdir(parents=True, exist_ok=True)
        self.root = root
        self._saved = {k: os.environ.get(k) for k in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(root / "data")
        os.environ["HERMES_HOME"] = str(root / "home")
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = _ok_checks
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        sc._run_offline_checks = self._orig_checks
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _ns(self, **overrides):
        base = dict(force=False, no_cron=False, json=True, fixture="healthy_empty",
                    deliver=None, data_dir=None, import_from=None, import_dry_run=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _capture(self, ns, backend=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(ns, backend=backend)
        return rc, buf.getvalue()

    def _legacy_tree(self, paused=False):
        legacy = self.root / "legacy"
        (legacy / "var" / "registries").mkdir(parents=True, exist_ok=True)
        (legacy / "policy").mkdir(parents=True, exist_ok=True)
        (legacy / "var" / "reminders.json").write_text(json.dumps(
            {"schema": 1, "reminders": {"r1": {"status": "pending", "text": "Ship it"}}}),
            encoding="utf-8")
        (legacy / "var" / "registries" / "adaptive.json").write_text(
            json.dumps({"registries": {"public": {"v": 1}}}), encoding="utf-8")
        (legacy / "policy" / "policy.json").write_text(
            json.dumps({"persona": "Legacy", "daily_normal_cap": 7}), encoding="utf-8")
        (legacy / "policy" / "preferences.json").write_text(json.dumps({
            "schema": 1, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": paused, "provenance": "legacy"},
        }), encoding="utf-8")
        return legacy


class CheckBeforeCronTests(_DataDirMixin):
    def test_failing_check_creates_zero_jobs_and_no_runners(self):
        class RecordingBackend(cron_mod.InMemoryCronBackend):
            def __init__(self):
                super().__init__()
                self.added: list[str] = []

            def add_job(self, spec):
                self.added.append(spec.name)
                return super().add_job(spec)

        sc._run_offline_checks = _fail_checks
        backend = RecordingBackend()
        rc, _out = self._capture(self._ns(), backend=backend)
        self.assertEqual(rc, 1)
        self.assertEqual(backend.added, [], "no cron job may be created before verification")
        self.assertEqual(backend.list_jobs(), [])
        self.assertEqual(runner_install.owned_files(paths.runner_ownership_path()), [])
        if paths.scripts_dir().exists():
            self.assertEqual(list(paths.scripts_dir().iterdir()), [])

    def test_checks_run_before_any_job_exists(self):
        backend = cron_mod.InMemoryCronBackend()
        observed = {}

        def checks(_fixture):
            observed["jobs_at_check"] = backend.list_jobs()
            return _ok_checks(_fixture)

        sc._run_offline_checks = checks
        rc, _out = self._capture(self._ns(), backend=backend)
        self.assertEqual(rc, 0)
        self.assertEqual(observed["jobs_at_check"], [],
                         "verification must see zero jobs (nothing armed yet)")
        self.assertEqual({j["name"] for j in backend.list_jobs()}, {PILOT, REMINDER})

    def test_successful_setup_still_installs_runners_after_checks(self):
        backend = cron_mod.InMemoryCronBackend()
        rc, _out = self._capture(self._ns(), backend=backend)
        self.assertEqual(rc, 0)
        owned = {Path(p).name for p in runner_install.owned_files(paths.runner_ownership_path())}
        self.assertEqual(owned, set(runner_install.RUNNER_FILENAMES))


class HonestRollbackTests(_DataDirMixin):
    def test_rollback_failure_is_reported_not_claimed(self):
        class StickyBackend(cron_mod.InMemoryCronBackend):
            def add_job(self, spec):
                if spec.name == REMINDER:
                    raise cron_mod.CronError("boom")
                return super().add_job(spec)

            def remove_job(self, job_id):
                raise cron_mod.CronError("cannot remove")

        backend = StickyBackend()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(self._ns(), backend=backend)
        result = json.loads(buf.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(result["ok"])
        self.assertNotIn("rolled_back", result,
                         "a removal that failed must not be claimed as rolled back")
        self.assertEqual([f["name"] for f in result.get("rollback_failures", [])], [PILOT])
        self.assertEqual([j["name"] for j in backend.list_jobs()], [PILOT],
                         "a job that could not be removed must remain present")


class ImportPauseTests(_DataDirMixin):
    def test_imported_global_pause_is_preserved(self):
        legacy = self._legacy_tree(paused=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(
                self._ns(no_cron=True, import_from=str(legacy)), backend=None)
        result = json.loads(buf.getvalue())
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["preferences"]["action"], "kept")
        prefs = json.loads(paths.preferences_path().read_text(encoding="utf-8"))
        self.assertTrue(prefs["paused"]["active"], "imported pause must not be silently resumed")
        # Imported registries/policy are preserved byte-for-byte.
        self.assertEqual(json.loads((paths.state_dir() / "reminders.json").read_text(
            encoding="utf-8"))["reminders"]["r1"]["status"], "pending")
        self.assertEqual(json.loads(paths.policy_path().read_text(
            encoding="utf-8"))["persona"], "Legacy")

    def test_dry_run_preview_makes_no_changes(self):
        legacy = self._legacy_tree(paused=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(
                self._ns(no_cron=True, import_from=str(legacy), import_dry_run=True),
                backend=None)
        result = json.loads(buf.getvalue())
        self.assertEqual(rc, 0, result)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["import"]["dry_run"])
        self.assertGreaterEqual(result["import"]["counts"]["total"], 3)
        self.assertFalse(paths.data_root().exists(),
                         "a dry-run preview must not create or change target state")

    def test_dry_run_requires_import_from(self):
        rc, out = self._capture(self._ns(import_dry_run=True, import_from=None))
        self.assertEqual(rc, 2)
        self.assertIn("--import-from", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
