"""Stage 2: one honest notice when no source has been consented."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import gates  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def _policy(**overrides):
    policy = {
        "coverage_notice_cooldown_minutes": 60,
        "mail_accounts": [],
        "calendar_accounts": [],
        "task_accounts": [],
        "feeds_enabled": False,
    }
    policy.update(overrides)
    return policy


class DegradedConfigurationNoticeTests(unittest.TestCase):
    def test_emitted_once_when_nothing_is_consented(self):
        state: dict = {}
        message = gates.coverage_notice([], state, _policy(), now=NOW, commit=True)
        self.assertIsNotNone(message)
        self.assertIn("no source is consented", message.lower())
        self.assertIn("hermes secretary setup", message)
        for provider in ("mail", "calendar", "tasks", "inbox"):
            self.assertNotIn(provider, message.lower())
        self.assertEqual(set(state), {"coverage_notified_at"})
        self.assertIn("configuration", state["coverage_notified_at"])

    def test_not_repeated_inside_the_existing_cooldown(self):
        state: dict = {}
        policy = _policy()
        self.assertIsNotNone(
            gates.coverage_notice([], state, policy, now=NOW, commit=True))
        self.assertIsNone(
            gates.coverage_notice([], state, policy,
                                  now=NOW + timedelta(minutes=30), commit=True))
        self.assertIsNotNone(
            gates.coverage_notice([], state, policy,
                                  now=NOW + timedelta(minutes=61), commit=True))

    def test_not_emitted_when_a_source_is_consented(self):
        state: dict = {}
        policy = _policy(mail_accounts=["owner"])
        self.assertIsNone(
            gates.coverage_notice([], state, policy, now=NOW, commit=True))
        self.assertEqual(state, {})

    def test_explicit_configuration_variant_is_directly_available(self):
        state: dict = {}
        message = gates.configuration_notice(state, _policy(), now=NOW)
        self.assertIn("no source is consented", message.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
