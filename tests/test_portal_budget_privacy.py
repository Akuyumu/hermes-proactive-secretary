"""Budget receipt must not leak delivery-ledger content.

This is an acceptance test for the real authenticated ``budget`` action.  It
reuses the ``_Base`` fixture and its real budget invocation from
``test_portal_notices`` and seeds the real delivery ledger with a unique notice
before reading the budget again.  The budget receipt may expose numeric
allowances, but it must never expose the emitted topic, body, notice id or
destination, and it must never offer recent emissions through this surface.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_sibling(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_FIXTURE = _load_sibling(
    "_portal_notices_privacy_fixture",
    Path(__file__).resolve().with_name("test_portal_notices.py"),
)
_Base = _FIXTURE._Base

from proactive_pilot import deliveries  # noqa: E402


class BudgetPrivacyTests(_Base):
    TOPIC = "PRIVATE-TOPIC-budget-privacy-9f3a"
    BODY = "PRIVATE-BODY-budget-privacy-7c2d"
    NOTICE_ID = "PRIVATE-NOTICE-budget-privacy-4e1b"
    DESTINATION = "PRIVATE-DESTINATION-budget-privacy-8a6f"

    def _write_user_config(self, *, cap: int) -> None:
        (self.data / "user_config.json").write_text(
            json.dumps({
                "schema": 1,
                "version": 1,
                "timezone": "UTC",
                "notification": {
                    "daily_normal_cap": cap,
                    "destination": self.DESTINATION,
                },
                "sources": {
                    "public_feeds": False,
                    "obligations": False,
                    "mail": False,
                    "calendar": False,
                    "tasks": False,
                },
                "public_sources": [],
            }),
            encoding="utf-8",
        )

    def test_budget_receipt_has_allowance_but_no_ledger_content(self):
        self._seed_ledger()
        self._write_policy(daily_normal_cap=6)
        self._write_user_config(cap=6)

        store = self._new_store()
        deliveries.record_emission(
            store,
            message=self.BODY,
            kind="portal-notice",
            event_key="evt-budget-privacy",
            notice_id=self.NOTICE_ID,
            destination=self.DESTINATION,
            topic=self.TOPIC,
            now=self.clock.now(),
        )

        rows = deliveries.recent(store)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["notice_id"], self.NOTICE_ID)
        self.assertEqual(rows[0]["destination"], self.DESTINATION)
        self.assertEqual(rows[0]["topic"], self.TOPIC)
        self.assertEqual(rows[0]["message"], self.BODY)

        payload = self._budget()
        self.assertTrue(payload["ok"], payload)

        for field in ("cap", "remaining"):
            self.assertIsInstance(payload[field], int, payload)
            self.assertNotIsInstance(payload[field], bool)
        self.assertEqual(payload["cap"], 6)
        self.assertEqual(payload["remaining"], 6)

        self.assertFalse(payload["recent_emissions_available"], payload)

        receipt = json.dumps(payload, sort_keys=True)
        for token in (self.TOPIC, self.BODY, self.NOTICE_ID, self.DESTINATION):
            self.assertNotIn(token, receipt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
