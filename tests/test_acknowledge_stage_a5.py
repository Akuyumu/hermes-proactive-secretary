"""Stage A.5 regressions: the six reproduced blockers stay closed.

Every test drives the real tool handler or the real acknowledgement helpers.
Only paths, the clock and the fake host facade are synthetic; no model, network
or background runner is involved.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
from proactive_pilot import notice_voice  # noqa: E402
from proactive_pilot import pending_ack as P  # noqa: E402
from proactive_pilot import policy  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

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


class StageA5Harness(unittest.TestCase):
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

    def _followup(self, text="Test reminder"):
        return F.create(self.store, text, kind="one_shot",
                        check_hint=f"check {text}", now=self.clock.now())

    def _call(self, body, *, session_id="conv-1", task_id=None):
        return json.loads(tool.tool_handler(self.ctx)(
            body, session_id=session_id, task_id=task_id))

    def _propose(self, *, text, quote, action="stop", **extra):
        body = {"mode": "propose", "text": text, "action": action,
                "quote": quote, "evidence_quote": quote}
        body.update(extra)
        return self._call(body)

    def _confirm(self, *, text="yes"):
        return self._call({"mode": "confirm", "text": text,
                           "evidence_quote": text})

    @staticmethod
    def _strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from StageA5Harness._strings(key)
                yield from StageA5Harness._strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from StageA5Harness._strings(item)


class OneTurnOneModeTests(StageA5Harness):
    def test_the_proposal_turn_cannot_confirm_itself(self):
        entry = self._followup("Dentist")
        self._turn("yes", reply_to=entry["id"])
        proposal = self._propose(text="yes", quote="yes", action="stop")
        self.assertTrue(proposal["ok"], proposal)

        before = (self.state_dir / F.STORE).read_bytes()
        confirmed = self._confirm()
        self.assertFalse(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["reason"]["code"], "same_turn")
        self.assertEqual((self.state_dir / F.STORE).read_bytes(), before)
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")
        self.assertEqual(P.rows(self.store)[0]["consumed_at"], "")

    def test_a_second_proposal_refuses_and_never_replaces_the_first_effect(self):
        first = self._followup("Dentist")
        second = self._followup("Invoice tomorrow morning")
        turn = self._turn("stop the dentist", reply_to=first["id"])
        made = self._propose(text=turn["text"], quote="stop the dentist")
        self.assertTrue(made["ok"], made)
        stored = P.rows(self.store)[0]
        frozen = deepcopy(stored["effect"])

        turn = self._turn("snooze the invoice for 20 minutes", at=time.time() + 1,
                          reply_to=second["id"])
        refused = self._propose(
            text=turn["text"], quote="snooze the invoice", action="later",
            minutes=20, scope="invoice")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["reason"]["code"], "proposal_live")
        rows = P.rows(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["effect"], frozen)
        self.assertEqual(rows[0]["target_id"], f"followup:{first['id']}")
        self.assertEqual(F.get(self.store, first["id"])["status"], "active")
        self.assertEqual(F.get(self.store, second["id"])["status"], "active")
        self.assertFalse(self.preferences.exists())

    def test_apply_refuses_a_turn_already_used_by_propose(self):
        entry = self._followup("Dentist")
        self._turn("stop", reply_to=entry["id"])
        self.assertTrue(self._propose(text="stop", quote="stop")["ok"])
        applied = self._call({"text": "stop", "evidence_quote": "stop"})
        self.assertFalse(applied["ok"], applied)
        self.assertEqual(applied["reason"]["code"], "turn_consumed")
        self.assertEqual(F.get(self.store, entry["id"])["status"], "active")


class RenderedEffectTests(StageA5Harness):
    def test_the_question_and_matcher_come_from_the_pinned_target(self):
        dentist = self._followup("Dentist")
        invoice = self._followup("Invoice tomorrow morning")
        turn = self._turn("snooze the dentist for 20 minutes",
                          reply_to=dentist["id"])
        proposal = self._propose(
            text=turn["text"], quote="snooze the dentist",
            action="later", minutes=20, scope="invoice")
        self.assertTrue(proposal["ok"], proposal)
        self.assertIn("Dentist", proposal["message"])
        self.assertNotIn("Invoice tomorrow morning", proposal["message"])
        self.assertEqual(proposal["deadline"], "20 minutes")
        row = P.rows(self.store)[0]
        self.assertEqual(row["effect"]["scope"], "Dentist")
        self.assertNotEqual(row["effect"]["scope"], "invoice")

        self._turn("yes", at=time.time() + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        snooze = prefs["snoozes"][0]
        target = f"followup:{dentist['id']}"
        self.assertEqual(snooze["target_id"], target)
        self.assertEqual(snooze["key"], row["key"])
        self.assertEqual(snooze["match"], target.lower())
        self.assertEqual(snooze["scope"], "Dentist")

        self.assertTrue(policy.preference_blocks(
            {"kind": "one_shot", "subject": "Dentist", "message": "Dentist",
             "target_id": target}, prefs, self.clock.now()))
        self.assertFalse(policy.preference_blocks(
            {"kind": "one_shot", "subject": "Invoice tomorrow morning",
             "message": "Invoice tomorrow morning",
             "target_id": f"followup:{invoice['id']}"}, prefs, self.clock.now()))
        # No id to compare: fail open to delivery, never guess by label.
        self.assertFalse(policy.preference_blocks(
            {"kind": "one_shot", "subject": "Dentist", "message": "Dentist"},
            prefs, self.clock.now()))
        self.assertFalse(policy.active_snooze(prefs, self.clock.now()))
        self.assertTrue(policy.active_snooze(prefs, self.clock.now(), target))
        self.assertFalse(policy.active_snooze(
            prefs, self.clock.now(), f"followup:{invoice['id']}"))

        # Legacy rows keep their old text matching unchanged.
        legacy = {"snoozes": [dict(snooze, target_id="", key="",
                                   match="dentist")]}
        self.assertTrue(policy.preference_blocks(
            {"kind": "one_shot", "subject": "Dentist", "message": "Dentist"},
            legacy, self.clock.now()))
        self.assertFalse(policy.preference_blocks(
            {"kind": "one_shot", "subject": "Invoice", "message": "Invoice"},
            legacy, self.clock.now()))

    def test_a_deadline_that_cannot_be_rendered_is_refused_not_vagued(self):
        entry = self._followup("Dentist")
        turn = self._turn("later until sometime tomorrow", reply_to=entry["id"])
        refused = self._propose(
            text=turn["text"], quote="later until sometime tomorrow",
            action="later", until="tomorrow")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["reason"]["code"], "effect_invalid")
        self.assertEqual(P.rows(self.store), [])
        with self.assertRaises(ValueError):
            A.render_question({"action": "snooze", "until": "not-a-date"},
                              "Dentist")


class ConfirmationSafetyTests(StageA5Harness):
    def _proposed_snooze(self):
        entry = self._followup("Dentist")
        turn = self._turn("snooze the dentist for 20 minutes",
                          reply_to=entry["id"])
        proposal = self._propose(
            text=turn["text"], quote="snooze the dentist",
            action="later", minutes=20)
        self.assertTrue(proposal["ok"], proposal)
        return entry, P.rows(self.store)[0]["key"]

    def test_injected_marker_save_failure_retries_without_a_second_effect(self):
        entry, key = self._proposed_snooze()
        self._turn("yes", at=time.time() + 1)
        real_save = P._save
        calls = {"count": 0}

        def flaky(store, data, now):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected failure between effect and marker")
            return real_save(store, data, now)

        with mock.patch.object(P, "_save", side_effect=flaky):
            with self.assertRaises(OSError):
                self._confirm()
        self.assertEqual(calls["count"], 2)
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        self.assertEqual(prefs["snoozes"][0]["key"], key)
        self.assertEqual(P.rows(self.store)[0]["consumed_at"], "")

        retried = self._confirm()
        self.assertTrue(retried["ok"], retried)
        again = self._confirm()
        self.assertEqual(again, retried)
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        row = P.rows(self.store)[0]
        self.assertEqual(row["receipt"]["status"], "snoozed")
        self.assertNotEqual(row["consumed_at"], "")

    def test_concurrent_confirms_write_exactly_one_effect(self):
        entry, key = self._proposed_snooze()
        self._turn("yes", at=time.time() + 1)
        results = []
        barrier = threading.Barrier(2)

        def race():
            barrier.wait()
            results.append(self._confirm())

        threads = [threading.Thread(target=race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        winners = [item for item in results if item.get("ok")]
        self.assertTrue(winners, results)
        for item in results:
            if not item.get("ok"):
                self.assertEqual(item.get("status"), "already_done")
        prefs = feedback.load_preferences_file(self.preferences, strict=True)
        self.assertEqual(len(prefs["snoozes"]), 1)
        self.assertEqual(prefs["snoozes"][0]["key"], key)
        retry = self._confirm()
        self.assertEqual(retry, winners[0])
        self.assertEqual(len(feedback.load_preferences_file(
            self.preferences, strict=True)["snoozes"]), 1)


class OwnerSelectionTests(StageA5Harness):
    def test_full_labels_select_and_never_score(self):
        candidates = [
            {"target_id": "dentist", "target_label": "Dentist"},
            {"target_id": "invoice",
             "target_label": "Invoice tomorrow morning"},
        ]
        picked, ambiguous = A.select_by_label(
            candidates, "stop the dentist tomorrow morning")
        self.assertEqual(ambiguous, [])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["target_id"], "dentist")

        self.assertEqual(
            A.select_by_label(candidates, "I will reply"), (None, []))
        self.assertEqual(
            A.select_by_label(
                [{"target_id": "reply", "target_label": "reply tomorrow"}],
                "I will reply"),
            (None, []))

        # The same choice through the real resolver never picks the sibling.
        dentist = self._followup("Dentist")
        self._followup("Invoice tomorrow morning")
        self._turn("stop the dentist tomorrow morning")
        target, refusal = A.resolve_target(
            self.ctx.state.data[R.TURN_KEY][-1], store=self.store,
            conversation="conv-1", owner="owner",
            message="stop the dentist tomorrow morning", now=self.clock.now())
        if target is not None:
            self.assertEqual(target["target_id"], f"followup:{dentist['id']}")
        else:
            self.assertEqual(refusal["status"], "needs_clarification")

    def test_an_unmatched_explicit_reply_never_falls_through(self):
        self._followup("Dentist")
        self._turn("stop the dentist", reply_to="fu_missing")
        target, refusal = A.resolve_target(
            self.ctx.state.data[R.TURN_KEY][-1], store=self.store,
            conversation="conv-1", owner="owner",
            message="stop the dentist", now=self.clock.now())
        self.assertIsNone(target)
        self.assertIn(refusal["status"], {"needs_clarification", "refused"})

    def test_multiple_live_pins_refuse_instead_of_picking_the_newest(self):
        self._followup("Dentist")
        self._turn("stop")
        first = P.create(
            conversation="conv-1", owner="owner",
            target_id="followup:one", target_label="Dentist",
            effect={"action": "stop", "kind": "", "scope": "Dentist"},
            question="Stop for “Dentist” only?", turn_id="turn-1",
            now=self.clock.now(), store=self.store)
        self.assertTrue(first["ok"], first)
        data = self.store.load(P.STORE, P.empty_store(), strict=True)
        second = deepcopy(data["rows"][0])
        second.update(id="ack-second", turn_id="turn-2", key="ack-second")
        data["rows"].append(second)
        self.store.save(P.STORE, data)

        target, refusal = A.resolve_target(
            self.ctx.state.data[R.TURN_KEY][-1], store=self.store,
            conversation="conv-1", owner="owner", message="stop",
            now=self.clock.now())
        self.assertIsNone(target)
        self.assertEqual(refusal["status"], "refused")
        self.assertEqual(refusal["reason_code"], "ambiguous_pending")


class HonestEffectTests(StageA5Harness):
    def test_an_emitted_reminder_records_not_promises(self):
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
        turn = self._turn("stop the reminder", reply_to="rem-1")
        self.assertTrue(self._propose(text=turn["text"],
                                      quote="stop the reminder")["ok"])
        self._turn("yes", at=time.time() + 1)
        result = self._confirm()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "recorded")
        self.assertNotIn("no further", result["message"].lower())
        receipt = P.rows(self.store)[0]["receipt"]
        self.assertEqual(receipt["status"], "recorded")
        self.assertFalse(receipt.get("no_further_followup"))
        self.assertIn("Recorded", receipt["note"])
        self.assertEqual(P.dispositions(self.store)["rem-1"]["action"], "stop")

        # The seeder is deliberately not wired in this stage. It may still
        # create its one follow-up; the receipt must not pretend otherwise.
        seeded = F.portal_rows(self.store, now=self.clock.now())
        self.assertLessEqual(len(seeded), 1)
        again = F.portal_rows(self.store, now=self.clock.now() + timedelta(minutes=5))
        self.assertEqual([row["id"] for row in again],
                         [row["id"] for row in seeded])

    def test_a_notice_closure_this_stage_cannot_do_refuses(self):
        result = A.apply_target(
            "stop",
            target={"target_id": "notice:abc", "target_type": "notice",
                    "target_label": "A delivery-only notice"},
            now=self.clock.now(), followup_store=self.store,
            pending_store=self.store)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "notice_not_supported")
        self.assertEqual(P.dispositions(self.store), {})


class PublicProseTests(StageA5Harness):
    ALLOWED = {"ok", "status", "action", "target_label", "deadline", "message",
               "reason"}

    def _assert_public(self, body):
        self.assertLessEqual(set(body), self.ALLOWED)
        text = json.dumps(body, ensure_ascii=False)
        for forbidden in ("fu_deadbeef", "ack-ab12", "/home/owner/private",
                          "a@b.com", "hunter2", "Traceback", "KeyError"):
            self.assertNotIn(forbidden, text)
        if "reason" in body:
            self.assertLessEqual(set(body["reason"]), {"code", "message"})

    def test_question_receipt_and_refusal_are_shaped(self):
        entry = self._followup(
            "fu_deadbeef ack-ab12 /home/owner/private a@b.com Dentist")
        turn = self._turn(
            "stop the dentist because hunter2 is private", reply_to=entry["id"])
        proposal = self._propose(text=turn["text"],
                                 quote="stop the dentist")
        self.assertTrue(proposal["ok"], proposal)
        self._assert_public(proposal)
        self.assertNotIn("hunter2", proposal["message"])

        self._turn("yes", at=time.time() + 1)
        confirmed = self._confirm()
        self.assertTrue(confirmed["ok"], confirmed)
        self._assert_public(confirmed)

        refusal = self._call({"text": "banana hunter2",
                              "evidence_quote": "banana hunter2"})
        self.assertFalse(refusal["ok"], refusal)
        self._assert_public(refusal)
        self.assertNotIn("hunter2", json.dumps(refusal))

        self._turn("stop the dentist", at=time.time() + 2)
        bad_quote = self._call({
            "mode": "propose", "text": "stop the dentist", "action": "stop",
            "quote": "words that are not there",
            "evidence_quote": "stop the dentist"})
        self.assertFalse(bad_quote["ok"], bad_quote)
        self._assert_public(bad_quote)
        self.assertEqual(bad_quote["status"], "refused")

        fresh = self._followup("Dentist check")
        self._turn("stop the dentist check", at=time.time() + 3,
                   reply_to=fresh["id"])
        with mock.patch.object(
                A, "propose",
                side_effect=RuntimeError("ack-ab12 /home/private a@b.com")):
            crashed = self._propose(text="stop the dentist check",
                                    quote="stop the dentist")
        self.assertFalse(crashed["ok"], crashed)
        self.assertEqual(crashed["reason"]["code"], "internal_error")
        self._assert_public(crashed)

    def test_every_mode_denies_sockets(self):
        entry = self._followup("Dentist")
        turn = self._turn("snooze the dentist for 20 minutes",
                          reply_to=entry["id"])
        with mock.patch("socket.socket",
                        side_effect=AssertionError("network")), \
                mock.patch("socket.create_connection",
                           side_effect=AssertionError("network")):
            self.assertTrue(self._propose(
                text=turn["text"], quote="snooze the dentist",
                action="later", minutes=20)["ok"])
            self._turn("yes", at=time.time() + 1)
            self.assertTrue(self._confirm()["ok"])
            self._turn("stop", at=time.time() + 2, reply_to=entry["id"])
            self.assertTrue(self._call(
                {"text": "stop", "evidence_quote": "stop"})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
