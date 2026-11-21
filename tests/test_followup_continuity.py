"""Offline continuity acceptance for queued/follow-up notices.

These cases use the real portal queue and dispatcher. Only the profile roots,
clock and native execution metadata are synthetic.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import deliveries  # noqa: E402
from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import ownerpaths  # noqa: E402
from proactive_pilot import policy as policy_mod  # noqa: E402
from proactive_pilot import portal_notices as core  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = "2026-11-20T10:00:00+00:00"
LEDGER_ID = "job-portal-continuity"


def _copy(value):
    return json.loads(json.dumps(value))


class FollowupContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "A" / "home"
        self.data = self.root / "A" / "data"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.state = self.data / "var"

        self._keys = ("HERMES_HOME", ownerpaths.DATA_ENV)
        self._saved = {key: os.environ.get(key) for key in self._keys}
        self.addCleanup(self._restore_env)
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ[ownerpaths.DATA_ENV] = str(self.data)

        native = types.SimpleNamespace(
            get_hermes_home=lambda: self.home,
            get_routing_process_hermes_home=lambda: self.home,
        )
        patch = mock.patch.object(ownerpaths, "_native_constants",
                                  return_value=native)
        patch.start()
        self.addCleanup(patch.stop)

        (self.data / "cron_job_ownership.json").write_text(json.dumps({
            "schema": 1,
            "plugin": "hermes-proactive-portal",
            "jobs": {core.PORTAL_JOB_NAME: {"id": LEDGER_ID,
                                            "name": core.PORTAL_JOB_NAME}},
        }), encoding="utf-8")

        self.clock = SeededClock(T0, timezone_name="UTC")
        self.store = StateStore(self.state, self.clock)
        self.policy = self._policy()
        self.prefs = _copy(policy_mod.DEFAULT_PREFERENCES)
        self.ctx = types.SimpleNamespace(profile_name="A")

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _policy(**overrides):
        policy = _copy(policy_mod.DEFAULT_POLICY)
        policy.update({
            "timezone": "UTC",
            "quiet_start_hour": 0,
            "quiet_end_hour": 0,
            "daily_normal_cap": None,
            "cooldown_minutes": 0,
        })
        policy.update(overrides)
        return policy

    def _identity(self, execution_id="exec-1"):
        return {"job_id": LEDGER_ID, "job_name": core.PORTAL_JOB_NAME,
                "execution_id": execution_id, "profile": "A"}

    def _proposal(self, *, event_key, message, expiry):
        return {"event_key": event_key, "topic": "Topic", "message": message,
                "evidence_refs": ["evidence-1"], "expiry": expiry}

    def _enqueue(self, *, event_key, message, expiry, execution_id="exec-1"):
        return core.submit_notice(
            self.store, self.clock,
            identity=self._identity(execution_id),
            proposal=self._proposal(event_key=event_key, message=message,
                                    expiry=expiry))

    def _dispatch(self, clock, *, policy=None):
        return core.dispatch_notice(
            self.store, clock,
            policy=policy or self.policy,
            prefs=self.prefs,
            state=self.store.load_notify_state(),
            native_ctx=self.ctx,
            job_reader=lambda job_id: ({
                "id": job_id, "name": core.PORTAL_JOB_NAME, "state": "active"
            }, True),
        )

    def _rows(self):
        return deliveries.recent(self.store)

    def test_queued_notice_reaches_ledger_only_through_real_dispatch(self):
        followup = F.create(self.store, text="Watch the consultation",
                            kind="one_shot",
                            check_hint="consultation reopening",
                            now=self.clock.now())
        event_key = F.notice_event_key(followup["id"])
        F.queue_notice(self.store, followup["id"], event_key,
                       now=self.clock.now())
        self.assertEqual(self._rows(), [])

        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        receipt = self._enqueue(event_key=event_key,
                                message="The consultation reopened.",
                                expiry=expiry)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(self._rows(), [])

        result = self._dispatch(self.clock)
        self.assertEqual(result["status"], "emitted-unconfirmed")
        self.assertEqual(result["message"], "The consultation reopened.")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "portal-notice")
        self.assertEqual(rows[0]["event_key"], event_key)
        self.assertEqual(rows[0]["message"], "The consultation reopened.")
        self.assertEqual(rows[0]["job_id"], LEDGER_ID)
        self.assertEqual(rows[0]["status"], "emitted-unconfirmed")

    def test_quiet_hours_blocked_notice_never_appears_until_emitted(self):
        expiry = (self.clock.now() + timedelta(hours=6)).isoformat()
        self._enqueue(event_key="evt-quiet", message="Quiet notice",
                      expiry=expiry)
        quiet = self._policy(quiet_start_hour=12, quiet_end_hour=13)

        blocked = self._dispatch(
            SeededClock("2026-11-20T12:30:00+00:00", timezone_name="UTC"),
            policy=quiet)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self._rows(), [])

        emitted = self._dispatch(
            SeededClock("2026-11-20T13:30:00+00:00", timezone_name="UTC"),
            policy=quiet)
        self.assertEqual(emitted["message"], "Quiet notice")
        self.assertEqual([row["message"] for row in self._rows()],
                         ["Quiet notice"])

    def test_one_shot_frozen_without_emission_never_appears(self):
        followup = F.create(self.store, text="Watch the result",
                            kind="one_shot", check_hint="result published",
                            now=self.clock.now())
        event_key = F.notice_event_key(followup["id"])
        queued = F.queue_notice(self.store, followup["id"], event_key,
                                now=self.clock.now())
        self.assertEqual(queued["status"], "awaiting_delivery")
        self.assertEqual(self._rows(), [])

    def test_daily_cap_blocked_notice_never_appears(self):
        expiry = (self.clock.now() + timedelta(hours=2)).isoformat()
        self._enqueue(event_key="evt-cap", message="Capped notice",
                      expiry=expiry)
        capped = self._policy(daily_normal_cap=0)
        blocked = self._dispatch(self.clock, policy=capped)
        self.assertEqual(blocked["message"], "")
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
