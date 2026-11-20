"""Owner-facing monitoring naming and safe in-place legacy migration.

Offline only: ``InMemoryCronBackend`` stands in for the native scheduler, so no
real job is created, renamed or removed.
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
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_naming_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


def _legacy_job(job_id="legacy-1"):
    return {
        "id": job_id,
        "name": cron_mod.LEGACY_PILOT_JOB_NAME,
        "schedule": "*/15 * * * *",
        "deliver": "origin",
        "script": cron_mod.MONITORING_SCRIPT,
        "workdir": "/plugins/hermes-proactive-secretary",
        "mode": "no-agent",
        "state": "active",
        "enabled": True,
        "next_run": "2026-10-26T12:15:00+00:00",
        "last_run": "2026-10-26T12:00:00+00:00",
        "run_history": [{"at": "2026-10-26T12:00:00+00:00", "status": "ok"}],
        "unrelated_owner_note": "keep me",
    }


def _ledger(*names, **extra):
    jobs = {}
    for name in names:
        record = {"id": f"{name}-id", "name": name,
                  "created_at": "2026-10-20T00:00:00+00:00",
                  "custom_owner_field": "preserve"}
        record.update(extra.get(name, {}))
        jobs[name] = record
    return {"schema": cron_mod.JOB_OWNERSHIP_SCHEMA,
            "plugin": cron_mod.OWNED_PLUGIN, "jobs": jobs}


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        (root / "home").mkdir(parents=True, exist_ok=True)
        self.root = root
        self._saved = {k: os.environ.get(k) for k in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(root / "data")
        os.environ["HERMES_HOME"] = str(root / "home")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args, **kwargs)
        return rc, json.loads(buf.getvalue())


class CanonicalNamingTests(unittest.TestCase):
    def test_default_jobs_use_monitoring_not_pilot(self):
        specs = cron_mod.build_job_specs()
        names = [s.name for s in specs]
        self.assertEqual(names, [cron_mod.MONITORING_JOB_NAME,
                                 cron_mod.REMINDER_JOB_NAME])
        self.assertEqual(cron_mod.MONITORING_JOB_NAME,
                         "hermes-proactive-secretary-monitoring")
        self.assertNotIn("pilot", names[0])
        self.assertEqual(set(cron_mod.OWNED_JOB_NAMES), set(names))
        self.assertEqual(cron_mod.PILOT_JOB_NAME, cron_mod.MONITORING_JOB_NAME)

    def test_preparation_is_optional_and_never_part_of_default_count(self):
        specs = cron_mod.build_job_specs(include_preparation=True)
        self.assertEqual([s.name for s in specs],
                         [cron_mod.MONITORING_JOB_NAME,
                          cron_mod.REMINDER_JOB_NAME,
                          cron_mod.PREPARATION_JOB_NAME])


class MigrationTests(_DataDirMixin):
    def _write(self, record, job):
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                                             "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: record}})
        backend = cron_mod.InMemoryCronBackend([job])
        return path, backend

    def test_rename_is_idempotent_and_preserves_every_payload_field(self):
        job = _legacy_job()
        record = {"id": "legacy-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME,
                  "created_at": "2026-10-20T00:00:00+00:00",
                  "custom_owner_field": "preserve"}
        path, backend = self._write(record, job)

        result = cron_mod.migrate_legacy_monitoring_job(
            backend, ownership=cron_mod.load_job_ownership(path),
            ownership_path=path)
        self.assertEqual(result.action, "migrated")

        rows = backend.list_jobs()
        self.assertEqual(len(rows), 1)
        renamed = rows[0]
        self.assertEqual(renamed["id"], job["id"])
        self.assertEqual(renamed["name"], cron_mod.MONITORING_JOB_NAME)
        for key, value in job.items():
            if key == "name":
                continue
            self.assertEqual(renamed[key], value, key)

        ledger = cron_mod.load_job_ownership(path)["jobs"]
        self.assertEqual(set(ledger), {cron_mod.MONITORING_JOB_NAME})
        self.assertEqual(ledger[cron_mod.MONITORING_JOB_NAME]["id"], job["id"])
        self.assertEqual(ledger[cron_mod.MONITORING_JOB_NAME]["custom_owner_field"],
                         "preserve")

        snapshot = backend.list_jobs()
        again = cron_mod.migrate_legacy_monitoring_job(
            backend, ownership=cron_mod.load_job_ownership(path),
            ownership_path=path)
        self.assertEqual(again.action, "already-migrated")
        self.assertEqual(backend.list_jobs(), snapshot)
        self.assertEqual(set(cron_mod.load_job_ownership(path)["jobs"]),
                         {cron_mod.MONITORING_JOB_NAME})

    def test_dry_run_changes_nothing(self):
        job = _legacy_job()
        path, backend = self._write(
            {"id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME}, job)
        before_ledger = path.read_bytes()
        result = cron_mod.migrate_legacy_monitoring_job(
            backend, ownership=cron_mod.load_job_ownership(path),
            ownership_path=path, apply=False)
        self.assertEqual(result.action, "would-migrate")
        self.assertEqual(backend.list_jobs(), [job])
        self.assertEqual(path.read_bytes(), before_ledger)

    def test_ownership_mismatch_is_rejected_without_touching_foreign_row(self):
        path, backend = self._write(
            {"id": "owned-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME},
            _legacy_job("intruder-1"))
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_legacy_monitoring_job(
                backend, ownership=cron_mod.load_job_ownership(path),
                ownership_path=path)
        self.assertIn("ownership mismatch", str(caught.exception))
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["intruder-1"])
        self.assertIn(cron_mod.LEGACY_PILOT_JOB_NAME,
                      cron_mod.load_job_ownership(path)["jobs"])

    def test_occupied_monitoring_name_is_rejected_without_overwrite(self):
        job = _legacy_job()
        path, backend = self._write(
            {"id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME}, job)
        backend.jobs.append({"id": "foreign-mon", "name": cron_mod.MONITORING_JOB_NAME,
                             "schedule": "0 0 * * *", "state": "active"})
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_legacy_monitoring_job(
                backend, ownership=cron_mod.load_job_ownership(path),
                ownership_path=path)
        self.assertIn("foreign monitoring job", str(caught.exception))
        self.assertEqual({j["id"] for j in backend.list_jobs()},
                         {"legacy-1", "foreign-mon"})

    def test_unrecorded_pilot_name_is_foreign_not_adopted(self):
        backend = cron_mod.InMemoryCronBackend([_legacy_job("foreign-1")])
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                                             "jobs": {}})
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_legacy_monitoring_job(
                backend, ownership=cron_mod.load_job_ownership(path),
                ownership_path=path)
        self.assertIn("foreign legacy job", str(caught.exception))
        self.assertEqual([j["name"] for j in backend.list_jobs()],
                         [cron_mod.LEGACY_PILOT_JOB_NAME])

    def test_cli_dry_run_then_apply_is_idempotent(self):
        job = _legacy_job()
        path, backend = self._write(
            {"id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME}, job)
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, planned = self._capture(
                sc.migrate_jobs_command, argparse.Namespace(json=True, data_dir=None))
            self.assertEqual(rc, 0)
            self.assertFalse(planned["applied"])
            self.assertEqual(planned["action"], "would-migrate")
            self.assertEqual(backend.list_jobs()[0]["name"],
                             cron_mod.LEGACY_PILOT_JOB_NAME)

            rc, applied = self._capture(
                sc.migrate_jobs_command,
                argparse.Namespace(json=True, data_dir=None, apply=True))
            self.assertEqual(rc, 0)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["action"], "migrated")
            self.assertEqual(backend.list_jobs()[0]["name"],
                             cron_mod.MONITORING_JOB_NAME)

            before = backend.list_jobs()
            rc, again = self._capture(
                sc.migrate_jobs_command,
                argparse.Namespace(json=True, data_dir=None, apply=True))
            self.assertEqual(rc, 0)
            self.assertEqual(again["action"], "already-migrated")
            self.assertEqual(backend.list_jobs(), before)
        finally:
            sc._backend = orig_backend

    def test_setup_refuses_a_live_recorded_legacy_job_before_duplicating(self):
        job = _legacy_job()
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(
            path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                   "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: {
                       "id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME}}})
        backend = cron_mod.InMemoryCronBackend([job])
        old_checks = sc._run_offline_checks
        sc._run_offline_checks = lambda _fixture: {
            "self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}
        self.addCleanup(lambda: setattr(sc, "_run_offline_checks", old_checks))
        ns = argparse.Namespace(force=False, no_cron=False, json=True,
                                fixture="healthy_empty", deliver=None, data_dir=None,
                                with_preparation=False)
        rc, result = self._capture(sc.setup_command, ns, backend=backend)
        self.assertEqual(rc, 1)
        self.assertFalse(result["ok"])
        self.assertIn("migrate-jobs --apply", result["error"])
        self.assertEqual([j["id"] for j in backend.list_jobs()], [job["id"]])

    def test_interrupted_rename_reconciles_ledger_without_a_second_mutation(self):
        job = _legacy_job()
        job["name"] = cron_mod.MONITORING_JOB_NAME
        record = {"id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME,
                  "created_at": "2026-10-20T00:00:00+00:00",
                  "custom_owner_field": "preserve"}
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(
            path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                   "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: record}})

        class CountingBackend(cron_mod.InMemoryCronBackend):
            rename_calls = 0

            def rename_job(self, job_id, name):
                self.rename_calls += 1
                return super().rename_job(job_id, name)

        backend = CountingBackend([job])
        result = cron_mod.migrate_legacy_monitoring_job(
            backend, ownership=cron_mod.load_job_ownership(path),
            ownership_path=path)
        self.assertEqual(result.action, "ledger-reconciled")
        self.assertEqual(backend.rename_calls, 0)
        self.assertEqual(backend.list_jobs(), [job])
        ledger = cron_mod.load_job_ownership(path)["jobs"]
        self.assertEqual(set(ledger), {cron_mod.MONITORING_JOB_NAME})
        self.assertEqual(ledger[cron_mod.MONITORING_JOB_NAME]["id"], job["id"])
        self.assertEqual(ledger[cron_mod.MONITORING_JOB_NAME]["custom_owner_field"],
                         "preserve")
        again = cron_mod.migrate_legacy_monitoring_job(
            backend, ownership=cron_mod.load_job_ownership(path),
            ownership_path=path)
        self.assertEqual(again.action, "already-migrated")
        self.assertEqual(backend.rename_calls, 0)

    def test_multiple_monitoring_rows_are_rejected_as_ambiguous(self):
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(
            path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                   "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: {
                       "id": "legacy-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME}}})
        backend = cron_mod.InMemoryCronBackend([
            {"id": "legacy-1", "name": cron_mod.MONITORING_JOB_NAME},
            {"id": "other-2", "name": cron_mod.MONITORING_JOB_NAME},
        ])
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_legacy_monitoring_job(
                backend, ownership=cron_mod.load_job_ownership(path),
                ownership_path=path)
        self.assertIn("multiple monitoring-named rows", str(caught.exception))

    def test_disable_removes_a_recorded_legacy_job_in_fake_backend(self):
        job = _legacy_job()
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(
            path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                   "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: {
                       "id": job["id"], "name": cron_mod.LEGACY_PILOT_JOB_NAME}}})
        backend = cron_mod.InMemoryCronBackend([job])
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            removed = sc._remove_own_jobs()
        finally:
            sc._backend = orig_backend
        actions = {row["name"]: row["action"] for row in removed}
        self.assertEqual(actions[cron_mod.LEGACY_PILOT_JOB_NAME], "removed")
        self.assertEqual(backend.list_jobs(), [])

    def test_disable_leaves_a_foreign_legacy_collision_untouched(self):
        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(
            path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                   "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: {
                       "id": "owned-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME}}})
        backend = cron_mod.InMemoryCronBackend([_legacy_job("intruder-1")])
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            removed = sc._remove_own_jobs()
        finally:
            sc._backend = orig_backend
        actions = {row["name"]: row["action"] for row in removed}
        self.assertEqual(actions[cron_mod.LEGACY_PILOT_JOB_NAME], "foreign")
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["intruder-1"])

    def test_native_rename_uses_the_profile_binding_not_ambient_home(self):
        job = {"id": "job-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME,
               "schedule": "*/15 * * * *", "next_run_at": "2026-10-26T12:15:00+00:00"}

        class NativeJobs:
            def get_job(self, job_id):
                return dict(job) if job_id == "job-1" else None

            def update_job(self, job_id, updates):
                job.update(updates)
                return dict(job)

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = NativeJobs()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}), \
                mock.patch.dict(os.environ, {"HERMES_HOME": "/profile-a"}):
            result = cron_mod.HermesCronBackend().rename_job(
                "job-1", cron_mod.MONITORING_JOB_NAME)
        self.assertEqual(result["id"], "job-1")
        self.assertEqual(result["name"], cron_mod.MONITORING_JOB_NAME)
        self.assertEqual(result["next_run_at"], "2026-10-26T12:15:00+00:00")

    def test_native_rename_rejects_any_field_change_beyond_name(self):
        job = {"id": "job-1", "name": cron_mod.LEGACY_PILOT_JOB_NAME,
               "schedule": "*/15 * * * *"}

        class MutatingJobs:
            def get_job(self, job_id):
                return dict(job)

            def update_job(self, job_id, updates):
                job.update(updates)
                job["next_run_at"] = "unexpected-reset"
                return dict(job)

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = MutatingJobs()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().rename_job(
                    "job-1", cron_mod.MONITORING_JOB_NAME)
        self.assertIn("beyond name", str(caught.exception))
        self.assertIn("next_run_at", str(caught.exception))

    def test_missing_host_rename_blocks_without_recreate_or_remove(self):
        job = _legacy_job()

        class NoRenameBackend:
            def list_jobs(self):
                return [dict(job)]

        path = paths.job_ownership_path()
        cron_mod.save_job_ownership(path, {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                                             "jobs": {cron_mod.LEGACY_PILOT_JOB_NAME: {
                                                 "id": job["id"],
                                                 "name": cron_mod.LEGACY_PILOT_JOB_NAME}}})
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_legacy_monitoring_job(
                NoRenameBackend(), ownership=cron_mod.load_job_ownership(path),
                ownership_path=path)
        self.assertIn("no safe in-place rename", str(caught.exception))
        self.assertIn(cron_mod.LEGACY_PILOT_JOB_NAME,
                      cron_mod.load_job_ownership(path)["jobs"])


class OptionalPreparationOwnershipTests(_DataDirMixin):
    def test_recorded_present_preparation_is_owned_and_counted(self):
        ledger = _ledger(cron_mod.MONITORING_JOB_NAME,
                         cron_mod.REMINDER_JOB_NAME,
                         cron_mod.PREPARATION_JOB_NAME)
        jobs = [
            {"id": f"{cron_mod.MONITORING_JOB_NAME}-id",
             "name": cron_mod.MONITORING_JOB_NAME, "state": "active"},
            {"id": f"{cron_mod.REMINDER_JOB_NAME}-id",
             "name": cron_mod.REMINDER_JOB_NAME, "state": "active"},
            {"id": f"{cron_mod.PREPARATION_JOB_NAME}-id",
             "name": cron_mod.PREPARATION_JOB_NAME, "state": "active"},
        ]
        classified = cron_mod.classify_jobs(jobs, ledger)
        self.assertEqual(classified["state"], "owned")
        self.assertEqual(len(classified["owned"]), 3)
        self.assertEqual([row["name"] for row in classified["optional"]],
                         [cron_mod.PREPARATION_JOB_NAME])
        self.assertNotIn(cron_mod.PREPARATION_JOB_NAME, classified["foreign"])

    def test_unrecorded_preparation_is_foreign_not_hidden(self):
        classified = cron_mod.classify_jobs(
            [{"id": "foreign-prep", "name": cron_mod.PREPARATION_JOB_NAME}], {})
        self.assertEqual(classified["state"], "foreign")
        self.assertEqual(classified["owned"], [])
        self.assertEqual([row["name"] for row in classified["foreign"]],
                         [cron_mod.PREPARATION_JOB_NAME])

    def test_recorded_absent_preparation_is_reported_missing(self):
        classified = cron_mod.classify_jobs([], _ledger(cron_mod.PREPARATION_JOB_NAME))
        self.assertEqual(classified["state"], "missing")
        self.assertEqual(classified["missing"], [cron_mod.PREPARATION_JOB_NAME])

    def test_remove_optional_job_uses_recorded_id_in_fake_backend(self):
        ledger = _ledger(cron_mod.PREPARATION_JOB_NAME)
        backend = cron_mod.InMemoryCronBackend([{
            "id": f"{cron_mod.PREPARATION_JOB_NAME}-id",
            "name": cron_mod.PREPARATION_JOB_NAME, "state": "active"}])
        result = cron_mod.remove_job(
            backend, cron_mod.PREPARATION_JOB_NAME, ownership=ledger,
            ownership_path=paths.job_ownership_path())
        self.assertEqual(result.action, "removed")
        self.assertEqual(backend.list_jobs(), [])

    def test_setup_summary_does_not_hide_preparation(self):
        result = {
            "ok": True,
            "jobs": [
                {"name": cron_mod.MONITORING_JOB_NAME, "action": "already-present",
                 "schedule": "*/15 * * * *"},
                {"name": cron_mod.REMINDER_JOB_NAME, "action": "already-present",
                 "schedule": "* * * * *"},
                {"name": cron_mod.PREPARATION_JOB_NAME, "action": "already-present",
                 "schedule": "*/10 * * * *"},
            ],
            "preparation": {"opt_in": True},
            "data_root": "/data",
            "policy": {"action": "kept", "policy": "/policy"},
        }
        text = sc._setup_human(result)
        self.assertIn(cron_mod.PREPARATION_JOB_NAME, text)
        self.assertNotIn("pilot", text.lower())


class StatusNamingTests(_DataDirMixin):
    def _fast_checks(self, _fixture):
        return {"self_test_rc": 0,
                "self_test": {"total": 1, "passed": 1, "failed": 0},
                "dry_run_rc": 0}

    def test_status_counts_preparation_and_has_no_pilot_prose(self):
        old_checks = sc._run_offline_checks
        sc._run_offline_checks = self._fast_checks
        self.addCleanup(lambda: setattr(sc, "_run_offline_checks", old_checks))
        ns = argparse.Namespace(force=False, no_cron=False, json=True,
                                fixture="healthy_empty", deliver=None, data_dir=None,
                                with_preparation=True)
        setup_backend = cron_mod.InMemoryCronBackend()
        rc, _ = self._capture(sc.setup_command, ns, backend=setup_backend)
        self.assertEqual(rc, 0)

        backend = setup_backend
        orig_backend = sc._backend
        sc._backend = lambda: backend
        try:
            rc, payload = self._capture(
                sc.status_command, argparse.Namespace(json=True, data_dir=None))
        finally:
            sc._backend = orig_backend
        self.assertEqual(rc, 0)
        names = {row["name"] for row in payload["jobs"]["owned"]}
        self.assertIn(cron_mod.MONITORING_JOB_NAME, names)
        self.assertIn(cron_mod.PREPARATION_JOB_NAME, names)
        self.assertEqual(len(names), 3)
        self.assertEqual(len(payload["owned_jobs"]), 3)
        self.assertFalse(any("pilot" in name for name in names))
        self.assertEqual(payload["last_pilot_run"], payload["last_monitoring_run"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc._backend = lambda: backend
            try:
                sc.status_command(argparse.Namespace(json=False, data_dir=None))
            finally:
                sc._backend = orig_backend
        human = buf.getvalue()
        self.assertIn(cron_mod.PREPARATION_JOB_NAME, human)
        self.assertNotIn("pilot", human.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
