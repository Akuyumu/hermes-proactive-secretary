"""Stage 2: the foreground ``complete`` action records owner_confirmed.

Reuses the synthetic profile/hook harness from the existing follow-up
integration tests; no live source, channel or job is touched.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_followup_integration as base  # noqa: E402


class FollowupCompleteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.FollowupIntegrationTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_done_closes_the_named_followup_as_owner_confirmed(self):
        ctx, handler = self.fixture._foreground()
        created = self.fixture._create(
            ctx, handler, text="completed parcel",
            quote="Please keep an eye on the completed parcel.")
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        quote = f"Done with {fid}."
        self.fixture._human(ctx, quote, session="s-done")
        result = self.fixture._call(handler, {
            "action": "complete", "session_id": "s-done",
            "evidence_quote": quote, "followup_id": fid})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["followup"]["status"], "done")
        self.assertEqual(result["followup"]["close_reason"], "owner_confirmed")

        stored = base.F.get(base.StateStore(self.fixture.state,
                                            self.fixture.clock), fid)
        self.assertEqual(stored["close_reason"], "owner_confirmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
