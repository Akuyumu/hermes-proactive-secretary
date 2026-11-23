"""Focused acceptance tests for the native in-place portal route migration.

The installed host ``cron.jobs`` surface is not imported or bound here: doing so
would require touching a real profile, its stored jobs or its credentials. The
real bridge (``HermesCronBackend.update_route``) and the public migration entry
point (``update_portal_route``) are exercised against an injected in-memory
stand-in for ``cron.jobs`` and an in-memory scheduler backend. Assertions are on
the values actually persisted by that stand-in, not just forwarded kwargs.

Final validation against the real installed host remains outstanding; nothing
in this module runs a shell, reads a production file, or mutates live state.
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
    name = "hermes_proactive_secretary_portal_route"
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

OLD_PROVIDER = "Synthetic-Provider-Original"
OLD_MODEL = "Synthetic-Model-Original"
OLD_EFFORT = "low"
NEW_PROVIDER = "Synthetic-Provider-Routed"
NEW_MODEL = "Synthetic-Model-Routed"
NEW_EFFORT = "high"


def _portal_job(**overrides):
    job = {
        "id": "portal-route-1",
        "name": PORTAL,
        "schedule": "0 * * * *",
        "next_run_at": "2026-11-23T13:00:00+00:00",
        "state": "paused",
        "enabled": False,
        "deliver": "local",
        "context_from": ["self"],
        "model": OLD_MODEL,
        "provider": OLD_PROVIDER,
        "reasoning": OLD_EFFORT,
        "skills": ["alpha", "Beta"],
        "workdir": "/Work/Dir",
        "prompt": "portal prompt",
        "last_run": "2026-11-23T12:00:00+00:00",
        "history": [{"at": "2026-11-23T12:00:00+00:00", "result": "ok"}],
        "custom_owner_field": "preserve",
    }
    job.update(overrides)
    return job


def _ledger(job_id: str = "portal-route-1", name: str = PORTAL):
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


def _empty_ledger():
    return {"schema": cron_mod.JOB_OWNERSHIP_SCHEMA,
            "plugin": cron_mod.OWNED_PLUGIN, "jobs": {}}


class _CountingBackend(cron_mod.InMemoryCronBackend):
    def __init__(self, jobs=None):
        super().__init__(jobs)
        self.add_calls = 0
        self.remove_calls = 0
        self.update_route_calls = 0

    def add_job(self, spec):
        self.add_calls += 1
        return super().add_job(spec)

    def remove_job(self, job_id):
        self.remove_calls += 1
        return super().remove_job(job_id)

    def update_route(self, job_id, provider, model, reasoning=None):
        self.update_route_calls += 1
        return super().update_route(job_id, provider, model, reasoning)


class _NativeRouteStandIn:
    """In-memory stand-in for the host ``cron.jobs`` module used by the bridge."""

    def __init__(self, job, *, response=None, raise_after_commit=None,
                 extra_field=None):
        self.job = job
        self.response = response
        self.raise_after_commit = raise_after_commit
        self.extra_field = extra_field
        self.get_calls = 0
        self.update_calls = []

    def get_job(self, job_id):
        self.get_calls += 1
        return dict(self.job) if job_id == self.job.get("id") else None

    def update_job(self, job_id, updates):
        self.update_calls.append((job_id, dict(updates)))
        if job_id != self.job.get("id"):
            return None
        self.job.update(updates)
        if self.extra_field is not None:
            self.job[self.extra_field] = "changed-by-host"
        if self.raise_after_commit is not None:
            raise self.raise_after_commit
        return self.response


def _patched_native(jobs_api):
    fake_pkg = types.ModuleType("cron")
    fake_pkg.jobs = jobs_api
    return mock.patch.dict(sys.modules, {"cron": fake_pkg})


class NativeRouteUpdateTests(unittest.TestCase):
    def test_route_update_persists_complete_route_and_preserves_every_other_field(self):
        job = _portal_job()
        native = _NativeRouteStandIn(job, response=dict(job))

        with _patched_native(native):
            result = cron_mod.HermesCronBackend().update_route(
                "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(
            native.update_calls,
            [("portal-route-1",
              {"provider": NEW_PROVIDER, "model": NEW_MODEL,
               "reasoning": NEW_EFFORT})],
        )
        # Assert the actual persisted row, not merely the forwarded kwargs.
        self.assertEqual(job["provider"], NEW_PROVIDER)
        self.assertEqual(job["model"], NEW_MODEL)
        self.assertEqual(job["reasoning"], NEW_EFFORT)
        self.assertEqual(result["provider"], NEW_PROVIDER)
        self.assertEqual(result["model"], NEW_MODEL)
        self.assertEqual(result["reasoning"], NEW_EFFORT)
        for key, value in _portal_job().items():
            if key in {"provider", "model", "reasoning"}:
                continue
            self.assertEqual(job[key], value, key)
            self.assertEqual(result[key], value, key)

        self.assertEqual(job["id"], "portal-route-1")
        self.assertEqual(job["schedule"], "0 * * * *")
        self.assertEqual(job["next_run_at"], "2026-11-23T13:00:00+00:00")
        self.assertEqual(job["state"], "paused")
        self.assertFalse(job["enabled"])
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["context_from"], ["self"])
        self.assertEqual(job["history"],
                         [{"at": "2026-11-23T12:00:00+00:00", "result": "ok"}])

    def test_omitted_reasoning_leaves_the_persisted_effort_untouched(self):
        job = _portal_job()
        native = _NativeRouteStandIn(job, response=dict(job))

        with _patched_native(native):
            result = cron_mod.HermesCronBackend().update_route(
                "portal-route-1", NEW_PROVIDER, NEW_MODEL)

        self.assertEqual(
            native.update_calls,
            [("portal-route-1",
              {"provider": NEW_PROVIDER, "model": NEW_MODEL})],
        )
        self.assertEqual(job["provider"], NEW_PROVIDER)
        self.assertEqual(job["model"], NEW_MODEL)
        self.assertEqual(job["reasoning"], OLD_EFFORT)
        self.assertEqual(result["reasoning"], OLD_EFFORT)

    def test_repeat_of_an_already_current_route_is_a_no_op(self):
        job = _portal_job(provider=NEW_PROVIDER, model=NEW_MODEL,
                          reasoning=NEW_EFFORT)
        native = _NativeRouteStandIn(job, response=dict(job))

        with _patched_native(native):
            result = cron_mod.HermesCronBackend().update_route(
                "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(native.update_calls, [])
        self.assertEqual(job, _portal_job(provider=NEW_PROVIDER,
                                          model=NEW_MODEL,
                                          reasoning=NEW_EFFORT))
        self.assertEqual(result["provider"], NEW_PROVIDER)
        self.assertEqual(result["model"], NEW_MODEL)
        self.assertEqual(result["reasoning"], NEW_EFFORT)

    def test_lost_response_after_commit_is_read_back_with_one_mutation(self):
        job = _portal_job()
        native = _NativeRouteStandIn(
            job, raise_after_commit=RuntimeError("transport lost after commit"))

        with _patched_native(native):
            result = cron_mod.HermesCronBackend().update_route(
                "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(len(native.update_calls), 1)
        self.assertEqual(job["provider"], NEW_PROVIDER)
        self.assertEqual(job["model"], NEW_MODEL)
        self.assertEqual(job["reasoning"], NEW_EFFORT)
        self.assertEqual(result["provider"], NEW_PROVIDER)
        self.assertEqual(result["model"], NEW_MODEL)
        self.assertEqual(result["reasoning"], NEW_EFFORT)
        self.assertEqual(result["id"], "portal-route-1")
        self.assertEqual(result["schedule"], "0 * * * *")
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["deliver"], "local")
        self.assertEqual(result["context_from"], ["self"])
        self.assertEqual(result["history"],
                         [{"at": "2026-11-23T12:00:00+00:00", "result": "ok"}])

    def test_ambiguous_none_response_after_commit_is_read_back(self):
        job = _portal_job()
        native = _NativeRouteStandIn(job, response=None)

        with _patched_native(native):
            result = cron_mod.HermesCronBackend().update_route(
                "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(len(native.update_calls), 1)
        self.assertEqual(job["provider"], NEW_PROVIDER)
        self.assertEqual(job["model"], NEW_MODEL)
        self.assertEqual(job["reasoning"], NEW_EFFORT)
        self.assertEqual(result["provider"], NEW_PROVIDER)
        self.assertEqual(result["model"], NEW_MODEL)
        self.assertEqual(result["reasoning"], NEW_EFFORT)

    def test_ambiguously_unapplied_update_is_refused_without_a_retry(self):
        job = _portal_job()

        class NoMutation:
            calls = []

            def get_job(self, job_id):
                return dict(job) if job_id == job["id"] else None

            def update_job(self, job_id, updates):
                type(self).calls.append((job_id, dict(updates)))
                return None

        with _patched_native(NoMutation()):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_route(
                    "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(len(NoMutation.calls), 1)
        self.assertIn("not applied", str(caught.exception))
        self.assertEqual(job["provider"], OLD_PROVIDER)
        self.assertEqual(job["model"], OLD_MODEL)
        self.assertEqual(job["reasoning"], OLD_EFFORT)

    def test_extra_field_change_is_refused_and_never_compensated(self):
        job = _portal_job()
        native = _NativeRouteStandIn(job, response=None, extra_field="name")

        with _patched_native(native):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_route(
                    "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertEqual(len(native.update_calls), 1)
        self.assertIn("beyond provider/model/reasoning", str(caught.exception))
        self.assertIn("name", str(caught.exception))
        self.assertNotIn("removed", str(caught.exception).lower())
        self.assertNotIn("recreated", str(caught.exception).lower())

    def test_missing_update_seam_is_refused_without_mutation(self):
        job = _portal_job()

        class NoUpdate:
            def get_job(self, job_id):
                return dict(job)

        with _patched_native(NoUpdate()):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_route(
                    "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertIn("no safe in-place route update", str(caught.exception))
        self.assertEqual(job, _portal_job())

    def test_missing_getter_is_refused_without_mutation(self):
        job = _portal_job()

        class NoGetter:
            def update_job(self, job_id, updates):
                raise AssertionError("must not mutate without a readback")

        with _patched_native(NoGetter()):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_route(
                    "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertIn("cannot verify a route update", str(caught.exception))
        self.assertEqual(job, _portal_job())

    def test_absent_job_is_refused_without_mutation(self):
        job = _portal_job(id="someone-else")

        class Native:
            calls = []

            def get_job(self, job_id):
                return None

            def update_job(self, job_id, updates):
                type(self).calls.append((job_id, dict(updates)))
                return None

        with _patched_native(Native()):
            with self.assertRaises(cron_mod.CronError) as caught:
                cron_mod.HermesCronBackend().update_route(
                    "portal-route-1", NEW_PROVIDER, NEW_MODEL, NEW_EFFORT)

        self.assertIn("is not present", str(caught.exception))
        self.assertEqual(Native.calls, [])
        self.assertEqual(job, _portal_job(id="someone-else"))


class PortalRouteMigrationTests(unittest.TestCase):
    def test_owned_portal_route_is_updated_in_place(self):
        backend = _CountingBackend([_portal_job()])
        ownership = _ledger()

        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT, ownership=ownership)

        self.assertEqual(result.action, "updated")
        self.assertIn("id=portal-route-1", result.detail)
        self.assertEqual(backend.update_route_calls, 1)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        rows = backend.list_jobs()
        self.assertEqual(len(rows), 1)
        updated = rows[0]
        self.assertEqual(updated["id"], "portal-route-1")
        self.assertEqual(updated["provider"], NEW_PROVIDER)
        self.assertEqual(updated["model"], NEW_MODEL)
        self.assertEqual(updated["reasoning"], NEW_EFFORT)
        for key, value in _portal_job().items():
            if key in {"provider", "model", "reasoning"}:
                continue
            self.assertEqual(updated[key], value, key)
        self.assertEqual(updated["schedule"], "0 * * * *")
        self.assertEqual(updated["next_run_at"], "2026-11-23T13:00:00+00:00")
        self.assertEqual(updated["state"], "paused")
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["deliver"], "local")
        self.assertEqual(updated["context_from"], ["self"])
        self.assertEqual(updated["history"],
                         [{"at": "2026-11-23T12:00:00+00:00", "result": "ok"}])
        self.assertEqual(ownership["jobs"][PORTAL]["id"], "portal-route-1")

    def test_reasoning_effort_alias_is_persisted(self):
        backend = _CountingBackend([_portal_job()])

        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, reasoning_effort=NEW_EFFORT,
            ownership=_ledger())

        self.assertEqual(result.action, "updated")
        self.assertEqual(backend.list_jobs()[0]["provider"], NEW_PROVIDER)
        self.assertEqual(backend.list_jobs()[0]["model"], NEW_MODEL)
        self.assertEqual(backend.list_jobs()[0]["reasoning"], NEW_EFFORT)

    def test_conflicting_reasoning_and_effort_are_refused(self):
        backend = _CountingBackend([_portal_job()])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, "low",
                reasoning_effort="high", ownership=_ledger())

        self.assertIn("conflicting reasoning", str(caught.exception))
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual(backend.list_jobs(), [_portal_job()])

    def test_unsupported_native_field_is_refused(self):
        backend = _CountingBackend([_portal_job()])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, ownership=_ledger(),
                fallback="somewhere")

        self.assertIn("unsupported native route update field", str(caught.exception))
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual(backend.list_jobs(), [_portal_job()])

    def test_repeated_route_update_is_idempotent_and_never_recreates(self):
        backend = _CountingBackend([_portal_job()])
        ownership = _ledger()

        first = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT, ownership=ownership)
        snapshot = backend.list_jobs()
        again = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT, ownership=ownership)

        self.assertEqual(first.action, "updated")
        self.assertEqual(again.action, "already-current")
        self.assertEqual(backend.list_jobs(), snapshot)
        self.assertEqual(backend.update_route_calls, 1)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_missing_ownership_and_absent_row_reports_missing(self):
        backend = _CountingBackend([])

        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
            ownership=_empty_ledger())

        self.assertEqual(result.action, "missing")
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual(backend.list_jobs(), [])

    def test_unrecorded_same_named_row_is_foreign_and_untouched(self):
        foreign = _portal_job(id="foreign-route-9")
        backend = _CountingBackend([foreign])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
                ownership=_empty_ledger())

        self.assertIn("foreign portal job", str(caught.exception))
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual(backend.list_jobs(), [foreign])

    def test_ownership_id_mismatch_is_refused_without_recreate(self):
        backend = _CountingBackend([_portal_job(id="intruder-route-1")])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
                ownership=_ledger("portal-route-1"))

        self.assertIn("ownership mismatch", str(caught.exception))
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual([j["id"] for j in backend.list_jobs()],
                         ["intruder-route-1"])

    def test_recorded_but_absent_row_reports_missing_without_recreate(self):
        backend = _CountingBackend([])

        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
            ownership=_ledger())

        self.assertEqual(result.action, "missing")
        self.assertIn("no longer present", result.detail)
        self.assertEqual(backend.update_route_calls, 0)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))

    def test_missing_update_seam_is_refused_without_delete_or_recreate(self):
        class NoRouteSeam(_CountingBackend):
            update_route = None

        job = _portal_job()
        backend = NoRouteSeam([job])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
                ownership=_ledger())

        self.assertIn("no safe in-place route update", str(caught.exception))
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual(backend.list_jobs(), [job])

    def test_lost_response_after_high_level_commit_is_read_back_without_recreate(self):
        class CommitThenRaise(_CountingBackend):
            def update_route(self, job_id, provider, model, reasoning=None):
                super().update_route(job_id, provider, model, reasoning)
                raise RuntimeError("ambiguous transport response")

        backend = CommitThenRaise([_portal_job()])
        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
            ownership=_ledger())

        self.assertEqual(result.action, "updated")
        self.assertIn("source of truth", result.detail)
        self.assertEqual(backend.update_route_calls, 1)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        rows = backend.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "portal-route-1")
        self.assertEqual(rows[0]["provider"], NEW_PROVIDER)
        self.assertEqual(rows[0]["model"], NEW_MODEL)
        self.assertEqual(rows[0]["reasoning"], NEW_EFFORT)
        self.assertEqual(rows[0]["schedule"], "0 * * * *")
        self.assertEqual(rows[0]["next_run_at"],
                         "2026-11-23T13:00:00+00:00")
        self.assertEqual(rows[0]["state"], "paused")
        self.assertEqual(rows[0]["deliver"], "local")
        self.assertEqual(rows[0]["context_from"], ["self"])

    def test_ambiguous_high_level_failure_never_deletes_or_recreates(self):
        class NeverApplied(_CountingBackend):
            def update_route(self, job_id, provider, model, reasoning=None):
                self.update_route_calls += 1
                raise RuntimeError("ambiguous transport response")

        job = _portal_job()
        backend = NeverApplied([job])

        with self.assertRaises(cron_mod.CronError) as caught:
            cron_mod.update_portal_route(
                backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT,
                ownership=_ledger())

        self.assertIn("not applied", str(caught.exception))
        self.assertEqual(backend.update_route_calls, 1)
        self.assertEqual((backend.add_calls, backend.remove_calls), (0, 0))
        self.assertEqual(backend.list_jobs(), [job])


if __name__ == "__main__":
    unittest.main(verbosity=2)
