"""Tests for the durable requested-reminder followthrough.

All sources here are SYNTHETIC test doubles and are clearly labelled. No test
performs a live calendar/mail read, send, or native job creation. Every state
directory is a cwd-local ``TemporaryDirectory(dir=cwd)``; never ``/tmp``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proactive_pilot import reminders as R  # noqa: E402
from proactive_pilot.clock import SG  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=SG)
LAUNCHER = ROOT / "runners" / "proactive_secretary_reminder_runner.py"


def iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# Synthetic (test-only) doubles
# --------------------------------------------------------------------------- #

class SyntheticEventGetter:
    """Test-only fixed-answer getter; never touches a live source."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str]] = []

    def set(self, alias: str, cal: str, eid: str, obs: dict) -> None:
        self.answers[(alias, cal, eid)] = obs

    def get(self, alias: str, cal: str, eid: str) -> dict:
        self.calls.append((alias, cal, eid))
        return self.answers.get((alias, cal, eid),
                                {"ok": False, "status": "error", "error": "synthetic not found"})


class SyntheticCalendarClient:
    """Test-only stand-in for ``secretary.google.CalendarClient`` (no network)."""

    base = "https://example.invalid/calendar/v3/calendars"

    def __init__(self, event: dict):
        self.event = event
        self.requests: list[tuple[str, str]] = []

    def calendars(self) -> dict:
        return {"items": [{"id": "primary", "primary": True}]}

    def _is_holiday_calendar(self, cal: dict) -> bool:
        return False

    def _events(self, calendar_id: str) -> str:
        return f"{self.base}/{calendar_id}/events"

    def _request(self, method: str, url: str) -> dict:
        self.requests.append((method, url))
        return self.event


def confirmed(start: datetime, updated: str = "2026-10-02T00:00:00Z") -> dict:
    return {"status": "confirmed",
            "start": {"dateTime": iso(start)},
            "end": {"dateTime": iso(start + timedelta(hours=1))},
            "updated": updated}


class ReminderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = Path(self._tmp.name)
        self.store_dir = self.root / "state"
        self.prefs_path = self.root / "preferences.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def store(self, now: datetime = T0) -> StateStore:
        return StateStore(self.store_dir, R.Clock())

    def write_prefs(self, paused: bool = False, until: str = "") -> None:
        self.prefs_path.write_text(json.dumps({
            "schema": 1, "version": 0, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": paused, "until": until},
        }), encoding="utf-8")

    def run_cli(self, argv: list[str], getter=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = R.main(argv, getter=getter)
        return code, out.getvalue(), err.getvalue()

    def base_argv(self, *extra: str, clock: datetime = T0) -> list[str]:
        # In-process CLI invocation (no launcher path; that is the program name).
        return ["--state-dir", str(self.store_dir),
                "--preferences", str(self.prefs_path), "--seeded-clock", iso(clock), *extra]


# --------------------------------------------------------------------------- #
# add-time validation
# --------------------------------------------------------------------------- #

class TestAddTime(ReminderTestCase):
    def test_timezone_aware_due_accepted(self):
        reg = R.empty_registry()
        entry = R.add_time(reg, text="Take meds", due=iso(T0 + timedelta(minutes=30)),
                           provenance="explicit user request", now=T0)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["kind"], "time")
        self.assertTrue(entry["due"].endswith("+08:00"))

    def test_naive_due_rejected(self):
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_time(reg, text="x", due="2026-10-02T12:30:00",
                       provenance="user request", now=T0)
        self.assertEqual(reg["reminders"], {})

    def test_malformed_due_rejected(self):
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_time(reg, text="x", due="tomorrow-ish",
                       provenance="user request", now=T0)

    def test_expired_beyond_grace_rejected(self):
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_time(reg, text="x", due=iso(T0 - timedelta(minutes=30)),
                       provenance="user request", now=T0)

    def test_provenance_required(self):
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_time(reg, text="x", due=iso(T0 + timedelta(minutes=5)),
                       provenance="   ", now=T0)


# --------------------------------------------------------------------------- #
# add-event validation + real getter adapter
# --------------------------------------------------------------------------- #

