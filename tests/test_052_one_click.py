"""Contract tests for the one-click ``setup --with-portal`` entry point.

The reusable isolation, parser, capture and offline-check fakes live in
``test_portal_lifecycle``.  This module imports that file as a module and
subclasses its ``_Isolated`` fixture, so ``unittest`` does not collect the
shared base class twice.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import test_portal_lifecycle as lifecycle

sc = lifecycle.sc
cron_mod = lifecycle.cron_mod

PORTAL = lifecycle.PORTAL
MONITORING = lifecycle.MONITORING
REMINDER = lifecycle.REMINDER

_THREE_HOUR_VALUES = {
    "180", "180m", "180min", "180mins", "180minutes",
    "3", "3h", "3hr", "3hrs", "3hour", "3hours",
    "every 3h", "every 3 hours", "every three hours",
}


def _items(value):
    """Yield every key/value pair in a nested JSON-like structure."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _items(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _items(item)


class OneClickPortalSetupTests(lifecycle._Isolated):
    def _setup_portal(self, backend, *extra):
        ns = lifecycle._parse("setup", "--with-portal", "--json", *extra)
        original = sc._run_offline_checks
        sc._run_offline_checks = lifecycle._fast_checks
        self.addCleanup(setattr, sc, "_run_offline_checks", original)
        rc, payload = lifecycle._capture_stdout(
            sc.setup_command, ns, backend=backend)
        return rc, payload, ns

    def _portal_jobs(self, backend):
        return [job for job in backend.list_jobs()
                if job.get("name") == PORTAL]

    def _foreign_portal(self, job_id="foreign-portal"):
        return {
            "id": job_id,
            "name": PORTAL,
            "schedule": "0 0 1 1 *",
            "script": "someone_elses_runner.py",
            "deliver": "origin",
            "workdir": "/elsewhere",
            "mode": "no-agent",
            "state": "active",
            "last_run": "",
        }

    def _assert_context_from_self(self, *mappings):
        for mapping in mappings:
            for key, value in _items(mapping):
                if "context" in key.lower() and value == ["self"]:
                    return
        self.fail(f"portal does not record context_from=self: {mappings!r}")

    def _assert_three_hour_default(self, *mappings):
        for mapping in mappings:
            for key, value in _items(mapping):
                text = str(value).strip().lower()
                if "*/3" in text:
                    return
                if text in _THREE_HOUR_VALUES:
                    key_l = key.lower()
                    if (key_l in {"window", "window_hours", "hours",
                                  "interval", "interval_hours", "schedule",
                                  "period", "frequency"}
                            or text not in {"3", "180"}):
                        return
        self.fail(f"portal does not record the default 3h window: {mappings!r}")

    def test_fresh_setup_creates_core_and_one_portal_in_same_receipt(self):
        backend = cron_mod.InMemoryCronBackend()

        rc, payload, _ = self._setup_portal(backend)
        self.assertEqual(rc, 0, payload)

        names = [job.get("name") for job in backend.list_jobs()]
        self.assertTrue({MONITORING, REMINDER}.issubset(names), names)
        self.assertEqual(names.count(PORTAL), 1, names)

        ledger = self.ledger()["jobs"]
        self.assertEqual(set(ledger), {MONITORING, REMINDER, PORTAL})

        job = self._portal_jobs(backend)[0]
        record = ledger[PORTAL]
        self._assert_context_from_self(job, record)
        self._assert_three_hour_default(job, record)

    def test_repeat_setup_with_changed_preference_leaves_portal_row_unchanged(self):
        backend = cron_mod.InMemoryCronBackend()
        rc, payload, _ = self._setup_portal(backend)
        self.assertEqual(rc, 0, payload)

        before = self._portal_jobs(backend)[0]

        rc, payload, _ = self._setup_portal(
            backend, "--portal-schedule", "0 9 * * *")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(len(self._portal_jobs(backend)), 1)
        self.assertEqual(self._portal_jobs(backend)[0], before)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["id"], before["id"])

    def test_repeat_setup_leaves_paused_portal_paused_and_reports_failure(self):
        backend = cron_mod.InMemoryCronBackend()
        rc, payload, _ = self._setup_portal(backend)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(self.pause(backend)[0], 0)

        job = self._portal_jobs(backend)[0]
        self.assertEqual(job.get("state"), "paused")
        before = dict(job)

        rc, payload, _ = self._setup_portal(backend)
        status = str(payload.get("status", "")).lower()
        self.assertTrue(
            rc != 0 or status in {"error", "failed", "failure", "paused"},
            f"setup did not report the paused portal as a failure: rc={rc}, "
            f"payload={payload!r}")
        self.assertEqual(self._portal_jobs(backend)[0], before)
        self.assertEqual(self._portal_jobs(backend)[0].get("state"), "paused")

    def test_foreign_portal_same_name_is_refused_and_untouched(self):
        foreign = self._foreign_portal()
        backend = cron_mod.InMemoryCronBackend([foreign])

        rc, payload, _ = self._setup_portal(backend)
        self.assertNotEqual(rc, 0, payload)
        self.assertEqual(self._portal_jobs(backend), [foreign])
        self.assertNotIn(PORTAL, self.ledger()["jobs"])

    def test_corrupt_ownership_is_refused_and_foreign_portal_untouched(self):
        foreign = self._foreign_portal("foreign-corrupt")
        backend = cron_mod.InMemoryCronBackend([foreign])

        ownership = lifecycle.paths.job_ownership_path()
        ownership.parent.mkdir(parents=True, exist_ok=True)
        ownership.write_text("{not valid ownership json", encoding="utf-8")

        rc, payload, _ = self._setup_portal(backend)
        self.assertNotEqual(rc, 0, payload)
        self.assertEqual(self._portal_jobs(backend), [foreign])


if __name__ == "__main__":
    unittest.main(verbosity=2)
