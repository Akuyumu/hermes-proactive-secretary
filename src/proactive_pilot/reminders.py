"""Durable, owner-only requested-reminder registry + followthrough dispatcher.

This is a NEW, isolated module for explicitly user-requested reminders. It does
NOT modify the existing pilot; it reuses the approved runtime pieces:

- ``StateStore`` for owner-only (0700 dir / 0600 files) atomic JSON and the
  shared single-writer lock used by the main pilot.
- ``load_policy`` / strict ``load_preferences_file`` and ``active_snooze`` for
  the user's deployed pause/preference envelope.
- The shipped secretary client via ``collectors._calendar_client`` /
  ``collectors._google_aliases`` for a read-only, exact event GET.

Hard rules enforced here:
- Typed source IDs only (explicit ``alias``/``calendarId``/``eventId``); never a
  natural-language guess, never a search.
- Shared pause ALWAYS stops reads and emissions. A requested reminder bypasses
  quiet hours / daily cap / cooldown (the user explicitly asked for it) but never
  a pause and never the requirement for explicit provenance consent.
- One bounded event GET: cadence <= 1/5 min before trigger, a single fresh read
  at due, per-GET timeout, and an overall dispatcher deadline.
- No raw event/mail bodies or URLs are persisted; only narrow IDs plus a content
  version hash and the tz-aware start/status.
- Exactly one emission per reminder across processes/overlap: the shared lock is
  held, and a reminder moves ``pending`` -> ``emitting`` (persisted) -> stdout ->
  ``emitted``. A stale ``emitting`` from an interrupted run becomes ``uncertain``
  and is never blindly resent. ``emitted`` is an emission, not a confirmed
  delivery (the native scheduler owns actual delivery).

Nothing here sends anything; ``dispatch`` only writes one bounded stdout line or
stays silent.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from .alertrefs import remember_alert
from .clock import Clock, SeededClock, build_clock
from .feedback import FeedbackError, load_preferences_file
from .policy import active_snooze, load_policy
from .state import CorruptStateError, StateStore

REGISTRY = "reminders.json"
SCHEMA = 1
MAX_REMINDERS = 100
DEFAULT_LATE_GRACE_MINUTES = 10
MAX_TEXT = 300
MAX_PROVENANCE = 300
MAX_LIST_PREVIEW = 40
POLL_CADENCE_MINUTES = 5
GET_TIMEOUT_SECONDS = 10
DISPATCH_BUDGET_SECONDS = 40
MAX_EVENT_READS_PER_RUN = 5
OUTPUT_CAP = 350
# Distinct process exit codes so the scheduler can tell a lock-contention skip
# from a genuine successful dispatch. For the SCHEDULED dispatch path a brief
# overlap is NOT a user-facing failure: the native scheduler treats any non-zero
# exit as an error, so a routine busy skip must stay a silent, exit-0 skip rather
# than become failure spam. Explicit mutation commands (add/cancel) are
# user-invoked and report busy truthfully via EXIT_BUSY.
EXIT_OK = 0
EXIT_BUSY = 75  # EX_TEMPFAIL: another run holds the shared lock (mutations only)
# Scheduled dispatch keeps the silent skip: the default scheduled stdout is empty
# and the exit status is OK. A busy dispatch therefore does NOT prove the registry
# was checked -- it only proves another run held the lock -- and that limitation
# is intentional until the scheduler can treat a distinct retry code as healthy.
DISPATCH_BUSY_EXIT = EXIT_OK
ACTIVE_STATUSES = {"pending", "uncertain"}
MISS_NOTE_PREFIX = "Missed reminder: "

_ROOT_ENV = "PROACTIVE_FOLLOWTHROUGH_ROOT"


def default_root() -> Path:
    override = os.environ.get(_ROOT_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def parse_aware(value: Any) -> datetime | None:
    """Parse an ISO timestamp, requiring an explicit UTC offset."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _new_id(parts: list[str], now: datetime) -> str:
    raw = "|".join(parts) + "|" + now.isoformat()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _preview(text: Any, limit: int = MAX_LIST_PREVIEW) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)] + "\u2026"


# --------------------------------------------------------------------------- #
# Event observation
# --------------------------------------------------------------------------- #

class EventGetter(Protocol):
    def get(self, alias: str, calendar_id: str, event_id: str) -> dict[str, Any]: ...


def normalize_event(raw: Any, *, status: str | None = None) -> dict[str, Any]:
    """Reduce a raw calendar event to a minimal, tz-exact observation.

    Never retains a body/description/location/URL. A Google cancellation
    tombstone legitimately has ``status="cancelled"`` and NO ``start``: that is a
    valid, actionable observation (it must retire a bound reminder), so it
    normalizes to ``ok=True`` with ``status="cancelled"``. A non-cancelled event
    with a missing or naive start is not an exact time and fails closed.
    """
    if not isinstance(raw, dict):
        return {"ok": False, "status": status or "error", "error": "malformed event"}
    event_status = str(status or raw.get("status") or "confirmed")
    cancelled = event_status == "cancelled"
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    start_raw = str(start.get("dateTime") or "")
    if not start_raw:
        if cancelled:
            # Legitimate cancellation tombstone: no time is required.
            version = hashlib.sha256("|".join([
                event_status, str(raw.get("updated") or ""), str(raw.get("id") or ""),
            ]).encode("utf-8")).hexdigest()[:16]
            return {"ok": True, "status": "cancelled", "start": "", "version": version, "error": ""}
        return {"ok": False, "status": event_status, "error": "event has no exact timezone-aware start"}
    parsed = parse_aware(start_raw)
    if parsed is None:
        if cancelled:
            version = hashlib.sha256("|".join([
                event_status, start_raw, str(raw.get("updated") or ""), str(raw.get("id") or ""),
            ]).encode("utf-8")).hexdigest()[:16]
            return {"ok": True, "status": "cancelled", "start": "", "version": version, "error": ""}
        return {"ok": False, "status": event_status, "error": "event start is malformed or naive"}
    version = hashlib.sha256("|".join([
        event_status,
        start_raw,
        str(end.get("dateTime") or ""),
        str(raw.get("updated") or ""),
    ]).encode("utf-8")).hexdigest()[:16]
    return {
        "ok": True,
        "status": "cancelled" if cancelled else "confirmed",
        "start": parsed.isoformat(),
        "version": version,
        "error": "",
    }


