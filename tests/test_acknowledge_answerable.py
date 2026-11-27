"""An unpinned verb must offer a concrete question a later turn can answer.

These tests drive the real tool handler and the real stores. They prove the
three shapes required by the answerable-question brief: one candidate, several
candidates, and no candidate, for ``done``, ``stop`` and ``later 30``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402
from secretary import secretary_responsibilities as R  # noqa: E402
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import pending_ack as P  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402


class _State:
    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _Ctx:
    def __init__(self, *, execution=None, profile_name="owner") -> None:
        self.state = _State()
        self.profile_name = profile_name
        self._execution = execution

    def current_cron_execution(self):
        return self._execution


class AnswerableHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "var"
        self.preferences = self.root / "preferences.json"
        self.clock = Clock()
        self.store = StateStore(self.state_dir, self.clock)
        self.ctx = _Ctx()
        self._patches = [
            mock.patch.object(tool.paths, "state_dir", return_value=self.state_dir),
            mock.patch.object(tool.paths, "preferences_path",
                              return_value=self.preferences),
            mock.patch.object(tool.paths, "data_root", return_value=self.root),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _turn(self, text, *, at=None):
        stamp = float(at if at is not None else time.time())
        R.remember_human_turn(
            self.ctx, session_id="conv-1", user_message=text,
            platform="telegram", chat_type="dm", at=stamp)
        return dict(self.ctx.state.data[R.TURN_KEY][-1])

    def _followup(self, text):
        entry = F.create(self.store, text, kind="one_shot",
                         check_hint=f"check {text}", now=self.clock.now())
        self._publish(entry, text)
        return entry

    def _publish(self, entry, message):
        from proactive_pilot import deliveries
        deliveries.record_emission(
            self.store, message=message, kind="portal-notice",
            event_key=F.notice_event_key(entry["id"]),
            notice_id=f"notice-{entry['id']}", now=self.clock.now())
        return entry

    def _call(self, body, *, session_id="conv-1"):
        return json.loads(tool.tool_handler(self.ctx)(
            body, session_id=session_id))

    def _apply(self, text):
        return self._call({"text": text, "evidence_quote": text})

    def _confirm(self, *, text="yes"):
        return self._call({"mode": "confirm", "text": text,
                           "evidence_quote": text})

    def _assert_question(self, result, question):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["message"], question)
        self.assertEqual(result["message"].count("?"), 1)
        rows = P.rows(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], question)
        self.assertEqual(rows[0]["consumed_at"], "")


class OneCandidateTests(AnswerableHarness):
    def test_bare_done_offers_a_pinned_question_and_a_later_yes_confirms(self):
        entry = self._followup("dentist")
        turn = self._turn("done")

        # The reviewer's repro: the model carries the follow-up id, but the
        # bare owner word still arrives unpinned.
        opened = self._call({"text": "done", "evidence_quote": "done",
                             "followup_id": entry["id"]})
        self._assert_question(opened, "Mark “dentist” as done?")
        self.assertEqual(opened["target_label"], "dentist")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{entry['id']}")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

        # A yes at the proposal's own timestamp is not strictly later.
        self._turn("yes", at=turn["at"])
        same_turn = self._confirm()
        self.assertFalse(same_turn["ok"], same_turn)
        self.assertEqual(same_turn["reason"]["code"], "not_later")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

        self._turn("yes", at=float(turn["at"]) + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["status"], "done")
        stored = F.get(self.store, entry["id"])
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["close_reason"], "owner_confirmed")
        self.assertEqual(P.rows(self.store)[0]["consumed_at"] != "", True)

        again = self._confirm()
        self.assertEqual(again, confirmed)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_bare_stop_offers_a_pinned_question_and_a_later_yes_confirms(self):
        entry = self._followup("dentist")
        turn = self._turn("stop")

        opened = self._apply("stop")
        self._assert_question(opened, "Stop follow-ups for “dentist” only?")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

        self._turn("yes", at=float(turn["at"]) + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        stored = F.get(self.store, entry["id"])
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["close_reason"], "owner_stopped")

    def test_bare_later_30_offers_a_pinned_question_and_a_later_yes_snoozes(self):
        entry = self._followup("dentist")
        turn = self._turn("later 30")

        opened = self._apply("later 30")
        self._assert_question(opened, "Snooze “dentist” for 30 minutes?")
        self.assertEqual(opened["deadline"], "30 minutes")
        self.assertFalse(self.preferences.exists())

        self._turn("yes", at=float(turn["at"]) + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        row = prefs["snoozes"][0]
        self.assertEqual(row["target_id"], f"followup:{entry['id']}")
        self.assertEqual(row["key"], P.rows(self.store)[0]["key"])
        self.assertTrue(row.get("until"))
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")


class SeveralCandidatesTests(AnswerableHarness):
    def test_done_lists_both_labels_then_a_full_label_offers_the_question(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice tomorrow morning")
        turn = self._turn("done")

        clarified = self._apply("done")
        self.assertFalse(clarified["ok"], clarified)
        self.assertEqual(clarified["status"], "needs_clarification")
        self.assertIn("Dentist", clarified["message"])
        self.assertIn("Invoice tomorrow morning", clarified["message"])
        self.assertEqual(P.rows(self.store), [])

        self._turn("Dentist", at=float(turn["at"]) + 1)
        opened = self._apply("Dentist")
        self._assert_question(opened, "Mark “Dentist” as done?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{dentist['id']}")
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")

        self._turn("yes", at=float(turn["at"]) + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "done")
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")

    def test_stop_lists_both_labels_then_a_full_label_offers_the_question(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice tomorrow morning")
        turn = self._turn("stop")

        clarified = self._apply("stop")
        self.assertFalse(clarified["ok"], clarified)
        self.assertEqual(clarified["status"], "needs_clarification")
        self.assertIn("Dentist", clarified["message"])
        self.assertIn("Invoice tomorrow morning", clarified["message"])
        self.assertEqual(P.rows(self.store), [])

        self._turn("Invoice tomorrow morning", at=float(turn["at"]) + 1)
        opened = self._apply("Invoice tomorrow morning")
        self._assert_question(
            opened, "Stop follow-ups for “Invoice tomorrow morning” only?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{invoice['id']}")

        self._turn("yes", at=float(turn["at"]) + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "done")
        self.assertEqual(F.get(self.store, invoice["id"])["close_reason"],
                         "owner_stopped")
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")

    def test_later_30_lists_both_labels_then_a_full_label_offers_the_question(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice tomorrow morning")
        turn = self._turn("later 30")

        clarified = self._apply("later 30")
        self.assertFalse(clarified["ok"], clarified)
        self.assertEqual(clarified["status"], "needs_clarification")
        self.assertIn("Dentist", clarified["message"])
        self.assertIn("Invoice tomorrow morning", clarified["message"])
        self.assertEqual(P.rows(self.store), [])

        self._turn("Dentist", at=float(turn["at"]) + 1)
        opened = self._apply("Dentist")
        self._assert_question(opened, "Snooze “Dentist” for 30 minutes?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{dentist['id']}")

        self._turn("yes", at=float(turn["at"]) + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        self.assertEqual(prefs["snoozes"][0]["target_id"],
                         f"followup:{dentist['id']}")
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")


class NoCandidateTests(AnswerableHarness):
    def test_unpinned_verbs_refuse_plainly_and_write_no_proposal(self):
        turn = self._turn("done")
        for index, text in enumerate(("done", "stop", "later 30")):
            with self.subTest(text=text):
                if index:
                    self._turn(text, at=float(turn["at"]) + index)
                result = self._apply(text)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["status"], "refused")
                self.assertEqual(result["reason"]["code"], "no_candidate")
                self.assertNotIn("?", result["message"])
                self.assertEqual(P.rows(self.store), [])


class QuestionSafetyTests(AnswerableHarness):
    FORBIDDEN = ("fu_deadbeef", "ack-ab12", "/home/owner/private", "a@b.com",
                 "hunter2")

    def test_the_question_carries_only_safe_label_and_effect(self):
        entry = self._followup(
            "fu_deadbeef ack-ab12 /home/owner/private a@b.com dentist")
        self._turn("done")
        opened = self._apply("done")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["status"], "confirmation_required")
        question = opened["message"]
        self.assertIn("dentist", question)
        self.assertTrue(question.endswith("?"))
        self.assertEqual(question.count("?"), 1)
        public = json.dumps(opened, ensure_ascii=False)
        stored = P.rows(self.store)[0]
        for forbidden in self.FORBIDDEN:
            self.assertNotIn(forbidden, public)
            self.assertNotIn(forbidden, stored["question"])
            self.assertNotIn(forbidden, stored["target_label"])
        self.assertEqual(stored["target_id"], f"followup:{entry['id']}")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
