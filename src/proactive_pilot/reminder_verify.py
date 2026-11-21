"""Safe end-to-end verification harness for the durable requested-reminder path.

This harness drives the REAL production modules (``proactive_pilot.reminders`` +
``proactive_pilot.state``) with:
  * an INJECTABLE fake event transport (the ``EventGetter`` protocol), and
  * an explicit fake clock (timestamps are passed in, never read from the wall),
  * isolated owner-only state (a caller-supplied ``StateStore`` root; 0700/0600).

It NEVER instantiates a private/production client, NEVER sends (no Telegram, no
network), and NEVER touches a real reminder registry. Every message text is
synthetic.

What it exercises: add, exact GET, move (earlier/later), cancel, dispatch,
whole-message output queue/overflow, dedup, restart/reload, quiet-hours bypass
for an explicitly-requested reminder, global pause, plus negative controls.

``parent_evidence_plan()`` returns a read-only plan the PARENT can use to verify
the already-requested 9pm school-fee reminder, distinguishing a standalone
native one-shot job from the durable dispatcher route. It fabricates no consent
and creates no synthetic live reminder.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import reminders as R
from .state import StateStore

# Synthetic, non-private labels only.
MSG = "synthetic reminder (test); no private content"
MSGS = [f"{i:02d} " + "x" * 296 for i in range(5)]  # <= MAX_TEXT


class HarnessError(AssertionError):
    """A required invariant of the verification run did not hold."""


class ReminderVerificationHarness:
    """Production-module E2E harness with fake transport and fake clock."""

    def __init__(self, root: Path, *, start: datetime, getter: R.EventGetter):
        self.root = Path(root)
        self.start = start
        self.getter = getter
        self.store = StateStore(self.root, R.Clock())
        self.steps: list[dict[str, Any]] = []
        self.getter_calls_before = 0

    # -- helpers -----------------------------------------------------------
    def _record(self, name: str, **fields: Any) -> None:
        self.steps.append({"step": name, "ok": True, **fields})

    def _load(self) -> dict[str, Any]:
        return R.load_registry(self.store)

    def _save(self, reg: dict[str, Any], now: datetime) -> None:
        R.prune_registry(reg, now)
        R.save_registry(self.store, reg, now)

    def _reload(self) -> dict[str, Any]:
        # A brand-new store object over the same root simulates a fresh process.
        self.store = StateStore(self.root, R.Clock())
        return self._load()

    def _getter_calls(self) -> int:
        return len(getattr(self.getter, "calls", []))

    def _active(self, reg: dict[str, Any]) -> dict[str, Any]:
        rems = reg.get("reminders", {})
        if len(rems) != 1:
            raise HarnessError(f"expected exactly one reminder, found {len(rems)}")
        return next(iter(rems.values()))

    # -- scenario steps ----------------------------------------------------
    def add(self, *, alias: str, cal: str, eid: str, due: datetime,
            lead: int = 10, provenance: str = "synthetic explicit user request",
            now: datetime | None = None) -> dict[str, Any]:
        now = now or self.start
        with self.store.lock() as acquired:
            if not acquired:
                raise HarnessError("unexpected lock contention in isolated harness")
            reg = self._load()
            entry = R.add_event(reg, text=MSG, alias=alias, calendar_id=cal,
                                event_id=eid, lead_minutes=lead, provenance=provenance,
                                now=now, getter=self.getter)
            self._save(reg, now)
        self._record("add", id=entry["id"], status=entry["status"],
                     trigger_at=entry["trigger_at"])
        return entry

    def add_time(self, text: str, due: datetime, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or self.start
        with self.store.lock() as acquired:
            if not acquired:
                raise HarnessError("unexpected lock contention in isolated harness")
            reg = self._load()
            entry = R.add_time(reg, text=text, due=due.isoformat(),
                               provenance="synthetic explicit user request", now=now)
            self._save(reg, now)
        return entry

    def dispatch(self, now: datetime, *, prefs: dict[str, Any] | None = None,
                 check: bool = False) -> tuple[str, dict[str, Any]]:
        """Production dispatch semantics: persist ``emitting`` then finalize."""
        prefs = prefs or {"paused": {"active": False}}
        with self.store.lock() as acquired:
            if not acquired:
                raise HarnessError("unexpected lock contention in isolated harness")
            reg = self._load()
            if R.pause_active(prefs, now):
                if check:
                    R._diag({"diagnostic": "skip-paused"})
                return "", {"blocked": "paused", "emitting": [], "reads": 0}
            plan = R.plan_dispatch(reg, now=now, prefs=prefs, getter=self.getter)
            if check:
                return R.combine_messages(plan.messages), {
                    "blocked": plan.blocked, "emitting": list(plan.emitting),
                    "reads": plan.checked,
                    "would_emit": [r["id"] for r in reg["reminders"].values()
                                   if r.get("status") == "emitting"],
                }
            self._save(reg, now)  # persist emitting BEFORE stdout
            message = R.combine_messages(plan.messages)
            if plan.emitting:
                R.finalize_dispatch(reg, plan.emitting, now=now)
            self._save(reg, now)
        return message, {"blocked": plan.blocked, "emitting": list(plan.emitting),
                         "reads": plan.checked, "overflow": plan.overflow,
                         "queued": list(plan.queued)}

    def cancel(self, rid: str, now: datetime) -> None:
        with self.store.lock() as acquired:
            if not acquired:
                raise HarnessError("unexpected lock contention in isolated harness")
            reg = self._load()
            R.cancel(reg, rid, now=now)
            self._save(reg, now)

    # -- full scenario -----------------------------------------------------
    def run(self) -> dict[str, Any]:
        """Execute the full scripted scenario; return non-private evidence."""
        t = self.start
        checks: list[dict[str, Any]] = []

        def check(name: str, condition: bool, detail: str = "") -> None:
            checks.append({"check": name, "passed": bool(condition), "detail": detail})
            if not condition:
                raise HarnessError(f"{name}: {detail or 'failed'}")

        # 1. add + exact GET + dedup
        due = t + timedelta(hours=1)
        self.getter.set("primary", "primary", "e1",
                        R.normalize_event(_confirmed(due)))
        first = self.add(alias="primary", cal="primary", eid="e1", due=due)
        second = self.add(alias="primary", cal="primary", eid="e1", due=due,
                          now=t + timedelta(minutes=1))
        check("add_pending", first["status"] == "pending")
        check("add_exact_trigger", first["trigger_at"] == (due - timedelta(minutes=10)).isoformat())
        check("dedup_same_id", first["id"] == second["id"])
        check("dedup_single_record", len(self._load()["reminders"]) == 1)

        # 2. restart/reload before dispatch: still exactly one pending
        reg = self._reload()
        check("restart_single_pending",
              len(reg["reminders"]) == 1 and self._active(reg)["status"] == "pending")

        # 3. dispatch at trigger -> one emission, then emitted after restart
        msg, info = self.dispatch(due - timedelta(minutes=10))
        check("dispatch_emits_once", msg == MSG and info["emitting"])
        reg = self._reload()
        check("dispatch_emitted", self._active(reg)["status"] == "emitted")
        msg2, _ = self.dispatch(due - timedelta(minutes=10) + timedelta(minutes=1))
        check("dispatch_no_duplicate", msg2 == "")

        # 4. move-earlier discovered before old trigger
        due2 = t + timedelta(hours=5)
        self.getter.set("primary", "primary", "e2", R.normalize_event(_confirmed(due2)))
        moved = self.add(alias="primary", cal="primary", eid="e2", due=due2)
        self.getter.set("primary", "primary", "e2",
                        R.normalize_event(_confirmed(t + timedelta(hours=1))))
        msg3, info3 = self.dispatch(t + timedelta(minutes=50))
        check("move_earlier_emits", msg3 == MSG and info3["emitting"] == [moved["id"]])

        # 5. cancel: no emission
        due3 = t + timedelta(hours=2)
        self.getter.set("primary", "primary", "e3", R.normalize_event(_confirmed(due3)))
        doomed = self.add(alias="primary", cal="primary", eid="e3", due=due3)
        self.cancel(doomed["id"], t)
        msg4, _ = self.dispatch(due3)
        check("cancel_no_emission", msg4 == "")

        # 6. whole-message queue / overflow: bounded line, overflow stays pending
        for i in range(5):
            self.add_time(MSGS[i], t)
        msg5, info5 = self.dispatch(t)
        check("queue_bounded", len(msg5) <= R.OUTPUT_CAP)
        check("queue_overflow_pending", info5["overflow"] >= 1)
        reg = self._reload()
        for rid in info5["queued"]:
            check(f"queued_pending_{rid[:6]}", reg["reminders"][rid]["status"] == "pending")

        # 7. quiet-hours bypass for an explicitly requested reminder
        #    03:00 SG is inside 00:00-08:00 quiet hours.
        quiet = t.replace(hour=3, minute=0, second=0, microsecond=0)
        qt = self.add_time("synthetic quiet-hours reminder", quiet, now=quiet)
        msg6, _ = self.dispatch(quiet)
        check("quiet_hours_bypass", msg6 == "synthetic quiet-hours reminder")

        # 8. global pause stops all reads and emissions
        due4 = t + timedelta(hours=3)
        self.getter.set("primary", "primary", "e4", R.normalize_event(_confirmed(due4)))
        self.add(alias="primary", cal="primary", eid="e4", due=due4)
        before = self._getter_calls()
        msg7, info7 = self.dispatch(due4, prefs={"paused": {"active": True}})
        check("pause_silent", msg7 == "" and info7["blocked"] == "paused")
        check("pause_zero_reads", self._getter_calls() == before)

        # 9. negative controls (each on its own fresh in-memory registry)
        check("negative_missing_provenance_rejected", _missing_provenance_rejected(t))
        check("negative_malformed_due_rejected", _malformed_due_rejected())
        check("negative_source_failure_uncertain", _source_failure_uncertain(t))

        passed = sum(1 for c in checks if c["passed"])
        return {
            "harness": "reminder-verification",
            "sends": False,
            "private_clients": False,
            "isolated_state_root": str(self.root),
            "steps": self.steps,
            "checks": checks,
            "checks_passed": passed,
            "checks_total": len(checks),
            "ok": passed == len(checks),
        }


# --------------------------------------------------------------------------- #
# Synthetic fixtures / negative controls (no production client)
# --------------------------------------------------------------------------- #

def _confirmed(start: datetime) -> dict[str, Any]:
    return {"status": "confirmed",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
            "updated": "2026-10-03T00:00:00Z"}


class FakeEventTransport:
    """Injectable fake event transport; records exact GETs, touches no network."""

    def __init__(self, answers: dict[tuple[str, str, str], dict[str, Any]] | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str]] = []

    def set(self, alias: str, cal: str, eid: str, obs: dict[str, Any]) -> None:
        self.answers[(alias, cal, eid)] = obs

    def get(self, alias: str, cal: str, eid: str) -> dict[str, Any]:
        self.calls.append((alias, cal, eid))
        return self.answers.get(
            (alias, cal, eid),
            {"ok": False, "status": "error", "error": "synthetic not found"})


def _missing_provenance_rejected(now: datetime) -> bool:
    reg = R.empty_registry()
    reg.setdefault("reminders", {})["neg1"] = {
        "id": "neg1", "kind": "time", "status": "pending", "text": "synthetic",
        "provenance": "", "due": (now + timedelta(minutes=5)).isoformat(),
        "late_grace_minutes": 10,
    }
    plan = R.plan_dispatch(reg, now=now + timedelta(minutes=6),
                           prefs={"paused": {"active": False}},
                           getter=FakeEventTransport())
    return reg["reminders"]["neg1"]["status"] == "rejected" and plan.messages == []


def _malformed_due_rejected() -> bool:
    reg = R.empty_registry()
    try:
        R.add_time(reg, text="synthetic", due="not-a-time",
                   provenance="synthetic explicit user request",
                   now=datetime(2026, 10, 3, 12, 0))
    except R.ReminderError:
        return reg["reminders"] == {}
    return False


def _source_failure_uncertain(now: datetime) -> bool:
    reg = R.empty_registry()
    key = "neg2"
    reg.setdefault("reminders", {})[key] = {
        "id": key, "kind": "event", "status": "pending", "text": "synthetic",
        "provenance": "synthetic explicit user request", "alias": "primary",
        "calendar_id": "primary", "event_id": "missing", "lead_minutes": 10,
        "late_grace_minutes": 10,
        "event_due": (now + timedelta(minutes=10)).isoformat(),
        "trigger_at": now.isoformat(),
        "last_check_at": "", "event_version": "v1", "attempts": 0,
    }
    getter = FakeEventTransport()  # every GET fails
    plan = R.plan_dispatch(reg, now=now, prefs={"paused": {"active": False}}, getter=getter)
    return reg["reminders"][key]["status"] == "uncertain" and plan.messages == []


# --------------------------------------------------------------------------- #
# Parent-only, READ-ONLY evidence plan (no live verification performed here)
# --------------------------------------------------------------------------- #

def parent_evidence_plan(*, reminder_time: str = "21:00", tz: str = "Asia/Singapore",
                         state_dir: str = "/path/to/state") -> dict[str, Any]:
    """Read-only plan to verify the already-requested 9pm school-fee reminder.

    This fabricates nothing and performs no live read/send. It tells the parent
    exactly which read-only evidence distinguishes the two routes.
    """
    return {
        "scope": "parent-only read-only verification; no send, no write, no live client here",
        "requested_reminder": {"time": reminder_time, "tz": tz,
                               "topic": "school fee (user-requested)"},
        "route_a_standalone_native_one_shot": {
            "what_it_is": "A one-off native scheduler job armed by the assistant, independent of reminders.json.",
            "where_to_look": [
                "native scheduler job list (parent-only): a single one-shot entry at " + reminder_time,
                "job payload must contain the exact user text and explicit provenance",
            ],
            "evidence_of_delivery": "scheduler delivery/ack log entry at the reminder instant",
        },
        "route_b_durable_dispatcher": {
            "what_it_is": "A reminders.json entry consumed by the plugin reminder runner on the 1-min cadence.",
            "where_to_look": [
                f"read-only registry summary: python3 runners/proactive_secretary_reminder_runner.py "
                f"--state-dir {state_dir} --list   (redacted: ids/status/trigger only)",
                "expect kind=time, status=pending, due=" + reminder_time + " " + tz,
                "trigger shows for kind=time as the exact due time (no lead)",
            ],
            "evidence_of_emission": "the dispatcher's single bounded stdout line at the due minute "
                                    "(scheduler log), then status transitions pending->emitting->emitted",
            "evidence_of_delivery": "the native scheduler's own delivery confirmation is the only "
                                    "proof of delivery; 'emitted' is an emission, not a confirmed receipt",
        },
        "distinguishing_checks": [
            "if a one-shot native job exists AND no reminders.json entry -> Route A",
            "if a reminders.json time entry exists -> Route B (even if a one-shot also exists)",
            "both present: treat as duplicate; confirm only one line was emitted at " + reminder_time,
        ],
        "hard_stops": [
            "no Telegram/network send from this harness",
            "no default/private client instantiation",
            "no synthetic live reminder is created",
            "no consent is fabricated",
        ],
    }
