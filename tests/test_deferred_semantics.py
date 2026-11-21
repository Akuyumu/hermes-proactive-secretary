"""Truthful deferral vs failure and expired structured evidence (fake only)."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import source_maintenance as sm  # noqa: E402
from proactive_pilot import source_truth as st  # noqa: E402
from proactive_pilot.adaptive import _raw_from_row  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _expired_rec():
    return {
        "id": "s1", "kind": "university_json", "url": "https://www.university.example/news/",
        "host": "www.university.example", "cadence_minutes": 60,
        "last_fetch_at": (NOW - timedelta(hours=3)).isoformat(),
        "cache_at": (NOW - timedelta(minutes=10)).isoformat(),
        "cache_items": [{"category": "weather_forecast",
                         "valid_from": (NOW - timedelta(hours=2)).isoformat(),
                         "valid_until": (NOW - timedelta(hours=1)).isoformat()}],
        "failures": 0,
    }


class DeferredSemantics(unittest.TestCase):
    def test_adaptive_deferral_does_not_mutate_registry_or_revive_cache(self):
        rec = _expired_rec()
        before = copy.deepcopy(rec)
        row = {"checked_at": rec["cache_at"], "reason": "budget deferred before attempt"}
        result = _raw_from_row(rec, row, NOW)
        self.assertEqual(rec, before)
        self.assertIs(result.attempted, False)
        self.assertEqual(result.status, "stale")
        self.assertIn("structured cache expired", result.failure)

    def test_source_truth_expired_structured_cache_is_not_healthy(self):
        rows = st.ordinary_source_rows({"sources": {"s1": _expired_rec()}}, {},
                                       now=NOW, stale_minutes=45)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["healthy"])
        self.assertIn(rows[0]["status"], {st.STATUS_STALE, st.STATUS_FAILING})

    def test_maintenance_budget_deferral_does_not_charge_failure_or_cooldown(self):
        rec = {"id": "s1", "kind": "rss", "url": "https://example.com/feed",
               "host": "example.com", "interest": "topic", "failures": 3,
               "stale_reason": "api error", "maintenance": {"attempts": 0}}
        registry = {"schema": 1, "sources": {"s1": rec}}

        def probe(*_args):
            raise AssertionError("budget deferral must not probe")

        adopted = sm.attempt_replacements(
            registry, policy={"public_source_maintenance_enabled": True},
            now=NOW, probe=probe, can_attempt=lambda: False, max_probes=1)
        self.assertEqual(adopted, [])
        self.assertEqual(rec["maintenance"]["attempts"], 0)
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"], "budget_deferred")
        self.assertNotIn("next_attempt_at", rec["maintenance"])
        self.assertTrue(sm._is_neutral("budget deferred before attempt"))

    def test_attempt_result_distinguishes_non_evidence_skip(self):
        skipped = SourceResult("feed:x", "deferred", failure="transport gate skipped",
                               attempted=False)
        self.assertIs(skipped.attempted, False)
        attempted = SourceResult("feed:x", "ok", attempted=True)
        self.assertIs(attempted.attempted, True)


if __name__ == "__main__":
    unittest.main()
