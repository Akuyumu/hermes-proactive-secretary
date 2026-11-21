"""Repair pass 2, Defect 2: namespaced cron jobs + recorded ownership.

Offline only: an in-memory backend stands in for the native scheduler, so no
real cron job is ever created or removed. Covers the foreign-name collision
(setup refuses to adopt, disable refuses to remove) and the owned-id removal
path.
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
    name = "hermes_proactive_portal_repair2_cron"
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

PILOT = cron_mod.PILOT_JOB_NAME
REMINDER = cron_mod.REMINDER_JOB_NAME


def _fast_checks(_fixture: str) -> dict:
    return {"self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}


def _foreign_job(name: str = PILOT, job_id: str = "foreign-7") -> dict:
    return {
        "id": job_id, "name": name, "schedule": "0 0 1 1 *",
        "script": "someone_elses_runner.py", "deliver": "origin",
        "workdir": "/elsewhere", "state": "active", "last_run": "",
    }


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        (root / "home").mkdir(parents=True, exist_ok=True)
        self.root = root
        self._saved = {k: os.environ.get(k) for k in
                       (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(root / "data")
        os.environ["HERMES_HOME"] = str(root / "home")
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = _fast_checks
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        sc._run_offline_checks = self._orig_checks
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _setup_ns(self, *, no_cron=False):
        return argparse.Namespace(force=False, no_cron=no_cron, json=True,
                                  fixture="healthy_empty", deliver=None, data_dir=None)

    def _capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args, **kwargs)
        return rc, json.loads(buf.getvalue())


class NamespacedSpecTests(unittest.TestCase):
    def test_build_job_specs_are_namespaced(self):
        names = {s.name for s in cron_mod.build_job_specs()}
        self.assertEqual(names, {"hermes-proactive-portal-monitoring",
                                 "hermes-proactive-portal-reminders"})
        for name in names:
            self.assertTrue(name.startswith("hermes-proactive-portal-"))
        self.assertEqual(set(sc.OWNED_JOB_NAMES), set(cron_mod.OWNED_JOB_NAMES))


class OwnershipLedgerTests(_DataDirMixin):
    def test_recorded_id_and_creation_time_round_trip(self):
        path = paths.job_ownership_path()
        spec = cron_mod.CronJobSpec(name=PILOT, schedule="*/15 * * * *",
                                    script=cron_mod.PILOT_SCRIPT)
        backend = cron_mod.InMemoryCronBackend()
        joined = cron_mod.load_job_ownership(path)
        cron_mod.ensure_job(backend, spec, ownership=joined, ownership_path=path,
                            now="2026-10-05T00:00:00+00:00")
        reloaded = cron_mod.load_job_ownership(path)
        record = reloaded["jobs"][PILOT]
        self.assertEqual(record["id"], "job-1")
        self.assertEqual(record["name"], PILOT)
        self.assertEqual(record["created_at"], "2026-10-05T00:00:00+00:00")
        # A same-named foreign job is never adopted when ownership is enforced.
        self.assertEqual(record["id"], backend.list_jobs()[0]["id"])

    def test_remove_owned_job_uses_recorded_id(self):
        path = paths.job_ownership_path()
        spec = cron_mod.CronJobSpec(name=PILOT, schedule="*/15 * * * *",
                                    script=cron_mod.PILOT_SCRIPT)
        backend = cron_mod.InMemoryCronBackend()
        ownership = cron_mod.load_job_ownership(path)
        cron_mod.ensure_job(backend, spec, ownership=ownership, ownership_path=path)
        result = cron_mod.remove_job(backend, PILOT, ownership=ownership,
                                     ownership_path=path)
        self.assertEqual(result.action, "removed")
        self.assertIn("id=job-1", result.detail)
        self.assertEqual(backend.list_jobs(), [])
        self.assertEqual(cron_mod.load_job_ownership(path)["jobs"], {})

    def test_same_name_different_id_is_left_untouched(self):
        path = paths.job_ownership_path()
        backend = cron_mod.InMemoryCronBackend([_foreign_job(job_id="intruder-1")])
        ownership = {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                     "jobs": {PILOT: {"id": "owned-1", "name": PILOT,
                                      "created_at": "2026-10-05T00:00:00+00:00"}}}
        result = cron_mod.remove_job(backend, PILOT, ownership=ownership,
                                     ownership_path=path)
        self.assertEqual(result.action, "foreign")
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["intruder-1"])
        # The stale record is dropped, but the foreign job survives.
        self.assertNotIn(PILOT, cron_mod.load_job_ownership(path)["jobs"])

    def test_classify_distinguishes_no_jobs_from_foreign_collision(self):
        none = cron_mod.classify_jobs([], {})
        self.assertEqual(none["state"], "none")
        self.assertEqual(none["owned"], [])
        self.assertEqual(none["foreign"], [])

        foreign = cron_mod.classify_jobs([_foreign_job()], {})
        self.assertEqual(foreign["state"], "foreign")
        self.assertEqual([j["name"] for j in foreign["foreign"]], [PILOT])
        self.assertEqual(foreign["owned"], [])

        owned_ledger = {"jobs": {PILOT: {"id": "owned-1", "name": PILOT}}}
        owned = cron_mod.classify_jobs([{"id": "owned-1", "name": PILOT}], owned_ledger)
        self.assertEqual(owned["state"], "owned")
        self.assertEqual([j["name"] for j in owned["owned"]], [PILOT])
        self.assertEqual(owned["foreign"], [])


class ForeignNameSetupRefusalTests(_DataDirMixin):
    def test_setup_refuses_to_adopt_foreign_same_name(self):
        backend = cron_mod.InMemoryCronBackend([_foreign_job()])
        rc, result = self._capture(sc.setup_command, self._setup_ns(), backend=backend)
        self.assertEqual(rc, 1)
        self.assertFalse(result["ok"])
        self.assertIn("foreign", result["error"].lower())
        self.assertIn(PILOT, result["error"])
        # The foreign job is untouched and no owned job/record was created.
        jobs = backend.list_jobs()
        self.assertEqual([j["id"] for j in jobs], ["foreign-7"])
        self.assertEqual(jobs[0]["script"], "someone_elses_runner.py")
        self.assertEqual(cron_mod.load_job_ownership(paths.job_ownership_path())["jobs"], {})

    def test_setup_still_creates_when_no_collision(self):
        backend = cron_mod.InMemoryCronBackend()
        rc, result = self._capture(sc.setup_command, self._setup_ns(), backend=backend)
        self.assertEqual(rc, 0)
        self.assertEqual({j["name"] for j in backend.list_jobs()}, {PILOT, REMINDER})
        ledger = cron_mod.load_job_ownership(paths.job_ownership_path())
        self.assertEqual(set(ledger["jobs"]), {PILOT, REMINDER})
        self.assertTrue(all(ledger["jobs"][n]["id"] for n in (PILOT, REMINDER)))


class ForeignNameDisableRefusalTests(_DataDirMixin):
    def test_disable_never_removes_a_foreign_same_named_job(self):
        # Initialise preferences/ownership without creating any job.
        rc, _ = self._capture(sc.setup_command, self._setup_ns(no_cron=True))
        self.assertEqual(rc, 0)
        backend = cron_mod.InMemoryCronBackend([_foreign_job()])
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, result = self._capture(sc.disable_command,
                                       argparse.Namespace(json=True, data_dir=None))
        finally:
            sc._backend = orig_backend
        self.assertEqual(rc, 0)
        actions = {r["name"]: r["action"] for r in result["removed"]}
        self.assertEqual(actions[PILOT], "foreign")
        # The foreign job is still present after disable.
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["foreign-7"])

    def test_disable_removes_only_jobs_it_recorded(self):
        backend = cron_mod.InMemoryCronBackend()
        self.assertEqual(sc.setup_command(self._setup_ns(), backend=backend), 0)
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, result = self._capture(sc.disable_command,
                                       argparse.Namespace(json=True, data_dir=None))
        finally:
            sc._backend = orig_backend
        self.assertEqual(rc, 0)
        actions = {r["name"]: r["action"] for r in result["removed"]}
        self.assertEqual(actions[PILOT], "removed")
        self.assertEqual(actions[REMINDER], "removed")
        self.assertEqual(backend.list_jobs(), [])

    def test_status_reports_no_jobs_of_ours(self):
        self.assertEqual(sc.setup_command(self._setup_ns(no_cron=True), backend=None), 0)
        backend = cron_mod.InMemoryCronBackend()
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, payload = self._capture(sc.status_command,
                                        argparse.Namespace(json=True, data_dir=None))
        finally:
            sc._backend = orig_backend
        self.assertEqual(rc, 0)
        self.assertEqual(payload["jobs"]["state"], "none")
        self.assertEqual(payload["jobs"]["owned"], [])

    def test_status_flags_foreign_name_collision(self):
        self.assertEqual(sc.setup_command(self._setup_ns(no_cron=True), backend=None), 0)
        backend = cron_mod.InMemoryCronBackend([_foreign_job()])
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, payload = self._capture(sc.status_command,
                                        argparse.Namespace(json=True, data_dir=None))
        finally:
            sc._backend = orig_backend
        self.assertEqual(rc, 0)
        self.assertEqual(payload["jobs"]["state"], "foreign")
        self.assertEqual([j["name"] for j in payload["jobs"]["foreign"]], [PILOT])


if __name__ == "__main__":
    unittest.main(verbosity=2)
