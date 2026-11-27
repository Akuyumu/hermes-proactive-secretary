"""Agentic, fail-closed gate for the credentialed LTA train-alert source.

Owner directive: the LTA DataMall API must NOT be polled on a fixed cadence (to
avoid API abuse and a banned key). It is fetched only when a bounded, cheap
PRE-FILTER finds candidate travel facts AND the plugin auxiliary MODEL LANE
judges whether the user is likely to travel by MRT/LRT within about the next two hours.

Design (all fail-closed):

1. PRE-FILTER (no model, no network). Candidate facts are: an enabled calendar
   item starting within ``LTA_EVENT_LOOKAHEAD_HOURS``; OR a location report
   newer than ``LTA_LOCATION_FRESH_HOURS``; OR a live LTA disruption currently
   standing (so recovery is tracked). With no candidate facts: NO model call and
   NO LTA call.
2. When candidate facts exist, ask the plugin auxiliary model lane a strict
   yes/no JSON question (purpose ``secretary.transport``). The lane is resolved
   with :func:`build_model_lane`; no provider or model is ever named here.
3. Only a clean ``travel_likely is True`` verdict permits the fetch.
4. HARD FLOORS (persisted in the plugin private state directory, never in the
   repo/config): at least ``LTA_MIN_INTERVAL_MINUTES`` between LTA calls; at
   most ``LTA_MAX_CALLS_PER_DAY`` per configured local day; while a
   disruption stands a shorter ``LTA_DISRUPTION_RETRY_MINUTES`` retry applies,
   still subject to the daily cap.
5. Fail CLOSED: an unavailable/erroring/non-yes model lane never fetches.
6. A skip is silent/non-alerting (the caller reports a non-evidential
   ``deferred`` source result with a bounded reason).
7. Bounded telemetry (timestamp, verdict, bounded reason, whether a disruption
   was found, a one-line clue summary) is capped in private state. No raw
   private text or coordinates are stored.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .clock import SG
from .policy import policy_zone

# -- Hard floors / bounds ------------------------------------------------------
LTA_MAX_CALLS_PER_DAY = 24
LTA_MIN_INTERVAL_MINUTES = 20
LTA_DISRUPTION_RETRY_MINUTES = 15
LTA_EVENT_LOOKAHEAD_HOURS = 6
LTA_LOCATION_FRESH_HOURS = 2
LTA_TELEMETRY_HISTORY_CAP = 50
LTA_GATE_STATE_NAME = "lta_gate_state.json"
LTA_MODEL_PURPOSE = "secretary.transport"
LTA_MODEL_PATH = "transport"
_REASON_MAX = 160
_TITLE_MAX = 80
_PLACE_MAX = 60

_CANCELLED_STATUSES = {"cancelled", "canceled", "declined"}


def _bound(text: Any, max_chars: int = _REASON_MAX) -> str:
    return str(text or "").strip()[:max_chars]


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def _as_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=SG)
    return now


def _parse_time(value: Any, now: datetime) -> datetime | None:
    text = str(value or "").strip()
    if not text or len(text) == 10:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


# -- Candidate facts -----------------------------------------------------------


@dataclass
class TransportFacts:
    """Bounded candidate facts used by the pre-filter and the model question."""

    events: tuple[dict[str, Any], ...] = ()
    location_age_minutes: int | None = None
    location_place: str = ""
    disruption_standing: bool = False

    def has_candidate(self) -> bool:
        if self.events:
            return True
        if self.disruption_standing:
            return True
        if self.location_age_minutes is not None:
            if self.location_age_minutes <= LTA_LOCATION_FRESH_HOURS * 60:
                return True
        return False

    def location_fresh(self) -> bool:
        return (self.location_age_minutes is not None
                and self.location_age_minutes <= LTA_LOCATION_FRESH_HOURS * 60)

    def clue_summary(self) -> str:
        """One-line, coordinate-free clue-set summary (counts only)."""
        parts = [f"events={len(self.events)}"]
        if self.location_age_minutes is not None:
            parts.append(f"location_age={int(self.location_age_minutes)}m")
        else:
            parts.append("location_age=none")
        parts.append(f"disruption={1 if self.disruption_standing else 0}")
        return ";".join(parts)


def _recent_events(results: Any, now: datetime) -> tuple[dict[str, Any], ...]:
    """Enabled calendar items starting within the lookahead window."""
    out: list[dict[str, Any]] = []
    window = timedelta(hours=LTA_EVENT_LOOKAHEAD_HOURS)
    for result in results or []:
        if not str(getattr(result, "name", "")).startswith("calendar:"):
            continue
        if getattr(result, "healthy", False) is False:
            continue
        for item in getattr(result, "items", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "confirmed").lower() in _CANCELLED_STATUSES:
                continue
            start = _parse_time(item.get("start") or item.get("date"), now)
            if start is None or not (now < start <= now + window):
                continue
            out.append({
                "title": _bound(item.get("title") or item.get("subject"), _TITLE_MAX),
                "start": start.isoformat(),
                "end": _bound(item.get("end"), 40),
                "location": _bound(item.get("location") or item.get("venue"), _PLACE_MAX),
            })
    out.sort(key=lambda row: row["start"])
    return tuple(out[:8])


def _latest_location_age(now: datetime, location_state: Any,
                         location_fix_at: Any) -> int | None:
    candidates: list[datetime] = []
    explicit = _parse_iso(location_fix_at)
    if explicit is not None:
        candidates.append(_as_aware(explicit))
    if isinstance(location_state, dict):
        attempts = location_state.get("location_attempts")
        if isinstance(attempts, dict):
            for raw in attempts.values():
                parsed = _parse_iso(raw)
                if parsed is not None:
                    candidates.append(_as_aware(parsed))
    if not candidates:
        return None
    latest = max(candidates)
    age = int((now - latest).total_seconds() // 60)
    return max(0, age)


def _disruption_standing(registry: Any) -> bool:
    if not isinstance(registry, dict):
        return False
    for rec in (registry.get("sources") or {}).values():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("kind") or "") != "lta_train_alerts":
            continue
        for item in rec.get("cache_items") or []:
            if not isinstance(item, dict):
                continue
            if bool(item.get("live_service_alert")) and not bool(item.get("all_clear")):
                return True
    return False


def build_transport_facts(
    *,
    now: datetime,
    results: Any = None,
    location_state: Any = None,
    location_fix_at: Any = None,
    registry: Any = None,
) -> TransportFacts:
    """Build bounded candidate facts from already-collected pipeline context."""
    now = _as_aware(now)
    return TransportFacts(
        events=_recent_events(results, now),
        location_age_minutes=_latest_location_age(now, location_state, location_fix_at),
        location_place="",
        disruption_standing=_disruption_standing(registry),
    )


# -- Decision ------------------------------------------------------------------


@dataclass
class GateDecision:
    allow: bool
    verdict: str
    reason: str
    model_called: bool = False
    clue_summary: str = ""
    disruption_standing: bool = False

    def to_source_failure(self) -> str:
        return _bound(f"transport gate skipped ({self.verdict}): {self.reason}")


def _empty_state() -> dict[str, Any]:
    return {"schema": 1, "last_call_at": "", "day": "", "calls_today": 0, "history": []}


def _state_path(state_path: Any) -> Path:
    return Path(state_path)


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("schema", 1)
    data.setdefault("last_call_at", "")
    data.setdefault("day", "")
    data.setdefault("calls_today", 0)
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not path.parent.is_symlink():
            os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _local_day(now: datetime, policy: dict[str, Any] | None = None) -> str:
    """Return the day in the configured zone, else the system local zone."""
    if isinstance(policy, dict):
        zone = policy_zone(policy)
    else:
        zone = datetime.now().astimezone().tzinfo or timezone.utc
    aware = now if now.tzinfo is not None else now.replace(tzinfo=zone)
    return aware.astimezone(zone).date().isoformat()


def _append_telemetry(state: dict[str, Any], *, now: datetime, verdict: str,
                      reason: str, disruption_found: bool, clues: str) -> None:
    history = state.setdefault("history", [])
    history.append({
        "at": _as_aware(now).isoformat(),
        "verdict": _bound(verdict, 40),
        "reason": _bound(reason),
        "disruption_found": bool(disruption_found),
        "clues": _bound(clues, 120),
    })
    if len(history) > LTA_TELEMETRY_HISTORY_CAP:
        del history[:-LTA_TELEMETRY_HISTORY_CAP]


def _model_instruction(facts: TransportFacts, now: datetime) -> str:
    question = {
        "local_time": _as_aware(now).astimezone(SG).isoformat(),
        "next_events": [
            {"title": e["title"], "start": e["start"], "end": e.get("end", ""),
             "location": e.get("location", "")}
            for e in facts.events
        ],
        "location_age_minutes": facts.location_age_minutes,
        "location_place": facts.location_place,
        "lta_disruption_standing": facts.disruption_standing,
    }
    return (
        "You decide whether this user is likely to travel by MRT/LRT "
        "within about the next 2 hours, using ONLY the bounded facts below. "
        "Answer with a strict JSON object and nothing else: "
        '{"travel_likely": <true|false>, "reason": "<short reason>"}. '
        "travel_likely MUST be the boolean true or false. Facts: "
        + json.dumps(question, ensure_ascii=False, sort_keys=True)
    )


def evaluate_transport_gate(
    *,
    now: datetime,
    facts: TransportFacts,
    state_path: Any,
    model_lane: Any = None,
    policy: dict[str, Any] | None = None,
) -> tuple[GateDecision, dict[str, Any]]:
    """Decide whether one LTA fetch is permitted. Returns (decision, state).

    The state dict is the (possibly updated) persisted gate state that the
    caller may save via :func:`save_transport_state`. Floors are recorded at the
    moment a fetch is permitted, so an interval holds even if the fetch then
    fails.
    """
    now = _as_aware(now)
    path = _state_path(state_path)
    state = _load_state(path)
    clues = facts.clue_summary()
    if state is None:
        # A corrupt/unreadable floor ledger must never be silently reset (that
        # could unboundedly re-enable polling): fail closed.
        decision = GateDecision(False, "skip-state", "transport gate state unreadable",
                                clue_summary=clues,
                                disruption_standing=facts.disruption_standing)
        return decision, _empty_state()

    if not facts.has_candidate():
        decision = GateDecision(False, "skip-no-clue", "no candidate travel clue",
                                clue_summary=clues,
                                disruption_standing=facts.disruption_standing)
        _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                          disruption_found=facts.disruption_standing, clues=clues)
        return decision, state

    # Hard floors BEFORE any model/LTA spend.
    interval = (LTA_DISRUPTION_RETRY_MINUTES if facts.disruption_standing
                else LTA_MIN_INTERVAL_MINUTES)
    last = _parse_iso(state.get("last_call_at"))
    if last is not None:
        last = _as_aware(last)
        if now - last < timedelta(minutes=interval):
            age = int(max(0, (now - last).total_seconds()) // 60)
            decision = GateDecision(
                False, "skip-floor",
                f"last LTA call {age}m ago < {interval}m floor",
                clue_summary=clues, disruption_standing=facts.disruption_standing)
            _append_telemetry(state, now=now, verdict=decision.verdict,
                              reason=decision.reason,
                              disruption_found=facts.disruption_standing, clues=clues)
            return decision, state

    day = _local_day(now, policy)
    calls_today = int(state.get("calls_today", 0) or 0) if state.get("day") == day else 0
    if calls_today >= LTA_MAX_CALLS_PER_DAY:
        decision = GateDecision(
            False, "skip-floor", f"daily LTA call cap ({LTA_MAX_CALLS_PER_DAY}) reached",
            clue_summary=clues, disruption_standing=facts.disruption_standing)
        _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                          disruption_found=facts.disruption_standing, clues=clues)
        return decision, state

    # Model lane: any absence/error/non-yes fails closed (never fail open).
    if model_lane is None or not bool(getattr(model_lane, "available", False)):
        decision = GateDecision(False, "skip-model", "model lane unavailable",
                                clue_summary=clues,
                                disruption_standing=facts.disruption_standing)
        _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                          disruption_found=facts.disruption_standing, clues=clues)
        return decision, state

    try:
        obj = model_lane.complete_json(
            instructions=_model_instruction(facts, now),
            system_prompt=(
                "You answer a strict travel-likelihood yes/no question. Never "
                "invent facts; return only the requested JSON object."),
            schema_name="transport_travel_likely",
            purpose=LTA_MODEL_PURPOSE,
            max_tokens=200,
            timeout=20,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 - any lane failure fails closed, sanitized
        decision = GateDecision(False, "skip-model", "model lane error",
                                model_called=True, clue_summary=clues,
                                disruption_standing=facts.disruption_standing)
        _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                          disruption_found=facts.disruption_standing, clues=clues)
        return decision, state

    travel = obj.get("travel_likely") if isinstance(obj, dict) else None
    reason = _bound(obj.get("reason") if isinstance(obj, dict) else "", 100)
    if travel is not True:
        decision = GateDecision(
            False, "no", f"model verdict: not travelling ({reason or 'no reason'})",
            model_called=True, clue_summary=clues,
            disruption_standing=facts.disruption_standing)
        _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                          disruption_found=facts.disruption_standing, clues=clues)
        return decision, state

    # Permitted: record the floor immediately (before the fetch itself).
    state["last_call_at"] = now.isoformat()
    state["day"] = day
    state["calls_today"] = calls_today + 1
    decision = GateDecision(
        True, "yes", reason or "model verdict: likely travelling",
        model_called=True, clue_summary=clues,
        disruption_standing=facts.disruption_standing)
    _append_telemetry(state, now=now, verdict=decision.verdict, reason=decision.reason,
                      disruption_found=facts.disruption_standing, clues=clues)
    return decision, state


def save_transport_state(state_path: Any, state: dict[str, Any]) -> None:
    try:
        _atomic_write(_state_path(state_path), state)
    except OSError:
        pass


def record_transport_outcome(
    *,
    state_path: Any,
    now: datetime,
    disruption_found: bool,
) -> None:
    """Update the latest telemetry entry with whether a disruption was found."""
    path = _state_path(state_path)
    state = _load_state(path)
    if state is None:
        return
    history = state.get("history") or []
    if history and isinstance(history[-1], dict):
        history[-1]["disruption_found"] = bool(disruption_found)
    save_transport_state(path, state)


class TransportGate:
    """Run-scoped gate: binds candidate facts + model lane + private state file."""

    def __init__(self, *, facts: TransportFacts, model_lane: Any, state_path: Any,
                 policy: dict[str, Any] | None = None):
        self.facts = facts
        self.model_lane = model_lane
        self.state_path = state_path
        self.policy = policy

    def decide(self, *, rec: Any = None, now: datetime) -> GateDecision:
        decision, state = evaluate_transport_gate(
            now=now, facts=self.facts, state_path=self.state_path,
            model_lane=self.model_lane, policy=self.policy)
        save_transport_state(self.state_path, state)
        return decision

    def record_outcome(self, *, decision: GateDecision, now: datetime,
                       disruption_found: bool) -> None:
        record_transport_outcome(state_path=self.state_path, now=now,
                                 disruption_found=disruption_found)


__all__ = [
    "LTA_MAX_CALLS_PER_DAY", "LTA_MIN_INTERVAL_MINUTES",
    "LTA_DISRUPTION_RETRY_MINUTES", "LTA_EVENT_LOOKAHEAD_HOURS",
    "LTA_LOCATION_FRESH_HOURS", "LTA_TELEMETRY_HISTORY_CAP",
    "LTA_GATE_STATE_NAME", "LTA_MODEL_PURPOSE", "LTA_MODEL_PATH",
    "TransportFacts", "GateDecision", "TransportGate",
    "build_transport_facts", "evaluate_transport_gate",
    "save_transport_state", "record_transport_outcome",
]