class TestAddEvent(ReminderTestCase):
    def test_event_bound_with_exact_ids(self):
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "evt-1", R.normalize_event(confirmed(T0 + timedelta(hours=1))))
        reg = R.empty_registry()
        entry = R.add_event(reg, text="Meeting soon", alias="primary", calendar_id="primary",
                            event_id="evt-1", lead_minutes=10, provenance="explicit user request",
                            now=T0, getter=getter)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["event_id"], "evt-1")
        self.assertEqual(entry["trigger_at"], iso(T0 + timedelta(minutes=50)))
        self.assertTrue(entry["event_version"])

    def test_preflight_failure_rejected_fail_closed(self):
        getter = SyntheticEventGetter()  # every answer is a failure
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_event(reg, text="x", alias="primary", calendar_id="primary", event_id="evt-1",
                        lead_minutes=10, provenance="user request", now=T0, getter=getter)
        self.assertEqual(reg["reminders"], {})

    def test_cancelled_preflight_rejected(self):
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "evt-1",
                   {"ok": True, "status": "cancelled", "start": iso(T0 + timedelta(hours=1)),
                    "version": "v", "error": ""})
        reg = R.empty_registry()
        with self.assertRaises(R.ReminderError):
            R.add_event(reg, text="x", alias="primary", calendar_id="primary", event_id="evt-1",
                        lead_minutes=10, provenance="user request", now=T0, getter=getter)

    def test_all_day_event_rejected(self):
        obs = R.normalize_event({"status": "confirmed", "start": {"date": "2026-10-03"}})
        self.assertFalse(obs["ok"])
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "evt-1", obs)
        with self.assertRaises(R.ReminderError):
            R.add_event(R.empty_registry(), text="x", alias="primary", calendar_id="primary",
                        event_id="evt-1", lead_minutes=10, provenance="user request", now=T0, getter=getter)

    def test_provenance_required(self):
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "evt-1", R.normalize_event(confirmed(T0 + timedelta(hours=1))))
        with self.assertRaises(R.ReminderError):
            R.add_event(R.empty_registry(), text="x", alias="primary", calendar_id="primary",
                        event_id="evt-1", lead_minutes=10, provenance="", now=T0, getter=getter)

    def test_real_getter_validates_alias_calendar_and_normalizes(self):
        client = SyntheticCalendarClient(confirmed(T0 + timedelta(hours=2)))
        getter = R.SecretaryEventGetter(client_factory=lambda alias: client, aliases=["primary"])
        self.assertFalse(getter.get("intruder", "primary", "e1")["ok"])
        self.assertFalse(getter.get("primary", "someone-else", "e1")["ok"])
        obs = getter.get("primary", "primary", "e1")
        self.assertTrue(obs["ok"])
        self.assertEqual(obs["status"], "confirmed")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0][0], "GET")
        # no raw body/URL persisted: only the version hash + exact start
        self.assertNotIn("summary", obs)
        self.assertNotIn("http", json.dumps(obs))


# --------------------------------------------------------------------------- #
# Dispatch semantics
# --------------------------------------------------------------------------- #

