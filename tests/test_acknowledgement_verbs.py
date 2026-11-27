"""Stage 2: acknowledgement verbs ride the existing durable stores.

Only the local follow-up/preferences/guidance stores are exercised. No source is
read, no message is sent, no cron job is created.
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
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import guidance  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

UTC = timezone.utc


class AcknowledgementVerbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root, Clock())
        self.now = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
        self.prefs = self.root / "preferences.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _followup(self, text="harbour tide"):
        return F.create(self.store, text, kind="one_shot",
                        check_hint=f"check {text}", now=self.now)

    def test_later_maps_to_the_existing_snooze_action(self):
        parsed = A.parse("later 30", now=self.now)
        self.assertEqual(parsed["action"], "snooze")
        self.assertEqual(parsed["minutes"], 30)

        result = A.apply("later 30", now=self.now,
                         preferences_path=self.prefs, scope="harbour tide")
        self.assertTrue(result["ok"], result)
        prefs = feedback.load_preferences_file(self.prefs, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        self.assertEqual(prefs["snoozes"][0]["scope"], "harbour tide")
        self.assertEqual(prefs["snoozes"][0]["until"],
                         (self.now + timedelta(minutes=30)).isoformat())

        self.assertEqual(A.parse("later 2h", now=self.now)["minutes"], 120)
        until = self.now + timedelta(hours=3)
        explicit = A.apply(f"later until {until.isoformat()}", now=self.now,
                           preferences_path=self.prefs)
        self.assertTrue(explicit["ok"], explicit)
        prefs = feedback.load_preferences_file(self.prefs, strict=True)
        self.assertEqual(prefs["snoozes"][-1]["until"], until.isoformat())

    def test_done_closes_the_right_store_entry(self):
        first = self._followup("first item")
        second = self._followup("second item")

        result = A.apply("done", now=self.now, followup_store=self.store,
                         followup_id=first["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "owner_confirmed")
        self.assertEqual(F.get(self.store, first["id"])["status"], "done")
        self.assertEqual(F.get(self.store, first["id"])["close_reason"],
                         "owner_confirmed")
        self.assertEqual(F.get(self.store, second["id"])["status"], "active")

        topic_result = A.apply("done", now=self.now, topic="Exams",
                               guidance_root=self.root)
        self.assertTrue(topic_result["ok"], topic_result)
        listed = guidance.list_active(self.root, now=self.now)
        self.assertEqual([row["labels"] for row in listed["entries"]],
                         [["Exams"]])

    def test_stop_is_scoped_and_never_a_global_ban(self):
        first = self._followup("first item")
        second = self._followup("second item")

        result = A.apply("stop", now=self.now, followup_store=self.store,
                         followup_id=first["id"], topic="Exams",
                         guidance_root=self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(F.get(self.store, first["id"])["close_reason"],
                         "owner_stopped")
        self.assertEqual(F.get(self.store, second["id"])["status"], "active")

        listed = guidance.list_active(self.root, now=self.now)
        self.assertEqual([row["labels"] for row in listed["entries"]],
                         [["Exams"]])
        self.assertFalse(listed["entries"][0]["permanent"])
        self.assertFalse(self.prefs.exists())
        prefs = feedback.empty_preferences()
        self.assertFalse(prefs["paused"]["active"])

    def test_unknown_word_changes_nothing_and_refuses_explicitly(self):
        entry = self._followup("unchanged item")
        before_followups = (self.root / F.STORE).read_bytes()

        result = A.apply("banana", now=self.now, preferences_path=self.prefs,
                         followup_store=self.store, followup_id=entry["id"],
                         topic="Exams", guidance_root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "refused")
        self.assertIn("unrecognised acknowledgement", result["reason"])
        self.assertEqual((self.root / F.STORE).read_bytes(), before_followups)
        self.assertFalse(self.prefs.exists())
        self.assertFalse(guidance.pending_path(self.root).exists())
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

    def test_bad_later_duration_is_refused_without_writing(self):
        result = A.apply("later 0", now=self.now, preferences_path=self.prefs)
        self.assertFalse(result["ok"])
        self.assertIn("out of range", result["reason"])
        self.assertFalse(self.prefs.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
