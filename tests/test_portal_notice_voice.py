"""The portal proposal path refuses identifier-bearing notice text first."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import portal_notices as core  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402


class PortalNoticeVoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = SeededClock("2026-10-20T12:00:00+00:00",
                                 timezone_name="UTC")

    def _identity(self):
        return {
            "job_id": "job-portal-1",
            "job_name": core.PORTAL_JOB_NAME,
            "execution_id": "exec-voice-1",
            "profile": "A",
        }

    def _proposal(self, message: str) -> dict:
        return {
            "event_key": "evt-voice-1",
            "topic": "Topic",
            "message": message,
            "evidence_refs": ["evidence-1"],
            "expiry": (self.clock.now() + timedelta(hours=1)).isoformat(),
        }

    def test_hash_message_is_refused_before_any_execution_row(self):
        store = StateStore(self.root / "state", self.clock)
        receipt = core.submit_notice(
            store,
            self.clock,
            identity=self._identity(),
            proposal=self._proposal(
                "The sha256:9f86d081884c7d659a2feaa0c55ad015 is attached."),
        )
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["accepted"])
        self.assertEqual(receipt["status"], "invalid_proposal")
        self.assertIn("hash_token", receipt["reason"])
        self.assertFalse(
            (self.root / "state" / core.PORTAL_NOTICES_ARTIFACT).exists())
        queue = store.load(core.PORTAL_NOTICES_ARTIFACT, {})
        self.assertEqual(queue, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
