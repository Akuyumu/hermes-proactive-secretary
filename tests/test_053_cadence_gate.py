"""Contract tests for the plugin-owned Portal cadence gate.

No live scheduler, no network, no model: the store lives in a temporary data
root, and the one host write path is a stand-in module injected into
``sys.modules`` for the duration of a test.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_package(name: str = "hermes_proactive_secretary_cadence_qa"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
cadence = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cadence")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


class _Store:
    """Minimal scheduler stand-in with the fields the gate inspects."""

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.updates = []

    def list_jobs(self):
        return [dict(row) for row in self.rows]

    def update(self, job_id, updates):
        self.updates.append((job_id, dict(updates)))
        for row in self.rows:
            if row.get("id") == job_id:
                row.update(updates)
        return None


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._keys = ("HERMES_HOME", paths.DATA_ENV, "PYTHONPATH")
        self._saved = {key: os.environ.get(key) for key in self._keys}
        self.addCleanup(self._restore)
        home = self.root / "home"
        home.mkdir()
        os.environ["HERMES_HOME"] = str(home)
        os.environ[paths.DATA_ENV] = str(self.root / "data")

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def row(self, **overrides):
        row = {"id": "job-1", "name": "hermes-proactive-secretary-portal",
               "schedule": "0 */3 * * *", "state": "active",
               "context_from": "self", "script": ""}
        row.update(overrides)
        return row


class IntervalParsingTests(unittest.TestCase):
    def test_accepts_minutes_and_durations(self):
        self.assertEqual(cadence.parse_interval("90"), 90)
        self.assertEqual(cadence.parse_interval(45), 45)
        self.assertEqual(cadence.parse_interval("6h"), 360)
        self.assertEqual(cadence.parse_interval("90m"), 90)

    def test_rejects_out_of_range_and_nonsense(self):
        for value in ("0", "5m", "25h", "abc", "", None, True):
            with self.assertRaises(ValueError):
                cadence.parse_interval(value)


class StoreTests(_Isolated):
    def test_missing_store_is_the_default_and_not_present(self):
        plan = cadence.read_plan()
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["present"])
        self.assertEqual(plan["interval_minutes"], cadence.DEFAULT_MINUTES)

    def test_write_is_idempotent(self):
        first = cadence.write_plan("6h")
        self.assertTrue(first["changed"])
        again = cadence.write_plan("360")
        self.assertFalse(again["changed"])
        self.assertEqual(again["revision"], first["revision"])

    def test_corrupt_store_is_reported_and_never_overwritten(self):
        path = cadence.plan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertFalse(cadence.read_plan()["ok"])
        with self.assertRaises(ValueError):
            cadence.write_plan("6h")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_store_file_is_owner_only(self):
        cadence.write_plan("3h")
        self.assertEqual(cadence.plan_path().stat().st_mode & 0o777, 0o600)


class DueTests(_Isolated):
    def test_never_woken_is_due(self):
        due, remaining = cadence.is_due({"ok": True, "interval_minutes": 360})
        self.assertTrue(due)
        self.assertEqual(remaining, 0)

    def test_window_respected(self):
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        plan = {"ok": True, "interval_minutes": 360,
                "last_wake": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        due, remaining = cadence.is_due(plan, now)
        self.assertFalse(due)
        self.assertEqual(remaining, 240)
        plan["last_wake"] = (now - timedelta(hours=6, minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(cadence.is_due(plan, now)[0])

    def test_second_stamp_in_the_same_window_is_skipped(self):
        cadence.write_plan("6h")
        now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        first = cadence.stamp_wake(cadence.read_plan(), now)
        self.assertTrue(first["stamped"])
        second = cadence.stamp_wake(cadence.read_plan(), now + timedelta(minutes=15))
        self.assertFalse(second["stamped"])
        self.assertEqual(second["last_wake"], first["last_wake"])
        later = cadence.stamp_wake(cadence.read_plan(), now + timedelta(hours=7))
        self.assertTrue(later["stamped"])


class EffectiveTests(_Isolated):
    def test_gated_row_is_detected(self):
        store = _Store([self.row(schedule=cadence.TICK_EXPR,
                                 script="/scripts/" + cadence.GATE_RUNNER)])
        view = cadence.effective(store)
        self.assertTrue(view["gated"])
        self.assertEqual(view["rows"], 1)

    def test_plain_schedule_is_not_gated(self):
        view = cadence.effective(_Store([self.row()]))
        self.assertFalse(view["gated"])


class ConvertTests(_Isolated):
    def setUp(self):
        super().setUp()
        self.host = types.ModuleType("cron.jobs")

        def update_job(job_id, updates):
            self.store.update(job_id, updates)
            return None

        self.host.update_job = update_job
        self.host.__dict__["_qa"] = True
        self._cron_pkg = sys.modules.get("cron")
        self._jobs_mod = sys.modules.get("cron.jobs")
        sys.modules["cron"] = types.ModuleType("cron")
        sys.modules["cron"].jobs = self.host
        sys.modules["cron.jobs"] = self.host
        self.addCleanup(self._restore_host)

    def _restore_host(self):
        for name, module in (("cron.jobs", self._jobs_mod), ("cron", self._cron_pkg)):
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_refuses_busy_or_paused_jobs_without_writing(self):
        for row in (self.row(state="paused"), self.row(pending_slot="s1"),
                    self.row(quota_hold_until="2026-11-25T00:00:00Z")):
            self.store = _Store([row])
            result = cadence.convert(self.store, "6h")
            self.assertFalse(result["ok"], row)
            self.assertEqual(self.store.updates, [], row)

    def test_convert_preserves_id_and_continuity(self):
        self.store = _Store([self.row()])
        result = cadence.convert(self.store, "6h")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["interval_minutes"], 360)
        row = self.store.rows[0]
        self.assertEqual(row["schedule"], cadence.TICK_EXPR)
        self.assertEqual(cadence._row_script(row), cadence.GATE_RUNNER)
        self.assertEqual(row["context_from"], "self")
        self.assertEqual(cadence.read_plan()["interval_minutes"], 360)

    def test_duplicate_portal_rows_are_refused(self):
        self.store = _Store([self.row(), self.row(id="job-2")])
        self.assertFalse(cadence.convert(self.store, "3h")["ok"])
        self.assertEqual(self.store.updates, [])


class RunnerTests(_Isolated):
    def _runner(self):
        path = ROOT / "runners" / cadence.GATE_RUNNER
        spec = importlib.util.spec_from_file_location("qa_portal_gate", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["qa_portal_gate"] = module
        spec.loader.exec_module(module)
        # The plugin-enabled gate reads the installed tree; a source checkout is
        # not installed, so the test supplies the same answer the host gives.
        module._plugin_enabled = lambda: True
        return module

    def _emit(self, module, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(argv)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        return code, json.loads(lines[-1])

    def test_status_reports_due_without_stamping(self):
        module = self._runner()
        code, payload = self._emit(module, ["--status"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["due"])
        self.assertEqual(payload["last_wake"], "")

    def test_due_wake_stamps_then_silences_the_next_tick(self):
        module = self._runner()
        code, first = self._emit(module, [])
        self.assertEqual(code, 0)
        self.assertTrue(first["wakeAgent"])
        code, second = self._emit(module, [])
        self.assertFalse(second["wakeAgent"])
        self.assertEqual(cadence.read_plan()["last_wake"] != "", True)


class CliApplyFlagTests(_Isolated):
    """``--apply`` must behave the same as the ``apply`` action."""

    def _cli(self):
        return importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")

    def _run(self, argv):
        import argparse
        cli = self._cli()
        parser = argparse.ArgumentParser(prog="hermes secretary")
        sub = parser.add_subparsers(dest="secretary_command")
        wrapper = sub.add_parser("secretary")
        cli.register_cli(wrapper)
        ns = parser.parse_args(["secretary", *argv])
        calls = []
        original = cadence.convert

        def fake(backend, minutes, **kwargs):
            calls.append((minutes, kwargs))
            return {"ok": True, "status": "gated", "interval_minutes": 360,
                    "job_id": "job-1"}

        cadence.convert = fake
        try:
            code = cli.cadence_command(ns, backend=_Store([self.row()]))
        finally:
            cadence.convert = original
        return code, calls

    def test_flag_form_applies(self):
        code, calls = self._run(["cadence", "--set", "6h", "--apply", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "the flag form must convert, not just report")
        self.assertEqual(calls[0][0], "6h")

    def test_action_form_applies(self):
        code, calls = self._run(["cadence", "apply", "--set", "6h", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_plain_set_does_not_convert(self):
        code, calls = self._run(["cadence", "--set", "6h", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])


class PromptGuidanceTests(unittest.TestCase):
    """The wake must treat its own history as evidence, not as an agenda."""

    def setUp(self):
        self.portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")

    def test_earlier_answer_is_not_an_agenda(self):
        text = self.portal.PROMPT
        self.assertIn("never an agenda", text)
        self.assertIn("outranks that earlier answer", text)

    def test_closed_topics_do_not_return(self):
        text = self.portal.PROMPT
        self.assertIn("signals that a topic is finished", text)
        self.assertIn("do not restate countdowns", text)
        self.assertIn("Keep continuity for useful state", text)

    def test_prompt_has_no_exam_specific_rule(self):
        # The guidance must stay general: no per-topic hardcoding in the wake.
        for word in ("exam", "MA3005", "MA3002", "notes"):
            self.assertNotIn(word.lower(), self.portal.PROMPT.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