class TestDispatch(ReminderTestCase):
    def _plan(self, reg, now, getter, prefs=None):
        return R.plan_dispatch(reg, now=now, prefs=prefs or {"paused": {"active": False}},
                               getter=getter)

    def test_quiet_hours_requested_still_delivered(self):
        night = datetime(2026, 10, 2, 3, 0, 0, tzinfo=SG)
        reg = R.empty_registry()
        R.add_time(reg, text="Wake for flight", due=iso(night), provenance="explicit user request", now=night)
        plan = self._plan(reg, night, SyntheticEventGetter())
        self.assertEqual(plan.messages, ["Wake for flight"])

    def test_global_pause_stops_all_reads(self):
        getter = SyntheticEventGetter()
        reg = R.empty_registry()
        R.add_event(reg, text="Meeting", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=0, provenance="explicit user request", now=T0,
                    getter=_ok_getter(T0 + timedelta(hours=1)))
        self.write_prefs(paused=True)
        code, out, _err = self.run_cli(self.base_argv("dispatch"), getter=getter)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(getter.calls, [])  # pause stopped every source read

    def test_pause_naive_until_fails_closed(self):
        now = T0
        self.assertTrue(R.pause_active({"paused": {"active": True, "until": "2026-10-02T13:00:00"}}, now))
        self.assertTrue(R.pause_active({"paused": {"active": True, "until": iso(now + timedelta(hours=1))}}, now))
        self.assertFalse(R.pause_active({"paused": {"active": True, "until": iso(now - timedelta(hours=1))}}, now))
        self.assertTrue(R.pause_active({"paused": {"active": True, "until": "garbage"}}, now))

    def test_no_pending_registry_fast_path_no_reads(self):
        getter = SyntheticEventGetter()
        reg = R.empty_registry()
        plan = self._plan(reg, T0, getter)
        self.assertEqual(plan.messages, [])
        self.assertEqual(getter.calls, [])

    def test_expired_time_marks_missed_without_reminder(self):
        reg = R.empty_registry()
        # Due 40m ago with a 10m grace: well beyond late grace.
        R.add_time(reg, text="Old thing", due=iso(T0 - timedelta(minutes=40)),
                   provenance="explicit user request", now=T0 - timedelta(hours=1))
        plan = self._plan(reg, T0, SyntheticEventGetter())
        rem = next(iter(reg["reminders"].values()))
        self.assertEqual(rem["status"], "missed")
        # Bounded, concise record; never a stale reminder phrased as live.
        self.assertEqual(len(plan.messages), 1)
        self.assertTrue(plan.messages[0].startswith("Missed reminder:"))
        self.assertIn("Old thing", plan.messages[0])

    def test_unsolicited_missing_provenance_fails_closed(self):
        reg = R.empty_registry()
        reg["reminders"]["x"] = {"id": "x", "kind": "time", "status": "pending",
                                 "text": "sneaky", "provenance": "", "due": iso(T0 + timedelta(minutes=5)),
                                 "late_grace_minutes": 10}
        plan = self._plan(reg, T0 + timedelta(minutes=6), SyntheticEventGetter())
        self.assertEqual(plan.messages, [])
        self.assertEqual(reg["reminders"]["x"]["status"], "rejected")

    def test_cancelled_event_retires(self):
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(T0 + timedelta(minutes=60))))
        reg = R.empty_registry()
        R.add_event(reg, text="Meeting", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        getter.set("primary", "primary", "e1",
                   {"ok": True, "status": "cancelled", "start": "", "version": "v2", "error": ""})
        # Within one poll cadence of the trigger: the read happens and retires it.
        plan = self._plan(reg, T0 + timedelta(minutes=46), getter)
        self.assertEqual(plan.messages, [])
        self.assertEqual(next(iter(reg["reminders"].values()))["status"], "retired")

    def test_moved_later_fires_once_at_new_due_not_old(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Standup", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        rem = next(iter(reg["reminders"].values()))
        # At the old trigger the event moved later -> rebind, no emission.
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due + timedelta(hours=1))))
        plan = self._plan(reg, due - timedelta(minutes=10), getter)
        self.assertEqual(plan.messages, [])
        self.assertEqual(rem["status"], "pending")
        self.assertEqual(rem["event_due"], iso(due + timedelta(hours=1)))
        # At the new due it fires exactly once.
        plan = self._plan(reg, due + timedelta(hours=1), getter)
        self.assertEqual(plan.messages, ["Standup"])
        R.finalize_dispatch(reg, plan.emitting, now=due + timedelta(hours=1))
        self.assertEqual(rem["status"], "emitted")
        plan2 = self._plan(reg, due + timedelta(hours=1, minutes=1), getter)
        self.assertEqual(plan2.messages, [])

    def test_moved_earlier_fires_if_before_event_within_grace(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Call", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        # Event moved earlier to +55m; at old trigger (+50m) it is still before the event.
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(T0 + timedelta(minutes=55))))
        plan = self._plan(reg, T0 + timedelta(minutes=50), getter)
        self.assertEqual(plan.messages, ["Call"])

    def test_moved_earlier_beyond_grace_marks_missed(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Call", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(T0 + timedelta(minutes=5))))
        plan = self._plan(reg, T0 + timedelta(minutes=50), getter)
        rem = next(iter(reg["reminders"].values()))
        self.assertEqual(rem["status"], "missed")
        # The sole record is a bounded missed note, not a stale "Call" reminder.
        self.assertEqual(len(plan.messages), 1)
        self.assertTrue(plan.messages[0].startswith("Missed reminder:"))

    def test_due_read_failure_uncertain_then_recovers(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Review", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        getter.answers[("primary", "primary", "e1")] = {"ok": False, "status": "error", "error": "503"}
        plan = self._plan(reg, T0 + timedelta(minutes=50), getter)
        rem = next(iter(reg["reminders"].values()))
        self.assertEqual(rem["status"], "uncertain")
        self.assertEqual(plan.messages, [])  # never a stale guessed alert
        getter.answers[("primary", "primary", "e1")] = R.normalize_event(confirmed(due))
        # Recovery read happens on the next cadence tick (<=5 min), still within
        # the event's late grace, so the reminder is delivered once, not missed.
        plan = self._plan(reg, T0 + timedelta(minutes=55), getter)
        self.assertEqual(plan.messages, ["Review"])

    def test_no_premature_read_before_due(self):
        # CORRECTED: the parent control requires active bound events to poll on a
        # <=5 min cadence *regardless* of the old trigger, so an earlier move is
        # discovered. The requirement is therefore "no read faster than cadence
        # and none in the same minute as the add-event preflight", NOT "no read
        # until near the old trigger".
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Review", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        after_add = len(getter.calls)  # preflight read seeded last_check_at = T0
        plan = self._plan(reg, T0 + timedelta(minutes=1), getter)  # same cadence window
        self.assertEqual(len(getter.calls), after_add)  # cadence: no immediate re-read
        self.assertEqual(plan.messages, [])
        # On the cadence tick (>=5 min) the active event IS polled.
        self._plan(reg, T0 + timedelta(minutes=5), getter)
        self.assertGreater(len(getter.calls), after_add)

    def test_interrupted_emitting_becomes_uncertain_not_resent(self):
        reg = R.empty_registry()
        R.add_time(reg, text="Ping", due=iso(T0 + timedelta(minutes=5)),
                   provenance="user request", now=T0)
        rem = next(iter(reg["reminders"].values()))
        rem["status"] = "emitting"
        rem["emitting_at"] = T0.isoformat()
        plan = R.plan_dispatch(reg, now=T0 + timedelta(minutes=5),
                               prefs={"paused": {"active": False}}, getter=SyntheticEventGetter())
        self.assertEqual(plan.messages, [])
        self.assertEqual(rem["status"], "unconfirmed")
        self.assertNotIn("unconfirmed", R.ACTIVE_STATUSES)

    def test_multiple_due_collapse_to_one_bounded_line(self):
        reg = R.empty_registry()
        R.add_time(reg, text="A" * 10, due=iso(T0), provenance="u", now=T0)
        R.add_time(reg, text="B" * 10, due=iso(T0), provenance="u", now=T0)
        plan = R.plan_dispatch(reg, now=T0, prefs={"paused": {"active": False}},
                               getter=SyntheticEventGetter())
        # Exactly two messages collapse into a single bounded stdout line.
        self.assertEqual(len(plan.messages), 2)
        self.assertEqual(sorted(plan.messages), ["A" * 10, "B" * 10])
        self.assertLessEqual(len(R.combine_messages(["x" * 1000])), R.OUTPUT_CAP)

    def test_current_source_required_before_missed(self):
        # Parent control: an old-time expiry cannot mark missed before a fresh
        # source read. A read failure stays uncertain (no stale missed).
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Review", alias="primary", calendar_id="primary", event_id="e1",
                    lead_minutes=10, provenance="user request", now=T0, getter=getter)
        getter.answers[("primary", "primary", "e1")] = {"ok": False, "status": "error", "error": "503"}
        # Well past the old event+grace: read fails -> uncertain, NOT missed.
        plan = self._plan(reg, due + timedelta(hours=2), getter)
        rem = next(iter(reg["reminders"].values()))
        self.assertEqual(rem["status"], "uncertain")
        self.assertEqual(plan.messages, [])

    def test_late_relative_text_recomputed_not_stale(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Standup in 10 min", alias="primary", calendar_id="primary",
                    event_id="e1", lead_minutes=10, provenance="user request", now=T0, getter=getter)
        # Delivered 3 minutes late (7 min before the event): "in 10 min" would be
        # stale -> recomputed from the current event time.
        plan = self._plan(reg, due - timedelta(minutes=7), getter)
        self.assertEqual(plan.messages, ["Standup in 7 min"])

    def test_neutral_text_preserved_verbatim(self):
        due = T0 + timedelta(minutes=60)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        R.add_event(reg, text="Call the dentist", alias="primary", calendar_id="primary",
                    event_id="e1", lead_minutes=10, provenance="user request", now=T0, getter=getter)
        plan = self._plan(reg, due - timedelta(minutes=7), getter)
        self.assertEqual(plan.messages, ["Call the dentist"])

    def test_overflow_keeps_reminder_pending_not_lost(self):
        # Negative control: many 300-char messages must not be silently dropped by
        # the output cap. Only fully represented messages become emitting.
        reg = R.empty_registry()
        for i in range(5):
            R.add_time(reg, text=f"{i:02d}" + "x" * 297, due=iso(T0), provenance="u", now=T0)
        plan = R.plan_dispatch(reg, now=T0, prefs={"paused": {"active": False}},
                               getter=SyntheticEventGetter())
        combined = R.combine_messages(plan.messages)
        self.assertLessEqual(len(combined), R.OUTPUT_CAP)
        self.assertTrue(plan.overflow >= 1)
        # Each emitting message is fully present in the combined line.
        for message in plan.messages:
            self.assertIn(message, combined)
        # No emitting record is missing representation; overflow stays pending.
        emitting = [r for r in reg["reminders"].values() if r["status"] == "emitting"]
        self.assertEqual(len(emitting), len(plan.emitting))
        for rid in plan.queued:
            self.assertEqual(reg["reminders"][rid]["status"], "pending")

    def test_idempotent_double_add_event(self):
        due = T0 + timedelta(hours=2)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        first = R.add_event(reg, text="M", alias="primary", calendar_id="primary", event_id="e1",
                            lead_minutes=10, provenance="user request", now=T0, getter=getter)
        second = R.add_event(reg, text="M", alias="primary", calendar_id="primary", event_id="e1",
                             lead_minutes=10, provenance="user request", now=T0 + timedelta(minutes=1),
                             getter=getter)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(reg["reminders"]), 1)

    def test_a_new_request_after_emit_is_allowed(self):
        # Idempotency must not blanket-suppress a genuinely new request for the
        # same event after the prior one emitted with different provenance.
        due = T0 + timedelta(hours=2)
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due)))
        reg = R.empty_registry()
        first = R.add_event(reg, text="M", alias="primary", calendar_id="primary", event_id="e1",
                            lead_minutes=10, provenance="first request", now=T0, getter=getter)
        first["status"] = "emitted"
        first["emitted_at"] = T0.isoformat()
        second = R.add_event(reg, text="M again", alias="primary", calendar_id="primary",
                             event_id="e1", lead_minutes=10, provenance="second request",
                             now=T0 + timedelta(minutes=1), getter=getter)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(reg["reminders"]), 2)

    def test_tombstones_retire_after_30_days(self):
        reg = R.empty_registry()
        R.add_time(reg, text="Old", due=iso(T0), provenance="u", now=T0 - timedelta(days=40))
        rem = next(iter(reg["reminders"].values()))
        rem["status"] = "emitted"
        rem["emitted_at"] = (T0 - timedelta(days=40)).isoformat()
        R.prune_registry(reg, T0)
        self.assertEqual(reg["reminders"], {})
        # A fresh tombstone is retained.
        R.add_time(reg, text="New", due=iso(T0), provenance="u", now=T0)
        rem2 = next(iter(reg["reminders"].values()))
        rem2["status"] = "emitted"
        rem2["emitted_at"] = T0.isoformat()
        R.prune_registry(reg, T0 + timedelta(days=1))
        self.assertEqual(len(reg["reminders"]), 1)

    def test_missed_notice_overflow_requeued_then_delivered(self):
        # A missed notice must not be truncated away by the cap; it is re-queued
        # on a later run and delivered exactly once.
        reg = R.empty_registry()
        for i in range(5):
            R.add_time(reg, text=f"{i:02d}" + "z" * 297, due=iso(T0 - timedelta(days=1)),
                       provenance="u", now=T0 - timedelta(days=2))
        plan = R.plan_dispatch(reg, now=T0, prefs={"paused": {"active": False}},
                               getter=SyntheticEventGetter())
        missed = [r for r in reg["reminders"].values() if r["status"] == "missed"]
        self.assertEqual(len(missed), 5)  # all correctly marked missed
        # Only notices that fit were queued; rest remain un-notified.
        un = [r for r in missed if not r["miss_notified"]]
        self.assertTrue(un)
        # Later run drains the remaining notices without any source read.
        getter = SyntheticEventGetter()
        for _ in range(6):
            plan = R.plan_dispatch(reg, now=T0, prefs={"paused": {"active": False}}, getter=getter)
            if all(r["miss_notified"] for r in reg["reminders"].values()):
                break
        self.assertTrue(all(r["miss_notified"] for r in reg["reminders"].values()))
        self.assertEqual(getter.calls, [])  # missed notices never read a source

    def test_no_pending_already_notified_zero_io(self):
        reg = R.empty_registry()
        R.add_time(reg, text="gone", due=iso(T0 - timedelta(days=1)), provenance="u",
                   now=T0 - timedelta(days=2))
        reg["reminders"][next(iter(reg["reminders"]))]["status"] = "missed"
        reg["reminders"][next(iter(reg["reminders"]))]["miss_notified"] = True
        getter = SyntheticEventGetter()
        plan = R.plan_dispatch(reg, now=T0, prefs={"paused": {"active": False}}, getter=getter)
        self.assertEqual(plan.messages, [])
        self.assertEqual(getter.calls, [])

    def test_active_cap_100(self):
        reg = R.empty_registry()
        for i in range(R.MAX_REMINDERS):
            R.add_time(reg, text=f"r{i}", due=iso(T0 + timedelta(hours=1)), provenance="u", now=T0)
        with self.assertRaises(R.ReminderError):
            R.add_time(reg, text="overflow", due=iso(T0 + timedelta(hours=1)), provenance="u", now=T0)


