"""Stage A.6 regressions: the third review's four blockers stay closed.

Every probe drives the real tool handler, the real acknowledgement helpers and
the real gate. Only paths, the clock and the fake host facade are synthetic; no
model, network or background runner is involved.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402
from secretary import secretary_responsibilities as R  # noqa: E402
from proactive_pilot import acknowledgements as A  # noqa: E402
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import gates  # noqa: E402
from proactive_pilot import pending_ack as P  # noqa: E402
from proactive_pilot import policy  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

UTC = timezone.utc
MIDDAY = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


class _FixedClock:
    def __init__(self, now=MIDDAY):
        self._now = now

    def now(self):
        return self._now


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


class StageA6Harness(unittest.TestCase):
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

    def _turn(self, text, *, at=None, reply_to=None):
        stamp = float(at if at is not None else time.time())
        R.remember_human_turn(
            self.ctx, session_id="conv-1", user_message=text,
            platform="telegram", chat_type="dm", at=stamp)
        row = dict(self.ctx.state.data[R.TURN_KEY][-1])
        if reply_to is not None:
            row["reply_to"] = reply_to
        self.ctx.state.data[R.TURN_KEY][-1] = row
        return row

    def _followup(self, text="Dentist"):
        return F.create(self.store, text, kind="one_shot",
                        check_hint=f"check {text}", now=self.clock.now())

    def _call(self, body, *, session_id="conv-1"):
        return json.loads(tool.tool_handler(self.ctx)(
            body, session_id=session_id))

    def _apply(self, text):
        return self._call({"text": text, "evidence_quote": text})

    def _propose(self, *, text, quote, action="stop", **extra):
        body = {"mode": "propose", "text": text, "action": action,
                "quote": quote, "evidence_quote": quote}
        body.update(extra)
        return self._call(body)

    def _confirm(self, *, text="yes"):
        return self._call({"mode": "confirm", "text": text,
                           "evidence_quote": text})

    def _decide(self, key, prefs, state):
        proposal = EvaluatorResult(
            action="notify", kind="feed", key=key, message="Dentist",
            urgency="normal", expiry="", evidence=[])
        packet = {"candidates": [{"ref": key, "kind": "feed", "version": ""}]}
        return gates.decide(
            proposal, packet, clock=_FixedClock(self.clock.now()),
            policy=policy.DEFAULT_POLICY, prefs=prefs, state=state)


class DeliveryKeyGateTests(StageA6Harness):
    def test_the_snooze_stores_the_portal_key_and_blocks_only_that_sibling(self):
        first = self._followup("Dentist")
        second = self._followup("Dentist")
        self._turn("later 30", reply_to=first["id"])
        applied = self._apply("later 30")
        self.assertTrue(applied["ok"], applied)

        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        row = prefs["snoozes"][0]
        first_event = F.notice_event_key(first["id"])
        second_event = F.notice_event_key(second["id"])
        portal_key = "portal:" + first_event
        self.assertIn(f"followup:{first['id']}", row["target_keys"])
        self.assertIn(portal_key, row["target_keys"])
        self.assertTrue(policy.active_snooze(prefs, self.clock.now(), portal_key))

        blocked = self._decide(portal_key, prefs, {})
        self.assertFalse(blocked["send"], blocked)
        delivered = self._decide("portal:" + second_event, prefs, {})
        self.assertTrue(delivered["send"], delivered)

        # Health and configuration notices are not target-scoped items.
        for key in ("health:mail", "config:setup", "coverage:mail"):
            with self.subTest(key=key):
                self.assertTrue(self._decide(key, prefs, {})["send"])

    def test_a_confirmed_snooze_also_stores_the_gate_key(self):
        first = self._followup("Dentist")
        turn = self._turn("later 30")
        opened = self._apply("later 30")
        self.assertEqual(opened["status"], "confirmation_required", opened)
        self._turn("yes", at=float(turn["at"]) + 1)
        self.assertTrue(self._confirm()["ok"])

        second = self._followup("Dentist")
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        portal_key = "portal:" + F.notice_event_key(first["id"])
        self.assertIn(portal_key, prefs["snoozes"][0]["target_keys"])
        self.assertFalse(self._decide(portal_key, prefs, {})["send"])
        self.assertTrue(self._decide(
            "portal:" + F.notice_event_key(second["id"]), prefs, {})["send"])

    def test_a_notice_already_queued_under_its_exact_event_key_is_blocked(self):
        entry = self._followup("Dentist")
        event_key = F.notice_event_key(entry["id"])
        F.queue_notice(self.store, entry["id"], event_key, now=self.clock.now())
        self._turn("later 30", reply_to=entry["id"])
        self.assertTrue(self._apply("later 30")["ok"])

        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        row = prefs["snoozes"][0]
        self.assertIn("portal:" + event_key, row["target_keys"])
        self.assertFalse(self._decide("portal:" + event_key, prefs, {})["send"])


class LiveAnswerTests(StageA6Harness):
    def test_a_live_question_owns_the_next_message_even_when_it_is_a_verb(self):
        dentist = self._followup("Dentist")
        stop_item = self._followup("stop")
        self._turn("done")
        opened = self._apply("done")
        self.assertFalse(opened["ok"], opened)
        self.assertEqual(opened["status"], "needs_clarification")
        self.assertIn("Dentist", opened["message"])
        self.assertIn("stop", opened["message"])

        self._turn("later 30", at=time.time() + 1)
        refused = self._apply("later 30")
        self.assertEqual(refused["status"], "needs_clarification", refused)
        self.assertEqual(P.rows(self.store), [])
        self.assertFalse(self.preferences.exists())
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")

        self._turn("stop", at=time.time() + 2)
        answered = self._apply("stop")
        self.assertTrue(answered["ok"], answered)
        self.assertEqual(answered["status"], "confirmation_required")
        self.assertEqual(answered["message"], "Mark “stop” as done?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{stop_item['id']}")
        self.assertEqual(F.get(self.store, stop_item["id"])["status"], "active")
        self.assertEqual(F.get(self.store, stop_item["id"])["close_reason"], "")

        self._turn("yes", at=time.time() + 3)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        stored = F.get(self.store, stop_item["id"])
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["close_reason"], "owner_confirmed")
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")

    def test_a_live_proposal_refuses_a_different_fresh_verb(self):
        entry = self._followup("Dentist")
        turn = self._turn("stop")
        opened = self._apply("stop")
        self.assertEqual(opened["status"], "confirmation_required", opened)
        self.assertEqual(opened["message"], "Stop follow-ups for “Dentist” only?")

        self._turn("done", at=float(turn["at"]) + 1)
        refused = self._apply("done")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["status"], "needs_clarification")
        self.assertEqual(refused["message"],
                         "Stop follow-ups for “Dentist” only?")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        row = P.rows(self.store)[0]
        self.assertEqual(row["consumed_at"], "")
        self.assertEqual(row["target_id"], f"followup:{entry['id']}")

        self._turn("yes", at=float(turn["at"]) + 2)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")


class QuestionBindingTests(StageA6Harness):
    def test_answers_resolve_only_against_the_offered_choices(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice")
        turn = self._turn("stop")
        clarified = self._apply("stop")
        self.assertEqual(clarified["status"], "needs_clarification", clarified)
        question = P.find_question(
            self.store, conversation="conv-1", owner="owner",
            now=self.clock.now())
        self.assertIsNotNone(question)
        choices = list(question["choices"])
        self.assertEqual([choice["position"] for choice in choices],
                         list(range(1, len(choices) + 1)))
        labels = [choice["label"] for choice in choices]
        self.assertIn("Dentist", labels)
        self.assertIn("Invoice", labels)

        late = self._followup("Third late item")
        self._turn("Third late item", at=float(turn["at"]) + 1)
        not_offered = self._apply("Third late item")
        self.assertEqual(not_offered["status"], "needs_clarification",
                         not_offered)
        self.assertEqual(P.rows(self.store), [])
        self.assertEqual(F.get(self.store, late["id"])["status"], "active")

        self._turn("do not stop Dentist", at=float(turn["at"]) + 2)
        negated = self._apply("do not stop Dentist")
        self.assertEqual(negated["status"], "needs_clarification", negated)
        self.assertEqual(P.rows(self.store), [])
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")
        self.assertEqual(F.get(self.store, dentist["id"])["close_reason"], "")

        dentist_choice = next(
            choice for choice in choices if choice["label"] == "Dentist")
        self._turn(str(dentist_choice["position"]),
                   at=float(turn["at"]) + 3)
        opened = self._apply(str(dentist_choice["position"]))
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"],
                         "Stop follow-ups for “Dentist” only?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{dentist['id']}")
        self.assertIsNone(P.find_question(
            self.store, conversation="conv-1", owner="owner",
            now=self.clock.now()))

        self._turn("Invoice", at=float(turn["at"]) + 4)
        stale = self._apply("Invoice")
        self.assertFalse(stale["ok"], stale)
        self.assertEqual(len(P.rows(self.store)), 1)
        self.assertEqual(P.rows(self.store)[0]["consumed_at"], "")
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")

        self._turn("yes", at=float(turn["at"]) + 5)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "done")
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")

        self._turn("Invoice", at=float(turn["at"]) + 6)
        after = self._apply("Invoice")
        self.assertFalse(after["ok"], after)
        self.assertEqual(len(P.rows(self.store)), 1)
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")


class PublicLabelTests(StageA6Harness):
    def test_a_crafted_label_is_not_echoed_in_question_or_receipt(self):
        entry = self._followup("Dentist because hunter2 is private")
        turn = self._turn("stop", reply_to=entry["id"])
        opened = self._propose(text="stop", quote="stop")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"],
                         "Stop follow-ups for “Dentist” only?")
        self.assertEqual(opened["target_label"], "Dentist")
        self.assertEqual(P.rows(self.store)[0]["target_label"], "Dentist")

        self._turn("yes", at=float(turn["at"]) + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["message"],
                         "Stopped follow-ups for “Dentist”.")
        self.assertEqual(confirmed["target_label"], "Dentist")
        public = json.dumps(confirmed, ensure_ascii=False)
        self.assertNotIn("hunter2", public)
        self.assertNotIn("because", public)

    def test_every_question_and_receipt_sentence_is_balanced_and_exact(self):
        cases = (
            ("done", "Mark “Dentist” as done?", "Marked “Dentist” as done."),
            ("stop", "Stop follow-ups for “Dentist” only?",
             "Stopped follow-ups for “Dentist”."),
            ("later 30", "Snooze “Dentist” for 30 minutes?",
             "Snoozed “Dentist” for 30 minutes."),
        )
        for index, (word, question, receipt) in enumerate(cases):
            with self.subTest(word=word):
                entry = self._followup("Dentist")
                self._turn(word, at=time.time() + index,
                           reply_to=entry["id"])
                action = "later" if word.startswith("later") else word
                opened = self._propose(
                    text=word, quote=word, action=action,
                    minutes=30 if action == "later" else None)
                self.assertTrue(opened["ok"], opened)
                self.assertEqual(opened["message"], question)
                self.assertEqual(P.rows(self.store)[-1]["question"], question)
                self._turn("yes", at=time.time() + index + 1)
                confirmed = self._confirm()
                self.assertTrue(confirmed["ok"], confirmed)
                self.assertEqual(confirmed["message"], receipt)
                self.assertEqual(P.rows(self.store)[-1]["receipt"]["note"],
                                 receipt)
                self.assertEqual(confirmed["message"].count("“"),
                                 confirmed["message"].count("”"))

    def test_an_unsafe_label_needs_a_trusted_reply_pin(self):
        entry = self._followup(
            "fu_deadbeef /home/owner/private a@b.com")
        self._turn("stop")
        refused = self._apply("stop")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["reason"]["code"], "label_unavailable")
        self.assertEqual(P.rows(self.store), [])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

        self._turn("stop", at=time.time() + 1, reply_to=entry["id"])
        opened = self._propose(text="stop", quote="stop")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["target_label"], "the item you replied to")
        self.assertEqual(opened["message"],
                         "Stop follow-ups for “the item you replied to” only?")
        self._turn("yes", at=time.time() + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["target_label"], "the item you replied to")
        self.assertEqual(confirmed["message"],
                         "Stopped follow-ups for “the item you replied to”.")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
