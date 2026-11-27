"""Strict, bounded pending acknowledgement storage and disposition export."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import pending_ack as P  # noqa: E402
from proactive_pilot.clock import Clock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

UTC = timezone.utc


class PendingAckStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root, Clock())
        self.now = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)

    def _create(self, index, *, conversation=None):
        return P.create(
            conversation=conversation or f"conv-{index}", owner="owner",
            target_id=f"followup:fu_{index}", target_label=f"item {index}",
            effect={"action": "stop", "kind": "", "scope": ""},
            question=f"Stop follow-ups for “item {index}” only?",
            turn_id=f"turn-{index}", now=self.now, store=self.store)

    def test_the_store_is_fifo_bounded_to_twenty_rows(self):
        for index in range(25):
            self.assertTrue(self._create(index)["ok"])
        rows = P.rows(self.store)
        self.assertEqual(len(rows), P.MAX_ROWS)
        self.assertEqual(rows[0]["conversation"], "conv-5")
        self.assertEqual(rows[-1]["conversation"], "conv-24")

    def test_a_live_proposal_refuses_a_second_one_in_the_same_conversation(self):
        self.assertTrue(self._create(1, conversation="same")["ok"])
        second = self._create(2, conversation="same")
        self.assertFalse(second["ok"], second)
        rows = P.rows(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], "turn-1")

    def test_a_corrupt_store_refuses_and_is_never_overwritten(self):
        self.assertTrue(self._create(1)["ok"])
        path = self.root / P.STORE
        path.write_text("{not json", encoding="utf-8")
        before = path.read_bytes()

        result = self._create(2)
        self.assertFalse(result["ok"], result)
        self.assertIn("corrupt", result["reason"])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(P.PendingAckError):
            P.rows(self.store)
        with self.assertRaises(P.PendingAckError):
            P.dispositions(self.store)

    def test_dispositions_are_exported_for_stage_b(self):
        P.record_disposition(
            "rem-1",
            {"action": "stop", "target_id": "reminder:rem-1",
             "target_label": "the reminder", "turn_id": "turn-1",
             "source": "owner acknowledgement"},
            now=self.now, store=self.store)
        dispositions = P.dispositions(self.store)
        self.assertEqual(dispositions["rem-1"]["action"], "stop")
        self.assertEqual(dispositions["rem-1"]["at"], self.now.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
