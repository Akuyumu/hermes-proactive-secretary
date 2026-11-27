"""Stage A.7 regressions: one door, and every effect needs a confirmation turn.

Every probe drives the real tool handler, the real acknowledgement helpers and
the real stores. Only paths, the clock and the fake host facade are synthetic;
no model, network or background runner is involved.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402
from secretary import secretary_responsibilities as R  # noqa: E402
from proactive_pilot import deliveries  # noqa: E402
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import gates  # noqa: E402
from proactive_pilot import pending_ack as P  # noqa: E402
from proactive_pilot import policy  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

class _FixedClock:
    def __init__(self, now):
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


class StageA7Harness(unittest.TestCase):
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

    def _followup(self, text, *, publish=True):
        entry = F.create(self.store, text, kind="one_shot",
                         check_hint=f"check {text}", now=self.clock.now())
        if publish:
            self._publish(entry, text)
        return entry

    def _publish(self, entry, message):
        deliveries.record_emission(
            self.store, message=message, kind="portal-notice",
            event_key=F.notice_event_key(entry["id"]),
            notice_id=f"notice-{entry['id']}", now=self.clock.now())
        return entry

    def _close(self, entry):
        if entry and F.get(self.store, entry["id"])["status"] == "active":
            F.close(self.store, entry["id"], "owner_confirmed",
                    now=self.clock.now())

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

    def _snoozes(self):
        if not self.preferences.exists():
            return []
        return list(feedback.load_preferences_file(
            self.preferences, strict=True).get("snoozes") or [])

    def _decide(self, key, prefs, state):
        proposal = EvaluatorResult(
            action="notify", kind="feed", key=key, message="Batch",
            urgency="normal", expiry="", evidence=[])
        packet = {"candidates": [{"ref": key, "kind": "feed", "version": ""}]}
        return gates.decide(
            proposal, packet, clock=_FixedClock(self.clock.now()),
            policy=policy.DEFAULT_POLICY, prefs=prefs, state=state)


class OneConfirmationTurnTests(StageA7Harness):
    def test_each_verb_needs_a_question_and_an_unrelated_reply_writes_nothing(self):
        verbs = (
            "done",
            "later 30",
            "later until 2027-01-01T12:00:00+00:00",
            "stop",
        )
        for index, verb in enumerate(verbs):
            with self.subTest(verb=verb):
                entry = self._followup(f"Task {index}")
                before = len(self._snoozes())
                turn = self._turn(verb)
                opened = self._apply(verb)
                self.assertTrue(opened["ok"], opened)
                self.assertEqual(opened["status"], "confirmation_required")
                self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
                self.assertEqual(len(self._snoozes()), before)
                row = P.rows(self.store)[-1]
                self.assertEqual(row["consumed_at"], "")

                self._turn("banana", at=float(turn["at"]) + 1)
                refused = self._apply("banana")
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(refused["status"], "needs_clarification")
                self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
                self.assertEqual(len(self._snoozes()), before)
                self.assertEqual(P.rows(self.store)[-1]["consumed_at"], "")

                self._turn("yes", at=float(turn["at"]) + 2)
                confirmed = self._confirm()
                self.assertTrue(confirmed["ok"], confirmed)
                if verb.startswith("later"):
                    snoozes = self._snoozes()
                    self.assertEqual(len(snoozes), before + 1)
                    self.assertEqual(snoozes[-1]["target_id"],
                                     f"followup:{entry['id']}")
                    self._close(entry)
                else:
                    self.assertEqual(len(self._snoozes()), before)
                    self.assertEqual(F.get(self.store, entry["id"])["status"],
                                     "done")

    def test_a_full_label_match_without_a_reply_pin_still_asks(self):
        entry = self._followup("done")
        turn = self._turn("done")
        opened = self._apply("done")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["status"], "confirmation_required")
        self.assertEqual(opened["message"], "Mark “done” as done?")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

        self._turn("yes", at=float(turn["at"]) + 1)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_a_repeated_request_while_a_proposal_is_live_never_writes(self):
        entry = self._followup("Dentist")
        turn = self._turn("done")
        opened = self._apply("done")
        self.assertEqual(opened["status"], "confirmation_required", opened)

        for offset, repeated in enumerate(("done", "later 30", "stop"), start=1):
            with self.subTest(repeated=repeated):
                self._turn(repeated, at=float(turn["at"]) + offset)
                refused = self._apply(repeated)
                self.assertFalse(refused["ok"], refused)
                self.assertIn(refused["status"],
                              {"needs_clarification", "refused"})
                self.assertEqual(F.get(self.store, entry["id"])["status"],
                                 "active")
                self.assertFalse(self.preferences.exists())
                row = P.rows(self.store)[0]
                self.assertEqual(row["consumed_at"], "")
                self.assertEqual(row["target_id"], f"followup:{entry['id']}")

        self._turn("yes", at=float(turn["at"]) + 4)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_a_bare_no_cancels_the_proposal_and_writes_nothing(self):
        entry = self._followup("Dentist")
        turn = self._turn("stop")
        opened = self._apply("stop")
        self.assertEqual(opened["status"], "confirmation_required", opened)

        self._turn("no", at=float(turn["at"]) + 1)
        cancelled = self._apply("no")
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertFalse(self.preferences.exists())
        self.assertEqual(P.live_for(self.store, conversation="conv-1",
                                    owner="owner", now=self.clock.now()), [])
        row = P.rows(self.store)[0]
        self.assertNotEqual(row["consumed_at"], "")
        self.assertEqual(row["receipt"]["status"], "cancelled")

        self._turn("yes", at=float(turn["at"]) + 2)
        late = self._confirm()
        self.assertFalse(late["ok"], late)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertFalse(self.preferences.exists())


class SelectionQuestionTests(StageA7Harness):
    def test_answers_resolve_only_against_the_printed_choices(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice")
        turn = self._turn("done")
        clarified = self._apply("done")
        self.assertFalse(clarified["ok"], clarified)
        self.assertEqual(clarified["status"], "needs_clarification")
        self.assertIn("Dentist", clarified["message"])
        self.assertIn("Invoice", clarified["message"])
        self.assertEqual(P.rows(self.store), [])
        question = P.find_question(
            self.store, conversation="conv-1", owner="owner",
            now=self.clock.now())
        self.assertIsNotNone(question)

        self._turn("third item", at=float(turn["at"]) + 1)
        no_match = self._apply("third item")
        self.assertEqual(no_match["status"], "needs_clarification", no_match)
        self.assertEqual(no_match["message"], clarified["message"])
        self.assertEqual(P.rows(self.store), [])
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "active")

        self._turn("Invoice", at=float(turn["at"]) + 2)
        opened = self._apply("Invoice")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"], "Mark “Invoice” as done?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{invoice['id']}")
        self.assertFalse(self.preferences.exists())

        self._turn("yes", at=float(turn["at"]) + 3)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, invoice["id"])["status"], "done")
        self.assertEqual(F.get(self.store, dentist["id"])["status"], "active")

    def test_a_contradictory_reply_pin_refuses_and_reasks(self):
        offered = self._followup("Dentist")
        self._followup("Invoice")
        turn = self._turn("done")
        clarified = self._apply("done")
        self.assertEqual(clarified["status"], "needs_clarification", clarified)
        self.assertEqual(P.rows(self.store), [])

        added = self._followup("Dentist")
        self._turn("Dentist", at=float(turn["at"]) + 1, reply_to=added["id"])
        contradicted = self._apply("Dentist")
        self.assertFalse(contradicted["ok"], contradicted)
        self.assertEqual(contradicted["status"], "needs_clarification")
        self.assertEqual(contradicted["message"], clarified["message"])
        self.assertEqual(P.rows(self.store), [])
        self.assertEqual(F.get(self.store, offered["id"])["status"], "active")
        self.assertEqual(F.get(self.store, added["id"])["status"], "active")

        self._turn("Dentist", at=float(turn["at"]) + 2)
        opened = self._apply("Dentist")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"], "Mark “Dentist” as done?")
        self.assertEqual(P.rows(self.store)[0]["target_id"],
                         f"followup:{offered['id']}")

        self._turn("yes", at=float(turn["at"]) + 3)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, offered["id"])["status"], "done")
        self.assertEqual(F.get(self.store, added["id"])["status"], "active")


class DeliveryKeyTests(StageA7Harness):
    def test_a_shared_prefix_never_matches_a_sibling_key(self):
        now = self.clock.now()
        until = (now + timedelta(hours=1)).isoformat()
        prefs = {"snoozes": [{
            "target_id": "portal:batch",
            "target_keys": ["portal:batch"],
            "kind": "",
            "match": "batch",
            "scope": "batch",
            "until": until,
        }]}
        self.assertTrue(policy.active_snooze(prefs, now, "portal:batch"))
        self.assertFalse(policy.active_snooze(prefs, now, "portal:batch:other"))
        self.assertTrue(policy.preference_blocks(
            {"kind": "feed", "target_id": "portal:batch"}, prefs, now))
        self.assertFalse(policy.preference_blocks(
            {"kind": "feed", "target_id": "portal:batch:other"}, prefs, now))

        self.assertFalse(self._decide("portal:batch", prefs, {})["send"])
        self.assertTrue(self._decide("portal:batch:other", prefs, {})["send"])

    def test_only_the_documented_followup_names_share_an_identity(self):
        now = self.clock.now()
        until = (now + timedelta(hours=1)).isoformat()
        prefs = {"snoozes": [{
            "target_id": "followup:fu_abc",
            "target_keys": ["followup:fu_abc"],
            "kind": "",
            "match": "abc",
            "scope": "abc",
            "until": until,
        }]}
        self.assertTrue(policy.active_snooze(
            prefs, now, "portal:followup-notice:fu_abc"))
        self.assertTrue(policy.active_snooze(
            prefs, now, "portal:followup-notice:fu_abc:deadbeef"))
        self.assertFalse(policy.active_snooze(
            prefs, now, "portal:followup-notice:fu_other:deadbeef"))

        stored_portal = {"snoozes": [{
            "target_id": "portal:followup-notice:fu_abc:deadbeef",
            "target_keys": ["portal:followup-notice:fu_abc:deadbeef"],
            "kind": "",
            "match": "abc",
            "scope": "abc",
            "until": until,
        }]}
        self.assertTrue(policy.active_snooze(
            stored_portal, now, "followup:fu_abc"))
        self.assertFalse(policy.active_snooze(
            stored_portal, now, "followup:fu_other"))


class PublicLabelTests(StageA7Harness):
    def test_only_the_published_message_can_supply_a_followup_label(self):
        entry = self._followup("Dentist access PIN 4829", publish=False)
        self._publish(entry, "Dentist appointment")
        turn = self._turn("done")
        opened = self._apply("done")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"], "Mark “Dentist appointment” as done?")
        public = json.dumps(opened, ensure_ascii=False)
        for forbidden in ("PIN", "4829"):
            self.assertNotIn(forbidden, public)

        self._turn("yes", at=float(turn["at"]) + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["target_label"], "Dentist appointment")
        receipt = json.dumps(confirmed, ensure_ascii=False)
        for forbidden in ("PIN", "4829"):
            self.assertNotIn(forbidden, receipt)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_a_crafted_followup_title_is_never_echoed(self):
        entry = self._followup("Dentist access PIN 4829", publish=False)
        turn = self._turn("stop")
        refused = self._apply("stop")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["reason"]["code"], "label_unavailable")
        self.assertEqual(P.rows(self.store), [])

        self._turn("stop the reminder", at=float(turn["at"]) + 1,
                   reply_to=entry["id"])
        opened = self._propose(text="stop the reminder", quote="stop")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["message"],
                         "Stop follow-ups for “the item you replied to” only?")
        self.assertEqual(opened["target_label"], "the item you replied to")
        public = json.dumps(opened, ensure_ascii=False)
        stored = P.rows(self.store)[0]
        for forbidden in ("Dentist", "PIN", "4829"):
            self.assertNotIn(forbidden, public)
            self.assertNotIn(forbidden, stored["question"])
            self.assertNotIn(forbidden, stored["target_label"])

        self._turn("yes", at=float(turn["at"]) + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        receipt = json.dumps(confirmed, ensure_ascii=False)
        for forbidden in ("Dentist", "PIN", "4829"):
            self.assertNotIn(forbidden, receipt)
        self.assertEqual(confirmed["target_label"], "the item you replied to")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")


class OneEffectTests(StageA7Harness):
    def test_a_model_interpretation_still_needs_confirmation_whatever_mode(self):
        entry = self._followup("Dentist")
        turn = self._turn("please stop the dentist")
        opened = self._call({
            "text": turn["text"], "action": "stop",
            "quote": "stop the dentist",
            "evidence_quote": "stop the dentist"})
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["status"], "confirmation_required")
        self.assertEqual(opened["message"], "Stop follow-ups for “Dentist” only?")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertEqual(P.rows(self.store)[0]["consumed_at"], "")

        self._turn("yes", at=float(turn["at"]) + 1)
        self.assertTrue(self._confirm()["ok"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_one_effect_per_pinned_turn_and_per_proposal(self):
        immediate = self._followup("Dentist")
        turn = self._turn("done", reply_to=immediate["id"])
        applied = self._apply("done")
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(F.get(self.store, immediate["id"])["status"], "done")

        again = self._apply("done")
        self.assertFalse(again["ok"], again)
        self.assertEqual(again["reason"]["code"], "turn_consumed")
        self.assertEqual(F.get(self.store, immediate["id"])["status"], "done")

        entry = self._followup("Invoice")
        self._turn("stop the reminder", at=float(turn["at"]) + 1,
                   reply_to=entry["id"])
        opened = self._propose(text="stop the reminder", quote="stop")
        self.assertTrue(opened["ok"], opened)

        self._turn("yes", at=float(turn["at"]) + 2)
        first = self._confirm()
        self.assertTrue(first["ok"], first)
        self.assertEqual(F.get(self.store, entry["id"])["close_reason"],
                         "owner_stopped")
        retry = self._confirm()
        self.assertEqual(retry, first)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_an_invalid_interpreted_effect_stores_nothing(self):
        entry = self._followup("Dentist")
        turn = self._turn("please handle the dentist")
        refused = self._propose(text=turn["text"], quote="handle",
                                action="later")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["reason"]["code"], "effect_invalid")
        self.assertEqual(P.rows(self.store), [])
        self.assertIsNone(P.find_question(
            self.store, conversation="conv-1", owner="owner",
            now=self.clock.now()))
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertFalse(self.preferences.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
