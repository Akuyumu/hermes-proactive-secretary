"""Independent acceptance tests for the portal observation journal actions.

These tests exercise the real registered tool handler and the real
``PlannerObservationJournal`` against the same ``paths.state_dir()`` the
production consumer opens. Only the fixture clock seam is pinned; the journal,
``StateStore`` locking and JSON persistence are real.

Every profile home/data root is a TemporaryDirectory under the ambient TMPDIR.
No host credentials, live cron jobs, network or repository state are touched.
The expectations are the user contract; a failure here is a product defect, not
a reason to weaken the test.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Importing the package executes dataclasses; keep the repository free of
# __pycache__ as a side effect of this acceptance module.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    # Dataclasses resolve their module through sys.modules during exec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_secretary_observations_qa")
cron = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
notices = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_portal_notices")

from proactive_pilot import ownerpaths  # noqa: E402
from proactive_pilot import portal_notices as core  # noqa: E402
from proactive_pilot import state as state_mod  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.planner_observations import (  # noqa: E402
    PlannerObservationJournal,
    event_id,
    version_id,
)
from proactive_pilot.state import StateStore  # noqa: E402

OBSERVATION_SCOPE = "owner-local"
ALLOWED_FIELDS = {
    "index",
    "kind",
    "source",
    "ref",
    "phase",
    "observed_at",
    "observation_id",
    "version_id",
}


class _Ctx:
    """Fake host context that captures the real registration arguments."""

    def __init__(self, *, profile_name="A", execution=None):
        self.profile_name = profile_name
        self._execution = execution
        self.registered: list[dict] = []

    def current_cron_execution(self):
        return self._execution

    def register_tool(self, name, category, schema, handler, **kwargs):
        self.registered.append({
            "name": name,
            "category": category,
            "schema": schema,
            "handler": handler,
            "kwargs": kwargs,
        })


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.home = self.root / "A" / "home"
        self.data = self.root / "A" / "data"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)

        self._keys = ("HERMES_HOME", paths.DATA_ENV)
        self._saved_env = {key: os.environ.get(key) for key in self._keys}
        self.addCleanup(self._restore_env)
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ[paths.DATA_ENV] = str(self.data)

        self.ledger_id = "job-portal-1"
        self.clock = SeededClock(
            "2026-10-20T12:00:00+00:00", timezone_name="UTC")

        self._native_a = types.SimpleNamespace(
            get_hermes_home=lambda: self.home,
            get_routing_process_hermes_home=lambda: self.home,
        )
        self._owner_patch = mock.patch.object(
            ownerpaths, "_native_constants", return_value=self._native_a
        )
        self._owner_patch.start()
        self.addCleanup(self._owner_patch.stop)

        # This must be exactly the directory the production stakeholder opens.
        self.state = paths.state_dir()
        self.assertEqual(self.state, self.data / "var")

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _execution(self, *, job_id=None, job_name=None, execution_id="exec-1",
                   profile="A"):
        return types.SimpleNamespace(
            job_id=self.ledger_id if job_id is None else job_id,
            job_name=cron.PORTAL_JOB_NAME if job_name is None else job_name,
            execution_id=execution_id,
            profile=profile,
        )

    def _seed_ledger(self, *, job_id=None, name=None):
        job_name = name or cron.PORTAL_JOB_NAME
        cron.save_job_ownership(
            paths.job_ownership_path(),
            {
                "schema": 1,
                "plugin": cron.OWNED_PLUGIN,
                "jobs": {job_name: {
                    "id": self.ledger_id if job_id is None else job_id,
                    "name": job_name,
                }},
            },
        )

    def _journal(self, clock=None):
        store = StateStore(paths.state_dir(), clock or self.clock)
        return PlannerObservationJournal(store, OBSERVATION_SCOPE)

    def _seed_observation(self, observed_at, *, ref, kind="task",
                          source="owner", phase="due_today", version="v1",
                          extra=None):
        store = StateStore(
            paths.state_dir(), SeededClock(observed_at, timezone_name="UTC"))
        journal = PlannerObservationJournal(store, OBSERVATION_SCOPE)
        candidate = {
            "kind": kind,
            "source": source,
            "ref": ref,
            "phase": phase,
            "version": version,
        }
        if extra:
            candidate.update(extra)
        journal.record([candidate])
        return {
            "observation_id": event_id(candidate),
            "version_id": version_id(candidate),
            "kind": kind,
            "source": source,
            "ref": ref,
            "phase": phase,
            "observed_at": observed_at,
        }

    def _register(self, ctx):
        result = notices.register_tool(ctx)
        registered = getattr(ctx, "registered", [])
        handler = registered[0]["handler"] if registered else None
        return result, handler

    def _invoke(self, ctx, args):
        # Pin only the wrapper's own Clock seam; registration, validation and
        # the real store/journal path stay intact.  Unsupported hosts do not
        # register, so exercise the bounded handler directly to see the denial.
        with mock.patch("proactive_pilot.clock.Clock", return_value=self.clock):
            result, handler = self._register(ctx)
            if handler is None:
                handler = notices.tool_handler(ctx)
            return result, json.loads(handler(args))

    @staticmethod
    def _serialized(payload):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ObservationListingTests(_Base):
    def test_authenticated_observations_are_bounded_ordered_and_pending_only(self):
        self._seed_ledger()
        seeded = [
            self._seed_observation(
                f"2026-10-20T12:{index:02d}:00+00:00", ref=f"ref-{index}")
            for index in range(7)
        ]

        result, payload = self._invoke(
            _Ctx(profile_name="A", execution=self._execution()),
            {"action": "observations"},
        )

        self.assertTrue(result["portal_notice_tool"])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "ok")
        observations = payload["observations"]
        # A hard cap of five even though seven distinct entries are pending.
        self.assertEqual(len(observations), 5)
        self.assertEqual([item["index"] for item in observations],
                         [0, 1, 2, 3, 4])
        self.assertEqual([item["ref"] for item in observations],
                         [entry["ref"] for entry in seeded[:5]])

        for index, item in enumerate(observations):
            with self.subTest(index=index):
                self.assertEqual(set(item), ALLOWED_FIELDS)
                expected = seeded[index]
                self.assertEqual(item["index"], index)
                self.assertEqual(item["kind"], expected["kind"])
                self.assertEqual(item["source"], expected["source"])
                self.assertEqual(item["ref"], expected["ref"])
                self.assertEqual(item["phase"], expected["phase"])
                self.assertEqual(item["observed_at"], expected["observed_at"])
                self.assertEqual(item["observation_id"],
                                 expected["observation_id"])
                self.assertEqual(item["version_id"], expected["version_id"])

        returned = {item["ref"] for item in observations}
        self.assertNotIn("ref-5", returned)
        self.assertNotIn("ref-6", returned)

    def test_observations_expose_no_body_destination_or_raw_store_contents(self):
        self._seed_ledger()
        body_marker = "SECRET-BODY-MARKER-7f3a"
        dest_marker = "SECRET-DESTINATION-MARKER-9c1d"

        # The same private store already holds a notice with both markers, so a
        # response that leaks any store contents would expose them here.
        store = StateStore(paths.state_dir(), self.clock)
        store.save(core.PORTAL_NOTICES_ARTIFACT, {
            "schema": 1,
            "pending": [{
                "message": body_marker,
                "destination": dest_marker,
            }],
            "seen": {},
            "executions": {},
        })
        store.save(core.NOTIFY_STATE_ARTIFACT, {
            "schema": 1,
            "items": {},
            "delivered": [{"message": body_marker, "destination": dest_marker}],
            "health": {},
        })

        self._seed_observation(
            "2026-10-20T12:00:00+00:00",
            ref="ref-bounded",
            extra={"message": body_marker, "destination": dest_marker},
        )

        _result, payload = self._invoke(
            _Ctx(profile_name="A", execution=self._execution()),
            {"action": "observations"},
        )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(len(payload["observations"]), 1)
        item = payload["observations"][0]
        self.assertEqual(set(item), ALLOWED_FIELDS)
        for forbidden in ("message", "destination", "body", "preview"):
            self.assertNotIn(forbidden, item)

        serialized = self._serialized(payload)
        self.assertNotIn(body_marker, serialized)
        self.assertNotIn(dest_marker, serialized)

        # Prove the markers really are elsewhere in the shared store and that
        # the journal itself never persisted them either.
        portal_text = (self.state / core.PORTAL_NOTICES_ARTIFACT).read_text(
            encoding="utf-8")
        self.assertIn(body_marker, portal_text)
        self.assertIn(dest_marker, portal_text)
        journal_text = self._journal().path.read_text(encoding="utf-8")
        self.assertNotIn(body_marker, journal_text)
        self.assertNotIn(dest_marker, journal_text)

    def test_already_acknowledged_entry_is_not_returned_later(self):
        self._seed_ledger()
        acknowledged = self._seed_observation(
            "2026-10-20T12:00:00+00:00", ref="ref-acked")
        still_pending = self._seed_observation(
            "2026-10-20T12:01:00+00:00", ref="ref-pending")

        self._journal().acknowledge(
            acknowledged["observation_id"], acknowledged["version_id"])

        _result, payload = self._invoke(
            _Ctx(profile_name="A", execution=self._execution()),
            {"action": "observations"},
        )

        self.assertTrue(payload["ok"], payload)
        observations = payload["observations"]
        self.assertEqual([item["ref"] for item in observations],
                         [still_pending["ref"]])
        ids = {item["observation_id"] for item in observations}
        self.assertNotIn(acknowledged["observation_id"], ids)


class ObservationAckTests(_Base):
    def test_exact_id_and_version_succeeds_and_repeat_is_idempotent(self):
        self._seed_ledger()
        seeded = self._seed_observation(
            "2026-10-20T12:00:00+00:00", ref="ref-ack-exact")
        args = {
            "action": "observations_ack",
            "observation_id": seeded["observation_id"],
            "version_id": seeded["version_id"],
        }
        ctx = _Ctx(profile_name="A", execution=self._execution())

        _result, first = self._invoke(ctx, dict(args))
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["status"], "ok")
        self.assertIs(first["acknowledged"], True)
        self.assertEqual(first["observation_id"], seeded["observation_id"])
        self.assertEqual(first["version_id"], seeded["version_id"])
        self.assertTrue(first["acknowledged_at"])

        _result, second = self._invoke(ctx, dict(args))
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["status"], "ok")
        self.assertIs(second["acknowledged"], True)
        self.assertEqual(second["observation_id"], seeded["observation_id"])
        self.assertEqual(second["version_id"], seeded["version_id"])
        self.assertEqual(second["acknowledged_at"], first["acknowledged_at"])

        _result, listing = self._invoke(
            _Ctx(profile_name="A", execution=self._execution()),
            {"action": "observations"},
        )
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(listing["observations"], [])

    def test_ack_refuses_stale_unknown_blank_missing_and_index_only(self):
        self._seed_ledger()
        original = self._seed_observation(
            "2026-10-20T12:00:00+00:00", ref="ref-revised", version="v1")
        revised = self._seed_observation(
            "2026-10-20T12:01:00+00:00", ref="ref-revised", version="v2")

        # Same event identity, newer revision: the old version is stale.
        self.assertEqual(original["observation_id"], revised["observation_id"])
        self.assertNotEqual(original["version_id"], revised["version_id"])

        ctx = _Ctx(profile_name="A", execution=self._execution())
        cases = [
            (
                "stale-version",
                {
                    "action": "observations_ack",
                    "observation_id": original["observation_id"],
                    "version_id": original["version_id"],
                },
                "stale_observation",
            ),
            (
                "unknown-id",
                {
                    "action": "observations_ack",
                    "observation_id": "unknown-observation-id",
                    "version_id": "unknown-version-id",
                },
                "observation_not_found",
            ),
            (
                "blank-version",
                {
                    "action": "observations_ack",
                    "observation_id": revised["observation_id"],
                    "version_id": "",
                },
                "invalid_args",
            ),
            (
                "missing-version",
                {
                    "action": "observations_ack",
                    "observation_id": revised["observation_id"],
                },
                "invalid_args",
            ),
            (
                "index-only",
                {"action": "observations_ack", "index": 0},
                "invalid_args",
            ),
        ]

        for label, args, expected in cases:
            with self.subTest(label=label):
                _result, payload = self._invoke(ctx, args)
                self.assertFalse(payload["ok"], payload)
                self.assertFalse(payload["accepted"], payload)
                self.assertEqual(payload["status"], expected)
                self.assertTrue(payload["reason"])

        _result, stale = self._invoke(ctx, cases[0][1])
        self.assertEqual(stale["status"], "stale_observation")
        self.assertIn("latest", stale["reason"].lower())


class ObservationAuthenticationTests(_Base):
    def test_foreground_and_unsupported_hosts_refuse_both_new_actions(self):
        actions = [
            ("observations", {"action": "observations"}),
            (
                "observations_ack",
                {
                    "action": "observations_ack",
                    "observation_id": "a" * 32,
                    "version_id": "b" * 32,
                },
            ),
        ]
        contexts = [
            ("foreground", _Ctx(profile_name="A", execution=None),
             "not_cron_execution"),
            ("unsupported-host", types.SimpleNamespace(profile_name="A"),
             "unsupported_host"),
            ("foreign-job",
             _Ctx(profile_name="A",
                  execution=self._execution(job_name="other-job")),
             "foreign_job"),
        ]

        for action_label, args in actions:
            for ctx_label, ctx, expected in contexts:
                with self.subTest(action=action_label, context=ctx_label):
                    with mock.patch.object(
                        state_mod,
                        "StateStore",
                        side_effect=AssertionError(
                            "StateStore must not be created for a refusal"),
                    ):
                        _result, payload = self._invoke(ctx, dict(args))
                    self.assertFalse(payload["ok"], payload)
                    self.assertFalse(payload["accepted"], payload)
                    self.assertEqual(payload["status"], expected)
                    self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