def _ok_getter(start: datetime) -> SyntheticEventGetter:
    g = SyntheticEventGetter()
    g.set("primary", "primary", "e1", R.normalize_event(confirmed(start)))
    return g


# --------------------------------------------------------------------------- #
# CLI pipeline: persistence, exactly-once, locks, corruption, subprocess
# --------------------------------------------------------------------------- #

class TestCliPipeline(ReminderTestCase):
    def test_add_then_dispatch_exactly_once_and_persist(self):
        self.write_prefs()
        due = T0 + timedelta(minutes=30)
        code, out, _ = self.run_cli(self.base_argv(
            "--add-time", "--text", "Take meds", "--due", iso(due), "--provenance", "explicit user request"))
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        # Too early: silent.
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=T0))
        self.assertEqual((code, out), (0, ""))
        # At due: exactly one emission.
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=due))
        self.assertEqual(out, "Take meds\n")
        # Overlap/restart: never again.
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=due))
        self.assertEqual(out, "")
        reg = R.load_registry(self.store())
        self.assertEqual(next(iter(reg["reminders"].values()))["status"], "emitted")

    def test_lock_silence(self):
        self.write_prefs()
        due = T0 + timedelta(minutes=30)
        self.run_cli(self.base_argv("--add-time", "--text", "Locked", "--due", iso(due),
                                    "--provenance", "user request"))
        held = StateStore(self.store_dir, R.Clock())
        with held.lock() as acquired:
            self.assertTrue(acquired)
            code, out, err = self.run_cli(self.base_argv("dispatch", clock=due))
        self.assertEqual(out, "")
        self.assertIn("skip-lock", err)

    def test_check_is_stderr_only_and_non_mutating(self):
        self.write_prefs()
        due = T0 + timedelta(minutes=30)
        self.run_cli(self.base_argv("--add-time", "--text", "Check me", "--due", iso(due),
                                    "--provenance", "user request"))
        code, out, err = self.run_cli(self.base_argv("--check", clock=due))
        self.assertEqual(out, "")
        self.assertIn("would_emit", err)
        reg = R.load_registry(self.store())
        self.assertEqual(next(iter(reg["reminders"].values()))["status"], "pending")

    def test_list_does_not_dump_provenance(self):
        self.write_prefs()
        secret = "very private provenance sentence"
        self.run_cli(self.base_argv("--add-time", "--text", "Hello", "--due",
                                    iso(T0 + timedelta(minutes=5)), "--provenance", secret))
        code, out, _ = self.run_cli(self.base_argv("--list"))
        self.assertNotIn(secret, out)
        self.assertIn("present", out)

    def test_cancel(self):
        self.write_prefs()
        due = T0 + timedelta(minutes=30)
        code, out, _ = self.run_cli(self.base_argv("--add-time", "--text", "Bye", "--due",
                                                   iso(due), "--provenance", "user request"))
        rid = json.loads(out)["added"]
        code, out, _ = self.run_cli(self.base_argv("--cancel", "--id", rid))
        self.assertEqual(json.loads(out)["status"], "canceled")
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=due))
        self.assertEqual(out, "")

    def test_global_pause_cli_silent(self):
        self.write_prefs(paused=True)
        self.run_cli(self.base_argv("--add-time", "--text", "Paused", "--due",
                                    iso(T0 + timedelta(minutes=30)), "--provenance", "user request"))
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=T0 + timedelta(minutes=30)))
        self.assertEqual(out, "")

    def test_corrupt_state_fails_closed(self):
        self.write_prefs()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        (self.store_dir / R.REGISTRY).write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli(self.base_argv("dispatch"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("corrupt", err)

    def test_corrupt_preferences_fails_closed(self):
        self.prefs_path.write_text("{oops", encoding="utf-8")
        code, out, err = self.run_cli(self.base_argv("dispatch"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("preferences corrupt", err)

    def test_in_process_cli_event_move_with_injected_getter(self):
        """Full CLI pipeline (parse/lock/persist/stdout) with a synthetic getter."""
        self.write_prefs()
        due = T0 + timedelta(minutes=60)
        getter = _ok_getter(due)
        code, out, _ = self.run_cli(self.base_argv(
            "--add-event", "--text", "Standup", "--alias", "primary", "--calendar-id", "primary",
            "--event-id", "e1", "--lead-minutes", "10", "--provenance", "explicit user request",
            clock=T0), getter=getter)
        self.assertEqual(code, 0)
        # Move the event later; old due must not fire.
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(due + timedelta(hours=1))))
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=due - timedelta(minutes=10)), getter=getter)
        self.assertEqual(out, "")
        code, out, _ = self.run_cli(self.base_argv(
            "dispatch", clock=due + timedelta(hours=1)), getter=getter)
        self.assertEqual(out, "Standup\n")

    def test_cli_earlier_move_discovered_before_old_trigger(self):
        """A move hours earlier must be found by the CLI before the old trigger."""
        self.write_prefs()
        due = T0 + timedelta(hours=5)
        getter = _ok_getter(due)
        code, out, _ = self.run_cli(self.base_argv(
            "--add-event", "--text", "Meeting", "--alias", "primary", "--calendar-id", "primary",
            "--event-id", "e1", "--lead-minutes", "10", "--provenance", "explicit user request",
            clock=T0), getter=getter)
        self.assertEqual(code, 0)
        # Moved to +1h; query at +50m (old trigger is +4h50m away).
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(T0 + timedelta(hours=1))))
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=T0 + timedelta(minutes=50)),
                                    getter=getter)
        self.assertEqual(out, "Meeting\n")
        reg = R.load_registry(self.store())
        self.assertEqual(next(iter(reg["reminders"].values()))["event_due"],
                         (T0 + timedelta(hours=1)).isoformat())

    def test_cli_restart_after_old_time_revalidates_later(self):
        """Past old event+grace, a later move rebinds instead of being missed."""
        self.write_prefs()
        due = T0 + timedelta(hours=1)
        getter = _ok_getter(due)
        self.run_cli(self.base_argv(
            "--add-event", "--text", "Review", "--alias", "primary", "--calendar-id", "primary",
            "--event-id", "e1", "--lead-minutes", "10", "--provenance", "explicit user request",
            clock=T0), getter=getter)
        getter.set("primary", "primary", "e1", R.normalize_event(confirmed(T0 + timedelta(hours=4))))
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=T0 + timedelta(hours=2)),
                                    getter=getter)
        self.assertEqual(out, "")
        reg = R.load_registry(self.store())
        rem = next(iter(reg["reminders"].values()))
        self.assertEqual(rem["status"], "pending")
        self.assertEqual(rem["event_due"], (T0 + timedelta(hours=4)).isoformat())

    def test_cli_real_cancellation_shape_retires(self):
        self.write_prefs()
        due = T0 + timedelta(hours=1)
        getter = _ok_getter(due)
        self.run_cli(self.base_argv(
            "--add-event", "--text", "Meeting", "--alias", "primary", "--calendar-id", "primary",
            "--event-id", "e1", "--lead-minutes", "10", "--provenance", "explicit user request",
            clock=T0), getter=getter)
        # Exact Google cancellation tombstone shape: no start, only status/id.
        getter.answers[("primary", "primary", "e1")] = R.normalize_event({"status": "cancelled", "id": "e1"})
        code, out, _ = self.run_cli(self.base_argv("dispatch", clock=T0 + timedelta(minutes=5)),
                                    getter=getter)
        self.assertEqual(out, "")
        reg = R.load_registry(self.store())
        self.assertEqual(next(iter(reg["reminders"].values()))["status"], "retired")

    def test_malformed_calendar_json_fails_closed(self):
        getter = SyntheticEventGetter()
        getter.set("primary", "primary", "e1", {"ok": True, "status": "confirmed", "start": "", "version": "v"})
        self.write_prefs()
        code, out, err = self.run_cli(self.base_argv(
            "--add-event", "--text", "Bad", "--alias", "primary", "--calendar-id", "primary",
            "--event-id", "e1", "--lead-minutes", "10", "--provenance", "user request",
            clock=T0), getter=getter)
        self.assertEqual(code, 2)
        self.assertIn("rejected", err)
        reg = R.load_registry(self.store())
        self.assertEqual(reg["reminders"], {})

    def test_scheduler_command_does_not_create_jobs(self):
        code, out, _ = self.run_cli(["--scheduler-command"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("dispatch", payload["command"])
        self.assertIn("does not create", payload["note"])


class TestSubprocessCli(ReminderTestCase):
    """Exercise the real launcher as a subprocess (time-only; no source reads)."""

    def setUp(self) -> None:
        super().setUp()
        # The launcher checks the native toggle before doing any work. Use a
        # fake profile that explicitly enables the plugin; never read a live
        # profile config.
        (self.root / "config.json").write_text(json.dumps(
            {"plugins": {"enabled": ["hermes-proactive-secretary"]}}), encoding="utf-8")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        self.addCleanup(self._restore_home)

    def _restore_home(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home

    def _run(self, argv: list[str]):
        return subprocess.run([sys.executable, *argv], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)

    def _base(self) -> list[str]:
        policy = self.root / "policy.json"
        policy.write_text("{}", encoding="utf-8")
        return [str(LAUNCHER), "--state-dir", str(self.store_dir),
                "--policy", str(policy), "--preferences", str(self.prefs_path)]

    def test_subprocess_add_dispatch_restart(self):
        self.write_prefs()
        due = T0 + timedelta(minutes=30)
        base = self._base()
        add = self._run(base + ["--seeded-clock", iso(T0), "--add-time", "--text", "Ship it",
                                "--due", iso(due), "--provenance", "explicit user request"])
        self.assertEqual(add.returncode, 0, add.stderr)
        first = self._run(base + ["--seeded-clock", iso(due), "dispatch"])
        self.assertEqual(first.stdout, "Ship it\n")
        second = self._run(base + ["--seeded-clock", iso(due), "dispatch"])
        self.assertEqual(second.stdout, "")

    def test_subprocess_empty_is_silent(self):
        self.write_prefs()
        base = self._base() + ["--seeded-clock", iso(T0), "dispatch"]
        proc = self._run(base)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
