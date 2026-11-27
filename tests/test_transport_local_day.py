"""The LTA daily cap follows the configured zone, never a hardcoded region."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.transport import _local_day  # noqa: E402


class TransportLocalDayTests(unittest.TestCase):
    def test_configured_zone_moves_the_day_boundary(self):
        instant = datetime(2026, 11, 27, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(_local_day(instant, {"timezone": "UTC"}), "2026-11-27")
        self.assertEqual(_local_day(instant, {"timezone": "Asia/Tokyo"}), "2026-11-28")

    def test_missing_policy_uses_the_system_zone_not_singapore(self):
        instant = datetime(2026, 11, 27, 23, 30, tzinfo=timezone.utc)
        system_zone = datetime.now().astimezone().tzinfo or timezone.utc
        expected = instant.astimezone(system_zone).date().isoformat()
        self.assertEqual(_local_day(instant), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
