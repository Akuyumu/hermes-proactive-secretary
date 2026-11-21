import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_pilot import followups as F
from proactive_pilot.clock import Clock
from proactive_pilot.state import CorruptStateError, StateStore


SG = timezone(timedelta(hours=8))


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root, Clock())
        self.now = datetime(2026, 1, 1, 9, 0, tzinfo=SG)

    def tearDown(self):
        self._tmp.cleanup()

    def reload(self):
        return StateStore(self.root, Clock())

    def test_create_roundtrip_and_refusals(self):
        entry = F.create(
            self.store,
            "  keep   an eye on the tide  ",
            kind="ongoing",
            source="desktop",
            check_hint="check the harbour tide level",
            now=self.now,
        )
        self.assertEqual(entry["text"], "keep an eye on the tide")
        self.assertEqual(entry["kind"], "ongoing")
        self.assertEqual(entry["status"], "active")
        self.assertTrue(entry["id"].startswith("fu_"))
        self.assertEqual(F.get(self.reload(), entry["id"])["text"], entry["text"])

        before_refusals = (self.root / F.STORE).read_bytes()
        for bad in ("   ", "x" * 301, "*", "all", "everything", "anything"):
            with self.assertRaises(F.FollowupError):
                F.create(self.store, bad, now=self.now)
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "not a kind", kind="sometimes", now=self.now)
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "x" * 10, source="s" * 25, now=self.now)
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "x" * 10, check_hint="h" * 201, now=self.now)
        for missing_hint in (None, "", "   ", "\t\n"):
            with self.assertRaises(F.FollowupError):
                F.create(self.store, "missing hint watch", check_hint=missing_hint,
                         now=self.now)
        self.assertEqual((self.root / F.STORE).read_bytes(), before_refusals)

        for index in range(19):
            F.create(self.store, f"item {index}", check_hint=f"check item {index}",
                     now=self.now)
        before_limit = (self.root / F.STORE).read_bytes()
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "one too many", check_hint="check the extra item",
                     now=self.now)
        self.assertEqual((self.root / F.STORE).read_bytes(), before_limit)
        self.assertEqual(F.summary(self.store)["active"], 20)

    def test_missing_or_blank_hint_refuses_without_writing_and_legacy_needs_clarification(self):
        for missing_hint in (None, "", "   ", "\t\n"):
            with self.assertRaises(F.FollowupError):
                F.create(self.store, "missing hint watch", check_hint=missing_hint,
                         now=self.now)
            self.assertFalse((self.root / F.STORE).exists())

        legacy = {
            "id": "fu_legacy00000000",
            "text": "legacy blank hint watch",
            "kind": "ongoing",
            "status": "active",
            "created_at": self.now.isoformat(),
            "updated_at": self.now.isoformat(),
            "source": "legacy",
            "check_hint": "",
            "last_checked_at": "",
            "last_result": "",
            "check_count": 0,
            "notified_count": 0,
            "seen": [],
            "notice_reservations": [],
            "notice_event_key": "",
            "notice_status": "",
            "closed_at": "",
            "close_reason": "",
        }
        data = F.empty_store()
        data["entries"][legacy["id"]] = legacy
        self.store.save(F.STORE, data)

        for row in (F.active_entries(self.store)[0],
                    F.wake_entries(self.store)[0],
                    F.portal_rows(self.store)[0]):
            self.assertTrue(row["needs_clarification"], row)
            self.assertEqual(row["clarification"], F.NEEDS_CLARIFICATION_TEXT)

        hinted = F.create(self.store, "hinted watch", kind="ongoing",
                          check_hint="check the hinted tide gauge", now=self.now)
        hinted_row = next(row for row in F.active_entries(self.store)
                          if row["id"] == hinted["id"])
        self.assertFalse(hinted_row["needs_clarification"])
        self.assertNotIn("clarification", hinted_row)

    def test_provenance_is_bounded_visible_and_optional_for_legacy(self):
        entry = F.create(
            self.store,
            "provenance watch",
            check_hint="check the provenance tide",
            evidence_quote="Could you track the provenance tide?",
            source_ref="conversation:abc123",
            filed_by="owner",
            now=self.now,
        )
        self.assertEqual(entry["filed_by"], "owner")
        self.assertEqual(entry["evidence_quote"],
                         "Could you track the provenance tide?")
        self.assertEqual(entry["source_ref"], "conversation:abc123")
        row = F.active_entries(self.store)[0]
        self.assertEqual(row["filed_by"], "owner")
        self.assertEqual(row["evidence_quote"], entry["evidence_quote"])
        self.assertEqual(row["source_ref"], entry["source_ref"])

        agent_entry = F.create(
            self.store,
            "agent request watch",
            check_hint="check the agent request",
            filed_by="agent",
            now=self.now,
        )
        self.assertEqual(agent_entry["filed_by"], "agent")
        self.assertEqual(agent_entry["evidence_quote"], "")
        self.assertEqual(agent_entry["source_ref"], "")

        before = (self.root / F.STORE).read_bytes()
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "bad filer", check_hint="check the bad filer",
                     filed_by="robot", now=self.now)
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "long quote", check_hint="check the long quote",
                     evidence_quote="q" * (F.MAX_EVIDENCE_QUOTE_CHARS + 1),
                     now=self.now)
        with self.assertRaises(F.FollowupError):
            F.create(self.store, "long ref", check_hint="check the long ref",
                     source_ref="r" * (F.MAX_SOURCE_REF_CHARS + 1), now=self.now)
        self.assertEqual((self.root / F.STORE).read_bytes(), before)

    def test_one_shot_queue_and_delivered(self):
        entry = F.create(self.store, "check the parcel", kind="one_shot",
                         check_hint="check the parcel tracker", now=self.now)
        fid = entry["id"]
        F.record_check(self.store, fid, "match", fingerprint="fp-1", now=self.now)
        queued = F.queue_notice(self.store, fid, "evt-1", fingerprint="fp-1", now=self.now)
        self.assertEqual(queued["status"], "awaiting_delivery")
        self.assertEqual(queued["notice_event_key"], "evt-1")
        self.assertIn("fp-1", queued["seen"])
        with self.assertRaises(F.FollowupError):
            F.queue_notice(self.store, fid, "evt-2", fingerprint="fp-2", now=self.now)

        done = F.resolve_notice(self.store, fid, "delivered", now=self.now)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["close_reason"], "notified")
        self.assertEqual(done["notice_status"], "delivered")
        self.assertTrue(done["closed_at"])
        self.assertEqual(F.get(self.reload(), fid)["status"], "done")

    def test_owner_receipt_closes_distinctly_and_replay_is_idempotent(self):
        fid = F.create(self.store, "owner receipt watch", kind="one_shot",
                       check_hint="check the owner receipt parcel", now=self.now)["id"]
        key = F.notice_event_key(fid)
        ref = "notice-receipt:" + "a" * 24
        with self.assertRaises(F.FollowupError):
            F.confirm_owner_receipt(self.store, fid, key, ref, now=self.now)

        F.record_check(self.store, fid, "match", fingerprint="owner-fp", now=self.now)
        queued = F.queue_notice(
            self.store, fid, key, fingerprint="owner-fp", now=self.now)
        self.assertEqual(queued["status"], "awaiting_delivery")
        before = (self.root / F.STORE).read_bytes()

        with self.assertRaises(F.FollowupError):
            F.confirm_owner_receipt(
                self.store, fid, "followup-notice:wrong", ref, now=self.now)
        self.assertEqual((self.root / F.STORE).read_bytes(), before)

        confirmed = F.confirm_owner_receipt(
            self.store, fid, key, ref, now=self.now)
        self.assertEqual(confirmed["status"], "done")
        self.assertEqual(confirmed["close_reason"], "owner_confirmed")
        self.assertEqual(confirmed["notice_status"], "owner_confirmed")
        self.assertEqual(confirmed["owner_confirmed_ref"], ref)
        self.assertTrue(confirmed["owner_confirmed_at"])
        self.assertNotEqual(confirmed["notice_status"], "delivered")
        self.assertNotEqual(confirmed["close_reason"], "notified")
        self.assertEqual(F.summary(self.store)["awaiting_delivery"], 0)
        self.assertEqual(F.get(self.reload(), fid)["owner_confirmed_ref"], ref)

        after = (self.root / F.STORE).read_bytes()
        replay = F.confirm_owner_receipt(
            self.store, fid, key, ref, now=self.now + timedelta(hours=1))
        self.assertEqual(replay["owner_confirmed_ref"], ref)
        self.assertEqual((self.root / F.STORE).read_bytes(), after)

        with self.assertRaises(F.FollowupError):
            F.confirm_owner_receipt(
                self.store, fid, key, "notice-receipt:" + "b" * 24, now=self.now)
        self.assertEqual((self.root / F.STORE).read_bytes(), after)

        with self.assertRaises(F.FollowupError):
            F.queue_notice(
                self.store, fid, key, fingerprint="owner-fp", now=self.now)
        with self.assertRaises(F.FollowupError):
            F.resolve_notice(self.store, fid, "delivered", now=self.now)

        ongoing = F.create(self.store, "ongoing receipt refusal", kind="ongoing",
                           check_hint="check the ongoing receipt", now=self.now)["id"]
        with self.assertRaises(F.FollowupError):
            F.confirm_owner_receipt(
                self.store, ongoing,
                F.notice_event_key(ongoing, "ongoing-fp"),
                "notice-receipt:" + "c" * 24, now=self.now)

    def test_awaiting_budget_is_independent_from_active_checks(self):
        for index in range(F.MAX_AWAITING_FOLLOWUPS):
            fid = F.create(
                self.store, f"awaiting {index}", kind="one_shot",
                check_hint=f"check awaiting item {index}", now=self.now)["id"]
            F.record_check(self.store, fid, "match",
                           fingerprint=f"await-{index}", now=self.now)
            F.queue_notice(self.store, fid, f"await-evt-{index}",
                           fingerprint=f"await-{index}", now=self.now)

        summary = F.summary(self.store)
        self.assertEqual(summary["awaiting_delivery"], F.MAX_AWAITING_FOLLOWUPS)
        self.assertEqual(summary["active"], 0)
        self.assertLessEqual(
            summary["total"],
            F.MAX_AWAITING_FOLLOWUPS + F.MAX_ACTIVE_FOLLOWUPS + F.MAX_CLOSED,
        )

        created = F.create(self.store, "new active check", kind="ongoing",
                           check_hint="check the new active gauge", now=self.now)
        self.assertEqual(created["status"], "active")
        self.assertEqual(F.summary(self.store)["active"], 1)

    def test_pending_limit_defers_before_reservation_and_restart_keeps_watch(self):
        awaiting_ids = []
        for index in range(F.MAX_AWAITING_FOLLOWUPS):
            fid = F.create(
                self.store, f"pending {index}", kind="one_shot",
                check_hint=f"check pending item {index}", now=self.now)["id"]
            F.record_check(self.store, fid, "match",
                           fingerprint=f"pending-{index}", now=self.now)
            F.queue_notice(self.store, fid, f"pending-evt-{index}",
                           fingerprint=f"pending-{index}", now=self.now)
            awaiting_ids.append(fid)

        extra = F.create(self.store, "deferred one shot", kind="one_shot",
                         check_hint="check the deferred parcel", now=self.now)
        F.record_check(self.store, extra["id"], "match",
                       fingerprint="deferred-fp", now=self.now)
        before = (self.root / F.STORE).read_bytes()
        with self.assertRaises(F.FollowupCapacityError) as raised:
            F.queue_notice(self.store, extra["id"], "deferred-evt",
                           fingerprint="deferred-fp", now=self.now)
        self.assertEqual(raised.exception.capacity, "pending")
        self.assertEqual((self.root / F.STORE).read_bytes(), before)

        after_restart = F.get(self.reload(), extra["id"])
        self.assertEqual(after_restart["status"], "active")
        self.assertNotIn("deferred-fp", after_restart["seen"])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn("deferred-evt", data["dedupe"])

        stopped = F.close(self.store, awaiting_ids[0], "owner_stopped", now=self.now)
        self.assertEqual(stopped["status"], "done")
        queued = F.queue_notice(self.store, extra["id"], "deferred-evt",
                                fingerprint="deferred-fp", now=self.now)
        self.assertEqual(queued["status"], "awaiting_delivery")

    def test_release_refills_pending_capacity_without_stranding(self):
        for index in range(F.MAX_AWAITING_FOLLOWUPS - 1):
            fid = F.create(
                self.store, f"filled {index}", kind="one_shot",
                check_hint=f"check filled item {index}", now=self.now)["id"]
            F.record_check(self.store, fid, "match",
                           fingerprint=f"filled-{index}", now=self.now)
            F.queue_notice(self.store, fid, f"filled-evt-{index}",
                           fingerprint=f"filled-{index}", now=self.now)

        extra = F.create(self.store, "rollback one shot", kind="one_shot",
                         check_hint="check the rollback parcel", now=self.now)
        F.record_check(self.store, extra["id"], "match",
                       fingerprint="rollback-fp", now=self.now)
        frozen = F.queue_notice(self.store, extra["id"], "rollback-evt",
                                fingerprint="rollback-fp", now=self.now)
        self.assertEqual(F.summary(self.store)["awaiting_delivery"],
                         F.MAX_AWAITING_FOLLOWUPS)

        released = F.release_notice(
            self.store, extra["id"], "rollback-evt",
            fingerprint="rollback-fp",
            attempt_token=frozen["attempt_token"], now=self.now)
        self.assertEqual(released["status"], "active")
        self.assertEqual(F.summary(self.store)["awaiting_delivery"],
                         F.MAX_AWAITING_FOLLOWUPS - 1)

        refilled = F.queue_notice(self.store, extra["id"], "rollback-evt",
                                  fingerprint="rollback-fp", now=self.now)
        self.assertEqual(refilled["status"], "awaiting_delivery")
        self.assertEqual(F.summary(self.store)["awaiting_delivery"],
                         F.MAX_AWAITING_FOLLOWUPS)

    def test_delivery_uncertainty_never_completes_or_requeues(self):
        fid = F.create(self.store, "one-shot uncertainty", kind="one_shot",
                       check_hint="check the delivery status", now=self.now)["id"]
        F.record_check(self.store, fid, "match", fingerprint="fp-u", now=self.now)
        F.queue_notice(self.store, fid, "evt-u", fingerprint="fp-u", now=self.now)

        for status in ("emitted", "attempted", "unknown", "emitted"):
            entry = F.resolve_notice(self.store, fid, status, now=self.now)
            self.assertEqual(entry["status"], "awaiting_delivery")
            self.assertNotEqual(entry["status"], "done")
            self.assertEqual(entry["notice_status"], status)
            with self.assertRaises(F.FollowupError):
                F.queue_notice(self.store, fid, f"retry-{status}", now=self.now)

        done = F.resolve_notice(self.store, fid, "delivered", now=self.now)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["close_reason"], "notified")

    def test_ongoing_survives_quiet_and_failure_until_explicit_close(self):
        fid = F.create(self.store, "ongoing quiet watch", kind="ongoing",
                       check_hint="check the tide level", now=self.now)["id"]
        for result in ("no_match", "error", "blocked", "no_match"):
            entry = F.record_check(self.store, fid, result, now=self.now)
            self.assertEqual(entry["status"], "active")
            self.assertEqual(entry["last_result"], result)
            self.assertGreaterEqual(entry["check_count"], 1)

        F.record_check(self.store, fid, "match", fingerprint="m-1", now=self.now)
        first = F.queue_notice(self.store, fid, "evt-m1", fingerprint="m-1", now=self.now)
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["notified_count"], 1)
        self.assertIn("m-1", first["seen"])

        F.record_check(self.store, fid, "match", fingerprint="m-1", now=self.now)
        with self.assertRaises(F.FollowupError):
            F.queue_notice(self.store, fid, "evt-m2", fingerprint="m-1", now=self.now)
        self.assertEqual(F.get(self.reload(), fid)["notified_count"], 1)

        F.record_check(self.store, fid, "no_match", now=self.now)
        self.assertEqual(F.get(self.store, fid)["status"], "active")

        stopped = F.close(self.store, fid, "owner_stopped", now=self.now)
        self.assertEqual(stopped["status"], "done")
        self.assertEqual(stopped["close_reason"], "owner_stopped")
        with self.assertRaises(F.FollowupError):
            F.record_check(self.store, fid, "no_match", now=self.now)

    def test_ongoing_release_earlier_then_later_keeps_exact_pair(self):
        fid = F.create(self.store, "two ongoing matches", kind="ongoing",
                       check_hint="check both tide matches", now=self.now)["id"]
        first_key = F.notice_event_key(fid, "fp-first")
        second_key = F.notice_event_key(fid, "fp-second")
        first = F.queue_notice(
            self.store, fid, first_key, fingerprint="fp-first", now=self.now)
        second = F.queue_notice(
            self.store, fid, second_key, fingerprint="fp-second", now=self.now)

        released_first = F.release_notice(
            self.store, fid, first_key, fingerprint="fp-first",
            attempt_token=first["attempt_token"], now=self.now)
        self.assertEqual(released_first["status"], "active")
        self.assertEqual(released_first["notice_event_key"], second_key)
        self.assertEqual(released_first["notified_count"], 1)
        self.assertEqual(released_first["seen"], ["fp-second"])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn(first_key, data["dedupe"])
        self.assertIn(second_key, data["dedupe"])

        released_second = F.release_notice(
            self.store, fid, second_key, fingerprint="fp-second",
            attempt_token=second["attempt_token"], now=self.now)
        self.assertEqual(released_second["status"], "active")
        self.assertEqual(released_second["notice_event_key"], "")
        self.assertEqual(released_second["notified_count"], 0)
        self.assertEqual(released_second["seen"], [])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn(first_key, data["dedupe"])
        self.assertNotIn(second_key, data["dedupe"])

    def test_ongoing_release_later_then_earlier_keeps_exact_pair(self):
        fid = F.create(self.store, "reverse ongoing matches", kind="ongoing",
                       check_hint="check the reversed tide matches", now=self.now)["id"]
        first_key = F.notice_event_key(fid, "fp-first")
        second_key = F.notice_event_key(fid, "fp-second")
        first = F.queue_notice(
            self.store, fid, first_key, fingerprint="fp-first", now=self.now)
        second = F.queue_notice(
            self.store, fid, second_key, fingerprint="fp-second", now=self.now)

        released_second = F.release_notice(
            self.store, fid, second_key, fingerprint="fp-second",
            attempt_token=second["attempt_token"], now=self.now)
        self.assertEqual(released_second["status"], "active")
        self.assertEqual(released_second["notice_event_key"], first_key)
        self.assertEqual(released_second["notified_count"], 1)
        self.assertEqual(released_second["seen"], ["fp-first"])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertIn(first_key, data["dedupe"])
        self.assertNotIn(second_key, data["dedupe"])

        released_first = F.release_notice(
            self.store, fid, first_key, fingerprint="fp-first",
            attempt_token=first["attempt_token"], now=self.now)
        self.assertEqual(released_first["status"], "active")
        self.assertEqual(released_first["notice_event_key"], "")
        self.assertEqual(released_first["notified_count"], 0)
        self.assertEqual(released_first["seen"], [])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn(first_key, data["dedupe"])
        self.assertNotIn(second_key, data["dedupe"])

    def test_ongoing_release_preserves_an_accepted_match(self):
        fid = F.create(self.store, "accepted ongoing match", kind="ongoing",
                       check_hint="check the accepted tide match", now=self.now)["id"]
        earlier_key = F.notice_event_key(fid, "fp-earlier")
        accepted_key = F.notice_event_key(fid, "fp-accepted")
        earlier = F.queue_notice(
            self.store, fid, earlier_key, fingerprint="fp-earlier", now=self.now)
        F.queue_notice(self.store, fid, accepted_key, fingerprint="fp-accepted", now=self.now)
        accepted = F.resolve_notice(self.store, fid, "delivered", now=self.now)
        self.assertEqual(accepted["notice_status"], "delivered")

        released = F.release_notice(
            self.store, fid, earlier_key, fingerprint="fp-earlier",
            attempt_token=earlier["attempt_token"], now=self.now)
        self.assertEqual(released["notice_status"], "delivered")
        self.assertEqual(released["notice_event_key"], accepted_key)
        self.assertEqual(released["notified_count"], 1)
        self.assertEqual(released["seen"], ["fp-accepted"])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn(earlier_key, data["dedupe"])
        self.assertIn(accepted_key, data["dedupe"])
        self.assertEqual(F.get(self.reload(), fid)["notice_status"], "delivered")

    def test_ongoing_release_never_revives_owner_stopped(self):
        fid = F.create(self.store, "stopped ongoing matches", kind="ongoing",
                       check_hint="check the stopped tide matches", now=self.now)["id"]
        first_key = F.notice_event_key(fid, "fp-first")
        second_key = F.notice_event_key(fid, "fp-second")
        first = F.queue_notice(
            self.store, fid, first_key, fingerprint="fp-first", now=self.now)
        second = F.queue_notice(
            self.store, fid, second_key, fingerprint="fp-second", now=self.now)
        F.close(self.store, fid, "owner_stopped", now=self.now)

        for key, fingerprint, token in (
                (first_key, "fp-first", first["attempt_token"]),
                (second_key, "fp-second", second["attempt_token"])):
            released = F.release_notice(
                self.store, fid, key, fingerprint=fingerprint,
                attempt_token=token, now=self.now)
            self.assertEqual(released["status"], "done")
            self.assertEqual(released["close_reason"], "owner_stopped")
            self.assertEqual(released["notice_event_key"], second_key)
            self.assertEqual(released["notified_count"], 2)
            self.assertEqual(released["seen"], ["fp-first", "fp-second"])

        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertIn(first_key, data["dedupe"])
        self.assertIn(second_key, data["dedupe"])
        final = F.get(self.reload(), fid)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["close_reason"], "owner_stopped")

    def test_stale_attempt_token_cannot_release_a_later_reservation(self):
        fid = F.create(self.store, "same event retry", kind="ongoing",
                       check_hint="check the retry event", now=self.now)["id"]
        key = F.notice_event_key(fid, "fp-same")
        first = F.queue_notice(self.store, fid, key, fingerprint="fp-same", now=self.now)
        first_token = first["attempt_token"]
        F.release_notice(
            self.store, fid, key, fingerprint="fp-same",
            attempt_token=first_token, now=self.now)

        second = F.queue_notice(self.store, fid, key, fingerprint="fp-same", now=self.now)
        second_token = second["attempt_token"]
        self.assertNotEqual(first_token, second_token)

        delayed = F.release_notice(
            self.store, fid, key, fingerprint="fp-same",
            attempt_token=first_token, now=self.now)
        self.assertEqual(delayed["notice_event_key"], key)
        self.assertEqual(delayed["notified_count"], 1)
        self.assertEqual(delayed["seen"], ["fp-same"])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertIn(key, data["dedupe"])

        missing = F.release_notice(
            self.store, fid, key, fingerprint="fp-same", now=self.now)
        self.assertEqual(missing["notified_count"], 1)
        self.assertEqual(missing["seen"], ["fp-same"])

        valid = F.release_notice(
            self.store, fid, key, fingerprint="fp-same",
            attempt_token=second_token, now=self.now)
        self.assertEqual(valid["notice_event_key"], "")
        self.assertEqual(valid["notified_count"], 0)
        self.assertEqual(valid["seen"], [])
        data = json.loads((self.root / F.STORE).read_text(encoding="utf-8"))
        self.assertNotIn(key, data["dedupe"])

        # A one-shot uses the same stable event key for every attempt, so only
        # the current generation token may release it.
        fid = F.create(self.store, "same event one shot", kind="one_shot",
                       check_hint="check the one-shot event", now=self.now)["id"]
        key = F.notice_event_key(fid)
        first = F.queue_notice(self.store, fid, key, fingerprint="fp-one", now=self.now)
        first_token = first["attempt_token"]
        F.release_notice(
            self.store, fid, key, fingerprint="fp-one",
            attempt_token=first_token, now=self.now)
        second = F.queue_notice(self.store, fid, key, fingerprint="fp-one", now=self.now)
        second_token = second["attempt_token"]
        self.assertNotEqual(first_token, second_token)

        delayed = F.release_notice(
            self.store, fid, key, fingerprint="fp-one",
            attempt_token=first_token, now=self.now)
        self.assertEqual(delayed["status"], "awaiting_delivery")
        self.assertEqual(delayed["notice_event_key"], key)
        missing = F.release_notice(
            self.store, fid, key, fingerprint="fp-one", now=self.now)
        self.assertEqual(missing["status"], "awaiting_delivery")
        self.assertEqual(missing["notice_event_key"], key)

        valid = F.release_notice(
            self.store, fid, key, fingerprint="fp-one",
            attempt_token=second_token, now=self.now)
        self.assertEqual(valid["status"], "active")
        self.assertEqual(valid["notice_event_key"], "")
        self.assertEqual(valid["seen"], [])

        # A token cannot undo an accepted/unknown notice state.
        third = F.queue_notice(self.store, fid, key, fingerprint="fp-one", now=self.now)
        F.resolve_notice(self.store, fid, "unknown", now=self.now)
        guarded = F.release_notice(
            self.store, fid, key, fingerprint="fp-one",
            attempt_token=third["attempt_token"], now=self.now)
        self.assertEqual(guarded["status"], "awaiting_delivery")
        self.assertEqual(guarded["notice_status"], "unknown")

    def test_restart_persistence_after_each_transition(self):
        fid = F.create(self.store, "persist me", kind="one_shot",
                       check_hint="check the persisted state", now=self.now)["id"]
        self.assertEqual(F.get(self.reload(), fid)["status"], "active")

        F.record_check(self.store, fid, "match", fingerprint="p-1", now=self.now)
        self.assertEqual(F.get(self.reload(), fid)["last_result"], "match")
        self.assertEqual(F.get(self.reload(), fid)["check_count"], 1)

        F.queue_notice(self.store, fid, "evt-p", fingerprint="p-1", now=self.now)
        self.assertEqual(F.get(self.reload(), fid)["status"], "awaiting_delivery")

        F.resolve_notice(self.store, fid, "unknown", now=self.now)
        self.assertEqual(F.get(self.reload(), fid)["notice_status"], "unknown")
        self.assertEqual(F.get(self.reload(), fid)["status"], "awaiting_delivery")

        F.close(self.store, fid, "owner_canceled", now=self.now)
        final = F.get(self.reload(), fid)
        self.assertEqual(final["status"], "canceled")
        self.assertEqual(final["close_reason"], "owner_canceled")

    def test_corrupt_and_unsupported_state_fail_closed(self):
        path = self.root / "followups.json"
        path.write_text("{not valid json", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(CorruptStateError):
            F.create(self.store, "must not reset", check_hint="check the corrupted state",
                     now=self.now)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        with self.assertRaises(CorruptStateError):
            F.active_entries(self.store)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

        path.write_text(json.dumps({
            "schema": 999,
            "version": 4,
            "updated_at": "",
            "entries": {},
            "dedupe": [],
        }), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(CorruptStateError):
            F.get(self.store, "fu_missing")
        with self.assertRaises(CorruptStateError):
            F.create(self.store, "also must not reset",
                     check_hint="check the unsupported state", now=self.now)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_byte_budget_and_bounded_structured_entries(self):
        self.assertEqual(F.active_entries(self.store), [])
        self.assertEqual(F.awaiting_entries(self.store), [])
        self.assertEqual(F.wake_entries(self.store), [])
        self.assertEqual(F.summary(self.store)["total"], 0)

        fid = F.create(
            self.store,
            "x" * F.MAX_TEXT_CHARS,
            check_hint="y" * F.MAX_CHECK_HINT_CHARS,
            now=self.now,
        )["id"]
        for _ in range(200):
            F.record_check(self.store, fid, "no_match", now=self.now)
        size = (self.root / "followups.json").stat().st_size
        self.assertLess(size, 4096)
        self.assertEqual(F.get(self.store, fid)["check_count"], 200)

        for index in range(14):
            prefix = f"{index} "
            F.create(
                self.store,
                prefix + "z" * (F.MAX_TEXT_CHARS - len(prefix)),
                check_hint="h" * F.MAX_CHECK_HINT_CHARS,
                now=self.now,
            )
        rows = F.wake_entries(self.store, max_entries=15)
        self.assertEqual(len(rows), 15)
        self.assertLessEqual(len(json.dumps(rows, ensure_ascii=False, sort_keys=True)), 12000)
        self.assertEqual(len(F.wake_entries(self.store, max_entries=5)), 5)

    def test_closed_cap_and_receipts_exclude_private_text(self):
        secret_fid = F.create(
            self.store,
            "SECRET-WATCH-TEXT",
            kind="one_shot",
            source="desktop",
            check_hint="PRIVATE-HINT",
            now=self.now,
        )["id"]
        F.record_check(self.store, secret_fid, "match", fingerprint="s-1", now=self.now)
        F.queue_notice(self.store, secret_fid, "secret-evt", fingerprint="s-1", now=self.now)
        F.resolve_notice(self.store, secret_fid, "delivered", now=self.now)
        rows = F.receipts(self.store)
        blob = json.dumps(rows)
        self.assertNotIn("SECRET-WATCH-TEXT", blob)
        self.assertNotIn("PRIVATE-HINT", blob)
        self.assertNotIn("desktop", blob)
        self.assertNotIn("secret-evt", blob)
        for row in rows:
            self.assertFalse({"text", "source", "check_hint", "notice_event_key"} & set(row))

        closed_ids = []
        for index in range(25):
            stamp = self.now + timedelta(minutes=index + 1)
            fid = F.create(self.store, f"closed {index}", kind="one_shot",
                           check_hint=f"check closed item {index}",
                           now=stamp)["id"]
            F.record_check(self.store, fid, "match", fingerprint=f"old-{index}", now=stamp)
            F.queue_notice(self.store, fid, f"old-evt-{index}", now=stamp)
            F.close(self.store, fid, "owner_canceled", now=stamp)
            closed_ids.append(fid)

        summary = F.summary(self.store)
        self.assertEqual(summary["canceled"], 20)
        self.assertEqual(summary["total"], 20)
        self.assertEqual(len(F.receipts(self.store)), 20)
        self.assertEqual(F.get(self.store, closed_ids[-1])["status"], "canceled")
        with self.assertRaises(F.FollowupError):
            F.get(self.store, closed_ids[0])

        revived = F.create(self.store, "revival attempt", kind="one_shot",
                           check_hint="check the revival attempt", now=self.now)["id"]
        F.record_check(self.store, revived, "match", fingerprint="revive", now=self.now)
        with self.assertRaises(F.FollowupError):
            F.queue_notice(self.store, revived, "old-evt-0", now=self.now)

    def test_zero_cron_and_no_writes_outside_root(self):
        fid = F.create(self.store, "write boundary", kind="one_shot",
                       check_hint="check the write boundary", now=self.now)["id"]
        F.record_check(self.store, fid, "match", fingerprint="w-1", now=self.now)
        F.queue_notice(self.store, fid, "evt-w", fingerprint="w-1", now=self.now)
        F.resolve_notice(self.store, fid, "delivered", now=self.now)
        F.create(self.store, "cancel boundary",
                 check_hint="check the cancel boundary state", now=self.now)
        F.close(self.store, F.active_entries(self.store)[0]["id"], "owner_canceled", now=self.now)

        names = {path.name for path in self.root.iterdir()}
        self.assertEqual(names, {"followups.json", "followups.lock"})

        tree = ast.parse(Path(F.__file__).read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports.add(node.module.split(".")[0])
        banned = {"apscheduler", "cron", "crond", "sched", "schedule", "socket",
                  "subprocess", "urllib", "http", "requests"}
        self.assertEqual(imports & banned, set())

        other = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(os.listdir(other.name), [])
        finally:
            other.cleanup()


if __name__ == "__main__":
    unittest.main()
