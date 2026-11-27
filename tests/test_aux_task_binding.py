"""The auxiliary lane must never dispatch without its own task.

A taskless ``ctx.llm`` call is the host's main-model path: it would spend the
owner's primary model and send the prompt to a route this lane never
authorised. These tests pin the fail-closed behaviour and the strict-route
status line that goes with it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
sys.path.insert(0, str(ROOT))

from secretary import secretary_models  # noqa: E402


class StrictRouteStatusTests(unittest.TestCase):
    def test_strict_lane_without_a_pin_is_blocked(self):
        status = secretary_models.effective_route_status(
            settings={}, strict_active=True)
        self.assertFalse(status["configured_model_calls_allowed"])
        self.assertTrue(any("strict route requires" in reason
                            for reason in status["blockers"]), status["blockers"])

    def test_a_pinned_strict_lane_is_allowed(self):
        status = secretary_models.effective_route_status(
            settings={"primary_provider": "opencode-go", "primary_model": "deepseek-v4.1-flash"},
            strict_active=True)
        self.assertEqual(status["blockers"], [])
        self.assertTrue(status["configured_model_calls_allowed"])

    def test_non_strict_status_is_unchanged(self):
        status = secretary_models.effective_route_status(
            settings={}, strict_active=False)
        self.assertEqual(status["blockers"], [])
        self.assertFalse(status["configured_model_calls_allowed"])


if __name__ == "__main__":
    unittest.main()
