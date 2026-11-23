"""Focused acceptance tests for the native in-place portal schedule migration.

Only mocked/in-memory cron surfaces are used. No shell, live scheduler, data
tree or package mutation is involved.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_portal_schedule"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")

PORTAL = cron_mod.PORTAL_JOB_NAME
OLD = "0 * * * *"
NEW = "0 */2 * * *"


def _portal_job(**overrides):
    job = {
        "id": "portal-1",
        "name": PORTAL,
        "schedule": OLD,
        "next_run_at": "2026-11-23T13:00:00+00:00",
        "state": "paused",
        "enabled": False,
        "deliver": "local",
        "context_from": ["self"],
        "model": "Model-X",
        "provider": "Provider-Y",
        "reasoning": "high",
        "skills": ["alpha", "Beta"],
        "workdir": "/Work/Dir",
        "prompt": "portal prompt",
        "last_run": "2026-11-23T12:00:00+00:00",
        "history": [{"at": "2026-11-23T12:00:00+00:00", "result": "ok"}],
        "custom_owner_field": "preserve",
    }
    job.update(overrides)
    return job


def _ledger(job_id: str = "portal-1", name: str = PORTAL):
    return {
        "schema": cron_mod.JOB_OWNERSHIP_SCHEMA,
        "plugin": cron_mod.OWNED_PLUGIN,
        "jobs": {
            name: {
                "id": job_id,
                "name": name,
                "created_at": "2026-10-20T00:00:00+00:00",
                "custom_owner_field": "preserve",
            }
        },
    }


class _CountingBackend(cron_mod.InMemoryCronBackend):
    def __init__(self, jobs=None):
        super().__init__(jobs)
        self.add_calls = 0
        self.remove_calls = 0

    def add_job(self, spec):
        self.add_calls += 1
        return super().add_job(spec)

    def remove_job(self, job_id):
        self.remove_calls += 1
        return super().remove_job(job_id)


class NativeScheduleUpdateTests(unittest.TestCase):
    def test_update_uses_immutable_id_and_preserves_everything_else(self):
        job = _portal_job()

        class NativeJobs:
            calls = []

            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                self.calls.append((job_id, dict(updates)))
                job.update(updates)
                job["next_run_at"] = "2026-11-23T14:00:00+00:00"
                return dict(job)

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = NativeJobs()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            result = cron_mod.HermesCronBackend().update_schedule(
                "portal-1", NEW)

        self.assertEqual(NativeJobs.calls, [("portal-1", {"schedule": NEW})])
        self.assertEqual(result["id"], "portal-1")
        self.assertEqual(result["schedule"], NEW)
        self.assertEqual(result["next_run_at"], "2026-11-23T14:00:00+00:00")
        for key, value in _portal_job().items():
            if key in {"schedule", "next_run_at"}:
                continue
            self.assertEqual(result[key], value, key)

    def test_ambiguous_response_after_commit_is_read_back_not_retried(self):
        job = _portal_job()

        class MutatingThenAmbiguous:
            calls = 0

            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                type(self).calls += 1
                job.update(updates)
                job["next_run_at"] = "2026-11-23T14:00:00+00:00"
                return None

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = MutatingThenAmbiguous()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            result = cron_mod.HermesCronBackend().update_schedule("portal-1", NEW)

        self.assertEqual(MutatingThenAmbiguous.calls, 1)
        self.assertEqual(result["id"], "portal-1")
        self.assertEqual(result["schedule"], NEW)

    def test_exception_after_commit_is_confirmed_from_source(self):
        job = _portal_job()

        class MutatingThenRaising:
            calls = 0

            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                type(self).calls += 1
                job.update(updates)
                raise RuntimeError("transport lost after commit")

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = MutatingThenRaising()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            result = cron_mod.HermesCronBackend().update_schedule("portal-1", NEW)

        self.assertEqual(MutatingThenRaising.calls, 1)
        self.assertEqual(result["schedule"], NEW)
        self.assertEqual(result["id"], "portal-1")

    def test_unapplied_ambiguous_response_is_refused_without_a_retry(self):
        job = _portal_job()

        class NotApplied:
            calls = 0

            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                type(self).calls += 1
                return None

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = NotApplied()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_schedule("portal-1", NEW)

        self.assertEqual(NotApplied.calls, 1)
        self.assertIn("not applied", str(caught.exception))
        self.assertEqual(job["schedule"], OLD)

    def test_extra_field_change_after_commit_is_refused(self):
        job = _portal_job()

        class MutatingTooMuch:
            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                job.update(updates)
                job["name"] = "renamed-by-host"
                return dict(job)

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = MutatingTooMuch()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_schedule("portal-1", NEW)

        self.assertIn("beyond schedule", str(caught.exception))
        self.assertIn("name", str(caught.exception))

    def test_missing_update_seam_is_refused_without_mutation(self):
        job = _portal_job()

        class NoUpdate:
            def get_job(self, job_id):
                return dict(job)

        fake_pkg = types.ModuleType("cron")
        fake_pkg.jobs = NoUpdate()
        with mock.patch.dict(sys.modules, {"cron": fake_pkg}):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_schedule("portal-1", NEW)

        self.assertIn("no safe in-place schedule update", str(caught.exception))
        self.assertEqual(job["schedule"], OLD)


class PortalScheduleMigrationTests(unittest.TestCase):
    def test_same_id_and_disabled_context_local_output_are_unchanged(self):
        job = _portal_job()
        backend = _CountingBackend([job])
        ownership = _ledger()

        result = cron_mod.migrate_portal_schedule(
            backend, NEW, ownership=ownership)

        self.assertEqual(result.action, "migrated")
        self.assertIn("id=portal-1", result.detail)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        rows = backend.list_jobs()
        self.assertEqual(len(rows), 1)
        migrated = rows[0]
        self.assertEqual(migrated["id"], "portal-1")
        self.assertEqual(migrated["schedule"], NEW)
        for key, value in job.items():
            if key == "schedule":
                continue
            self.assertEqual(migrated[key], value, key)
        self.assertEqual(migrated["state"], "paused")
        self.assertFalse(migrated["enabled"])
        self.assertEqual(migrated["deliver"], "local")
        self.assertEqual(migrated["context_from"], ["self"])
        self.assertEqual(migrated["model"], "Model-X")
        self.assertEqual(migrated["provider"], "Provider-Y")
        self.assertEqual(migrated["reasoning"], "high")
        self.assertEqual(migrated["skills"], ["alpha", "Beta"])
        self.assertEqual(migrated["workdir"], "/Work/Dir")
        self.assertEqual(ownership["jobs"][PORTAL]["id"], "portal-1")

    def test_repeated_migration_is_idempotent_and_never_recreates(self):
        backend = _CountingBackend([_portal_job()])
        ownership = _ledger()

        first = cron_mod.migrate_portal_schedule(
            backend, NEW, ownership=ownership)
        snapshot = backend.list_jobs()
        again = cron_mod.migrate_portal_schedule(
            backend, NEW, ownership=ownership)

        self.assertEqual(first.action, "migrated")
        self.assertEqual(again.action, "already-current")
        self.assertEqual(backend.list_jobs(), snapshot)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_dry_run_changes_nothing(self):
        backend = _CountingBackend([_portal_job()])
        result = cron_mod.migrate_portal_schedule(
            backend, NEW, ownership=_ledger(), apply=False)

        self.assertEqual(result.action, "would-migrate")
        self.assertEqual(backend.list_jobs()[0]["schedule"], OLD)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_foreign_portal_is_refused_and_left_untouched(self):
        foreign = _portal_job(id="foreign-7")
        backend = _CountingBackend([foreign])
        empty = {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN, "jobs": {}}

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_portal_schedule(backend, NEW, ownership=empty)

        self.assertIn("foreign portal job", str(caught.exception))
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_ownership_id_mismatch_is_refused_without_recreate(self):
        backend = _CountingBackend([_portal_job(id="intruder-1")])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_portal_schedule(
                backend, NEW, ownership=_ledger("portal-1"))

        self.assertIn("ownership mismatch", str(caught.exception))
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["intruder-1"])
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_missing_backend_seam_is_refused_without_recreate(self):
        class NoScheduleSeam:
            def __init__(self, jobs):
                self._jobs = [dict(job) for job in jobs]

            def list_jobs(self):
                return [dict(job) for job in self._jobs]

        job = _portal_job()
        backend = NoScheduleSeam([job])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_portal_schedule(
                backend, NEW, ownership=_ledger())

        self.assertIn("no safe in-place schedule update", str(caught.exception))
        self.assertEqual(backend.list_jobs(), [job])

    def test_ambiguous_high_level_commit_is_read_back_without_recreate(self):
        job = _portal_job()

        class CommitThenRaise(cron_mod.InMemoryCronBackend):
            def __init__(self, jobs):
                super().__init__(jobs)
                self.add_calls = 0
                self.remove_calls = 0

            def update_schedule(self, job_id, schedule):
                result = super().update_schedule(job_id, schedule)
                raise RuntimeError("ambiguous transport response")

            def add_job(self, spec):
                self.add_calls += 1
                return super().add_job(spec)

            def remove_job(self, job_id):
                self.remove_calls += 1
                return super().remove_job(job_id)

        backend = CommitThenRaise([job])
        result = cron_mod.migrate_portal_schedule(
            backend, NEW, ownership=_ledger())

        self.assertEqual(result.action, "migrated")
        self.assertIn("source of truth", result.detail)
        self.assertEqual(backend.list_jobs()[0]["schedule"], NEW)
        self.assertEqual(backend.list_jobs()[0]["id"], "portal-1")
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_ambiguous_high_level_failure_never_recreates(self):
        class NeverApplied(cron_mod.InMemoryCronBackend):
            def __init__(self, jobs):
                super().__init__(jobs)
                self.add_calls = 0
                self.remove_calls = 0

            def update_schedule(self, job_id, schedule):
                raise RuntimeError("ambiguous transport response")

            def add_job(self, spec):
                self.add_calls += 1
                return super().add_job(spec)

            def remove_job(self, job_id):
                self.remove_calls += 1
                return super().remove_job(job_id)

        job = _portal_job()
        backend = NeverApplied([job])
        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.migrate_portal_schedule(
                backend, NEW, ownership=_ledger())

        self.assertIn("not applied", str(caught.exception))
        self.assertEqual(backend.list_jobs(), [job])
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