class _GetTimeout(Exception):
    pass


def _with_timeout(fn: Callable[[], Any], seconds: float) -> Any:
    """Run ``fn`` with a bounded wall-clock timeout in the main thread.

    Uses the shared watchdog deadline so a per-event GET cannot exceed its
    budget AND cannot clear a surrounding timer: an enclosing deadline keeps its
    earlier bound and exception identity. In a non-main thread (tests) the guard
    simply runs ``fn`` inline.
    """
    from .watchdog import deadline_guard

    try:
        limit = float(seconds)
    except (TypeError, ValueError):
        limit = 0.1
    if not math.isfinite(limit) or limit < 0.1:
        limit = 0.1
    with deadline_guard(limit, _GetTimeout):
        return fn()


class SecretaryEventGetter:
    """Read-only, exact, single GET through the shipped secretary client.

    Validates the account alias and the selected/primary calendar before the
    request. Failures are returned as observations, never raised to the caller.
    """

    def __init__(
        self,
        *,
        timeout: float = GET_TIMEOUT_SECONDS,
        client_factory: Callable[[str], Any] | None = None,
        aliases: list[str] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.timeout = float(timeout)
        self._client_factory = client_factory
        self._aliases = list(aliases) if aliases is not None else None
        self._policy = policy or {}
        # Explicit calendar scope. When the validated configuration DISABLES the
        # calendar source, no event getter/source call is ever made. When it
        # names an explicit (possibly empty) account set, only those aliases are
        # authorised -- mail accounts are NEVER unioned in.
        self._enabled = bool(self._policy.get("calendar_enabled", True))
        configured_ids = self._policy.get("calendar_ids")
        self._calendar_ids = [str(c) for c in configured_ids] if isinstance(
            configured_ids, (list, tuple)) and configured_ids else None

    @property
    def event_reads_allowed(self) -> bool:
        """False when no event source read may happen at all.

        A disabled calendar, or an explicit configuration with an empty account
        set, means the dispatcher must not even call :meth:`get` for an event:
        the reminder stays active/deferred rather than reading a default/foreign
        account. Legacy (no configuration) keeps library discovery.
        """
        if not self._enabled:
            return False
        if self._aliases is None:
            return True
        return bool(self._aliases)

    def _authorized_aliases(self) -> list[str]:
        if self._aliases is None:
            from .collectors import _google_aliases

            self._aliases = list(_google_aliases(self._policy))
        return self._aliases

    def _client(self, alias: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(alias)
        from .collectors import _calendar_client

        return _calendar_client(alias)

    def get(self, alias: str, calendar_id: str, event_id: str) -> dict[str, Any]:
        # Explicit configuration cannot be bypassed: a disabled calendar or an
        # empty explicit account set makes event verification explicitly
        # unavailable WITHOUT touching any client or source.
        if not self._enabled:
            return {"ok": False, "status": "unsupported",
                    "error": "calendar source disabled by configuration"}
        if self._aliases is not None and not self._aliases:
            return {"ok": False, "status": "unsupported",
                    "error": "no explicit calendar accounts configured"}
        # The optional Google Workspace adapter has NO verified read-only
        # fresh-event GET on this surface, so when it is the selected calendar
        # source (and the compatibility library is not), event verification is
        # explicitly UNSUPPORTED. It must never silently fall back to the
        # compatibility library or fetch a foreign/default account. The caller
        # treats this as a non-destructive deferred/uncertain state.
        if self._policy.get("gws_calendar_enabled") and not self._policy.get(
                "productivity_secretary_enabled"):
            return {"ok": False, "status": "unsupported",
                    "error": "event verification unsupported on the gws calendar adapter"}
        if alias not in self._authorized_aliases():
            return {"ok": False, "status": "error", "error": "alias not authorised"}
        if self._calendar_ids is not None and calendar_id not in self._calendar_ids:
            return {"ok": False, "status": "error", "error": "calendar not explicitly configured"}
        try:
            client = self._client(alias)
            from .collectors import _selected_calendars

            approved = {str(cal.get("id")) for cal in _selected_calendars(client)}
            if calendar_id not in approved:
                return {"ok": False, "status": "error", "error": "calendar not approved"}
            url = client._events(calendar_id) + "/" + urllib.parse.quote(event_id, safe="")
            raw = _with_timeout(lambda: client._request("GET", url), self.timeout)
        except _GetTimeout:
            return {"ok": False, "status": "error", "error": "event GET timed out"}
        except Exception:  # noqa: BLE001 - source failure must fail closed
            return {"ok": False, "status": "error", "error": "event GET failed"}
        return normalize_event(raw)


# --------------------------------------------------------------------------- #
# Registry mutations
# --------------------------------------------------------------------------- #

def empty_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": "", "reminders": {}, "poll_cursor": 0}


class ReminderError(ValueError):
    """Invalid reminder request or unavailable source; fail closed."""


def _require_text(value: Any, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReminderError(f"{name} is required")
    if len(text) > limit:
        raise ReminderError(f"{name} too long (max {limit})")
    return text


def _require_provenance(value: Any) -> str:
    return _require_text(value, "provenance", MAX_PROVENANCE)


def _active_count(reg: dict[str, Any]) -> int:
    return sum(1 for rem in reg.get("reminders", {}).values()
               if rem.get("status") in ACTIVE_STATUSES)


def _event_key(alias: str, calendar_id: str, event_id: str, lead: int, provenance: str) -> str:
    """Idempotency key for an explicit event reminder request.

    Scoped to the exact authorised IDs, the lead, and the explicit provenance so
    a replay of the same request is deduped while a genuinely different request
    (different lead/provenance) is not blanket-suppressed.
    """
    raw = "|".join([alias, calendar_id, event_id, str(lead), provenance])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _find_by_key(reg: dict[str, Any], key: str) -> dict[str, Any] | None:
    for rem in reg.get("reminders", {}).values():
        if isinstance(rem, dict) and rem.get("key") == key:
            return rem
    return None


def add_time(
    reg: dict[str, Any],
    *,
    text: Any,
    due: Any,
    provenance: Any,
    now: datetime,
    late_grace_minutes: int = DEFAULT_LATE_GRACE_MINUTES,
    rid: str | None = None,
) -> dict[str, Any]:
    """Register an exact, timezone-aware time reminder."""
    if _active_count(reg) >= MAX_REMINDERS:
        raise ReminderError("reminder registry is full")
    message = _require_text(text, "text", MAX_TEXT)
    prov = _require_provenance(provenance)
    parsed = parse_aware(due)
    if parsed is None:
        raise ReminderError("due must be an exact timezone-aware ISO timestamp")
    grace = max(0, min(int(late_grace_minutes), 24 * 60))
    if parsed < now - timedelta(minutes=grace):
        raise ReminderError("due is already past its late grace")
    rid = rid or _new_id(["time", message, parsed.isoformat(), prov], now)
    entry = {
        "id": rid,
        "kind": "time",
        "status": "pending",
        "text": message,
        "provenance": prov,
        "created_at": now.isoformat(),
        "due": parsed.isoformat(),
        "late_grace_minutes": grace,
        "attempts": 0,
        "last_check_at": "",
        "emitting_at": "",
        "emitted_at": "",
        "missed_at": "",
        "miss_notified": False,
        "reason": "",
    }
    reg.setdefault("reminders", {})[rid] = entry
    return entry


def add_event(
    reg: dict[str, Any],
    *,
    text: Any,
    alias: Any,
    calendar_id: Any,
    event_id: Any,
    lead_minutes: Any,
    provenance: Any,
    now: datetime,
    getter: EventGetter,
    late_grace_minutes: int = DEFAULT_LATE_GRACE_MINUTES,
    rid: str | None = None,
) -> dict[str, Any]:
    """Register a reminder bound to an exact authorised calendar event.

    A preflight fresh GET must succeed and yield an exact tz-aware time; a
    canceled, all-day, missing, or unreadable event is rejected (fail closed) so
    no reminder is ever bound to a guessed/stale time.
    """
    if _active_count(reg) >= MAX_REMINDERS:
        raise ReminderError("reminder registry is full")
    message = _require_text(text, "text", MAX_TEXT)
    prov = _require_provenance(provenance)
    src_alias = _require_text(alias, "alias", 64)
    cal_id = _require_text(calendar_id, "calendarId", 256)
    evt_id = _require_text(event_id, "eventId", 256)
    try:
        lead = int(lead_minutes)
    except (TypeError, ValueError) as exc:
        raise ReminderError("lead_minutes must be an integer") from exc
    if lead < 0 or lead > 24 * 60:
        raise ReminderError("lead_minutes out of range (0..1440)")
    grace = max(0, min(int(late_grace_minutes), 24 * 60))

    obs = getter.get(src_alias, cal_id, evt_id)
    if not obs.get("ok"):
        raise ReminderError(f"event unavailable: {obs.get('error', 'unknown')}")
    if obs.get("status") == "cancelled":
        raise ReminderError("event is cancelled")
    event_due = parse_aware(obs.get("start"))
    if event_due is None:
        raise ReminderError("event has no exact timezone-aware start")

    # Idempotency: an explicit replay of the same authorised event plus lead and
    # provenance must not create a duplicate request. Reuse a live record for the
    # same (alias, calendar, event, lead, provenance) key; never blanket-suppress
    # a *new* request merely because a prior one for that event already emitted.
    key = _event_key(src_alias, cal_id, evt_id, lead, prov)
    existing = _find_by_key(reg, key)
    if existing is not None:
        return existing

    rid = rid or _new_id(["event", message, src_alias, cal_id, evt_id, str(lead), prov], now)
    entry = {
        "id": rid,
        "kind": "event",
        "key": key,
        "status": "pending",
        "text": message,
        "provenance": prov,
        "created_at": now.isoformat(),
        "alias": src_alias,
        "calendar_id": cal_id,
        "event_id": evt_id,
        "lead_minutes": lead,
        "late_grace_minutes": grace,
        "event_due": event_due.isoformat(),
        "event_version": str(obs.get("version") or ""),
        "trigger_at": (event_due - timedelta(minutes=lead)).isoformat(),
        "bindings": [{"version": str(obs.get("version") or ""), "due": event_due.isoformat()}],
        "attempts": 0,
        # The preflight GET just read the source; seed the cadence clock so the
        # very next same-minute dispatch does not immediately re-read it.
        "last_check_at": now.isoformat(),
        "emitting_at": "",
        "emitted_at": "",
        "missed_at": "",
        "miss_notified": False,
        "reason": "",
    }
    reg.setdefault("reminders", {})[rid] = entry
    return entry


def cancel(reg: dict[str, Any], rid: str, *, now: datetime) -> dict[str, Any]:
    rem = reg.get("reminders", {}).get(str(rid))
    if not isinstance(rem, dict):
        raise ReminderError(f"no reminder with id {rid!r}")
    if rem.get("status") in {"emitted", "missed", "canceled", "retired"}:
        return rem
    rem["status"] = "canceled"
    rem["reason"] = "canceled by user"
    rem["canceled_at"] = now.isoformat()
    return rem


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def pause_active(prefs: dict[str, Any], now: datetime) -> bool:
    """Absolute global pause / unscoped snooze gate.

    Strict: an active pause with a malformed or naive ``until`` stays paused
    (fail closed), matching the main pilot. Never bypassed by requested reminders.
    """
    paused = prefs.get("paused") or {}
    if isinstance(paused, dict) and paused.get("active"):
        until = paused.get("until")
        if not until:
            return True
        parsed = parse_aware(until)
        if parsed is None:
            return True
        if parsed > now:
            return True
    return bool(active_snooze(prefs, now))


@dataclass
class DispatchPlan:
    messages: list[str] = field(default_factory=list)
    emitting: list[str] = field(default_factory=list)
    checked: int = 0
    pending: int = 0
    blocked: str = ""
    changes: bool = False
    overflow: int = 0
    queued: list[str] = field(default_factory=list)


def _fit_message(current: str, message: str, cap: int) -> str | None:
    """Return the combined line if ``message`` fits within ``cap``, else None.

    This is the overflow guard: a message that would push the single bounded
    stdout line past the cap must NOT be marked emitted, or the reminder is
    silently lost (the native scheduler only sees the truncated line). Returning
    None keeps that reminder ``pending`` for a later run.
    """
    combined = message if not current else f"{current} {message}"
    if len(combined) > cap:
        return None
    return combined


def _emit_decision(
    plan: DispatchPlan,
    rem: dict[str, Any],
    now: datetime,
    message: str,
    *,
    cap: int = OUTPUT_CAP,
) -> bool:
    """Queue a reminder for emission only if it fully fits the output cap.

    The caller persists the ``emitting`` set before writing stdout, so only
    records whose message is actually represented become ``emitting``. An
    overflowing reminder stays ``pending`` (queued for a later dispatch) rather
    than being silently truncated away.
    """
    current = combine_messages(plan.messages, cap=cap)
    fitted = _fit_message(current, message, cap)
    if fitted is None:
        plan.overflow += 1
        plan.queued.append(str(rem.get("id")))
        return False
    rem["status"] = "emitting"
    rem["emitting_at"] = now.isoformat()
    rem["attempts"] = int(rem.get("attempts", 0)) + 1
    plan.emitting.append(str(rem.get("id")))
    plan.messages.append(message)
    plan.changes = True
    return True


def _miss(plan: DispatchPlan, rem: dict[str, Any], now: datetime, reason: str) -> None:
    rem["status"] = "missed"
    rem["missed_at"] = now.isoformat()
    rem["reason"] = reason
    plan.changes = True
    _queue_miss_notice(plan, rem)


def _queue_miss_notice(plan: DispatchPlan, rem: dict[str, Any]) -> None:
    """Queue a bounded missed notice if it fits; otherwise re-queue next run.

    The missed record is terminal, but the informational notice is still subject
    to the output cap so it never truncates the active stdout line. An un-notified
    missed record is revisited on a later run until its notice is delivered once.
    """
    if rem.get("miss_notified"):
        return
    notice = f"{MISS_NOTE_PREFIX}{_preview(rem.get('text'), 120)}"
    current = combine_messages(plan.messages)
    if _fit_message(current, notice, OUTPUT_CAP) is None:
        return  # retried on a later run; record remains missed but un-notified
    rem["miss_notified"] = True
    plan.messages.append(notice)


def _retire(plan: DispatchPlan, rem: dict[str, Any], now: datetime, reason: str) -> None:
    rem["status"] = "retired"
    rem["reason"] = reason
    rem["retired_at"] = now.isoformat()
    plan.changes = True


def _reject(plan: DispatchPlan, rem: dict[str, Any], reason: str) -> None:
    rem["status"] = "rejected"
    rem["reason"] = reason
    plan.changes = True


def _rebind(rem: dict[str, Any], event_due: datetime, version: str) -> None:
    lead = int(rem.get("lead_minutes", 0))
    rem["event_due"] = event_due.isoformat()
    rem["event_version"] = version
    rem["trigger_at"] = (event_due - timedelta(minutes=lead)).isoformat()
    rem["last_check_at"] = ""  # a move invalidates the old cadence cache
    bindings = rem.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
    bindings.append({"version": version, "due": event_due.isoformat()})
    rem["bindings"] = bindings[-5:]


def _cadence_due(rem: dict[str, Any], now: datetime) -> bool:
    """True when this active bound event is due a cadence-limited fresh GET.

    Active event reminders always poll on a fixed <=5 min cadence, regardless of
    how far away the (possibly stale) old trigger is, so an event moved hours
    earlier (or later) is discovered promptly rather than only near the old time.
    """
    last = parse_aware(rem.get("last_check_at"))
    if last is None:
        return True
    return now - last >= timedelta(minutes=POLL_CADENCE_MINUTES)


def _reminder_text(rem: dict[str, Any], event_due: datetime, now: datetime) -> str:
    """Message text for a confirmed emission.

    The user's explicit text is preserved verbatim when neutral. A relative-lead
    phrase (e.g. "in 10 min") that would be misleading because the event moved or
    we are late is recomputed from the current event time so we never emit stale
    timing.
    """
    text = str(rem.get("text", ""))
    lead = int(rem.get("lead_minutes", 0))
    minutes_out = int(round((event_due - now).total_seconds() / 60.0))
    expected = f"in {lead} min"
    if expected in text and minutes_out != lead:
        recomputed = text.replace(expected, f"in {minutes_out} min")
        if len(recomputed) <= MAX_TEXT:
            return recomputed
    return text


def _evaluate_event(
    plan: DispatchPlan,
    rem: dict[str, Any],
    *,
    now: datetime,
    getter: EventGetter,
    deadline: float,
) -> None:
    event_due = parse_aware(rem.get("event_due"))
    lead = int(rem.get("lead_minutes", 0))
    grace = int(rem.get("late_grace_minutes", DEFAULT_LATE_GRACE_MINUTES))
    if event_due is None:
        _reject(plan, rem, "stored event time is malformed")
        return

    # A fresh, accountable source read is REQUIRED before any missed/expiry
    # decision. Polling is cadence-limited (<=5 min), regardless of the old
    # trigger, so a moved-earlier event is found early; a read failure leaves the
    # record active/uncertain and never produces a stale "missed" notice.
    if not _cadence_due(rem, now):
        return
    if time.monotonic() >= deadline:
        return  # resumable: leave active for the next run
    rem["last_check_at"] = now.isoformat()
    plan.checked += 1
    obs = getter.get(str(rem.get("alias", "")), str(rem.get("calendar_id", "")), str(rem.get("event_id", "")))
    if not obs.get("ok"):
        rem["status"] = "uncertain"
        rem["reason"] = f"source unavailable: {obs.get('error', 'unknown')}"
        plan.changes = True
        return  # deferred, never a stale guessed alert
    if obs.get("status") == "cancelled":
        _retire(plan, rem, now, "event cancelled")
        return

    observed_due = parse_aware(obs.get("start"))
    observed_version = str(obs.get("version") or "")
    if observed_due is None:
        # A confirmed-but-untimed event is unaccountable; stay active/uncertain.
        rem["status"] = "uncertain"
        rem["reason"] = "current source has no exact timed start"
        plan.changes = True
        return

    moved = observed_version != str(rem.get("event_version", "")) or observed_due != event_due
    if moved:
        _rebind(rem, observed_due, observed_version)
        plan.changes = True
        event_due = observed_due

    trigger = event_due - timedelta(minutes=lead)
    within_grace = now <= event_due + timedelta(minutes=grace)
    if now >= trigger and within_grace:
        # Pre-event reminder (possibly right at due) from the CURRENT source.
        _emit_decision(plan, rem, now, _reminder_text(rem, event_due, now))
        return
    if now > event_due + timedelta(minutes=grace):
        # The event is genuinely past its late grace *after* fresh revalidation;
        # emit one truthful bounded missed notice, never the stale reminder text.
        _miss(plan, rem, now, "event time passed (revalidated against source)")
        return
    # Before the trigger: keep polling on cadence; nothing to emit yet.


def _evaluate_time(plan: DispatchPlan, rem: dict[str, Any], *, now: datetime) -> None:
    due = parse_aware(rem.get("due"))
    if due is None:
        _reject(plan, rem, "stored due time is malformed")
        return
    grace = int(rem.get("late_grace_minutes", DEFAULT_LATE_GRACE_MINUTES))
    if now > due + timedelta(minutes=grace):
        _miss(plan, rem, now, "due time passed")
        return
    if now >= due:
        _emit_decision(plan, rem, now, str(rem.get("text", "")))


def _recover_interrupted(plan: DispatchPlan, rem: dict[str, Any], now: datetime) -> None:
    if rem.get("status") == "emitting":
        # A previous process was interrupted after persisting ``emitting`` but
        # before finishing. Never blindly resend; mark terminal uncertainty.
        rem["status"] = "unconfirmed"
        rem["reason"] = "interrupted before emission completed; not resent"
        rem["emitting_at"] = rem.get("emitting_at") or now.isoformat()
        plan.changes = True


def plan_dispatch(
    reg: dict[str, Any],
    *,
    now: datetime,
    prefs: dict[str, Any],
    getter: EventGetter,
    budget_seconds: int = DISPATCH_BUDGET_SECONDS,
    max_event_reads: int = MAX_EVENT_READS_PER_RUN,
) -> DispatchPlan:
    """Evaluate active reminders. Pure w.r.t. I/O: mutates ``reg`` in memory.

    Returns the messages to emit plus the ids marked ``emitting``. The caller
    must persist the ``emitting`` state before writing stdout, then finalize.
    """
    plan = DispatchPlan()
    reminders = reg.get("reminders", {})
    # Recover any interrupted ``emitting`` records first, regardless of active
    # status, so an interrupted emission is never silently left as in-flight.
    for rem in reminders.values():
        if isinstance(rem, dict) and rem.get("status") == "emitting":
            _recover_interrupted(plan, rem, now)
    active = [rem for rem in reminders.values() if rem.get("status") in ACTIVE_STATUSES]
    plan.pending = len(active)

    # Re-queue any missed notice whose bounded text did not fit a prior run's
    # cap. This is zero-IO (no source is ever read to report a missed reminder).
    unnotified = [
        rem for rem in reminders.values()
        if isinstance(rem, dict) and rem.get("status") == "missed" and not rem.get("miss_notified")
    ]
    for rem in sorted(unnotified, key=lambda r: str(r.get("id", ""))):
        _queue_miss_notice(plan, rem)

    if not active:
        # No pending work: never touch a source.
        return plan
    if pause_active(prefs, now):
        plan.blocked = "paused"
        return plan

    deadline = time.monotonic() + max(1, int(budget_seconds))
    ordered = sorted(active, key=lambda r: str(r.get("id", "")))

    # Fair rotating cursor: start each run where the previous run stopped reading
    # so a large registry cannot starve later events. Only a bounded number of
    # event GETs happen per run; each event still polls at most once per cadence.
    cursor = int(reg.get("poll_cursor", 0)) % max(1, len(ordered))
    rotated = ordered[cursor:] + ordered[:cursor]
    event_reads = 0

    for rem in rotated:
        if rem.get("status") not in ACTIVE_STATUSES:
            continue
        if not str(rem.get("provenance", "")).strip() or not str(rem.get("text", "")).strip():
            _reject(plan, rem, "no explicit provenance consent")
            continue
        if rem.get("kind") == "event":
            # No configuration authorises calendar reads (disabled source or an
            # empty explicit account set): never call the getter/source. The
            # event reminder stays active/deferred, deterministically.
            if not bool(getattr(getter, "event_reads_allowed", True)):
                continue
            if not _cadence_due(rem, now):
                continue
            if event_reads >= max(1, int(max_event_reads)):
                continue  # bounded reads this run; resumes next run (fair cursor)
            event_reads += 1
            _evaluate_event(plan, rem, now=now, getter=getter, deadline=deadline)
        else:
            _evaluate_time(plan, rem, now=now)
    # Advance the cursor past what this run could not read, so the next run begins
    # on the unread tail instead of re-reading the same head.
    reg["poll_cursor"] = (cursor + event_reads) % max(1, len(ordered))
    return plan


def finalize_dispatch(reg: dict[str, Any], emitting_ids: list[str], *, now: datetime) -> None:
    """Confirm process-side emission (``emitting`` -> ``emitted``)."""
    for rid in emitting_ids:
        rem = reg.get("reminders", {}).get(rid)
        if isinstance(rem, dict) and rem.get("status") == "emitting":
            rem["status"] = "emitted"
            rem["emitted_at"] = now.isoformat()
            rem["emitting_at"] = ""
            rem["reason"] = "emitted (delivery owned by native scheduler)"


def combine_messages(messages: list[str], cap: int = OUTPUT_CAP) -> str:
    """One bounded stdout line for the native scheduler (or empty)."""
    text = " ".join(m.strip() for m in messages if str(m).strip())
    if not text:
        return ""
    if len(text) > cap:
        text = text[: max(1, cap - 1)].rstrip() + "\u2026"
    return text


# --------------------------------------------------------------------------- #
# Persistence + CLI
# --------------------------------------------------------------------------- #

def load_registry(store: StateStore) -> dict[str, Any]:
    data = store.load(REGISTRY, empty_registry(), strict=True)
    if not isinstance(data.get("reminders"), dict):
        raise CorruptStateError("reminders registry is malformed")
    return data


def save_registry(store: StateStore, reg: dict[str, Any], now: datetime) -> None:
    reg["schema"] = SCHEMA
    reg["updated_at"] = now.isoformat()
    store.save(REGISTRY, reg)


TOMSTONE_STATUSES = {"emitted", "missed", "canceled", "retired", "rejected", "unconfirmed"}
TOMBSTONE_RETENTION_DAYS = 30
MAX_TOMBSTONES = 500
_TERMINAL_AT_FIELDS = ("emitted_at", "missed_at", "canceled_at", "retired_at")


def _terminal_at(rem: dict[str, Any]) -> str:
    for field_name in _TERMINAL_AT_FIELDS:
        if rem.get(field_name):
            return str(rem[field_name])
    return str(rem.get("created_at", ""))


def prune_registry(reg: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Retire terminal tombstones after the retention window.

    Terminal records are dropped after ``TOMBSTONE_RETENTION_DAYS`` (so requested
    text is not retained forever) and the tombstone set is bounded. Active
    (``pending`` / ``uncertain``) records are never pruned here.
    """
    reminders = reg.get("reminders", {})
    cutoff = now - timedelta(days=TOMBSTONE_RETENTION_DAYS)
    kept: dict[str, Any] = {}
    tombstones: list[tuple[str, dict[str, Any]]] = []
    for rid, rem in reminders.items():
        if not isinstance(rem, dict):
            continue
        if rem.get("status") in ACTIVE_STATUSES:
            kept[rid] = rem
            continue
        stamp = parse_aware(_terminal_at(rem))
        if stamp is not None and stamp < cutoff:
            continue  # aged out
        tombstones.append((rid, rem))
    # Keep only the newest bounded set of tombstones.
    tombstones.sort(key=lambda item: _terminal_at(item[1]), reverse=True)
    for rid, rem in tombstones[:MAX_TOMBSTONES]:
        kept[rid] = rem
    reg["reminders"] = kept
    return reg


def summarize_registry(reg: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rid, rem in sorted(reg.get("reminders", {}).items()):
        if not isinstance(rem, dict):
            continue
        rows.append({
            "id": rid,
            "kind": rem.get("kind"),
            "status": rem.get("status"),
            "due": rem.get("due") or rem.get("event_due") or "",
            "trigger": rem.get("trigger_at", ""),
            "lead_minutes": rem.get("lead_minutes"),
            "calendar_id": rem.get("calendar_id", ""),
            "event_id": rem.get("event_id", ""),
            "text_preview": _preview(rem.get("text")),
            "provenance": "present" if str(rem.get("provenance", "")).strip() else "missing",
        })
    return rows


def _default_parser() -> ArgumentParser:
    root = default_root()
    parser = ArgumentParser(prog="proactive-followthrough",
                            description="Durable requested-reminder registry (read-only sources).")
    parser.add_argument("--state-dir", default=str(root / "var"))
    parser.add_argument("--policy", default=str(root / "policy" / "policy.json"))
    parser.add_argument("--preferences", default=str(root / "policy" / "preferences.json"))
    parser.add_argument("--seeded-clock", default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add-time", action="store_true")
    group.add_argument("--add-event", action="store_true")
    group.add_argument("--list", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--cancel", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--scheduler-command", action="store_true")
    parser.add_argument("--id", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--due", default="")
    parser.add_argument("--provenance", default="")
    parser.add_argument("--alias", default="")
    parser.add_argument("--calendar-id", default="")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--lead-minutes", type=int, default=10)
    parser.add_argument("--late-grace-minutes", type=int, default=DEFAULT_LATE_GRACE_MINUTES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", nargs="?", default="dispatch",
                        help="dispatch (default) | check | list | status")
    return parser


def _diag(payload: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _emit(text: str) -> None:
    if text:
        sys.stdout.write(text.strip() + "\n")


def _load_prefs(path: str) -> dict[str, Any]:
    return load_preferences_file(Path(path), strict=True)


def load_effective_policy(
    policy_path: Any,
    *,
    native: Any = None,
    require_config: bool = False,
) -> tuple[dict[str, Any], list[str] | None, str]:
    """Return ``(policy, authorised_aliases, error)`` for the reminder path.

    This applies the SAME validated per-user configuration precedence as the
    monitoring pipeline (native settings preferred, then the file beside the
    policy). A present-but-invalid configuration fails closed (error returned, no
    dispatch). ``authorised_aliases`` is ``None`` only when NO configuration is
    present (legacy compatibility); when a configuration is present it is the
    explicit configuration's calendar account set -- possibly empty -- so the
    dispatcher never silently falls back to the broad ``_google_aliases`` list or
    unions mail accounts into calendar authorisation.

    ``require_config`` is the production-entry contract: with no configuration
    present a neutral, source-disabled configuration is applied (exact-time
    reminders still work deterministically; no legacy private source is read).
    """
    policy = load_policy(policy_path)
    try:
        from .userconfig import ConfigError, apply_user_config, default_config, load_user_config
    except Exception:  # noqa: BLE001 - config layer unavailable -> legacy behaviour
        return policy, None, ""
    uc_path = Path(policy_path).with_name("user_config.json") if policy_path else None
    try:
        loaded = load_user_config(uc_path, native=native)
    except ConfigError as exc:
        return policy, None, str(exc)
    if not loaded.present:
        if require_config:
            # Neutral no-config production default: no explicit calendar accounts
            # => the empty alias set forbids every event getter/source call.
            policy = apply_user_config(policy, default_config())
            return policy, [], ""
        return policy, None, ""
    policy = apply_user_config(policy, loaded.config)
    # Event authorisation comes ONLY from explicit calendar accounts. Mail
    # accounts are never unioned into calendar access. When the configuration
    # marks calendar scope as "legacy library discovery" (migrated from a policy
    # that omitted account scope), authorisation must follow that discovery
    # instead of collapsing to an empty (deny-everything) list.
    advanced = loaded.config.get("advanced") or {}
    discovery = advanced.get("legacy_account_discovery") if isinstance(advanced, dict) else None
    accounts = [str(name) for name in (loaded.config.get("calendar", {}).get("accounts") or [])]
    if not accounts and isinstance(discovery, list) and "calendar" in discovery:
        return policy, None, ""
    aliases: list[str] = []
    for name in accounts:
        if name not in aliases:
            aliases.append(name)
    return policy, aliases, ""


def main(
    argv: list[str] | None = None,
    *,
    getter: EventGetter | None = None,
    native_ctx: Any = None,
    require_config: bool = False,
) -> int:
    args = _default_parser().parse_args(argv)
    command = str(args.command or "dispatch")
    if command == "check":
        args.check = True
    elif command == "list":
        args.list = True
    elif command == "status":
        args.status = True
    elif command != "dispatch":
        print(f"proactive-followthrough: unknown command {command!r}", file=sys.stderr)
        return 2
    clock = SeededClock(args.seeded_clock) if args.seeded_clock else Clock()
    now = clock.now()
    store = StateStore(Path(args.state_dir), clock)

    if args.scheduler_command:
        launcher = (Path(__file__).resolve().parents[2] / "runners"
                    / "proactive_secretary_reminder_runner.py")
        print(json.dumps({
            "note": "parent installs the native one-minute script job; this does not create it",
            "schedule": "every 1 minute",
            "command": f"python3 {launcher} --state-dir {args.state_dir} "
                       f"--policy {args.policy} --preferences {args.preferences} dispatch",
        }, indent=2))
        return 0

    if args.add_time or args.add_event:
        try:
            prefs = _load_prefs(args.preferences)
        except FeedbackError as exc:
            print(f"proactive-followthrough: preferences corrupt, failing closed: {exc}", file=sys.stderr)
            return 1
        policy, configured_aliases, cfg_error = load_effective_policy(
            args.policy, native=native_ctx, require_config=require_config)
        if cfg_error:
            print(f"proactive-followthrough: user configuration invalid, failing closed: {cfg_error}",
                  file=sys.stderr)
            return 1
        clock = build_clock(policy.get("timezone"), args.seeded_clock)
        now = clock.now()
        store = StateStore(Path(args.state_dir), clock)
        with store.lock() as acquired:
            if not acquired:
                _diag({"diagnostic": "skip-lock", "reason": "another run holds the lock"})
                return EXIT_BUSY
            try:
                reg = load_registry(store)
            except CorruptStateError as exc:
                print(f"proactive-followthrough: state corrupt, failing closed: {exc}", file=sys.stderr)
                return 1
            try:
                if args.add_time:
                    entry = add_time(reg, text=args.text, due=args.due, provenance=args.provenance,
                                     now=now, late_grace_minutes=args.late_grace_minutes)
                else:
                    entry = add_event(reg, text=args.text, alias=args.alias, calendar_id=args.calendar_id,
                                      event_id=args.event_id, lead_minutes=args.lead_minutes,
                                      provenance=args.provenance, now=now,
                                      getter=getter or SecretaryEventGetter(
                                          aliases=configured_aliases, policy=policy),
                                      late_grace_minutes=args.late_grace_minutes)
            except ReminderError as exc:
                _diag({"diagnostic": "rejected", "reason": str(exc)})
                return 2
            prune_registry(reg, now)
            save_registry(store, reg, now)
        print(json.dumps({
            "added": entry["id"],
            "kind": entry["kind"],
            "status": entry["status"],
            "due": entry.get("due") or entry.get("event_due"),
            "trigger": entry.get("trigger_at", ""),
        }, indent=2))
        return 0

    if args.list or args.status:
        try:
            reg = load_registry(store)
        except CorruptStateError as exc:
            print(f"proactive-followthrough: state corrupt, failing closed: {exc}", file=sys.stderr)
            return 1
        rows = summarize_registry(reg)
        if args.status:
            counts: dict[str, int] = {}
            for row in rows:
                counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
            active = [r for r in rows if r["status"] in ACTIVE_STATUSES]
            next_due = min((r["trigger"] or r["due"] for r in active if (r["trigger"] or r["due"])), default="")
            print(json.dumps({"counts": counts, "active": len(active), "next": next_due}, indent=2))
        else:
            print(json.dumps({"reminders": rows, "count": len(rows)}, indent=2))
        return 0

    if args.cancel:
        with store.lock() as acquired:
            if not acquired:
                _diag({"diagnostic": "skip-lock", "reason": "another run holds the lock"})
                return EXIT_BUSY
            try:
                reg = load_registry(store)
                rem = cancel(reg, args.id, now=now)
            except (CorruptStateError, ReminderError) as exc:
                print(f"proactive-followthrough: cancel rejected: {exc}", file=sys.stderr)
                return 2
            prune_registry(reg, now)
            save_registry(store, reg, now)
        print(json.dumps({"canceled": rem["id"], "status": rem["status"]}, indent=2))
        return 0

    # dispatch (default) or check
    return _dispatch(store, args, clock, now, getter=getter, check=bool(args.check),
                     native_ctx=native_ctx, require_config=require_config)


def _dispatch(store: StateStore, args: Namespace, clock: Clock, now: datetime, *,
              getter: EventGetter | None, check: bool, native_ctx: Any = None,
              require_config: bool = False) -> int:
    try:
        prefs = _load_prefs(args.preferences)
    except FeedbackError as exc:
        print(f"proactive-followthrough: preferences corrupt, failing closed: {exc}", file=sys.stderr)
        _emit("")
        return 1
    policy, configured_aliases, cfg_error = load_effective_policy(
        args.policy, native=native_ctx, require_config=require_config)
    if cfg_error:
        print(f"proactive-followthrough: user configuration invalid, failing closed: {cfg_error}",
              file=sys.stderr)
        _emit("")
        return 1
    clock = build_clock(policy.get("timezone"), getattr(args, "seeded_clock", None))
    now = clock.now()
    store = StateStore(store.root, clock)
    getter = getter or SecretaryEventGetter(aliases=configured_aliases, policy=policy)
    with store.lock() as acquired:
        if not acquired:
            _diag({"diagnostic": "skip-lock", "reason": "another run holds the lock"})
            _emit("")
            # Scheduled dispatch: a brief lock overlap is normal and must not be
            # recorded as a native error (failure spam). Keep the silent exit-0
            # skip; this is explicitly NOT proof that the registry was checked.
            return DISPATCH_BUSY_EXIT
        try:
            reg = load_registry(store)
        except CorruptStateError as exc:
            print(f"proactive-followthrough: state corrupt, failing closed: {exc}", file=sys.stderr)
            _emit("")
            return 1
        if pause_active(prefs, now):
            # A global pause is absolute: no source read even for --check.
            if check:
                _diag({"diagnostic": "skip-paused", "reason": "global pause active; no source read"})
            _emit("")
            return 0
        working = json.loads(json.dumps(reg))
        plan = plan_dispatch(working, now=now, prefs=prefs, getter=getter)
        if check:
            _diag({
                "command": "check",
                "pending": plan.pending,
                "event_reads": plan.checked,
                "overflow_queued": plan.queued,
                "would_emit": [str(rem.get("id")) for rem in working.get("reminders", {}).values()
                               if rem.get("status") == "emitting"],
                "messages": combine_messages(plan.messages),
                "blocked": plan.blocked,
                "hours": policy.get("quiet_start_hour"),
            })
            _emit("")
            return 0
        # Persist the emitting set BEFORE writing stdout, then finalize afterwards:
        # an interrupted run leaves ``emitting`` records that recover to
        # ``unconfirmed`` (never blindly resent).
        save_registry(store, working, now)
        message = combine_messages(plan.messages)
        _emit(message)
        _remember_reminder_alert(native_ctx, working, plan.emitting, policy, now)
        if plan.emitting:
            finalize_dispatch(working, plan.emitting, now=now)
        prune_registry(working, now)
        save_registry(store, working, now)
        return 0


def _remember_reminder_alert(native_ctx: Any, registry: dict[str, Any],
                             emitting_ids: list[str], policy: dict[str, Any],
                             now: datetime) -> None:
    """Persist bounded reminder refs for safe mobile follow-up context."""
    facade = getattr(native_ctx, "state", None) if native_ctx is not None else None
    if facade is None or not emitting_ids:
        return
    reminders = registry.get("reminders") if isinstance(registry.get("reminders"), dict) else {}
    refs = []
    for rid in emitting_ids[:3]:
        row = reminders.get(str(rid))
        if isinstance(row, dict):
            refs.append({"ref": rid, "version": row.get("version", ""),
                         "source": "reminder"})
    try:
        remember_alert(
            facade, signature="reminder:" + ",".join(str(i) for i in emitting_ids[:3]),
            kind="reminder", candidate_refs=refs,
            destination=policy.get("deliver_target") or "",
            emitted_at=now.isoformat(),
        )
    except Exception:  # noqa: BLE001
        return


if __name__ == "__main__":
    raise SystemExit(main())
