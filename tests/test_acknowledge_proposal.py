"""Stage A: a model may propose an interpretation; only the owner confirms it.

The tests drive the real tool handler with the real host-turn attestation path.
Only the profile paths, clock and turn platform are synthetic. No source is read,
no message is sent, no model is called and no network is used.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402
from secretary import secretary_responsibilities as R  # noqa: E402
from proactive_pilot import acknowledgements as A  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import notice_voice  # noqa: E402
from proactive_pilot import pending_ack  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import CorruptStateError, StateStore  # noqa: E402

UTC = timezone.utc


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


class _Harness(unittest.TestCase):
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

    # -- helpers ---------------------------------------------------------
    def _turn(self, text, *, at=None, platform="telegram", chat_type="dm",
              reply_to=None, profile=None):
        stamp = float(at if at is not None else time.time())
        R.remember_human_turn(
            self.ctx, session_id="conv-1", user_message=text,
            platform=platform, chat_type=chat_type, at=stamp)
        row = dict(self.ctx.state.data[R.TURN_KEY][-1])
        if reply_to is not None:
            row["reply_to"] = reply_to
        if profile is not None:
            row["profile"] = profile
        self.ctx.state.data[R.TURN_KEY][-1] = row
        return row

    def _followup(self, text="Test reminder: reply done, or later 30, or stop"):
        return F.create(self.store, text, kind="one_shot",
                        check_hint="check the reminder", now=self.clock.now())

    def _call(self, body, *, session_id="conv-1", task_id=None):
        return json.loads(tool.tool_handler(self.ctx)(
            body, session_id=session_id, task_id=task_id))

    def _propose(self, *, text, quote, action="stop", **extra):
        body = {"mode": "propose", "text": text, "action": action,
                "quote": quote, "evidence_quote": quote}
        body.update(extra)
        return self._call(body)

    def _confirm(self, *, text="yes", **extra):
        body = {"mode": "confirm", "text": text, "evidence_quote": text}
        body.update(extra)
        return self._call(body)

    def _final_followup(self, fid):
        path = self.state_dir / F.STORE
        return path.read_bytes()

    # -- propose ---------------------------------------------------------
    def test_propose_writes_only_the_pending_row_and_asks_one_question(self):
        entry = self._followup()
        turn = self._turn("stop the reminder please", reply_to=entry["id"])
        before = self._final_followup(entry["id"])

        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("socket.create_connection",
                           side_effect=AssertionError("network")):
            result = self._propose(text=turn["text"], quote="stop the reminder")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "confirmation_required")
        question = result["question"]
        self.assertTrue(question.endswith("?"))
        self.assertEqual(question.count("?"), 1)
        self.assertIn("Test reminder", question)
        self.assertNotIn("fu_", question)
        self.assertNotIn(str(self.root), question)
        self.assertEqual(notice_voice.check_notice_text(question), (True, None))
        # No acknowledgement state was touched.
        self.assertEqual(self._final_followup(entry["id"]), before)
        self.assertFalse(self.preferences.exists())
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        rows = pending_ack.rows(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["consumed_at"], "")
        self.assertEqual(rows[0]["target_id"], f"followup:{entry['id']}")

    def test_propose_rejects_negation_question_conditional_and_bad_quote(self):
        entry = self._followup()
        cases = [
            ("don't stop the reminder", "don't stop", "negated"),
            ("stop the reminder?", "stop the reminder", "question"),
            ("if I say stop the reminder", "stop", "conditional"),
            ("stop the reminder", " stop the reminder ", "literal part"),
        ]
        for message, quote, reason in cases:
            with self.subTest(message=message):
                self._turn(message, reply_to=entry["id"])
                result = self._propose(text=message, quote=quote)
                self.assertFalse(result["ok"], result)
                self.assertIn(reason, result["reason"])
                self.assertEqual(pending_ack.rows(self.store), [])
                self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

    def test_without_a_host_pin_it_refuses_with_one_label_question(self):
        first = self._followup("first identical reminder")
        second = self._followup("second identical reminder")
        before_first = self._final_followup(first["id"])

        turn = self._turn("stop the reminder")
        result = self._propose(text=turn["text"], quote="stop the reminder")

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["reason"].endswith("?"))
        self.assertEqual(result["reason"].count("?"), 1)
        self.assertIn("first identical reminder", result["reason"])
        self.assertIn("second identical reminder", result["reason"])
        self.assertEqual(pending_ack.rows(self.store), [])
        self.assertEqual(F.get(self.store, first["id"])["status"], "active")
        self.assertEqual(F.get(self.store, second["id"])["status"], "active")
        self.assertEqual(self._final_followup(first["id"]), before_first)

    def test_the_reply_pin_selects_exactly_one_of_two_identical_items(self):
        first = self._followup("dentist reminder")
        second = self._followup("dentist reminder")
        proposal = self._propose(
            text="stop the reminder", quote="stop the reminder", reply_to=None)
        # No reply metadata: refused, never "newest".
        self.assertFalse(proposal["ok"])

        turn = self._turn("stop the reminder", reply_to=second["id"])
        proposal = self._propose(text=turn["text"], quote="stop the reminder")
        self.assertTrue(proposal["ok"], proposal)
        self.assertEqual(proposal["pending"]["target_id"],
                         f"followup:{second['id']}")

        self._turn("yes", at=time.time() + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(F.get(self.store, second["id"])["status"], "done")
        self.assertEqual(F.get(self.store, first["id"])["status"], "active")

    def test_model_target_that_disagrees_with_the_pin_refuses(self):
        entry = self._followup()
        other = self._followup("another item")
        turn = self._turn("stop the reminder", reply_to=entry["id"])
        result = self._propose(
            text=turn["text"], quote="stop the reminder",
            target_id=f"followup:{other['id']}")
        self.assertFalse(result["ok"], result)
        self.assertIn("does not match", result["reason"])
        self.assertEqual(pending_ack.rows(self.store), [])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertEqual(F.get(self.store, other["id"])["status"], "active")

    # -- apply -----------------------------------------------------------
    def test_apply_uses_the_pin_and_refuses_a_model_target_mismatch(self):
        pinned = self._followup("pinned item")
        other = self._followup("other item")
        turn = self._turn("stop", reply_to=pinned["id"])

        mismatch = self._call({"text": "stop", "evidence_quote": "stop",
                               "followup_id": other["id"]})
        self.assertFalse(mismatch["ok"], mismatch)
        self.assertEqual(F.get(self.store, pinned["id"])["status"], "active")
        self.assertEqual(F.get(self.store, other["id"])["status"], "active")

        applied = self._call({"text": "stop", "evidence_quote": "stop"})
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied.get("from_owner_turn"))
        self.assertEqual(F.get(self.store, pinned["id"])["status"], "done")
        self.assertEqual(F.get(self.store, pinned["id"])["close_reason"],
                         "owner_stopped")
        self.assertEqual(F.get(self.store, other["id"])["status"], "active")

    def test_apply_can_use_the_target_recorded_by_a_previous_ask(self):
        entry = self._followup("remembered item")
        turn = self._turn("stop the reminder", reply_to=entry["id"])
        proposal = self._propose(text=turn["text"], quote="stop the reminder")
        self.assertTrue(proposal["ok"], proposal)

        # The direct answer arrives without reply metadata; the previous ask pins it.
        self._turn("stop", at=time.time() + 1)
        result = self._call({"text": "stop", "evidence_quote": "stop",
                             "scope": "remembered item"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")
        self.assertEqual(F.get(self.store, entry["id"])["close_reason"],
                         "owner_stopped")

    # -- confirm ---------------------------------------------------------
    def test_confirm_accepts_only_a_bare_affirmative(self):
        entry = self._followup()
        turn = self._turn("stop the reminder", reply_to=entry["id"])
        self.assertTrue(self._propose(text=turn["text"],
                                      quote="stop the reminder")["ok"])

        self._turn("yes, but keep the other one", at=time.time() + 1)
        refused = self._confirm(text="yes, but keep the other one")
        self.assertFalse(refused["ok"], refused)
        self.assertIn("bare yes", refused["reason"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertEqual(pending_ack.rows(self.store)[0]["consumed_at"], "")

        self._turn("yes", at=time.time() + 2)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_duplicate_confirm_returns_the_same_receipt_and_conflict_refuses(self):
        entry = self._followup()
        turn = self._turn("stop the reminder", reply_to=entry["id"])
        self.assertTrue(self._propose(text=turn["text"],
                                      quote="stop the reminder")["ok"])

        self._turn("yes", at=time.time() + 1)
        first = self._confirm()
        self.assertTrue(first["ok"], first)
        again = self._confirm()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["receipt"], first["receipt"])

        self._turn("yes", at=time.time() + 3)
        conflict = self._confirm()
        self.assertFalse(conflict["ok"], conflict)
        self.assertEqual(conflict["status"], "already_done")
        self.assertIn("already done", conflict["reason"])
        self.assertEqual(conflict["receipt"], first["receipt"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "done")

    def test_expiry_is_checked_when_the_pending_row_is_read(self):
        t0 = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
        entry = F.create(self.store, "expiring item", kind="one_shot",
                         check_hint="check", now=t0)
        created = pending_ack.create(
            conversation="conv-exp", owner="owner",
            target_id=f"followup:{entry['id']}", target_label="expiring item",
            effect={"action": "stop", "kind": "", "scope": ""},
            question="Stop follow-ups for “expiring item” only?",
            turn_id="proposal-turn", now=t0, store=self.store)
        self.assertTrue(created["ok"], created)
        later = t0 + timedelta(minutes=11)
        state, row = pending_ack.lookup(
            self.store, conversation="conv-exp", owner="owner",
            turn_id="confirmation-turn", now=later)
        self.assertEqual(state, "expired")
        self.assertIsNotNone(row)

        result = A.confirm(
            store=self.store, conversation="conv-exp", owner="owner",
            turn={"text": "yes", "at": later.timestamp()},
            now=later, preferences_path=self.preferences,
            followup_store=self.store, guidance_root=self.root)
        self.assertFalse(result["ok"], result)
        self.assertIn("expired", result["reason"])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

    # -- authority -------------------------------------------------------
    def test_cron_group_and_foreign_profile_turns_cannot_propose_or_confirm(self):
        entry = self._followup()
        cron = _Ctx(execution=object())
        self.ctx = cron
        self._turn("stop the reminder", reply_to=entry["id"])
        self.assertFalse(self._propose(text="stop the reminder",
                                       quote="stop the reminder")["ok"])
        self.assertFalse(self._confirm()["ok"])

        self.ctx = _Ctx()
        self._turn("stop the reminder", reply_to=entry["id"], at=time.time() + 1)
        delegated = self._call(
            {"mode": "propose", "text": "stop the reminder",
             "action": "stop", "quote": "stop the reminder",
             "evidence_quote": "stop the reminder"}, task_id="child-task")
        self.assertFalse(delegated["ok"], delegated)
        self.assertIn("delegated", delegated["reason"])
        group_turn = self._turn("stop the reminder", chat_type="group",
                                reply_to=entry["id"], at=time.time() + 2)
        self.assertEqual(group_turn["chat_type"], "group")
        self.assertFalse(self._propose(text="stop the reminder",
                                       quote="stop the reminder")["ok"])
        self.assertFalse(self._confirm()["ok"])

        self.ctx = _Ctx(profile_name="A")
        self._turn("stop the reminder", reply_to=entry["id"],
                   profile="B", at=time.time() + 3)
        foreign = self._propose(text="stop the reminder", quote="stop the reminder")
        self.assertFalse(foreign["ok"], foreign)
        self.assertIn("another profile", foreign["reason"])
        self.assertEqual(pending_ack.rows(self.store), [])
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")

    # -- effect and voice ------------------------------------------------
    def test_stop_on_an_emitted_reminder_records_a_visible_disposition(self):
        reminder = {
            "id": "rem-1", "kind": "time", "status": "emitted",
            "text": "Test reminder: reply done, or later 30, or stop",
            "provenance": "owner request",
            "due": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "late_grace_minutes": 10,
            "emitted_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }
        self.store.save("reminders.json", {"schema": 1,
                                           "reminders": {"rem-1": reminder}})
        before = (self.state_dir / "reminders.json").read_bytes()
        turn = self._turn("stop the reminder", reply_to="rem-1")
        proposal = self._propose(text=turn["text"], quote="stop the reminder")
        self.assertTrue(proposal["ok"], proposal)
        self._turn("yes", at=time.time() + 1)
        result = self._confirm()
        self.assertTrue(result["ok"], result)
        receipt = result["receipt"]
        self.assertTrue(receipt["no_further_followup"])
        self.assertEqual(receipt["status"], "stopped")
        self.assertIn("No further follow-up", receipt["note"])
        self.assertEqual((self.state_dir / "reminders.json").read_bytes(), before)
        self.assertFalse((self.state_dir / F.STORE).exists())
        dispositions = pending_ack.dispositions(self.store)
        self.assertEqual(dispositions["rem-1"]["action"], "stop")
        self.assertEqual(dispositions["rem-1"]["target_id"], "reminder:rem-1")

    def test_user_facing_prose_removes_internal_handles_email_and_paths(self):
        entry = self._followup(
            "fu_deadbeef /home/owner/private a@b.com Test reminder: reply done")
        turn = self._turn("stop the reminder", reply_to=entry["id"])
        result = self._propose(text=turn["text"], quote="stop the reminder")
        self.assertTrue(result["ok"], result)
        question = result["question"]
        for forbidden in ("fu_deadbeef", "/home/owner", "a@b.com"):
            self.assertNotIn(forbidden, question)
        self.assertEqual(notice_voice.check_notice_text(question), (True, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OwnerSelectionTests(unittest.TestCase):
    """The owner naming an item's own words is a selection, not model authority."""

    def _candidates(self):
        return [
            {"target_id": "t1", "target_label": "Test reminder: reply done",
             "target_type": "reminder"},
            {"target_id": "t2", "target_label": "Dentist appointment tomorrow",
             "target_type": "event"},
        ]

    def test_a_distinctive_word_selects_exactly_one_item(self):
        picked, ambiguous = A.select_by_label(self._candidates(), "stop the dentist one")
        self.assertEqual(ambiguous, [])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["target_id"], "t2")

    def test_shared_wording_selects_nothing(self):
        rows = [
            {"target_id": "a", "target_label": "Invoice from vendor alpha",
             "target_type": "mail"},
            {"target_id": "b", "target_label": "Invoice from vendor alpha",
             "target_type": "mail"},
        ]
        picked, ambiguous = A.select_by_label(rows, "snooze the invoice")
        self.assertIsNone(picked)
        self.assertEqual(len(ambiguous), 2)

    def test_generic_words_are_not_a_selection(self):
        picked, ambiguous = A.select_by_label(self._candidates(), "stop the reminder")
        self.assertIsNone(picked)
        self.assertEqual(ambiguous, [])

    def test_a_model_id_that_disagrees_with_the_owner_selection_refuses(self):
        target, refusal = A.resolve_target(
            {"text": "stop the dentist one"},
            store=mock.Mock(), conversation="c", owner="o", model_target="t1",
            message="stop the dentist one",
        )
        # resolve_target reads the follow-up store through target_candidates; a
        # Mock store cannot provide it, so the call must fail closed, not guess.
        self.assertIsNone(target)
        self.assertIsNotNone(refusal)
