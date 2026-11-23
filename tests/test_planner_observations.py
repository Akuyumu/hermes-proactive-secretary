"""Real-store tests for the isolated durable observation journal (v1.1.2).

Only temporary owner-local stores and a seeded clock are used. Nothing here
reads live state or invokes a scheduler/runner.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.planner_observations import (  # noqa: E402
    JournalOverflowError,
    ObservationNotFoundError,
    PlannerObservationJournal,
    StaleObservationError,
    event_id,
    version_id,
)
from proactive_pilot.state import CorruptStateError, StateStore  # noqa: E402

T0 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def candidate(
    *,
    ref: str = "evt-1",
    version: str = "v1",
    phase: str = "far",
    kind: str = "calendar",
    source: str = "calendar:primary",
    **extra: object,
) -> dict[str, object]:
    payload = {
        "kind": kind,
        "source": source,
        "ref": ref,
        "version": version,
        "phase": phase,
    }
    payload.update(extra)
    return payload


class PlannerObservationJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "profile-a" / "var"
        self.clock = SeededClock(T0.isoformat(), timezone_name="UTC")
        self.store = StateStore(self.root, self.clock)
        self.journal = PlannerObservationJournal(self.store, "owner-a")

    def _reopen(self, *, scope: str = "owner-a") -> PlannerObservationJournal:
        store = StateStore(self.root, self.clock)
        return PlannerObservationJournal(store, scope)

    def test_empty_input_and_exact_duplicates_never_write(self) -> None:
        result = self.journal.record([])
        self.assertFalse(result["wrote"])
        self.assertFalse(self.journal.path.exists())

        first = self.journal.record([candidate()])
        self.assertTrue(first["wrote"])
        before = self.journal.path.read_bytes()

        duplicate = self.journal.record([candidate()])
        self.assertFalse(duplicate["wrote"])
        self.assertEqual(duplicate["unchanged"], 1)
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_changed_revision_replaces_pending_and_refuses_old_ack(self) -> None:
        old = candidate(version="v1")
        self.journal.record([old])
        observation = event_id(old)
        old_version = version_id(old)

        self.assertTrue(self.journal.claim(observation, old_version)["acknowledged"])

        new = candidate(version="v2")
        changed = self.journal.record([new])
        self.assertTrue(changed["wrote"])
        self.assertEqual(changed["updated"], 1)

        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["version_id"], version_id(new))

        with self.assertRaises(StaleObservationError):
            self.journal.claim(observation, old_version)

        self.assertEqual(self.journal.pending()[0]["version_id"], version_id(new))
        self.assertTrue(
            self.journal.claim(observation, version_id(new))["acknowledged"]
        )
        self.assertEqual(self.journal.pending(), [])

    def test_reopen_persists_pending_and_acknowledgements(self) -> None:
        item = candidate(version="v1")
        self.journal.record([item])
        observation = event_id(item)

        reopened = self._reopen()
        pending = reopened.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["observation_id"], observation)
        self.assertEqual(pending[0]["ref"], "evt-1")

        reopened.claim(observation, version_id(item))
        after = self._reopen().pending()
        self.assertEqual(after, [])
        self.assertTrue(self._reopen().get(observation)["acknowledged"])

    def test_scope_is_isolated_and_cross_scope_payload_is_refused(self) -> None:
        self.journal.record([candidate()])
        other = self._reopen(scope="owner-b")
        self.assertNotEqual(other.path, self.journal.path)
        self.assertEqual(other.pending(), [])

        other.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "scope": "owner-a",
                    "records": {},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CorruptStateError):
            other.load()

    def test_corruption_is_refused_not_treated_as_empty(self) -> None:
        self.journal.path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(CorruptStateError):
            self.journal.pending()

    def test_overflow_refuses_without_evicting_pending_records(self) -> None:
        bounded = PlannerObservationJournal(self.store, "owner-a", max_records=1)
        bounded.record([candidate(ref="evt-a")])

        with self.assertRaises(JournalOverflowError):
            bounded.record([candidate(ref="evt-b")])

        pending = bounded.pending()
        self.assertEqual([record["ref"] for record in pending], ["evt-a"])

    def test_timestamps_are_utc_aware(self) -> None:
        self.journal.record([candidate()])
        observed_at = self.journal.pending()[0]["observed_at"]
        self.assertTrue(observed_at.endswith("+00:00"))
        self.assertIsNotNone(datetime.fromisoformat(observed_at).tzinfo)

    def test_only_minimum_bounded_references_are_persisted(self) -> None:
        self.journal.record(
            [
                candidate(
                    preview="SECRET PREVIEW",
                    body="SECRET BODY",
                    destination="owner@example.test",
                    subject="Visible subject is hashed only",
                )
            ]
        )
        text = self.journal.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("destination", text)
        self.assertNotIn("owner@example.test", text)

    def test_event_identity_is_stable_across_revisions(self) -> None:
        first = candidate(version="v1", phase="far")
        revised = candidate(version="v2", phase="within30min")
        self.assertEqual(event_id(first), event_id(revised))
        self.assertNotEqual(version_id(first), version_id(revised))

    def test_unknown_observation_cannot_be_acknowledged(self) -> None:
        with self.assertRaises(ObservationNotFoundError):
            self.journal.claim("missing", "version")


if __name__ == "__main__":
    unittest.main()
