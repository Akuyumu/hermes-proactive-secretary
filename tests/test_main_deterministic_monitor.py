"""The deterministic monitor path must never select or run an evaluator."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_main_final_paths import T0, Workspace  # noqa: E402
from test_obligations import _env, _NativeRunner  # noqa: E402

from proactive_pilot import main as pipeline_main  # noqa: E402
from proactive_pilot import obligations  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.planner_observations import (  # noqa: E402
    PlannerObservationJournal,
    event_id,
)
from proactive_pilot.state import StateStore  # noqa: E402


class DeterministicMonitorNoEvaluatorTests(Workspace):
    def test_two_runs_never_select_evaluator_and_keep_last_inference(self):
        args = pipeline_main.build_parser().parse_args([
            "--run",
            "--monitor-deterministic",
            "--state-dir", str(self.store.root),
            "--policy", str(self.root / "policy.json"),
            "--preferences", str(self.root / "preferences.json"),
            "--seeded-clock", T0.isoformat(),
        ])
        state = {"last_inference": {"at": T0.isoformat(), "fingerprint": "sentinel"}}
        self.store.save("notify_state.json", state)
        calls = []

        def forbidden_select_evaluator(*a, **kw):
            calls.append((a, kw))
            raise AssertionError("select_evaluator is forbidden on the deterministic path")

        with mock.patch.object(pipeline_main, "select_evaluator",
                               forbidden_select_evaluator), \
                mock.patch.object(pipeline_main, "_run_deterministic_monitor",
                                  wraps=pipeline_main._run_deterministic_monitor) as monitor:
            results = [pipeline_main.run_once(args), pipeline_main.run_once(args)]

        self.assertEqual(calls, [])
        self.assertEqual(monitor.call_count, 2)
        self.assertEqual(results, [0, 0])
        reread = self.store.load_notify_state(strict=True)
        self.assertEqual(reread["last_inference"],
                         {"at": T0.isoformat(), "fingerprint": "sentinel"})

    def test_synthetic_obligation_survives_pipeline_and_journal_reopen(self):
        subject = "Action required: confirm your order INV-48213 by 2026-10-10"
        runner = _NativeRunner({1: [_env("m1", subject)]})
        actual = obligations.collect_himalaya_native
        calls = []

        def injecting_runner(*args, **kwargs):
            calls.append((args, dict(kwargs)))
            kwargs["runner"] = runner
            return actual(*args, **kwargs)

        args = pipeline_main.build_parser().parse_args([
            "--run",
            "--monitor-deterministic",
            "--state-dir", str(self.store.root),
            "--policy", str(self.root / "policy.json"),
            "--preferences", str(self.root / "preferences.json"),
            "--seeded-clock", T0.isoformat(),
        ])
        (self.root / "policy.json").write_text(json.dumps({
            "obligations_enabled": True,
            "mail_accounts": ["formal"],
            "mail_enabled": False,
            "calendar_enabled": False,
            "tasks_enabled": False,
            "feeds_enabled": False,
            "preparation_enabled": False,
            "adaptive_mode": "disabled",
        }), encoding="utf-8")
        (self.root / "preferences.json").write_text(json.dumps({
            "schema": 1, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": False},
        }), encoding="utf-8")

        out = io.StringIO()
        with mock.patch.object(obligations, "collect_himalaya_native",
                               side_effect=injecting_runner), \
                contextlib.redirect_stdout(out):
            rc = pipeline_main.run_once(args)
        self.assertEqual(rc, 0)
        self.assertTrue(calls, "the real obligations collector must run")
        self.assertEqual(calls[0][0][0], "formal")
        self.assertNotIn("INV-48213", out.getvalue())

        reopened = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        stored = reopened.load("obligations.json", {}, strict=True)
        records = [
            rec for rec in stored["records"].values()
            if rec.get("ref_number") == "inv48213"
            and rec.get("status") == "pending"
        ]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["due_date"], "2026-10-10")
        self.assertEqual(record["category"], "order_action")
        self.assertIn("mail:formal:m1", record["evidence"])

        source = event_id({
            "kind": "obligation",
            "source": "obligation:formal",
            "ref": f"obligation:formal:{record['object_key']}",
        })
        journal = PlannerObservationJournal(reopened, pipeline_main._OBSERVATION_SCOPE)
        pending = journal.pending()
        matches = [item for item in pending if item["observation_id"] == source]
        self.assertEqual(len(matches), 1, pending)
        self.assertEqual(matches[0]["kind"], "obligation")
        self.assertEqual(matches[0]["source"], "obligation:formal")
        self.assertTrue(
            matches[0]["ref"].endswith(record["object_key"]), matches[0]["ref"])


class DeterministicMonitorRestartTests(Workspace):
    SUBJECT = "Action required: confirm your order INV-48213 by 2026-10-10"

    def setUp(self):
        super().setUp()
        (self.root / "policy.json").write_text(json.dumps({
            "obligations_enabled": True,
            "mail_accounts": ["formal"],
            "mail_enabled": False,
            "calendar_enabled": False,
            "tasks_enabled": False,
            "feeds_enabled": False,
            "preparation_enabled": False,
            "adaptive_mode": "disabled",
        }), encoding="utf-8")
        (self.root / "preferences.json").write_text(json.dumps({
            "schema": 1, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": False},
        }), encoding="utf-8")

    def _argv(self):
        return [
            "--run",
            "--monitor-deterministic",
            "--state-dir", str(self.store.root),
            "--policy", str(self.root / "policy.json"),
            "--preferences", str(self.root / "preferences.json"),
            "--seeded-clock", T0.isoformat(),
        ]

    def _runner(self):
        return _NativeRunner({1: [_env("m1", self.SUBJECT)]})

    @staticmethod
    def _transport(runner):
        actual = obligations.collect_himalaya_native

        def injecting_runner(*args, **kwargs):
            kwargs["runner"] = runner
            return actual(*args, **kwargs)

        return mock.patch.object(obligations, "collect_himalaya_native",
                                 side_effect=injecting_runner)

    def _reopen(self):
        return StateStore(self.data / "var", SeededClock(T0.isoformat()))

    def _pending(self, store):
        return PlannerObservationJournal(
            store, pipeline_main._OBSERVATION_SCOPE).pending()

    def test_journal_write_failure_is_visible_and_preserves_no_cursor(self):
        def fail_journal_write(self, payload):
            raise OSError("simulated journal write failure")

        stdout, stderr = io.StringIO(), io.StringIO()
        with self._transport(self._runner()), \
                mock.patch.object(PlannerObservationJournal, "_save_locked",
                                  fail_journal_write), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = pipeline_main.main(self._argv())

        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("planner observation journal failed", stderr.getvalue())

        failed = self._reopen()
        stored = obligations.load_obligations(failed)
        self.assertEqual(stored["records"], {})
        self.assertEqual(stored["cursors"], {})
        self.assertEqual(stored["last_scan_at"], "")
        self.assertEqual(self._pending(failed), [])

        stdout = io.StringIO()
        with self._transport(self._runner()), contextlib.redirect_stdout(stdout):
            rerun_rc = pipeline_main.main(self._argv())
        self.assertEqual(rerun_rc, 0)
        self.assertEqual(stdout.getvalue(), "")

        reopened = self._reopen()
        records = [
            rec for rec in obligations.load_obligations(reopened)["records"].values()
            if rec.get("ref_number") == "inv48213"
            and rec.get("status") == "pending"
        ]
        self.assertEqual(len(records), 1)
        record = records[0]
        source = event_id({
            "kind": "obligation",
            "source": "obligation:formal",
            "ref": f"obligation:formal:{record['object_key']}",
        })
        pending = self._pending(reopened)
        self.assertEqual(len(pending), 1, pending)
        self.assertEqual(pending[0]["observation_id"], source)
        self.assertEqual(pending[0]["kind"], "obligation")
        self.assertEqual(pending[0]["source"], "obligation:formal")
        self.assertTrue(pending[0]["ref"].endswith(record["object_key"]))

    def test_crash_after_journal_write_before_obligation_commit_replays_once(self):
        def crash_before_commit(store, data):
            raise SystemExit("simulated crash before obligation commit")

        stdout = io.StringIO()
        with self._transport(self._runner()), \
                mock.patch.object(obligations, "save_obligations",
                                  side_effect=crash_before_commit), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                pipeline_main.main(self._argv())
        self.assertEqual(stdout.getvalue(), "")

        crashed = self._reopen()
        self.assertEqual(obligations.load_obligations(crashed)["records"], {})
        before = self._pending(crashed)
        self.assertEqual(len(before), 1, before)
        before_record = before[0]
        self.assertFalse(before_record["acknowledged"])

        stdout = io.StringIO()
        with self._transport(self._runner()), contextlib.redirect_stdout(stdout):
            rc = pipeline_main.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")

        reopened = self._reopen()
        after = self._pending(reopened)
        self.assertEqual(len(after), 1, after)
        self.assertEqual(after[0]["observation_id"],
                         before_record["observation_id"])
        self.assertEqual(after[0]["version_id"], before_record["version_id"])
        self.assertEqual(after[0]["ref"], before_record["ref"])

        data = obligations.load_obligations(reopened)
        self.assertIn("formal", data["cursors"])
        self.assertTrue(data["cursors"]["formal"]["bootstrap_done"])
        self.assertTrue(data["last_scan_at"])
        records = [
            rec for rec in data["records"].values()
            if rec.get("ref_number") == "inv48213"
            and rec.get("status") == "pending"
        ]
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
