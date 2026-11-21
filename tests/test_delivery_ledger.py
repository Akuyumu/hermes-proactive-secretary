"""Offline acceptance for the owner-only delivery ledger.

Only temporary profiles are used. Nothing here reads a host database, the
network, private sources or a live scheduler.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import deliveries  # noqa: E402
from proactive_pilot import portal_notices as core  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import CorruptStateError, StateStore  # noqa: E402

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class DeliveryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "profile-a" / "var"
        self.clock = SeededClock(T0.isoformat(), timezone_name="UTC")
        self.store = StateStore(self.state, self.clock)

    def _record(self, store=None, **kwargs):
        params = {
            "message": "A bounded notice",
            "kind": "portal-notice",
            "event_key": "evt-1",
            "notice_id": "notice-1",
            "evidence_refs": ("evidence-1",),
            "job_id": "job-1",
            "job_name": "hermes-proactive-portal-portal",
            "destination": "telegram:owner",
            "now": T0,
            "status": "emitted-unconfirmed",
        }
        params.update(kwargs)
        deliveries.record_emission(store or self.store, **params)

    def test_records_message_kind_job_destination_and_evidence(self):
        self._record()
        rows = deliveries.recent(self.store)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["message"], "A bounded notice")
        self.assertEqual(row["kind"], "portal-notice")
        self.assertEqual(row["job_id"], "job-1")
        self.assertEqual(row["job_name"], "hermes-proactive-portal-portal")
        self.assertEqual(row["destination"], "telegram:owner")
        self.assertEqual(row["event_key"], "evt-1")
        self.assertEqual(row["notice_id"], "notice-1")
        self.assertEqual(row["evidence_refs"], ["evidence-1"])
        self.assertEqual(row["status"], "emitted-unconfirmed")

    def test_message_is_truncated_and_evidence_capped(self):
        self._record(message="x" * 900, evidence_refs=[f"ref-{i}" for i in range(20)])
        row = deliveries.recent(self.store)[0]
        self.assertEqual(len(row["message"]), deliveries.MAX_MESSAGE_CHARS)
        self.assertEqual(len(row["evidence_refs"]), deliveries.MAX_EVIDENCE_REFS)

    def test_rows_are_capped_and_old_rows_pruned(self):
        for index in range(deliveries.MAX_ROWS + 12):
            self._record(event_key=f"evt-{index}", notice_id=f"n-{index}",
                         message=f"notice {index}",
                         now=T0 + timedelta(minutes=index))
        rows = deliveries.recent(self.store, limit=deliveries.MAX_ROWS)
        self.assertEqual(len(rows), deliveries.MAX_ROWS)
        self.assertEqual(rows[0]["message"], f"notice {deliveries.MAX_ROWS + 11}")

        old = T0 - timedelta(days=deliveries.RETENTION_DAYS + 2)
        self._record(event_key="evt-old", notice_id="old", message="stale", now=old)
        self._record(event_key="evt-fresh", notice_id="fresh", message="fresh",
                     now=T0 + timedelta(days=1))
        messages = [row["message"] for row in deliveries.recent(self.store, limit=50)]
        self.assertNotIn("stale", messages)
        self.assertIn("fresh", messages)

    def test_dedupe_by_event_key_and_notice_id(self):
        self._record()
        self._record(event_key="evt-1", notice_id="different", message="second")
        self._record(event_key="different", notice_id="notice-1", message="third")
        self.assertEqual(len(deliveries.recent(self.store)), 1)

    def test_queued_notice_is_not_recorded(self):
        identity = {"job_id": "job-1", "job_name": core.PORTAL_JOB_NAME,
                    "execution_id": "exec-1", "profile": "A"}
        proposal = {"event_key": "evt-queued", "topic": "Topic",
                    "message": "Queued notice",
                    "evidence_refs": ["evidence-1"],
                    "expiry": (T0 + timedelta(hours=1)).isoformat()}
        receipt = core.submit_notice(self.store, self.clock,
                                     identity=identity, proposal=proposal)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(deliveries.recent(self.store), [])

    def test_blocked_dispatch_is_not_recorded(self):
        queue = {
            "schema": core.QUEUE_SCHEMA,
            "pending": [{
                "id": "notice-blocked",
                "event_key": "evt-blocked",
                "topic": "Topic",
                "message": "Blocked notice",
                "evidence_refs": ["evidence-1"],
                "expiry": (T0 + timedelta(hours=2)).isoformat(),
                "received_at": T0.isoformat(),
                "origin": {"job_id": "job-1",
                           "job_name": core.PORTAL_JOB_NAME, "profile": "A"},
            }],
            "seen": {},
            "executions": {},
        }
        self.store.save(core.PORTAL_NOTICES_ARTIFACT, queue)
        with mock.patch.object(core, "_authorize_pending", return_value=None), \
                mock.patch.object(core, "gates_decide",
                                  return_value={"send": False,
                                                "reason": "quiet hours"}):
            result = core.dispatch_notice(
                self.store, self.clock,
                policy={"deliver_target": "telegram:owner"},
                prefs={}, state=self.store.load_notify_state())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(deliveries.recent(self.store), [])

    def test_corrupt_store_never_blocks_recording(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / deliveries.STORE).write_text("{not json", encoding="utf-8")
        self._record(event_key="evt-after-corrupt", notice_id="n")
        rows = deliveries.recent(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_key"], "evt-after-corrupt")
        json.loads((self.state / deliveries.STORE).read_text(encoding="utf-8"))

    def test_corrupt_store_summary_is_safe_and_recent_is_honest(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / deliveries.STORE).write_text("[]", encoding="utf-8")
        self.assertIsNone(deliveries.summary_since(self.store))
        self.assertEqual(deliveries.recent(self.store, strict=False), [])
        with self.assertRaises(CorruptStateError):
            deliveries.recent(self.store, strict=True)

    def test_symlinked_store_is_refused_without_writing_target(self):
        self.state.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside.json"
        target.write_text("outside", encoding="utf-8")
        (self.state / deliveries.STORE).symlink_to(target)
        self._record(event_key="evt-symlink", notice_id="n")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")
        with self.assertRaises(CorruptStateError):
            deliveries.recent(self.store, strict=True)

    def test_symlinked_lock_is_refused(self):
        self.state.mkdir(parents=True, exist_ok=True)
        target = self.root / "outside.lock"
        target.write_text("outside", encoding="utf-8")
        (self.state / deliveries.LOCK).symlink_to(target)
        self._record(event_key="evt-lock", notice_id="n")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")
        self.assertFalse((self.state / deliveries.STORE).exists())

    def test_per_profile_isolation(self):
        other_state = self.root / "profile-b" / "var"
        other = StateStore(other_state, self.clock)
        self._record(event_key="evt-a", notice_id="a", message="profile a")
        self._record(store=other, event_key="evt-b", notice_id="b",
                     message="profile b")
        self.assertEqual([r["message"] for r in deliveries.recent(self.store)],
                         ["profile a"])
        self.assertEqual([r["message"] for r in deliveries.recent(other)],
                         ["profile b"])

    def test_summary_contains_only_count_and_newest(self):
        self._record()
        summary = deliveries.summary_since(self.store, within_hours=24)
        self.assertEqual(set(summary), {"count", "newest"})
        self.assertEqual(summary["count"], 1)
        self.assertIn("A bounded notice", deliveries.recent(self.store)[0]["message"])
        self.assertNotIn("A bounded notice", json.dumps(summary))

    def test_fingerprint_is_stable_and_changes_with_new_rows(self):
        self.assertEqual(deliveries.emit_fingerprint(self.store), "")
        self._record()
        first = deliveries.emit_fingerprint(self.store)
        self.assertNotEqual(first, "")
        self.assertEqual(first, deliveries.emit_fingerprint(self.store))
        self._record(event_key="evt-2", notice_id="notice-2", now=T0 + timedelta(minutes=1))
        self.assertNotEqual(first, deliveries.emit_fingerprint(self.store))


if __name__ == "__main__":
    unittest.main()
