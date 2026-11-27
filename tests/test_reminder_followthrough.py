"""Stage 2: one follow-through for an emitted requested reminder.

The reminder registry is synthetic. The existing follow-up store, its lock and
its persistence are real. Evaluation rides ``portal_rows`` (the portal's existing
due-follow-up read); no timer, job or sender is created.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import acknowledgements as A  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

UTC = timezone.utc


class ReminderFollowthroughTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root, Clock())
        self.now = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)

    def tearDown(self):
        self._tmp.cleanup()

    def _emitted(self, rid="rem-1", *, due_delta=timedelta(hours=-1)):
        self.store.save("reminders.json", {
            "schema": 1,
            "reminders": {
                rid: {
                    "id": rid,
                    "kind": "time",
                    "status": "emitted",
                    "text": "call the dentist",
                    "provenance": "owner request",
                    "due": (self.now + due_delta).isoformat(),
                    "late_grace_minutes": 10,
                    "emitted_at": (self.now + due_delta).isoformat(),
                }
            },
        })

    def test_one_followup_at_most_and_second_evaluation_is_silent(self):
        self._emitted()
        rows = F.portal_rows(self.store, now=self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "one_shot")
        self.assertEqual(F.get(self.store, rows[0]["id"])["source_ref"],
                         "reminder:rem-1")

        again = F.portal_rows(self.store, now=self.now + timedelta(minutes=5))
        self.assertEqual([row["id"] for row in again], [rows[0]["id"]])
        self.assertEqual(F.summary(self.store)["total"], 1)

        event_key = F.notice_event_key(rows[0]["id"])
        F.queue_notice(self.store, rows[0]["id"], event_key, now=self.now)
        with self.assertRaises(F.FollowupError):
            F.queue_notice(self.store, rows[0]["id"], event_key, now=self.now)
        self.assertEqual(len(F.awaiting_entries(self.store)), 1)

    def test_not_seeded_before_the_reminder_window(self):
        self._emitted(due_delta=timedelta(hours=1))
        self.assertEqual(F.portal_rows(self.store, now=self.now), [])

    def test_answering_closes_the_followup(self):
        self._emitted()
        row = F.portal_rows(self.store, now=self.now)[0]
        result = A.apply("done", now=self.now, followup_store=self.store,
                         followup_id=row["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(F.get(self.store, row["id"])["status"], "done")
        self.assertEqual(F.get(self.store, row["id"])["close_reason"],
                         "owner_confirmed")
        self.assertEqual(F.portal_rows(self.store, now=self.now), [])

    def test_state_survives_reload(self):
        self._emitted()
        row = F.portal_rows(self.store, now=self.now)[0]
        reloaded = StateStore(self.root, Clock())
        self.assertEqual(F.get(reloaded, row["id"])["source_ref"],
                         "reminder:rem-1")
        self.assertEqual(F.portal_rows(reloaded, now=self.now), [row])


if __name__ == "__main__":
    unittest.main(verbosity=2)
