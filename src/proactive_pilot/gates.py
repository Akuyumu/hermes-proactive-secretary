"""Deterministic gates + final delivery decision.

Order matters: the model only proposes; these gates decide whether anything may
leave the process. Gates never rank importance.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .candidates import candidate_signature
from .evaluator import EvaluatorResult
from .policy import (
    active_snooze,
    cooldown_active,
    daily_normal_cap,
    in_quiet_hours,
    local_day,
    preference_blocks,
    stored_local_day,
)
from .state import parse_iso


def _configured_source_labels(policy: dict[str, Any]) -> list[str]:
    """Source labels that are switched on or carry a consented reference.

    Deliberately broader than "has data": the notice exists for a genuinely
    empty configuration, so an enabled scope with no data yet must not be
    described as "nothing is set up".
    """
    labels: list[str] = []
    consent = (
        ("mail", ("mail_enabled", "preparation_source_consent"), ("mail_accounts", "mail_account_aliases")),
        ("calendar", ("calendar_enabled",), ("calendar_accounts", "calendar_ids")),
        ("tasks", ("tasks_enabled",), ("task_accounts", "task_lists", "task_adapter")),
    )
    for label, switches, keys in consent:
        if any(policy.get(key) for key in switches + keys):
            labels.append(label)
    if policy.get("obligations_enabled"):
        labels.append("obligations")
    if policy.get("feeds_enabled") and (
            policy.get("public_sources") or policy.get("feed_seed_sources")):
        labels.append("public")
    return labels


def configuration_notice(
    state: dict[str, Any],
    policy: dict[str, Any],
    *,
    now: Any = None,
    commit: bool = True,
) -> str | None:
    """One deduped notice when no source is consented yet.

    This is the "never configured" variant of :func:`coverage_notice`: it reuses
    the same ``coverage_notified_at`` cooldown record (no second dedupe store)
    and names only the missing piece plus the one-line fix. It never names a
    provider the owner did not grant.
    """
    if _configured_source_labels(policy):
        return None
    now = now if now is not None else datetime.now()
    last_notified = state.get("coverage_notified_at")
    if not isinstance(last_notified, dict):
        last_notified = {}
    cooldown = timedelta(minutes=int(policy.get("coverage_notice_cooldown_minutes", 60)))
    stamp = last_notified.get("configuration")
    if stamp:
        try:
            last = datetime.fromisoformat(str(stamp))
        except ValueError:
            last = None
        if last is not None:
            if last.tzinfo is None and getattr(now, "tzinfo", None) is not None:
                last = last.replace(tzinfo=now.tzinfo)
            if now - last < cooldown:
                return None
    if commit:
        last_notified["configuration"] = now.isoformat()
        state["coverage_notified_at"] = last_notified
    return (
        "Nothing is set up to watch yet: no source is consented. "
        "Run `hermes secretary setup` and choose one source to watch."
    )


def coverage_notice(
    results: list[Any],
    state: dict[str, Any],
    policy: dict[str, Any],
    *,
    now: Any = None,
    commit: bool = True,
) -> str | None:
    """One deduped coverage notice when a source newly fails.

    Transactional: the failed-source signature is only consumed when a notice is
    actually produced (``commit=True``) so a caller that suppresses the notice
    (quiet hours, cooldown, cap, pause/snooze, or a competing proposal) can leave
    the state untouched and surface it on a later allowed run. Recovery is not
    recorded, which also stops a fail/recover/fail flap from re-notifying.

    Per-source cooldown: a source that keeps flapping (A fail, B fail, A fail)
    is not re-notified within ``coverage_notice_cooldown_minutes`` of its own last
    notice. The cooldown is bounded by that clock, so a genuine later loss of the
    same source still surfaces once the window lapses; it is never permanently
    suppressed.
    """
    if not results:
        return configuration_notice(state, policy, now=now, commit=commit)
    failed = sorted(r.name for r in results if not r.healthy)
    # A positive healthy read ends a failure episode. Observe recovery even
    # when notification is deferred; absence/truncation is not recovery and no
    # newly failed source is consumed here.
    recovered = {r.name for r in results if r.healthy}
    if recovered and "failed_sources" in state:
        state["failed_sources"] = [
            name for name in state["failed_sources"] if name not in recovered
        ]
    previous = set(state.get("failed_sources", []))
    last_notified = state.get("coverage_notified_at")
    if not isinstance(last_notified, dict):
        last_notified = {}
    now = now if now is not None else datetime.now()
    cooldown = timedelta(minutes=int(policy.get("coverage_notice_cooldown_minutes", 60)))

    def _cooling(name: str) -> bool:
        stamp = last_notified.get(name)
        if not stamp:
            return False
        try:
            last = datetime.fromisoformat(str(stamp))
        except ValueError:
            return False
        if last.tzinfo is None and getattr(now, "tzinfo", None) is not None:
            last = last.replace(tzinfo=now.tzinfo)
        return now - last < cooldown

    newly = [name for name in failed if name not in previous and not _cooling(name)]
    if not newly:
        return None
    if commit:
        state["failed_sources"] = failed
        for name in newly:
            last_notified[name] = now.isoformat()
        state["coverage_notified_at"] = last_notified
    labels = ", ".join(newly)
    if len(newly) > 1:
        return f"Coverage note: {len(newly)} sources could not be read just now ({labels})."
    return f"Coverage note: {labels} could not be read just now."


def commit_coverage_notice(results: list[Any], state: dict[str, Any], now: Any = None) -> dict[str, Any]:
    """Consume the failed-source signature after a notice is actually emitted."""
    failed = sorted(r.name for r in results if not r.healthy)
    state["failed_sources"] = failed
    notified = state.get("coverage_notified_at")
    if not isinstance(notified, dict):
        notified = {}
    stamp = now if now is not None else datetime.now()
    for name in failed:
        notified[name] = stamp.isoformat()
    state["coverage_notified_at"] = notified
    return state


def deferred_coverage_notice(
    results: list[Any],
    state: dict[str, Any],
    policy: dict[str, Any],
    now: Any,
    *,
    commit: bool = True,
) -> str | None:
    """Coverage notice that is never consumed while delivery is suppressed.

    During quiet hours the failed-source signature is left untouched so the
    next allowed run still sees it as new and can surface it once at wake.
    """
    if in_quiet_hours(policy, now):
        return None
    return coverage_notice(results, state, policy, now=now, commit=commit)


def notice_allowed(
    state: dict[str, Any], policy: dict[str, Any], prefs: dict[str, Any], now: Any
) -> tuple[bool, str]:
    """Shared envelope guards for health/coverage notices.

    Health notices are not an escape hatch: they obey the same quiet, pause,
    snooze, cooldown and daily-cap limits as ordinary deliveries. When blocked,
    the caller must not consume the notice state so it stays available.
    """
    if in_quiet_hours(policy, now):
        return False, "quiet hours"
    if active_snooze(prefs, now):
        return False, "snoozed"
    if cooldown_active(state, now, int(policy.get("cooldown_minutes", 30))):
        return False, "cooldown"
    if _over_cap(state, policy, now, "normal"):
        return False, "daily cap"
    return True, "ok"


EVALUATOR_FAILURE_NOTICE = (
    "Proactive checks are having trouble and will keep retrying. "
    "You may miss important time-sensitive alerts in the meantime."
)


def needs_evaluator_notice(
    state: dict[str, Any], policy: dict[str, Any]
) -> bool:
    """True when a repeated-failure notice is armed but not yet emitted.

    Lets the caller prioritise a real coverage notice without consuming the
    evaluator notice: it stays armed for the next run.
    """
    failures = state.get("evaluator_failures")
    if not isinstance(failures, dict):
        return False
    threshold = int(policy.get("evaluator_failure_notice_threshold", 3))
    return int(failures.get("count", 0)) >= threshold and not failures.get("notified_at")


def note_evaluator_outcome(
    proposal: EvaluatorResult,
    state: dict[str, Any],
    policy: dict[str, Any],
    prefs: dict[str, Any],
    now: Any,
) -> str | None:
    """Persist a minimal, redacted evaluator failure counter.

    Repeated failures produce exactly one short waking-hours notice per failure
    streak; a transient success resets the streak. No raw error text, model
    output or message body is ever stored or emitted.
    """
    failures = state.get("evaluator_failures")
    if not isinstance(failures, dict):
        failures = {"count": 0, "last_at": "", "notified_at": ""}
        state["evaluator_failures"] = failures
    if proposal.action in {"error", "invalid"}:
        failures["count"] = int(failures.get("count", 0)) + 1
        failures["last_at"] = now.isoformat()
        threshold = int(policy.get("evaluator_failure_notice_threshold", 3))
        if failures["count"] >= threshold and not failures.get("notified_at"):
            allowed, _reason = notice_allowed(state, policy, prefs, now)
            if not allowed:
                return None  # stay armed; retried on the next allowed run
            failures["notified_at"] = now.isoformat()
            return EVALUATOR_FAILURE_NOTICE
        return None
    failures["count"] = 0
    failures["notified_at"] = ""
    return None


def decide(
    proposal: EvaluatorResult,
    packet: dict[str, Any],
    *,
    clock: Any,
    policy: dict[str, Any],
    prefs: dict[str, Any],
    state: dict[str, Any],
    requested_reminder: bool = False,
) -> dict[str, Any]:
    """Return {'send': bool, 'message': str, 'reason': str, 'urgent': bool}.

    ``requested_reminder`` bypasses the discretionary cap/cooldown per the brief
    (user-requested reminders are not discretionary).
    """
    now = clock.now()
    if proposal.action in {"invalid", "error", "silence"}:
        return {"send": False, "message": "", "reason": proposal.action, "urgent": False}

    # Expiry is validated before any bypass: an expired / malformed / source-
    # outliving proposal must never send, even if it claims urgency.
    ok, reason = _expiry_ok(proposal, packet, now)
    if not ok:
        return {"send": False, "message": "", "reason": reason, "urgent": False}

    urgent = proposal.urgency == "urgent"
    if urgent:
        ok, reason = _urgent_substantiated(proposal, packet, now, policy)
        if not ok:
            return {"send": False, "message": "", "reason": reason, "urgent": True}
    else:
        if in_quiet_hours(policy, now):
            return {"send": False, "message": "", "reason": "quiet hours", "urgent": False}

    if active_snooze(prefs, now):
        # An explicit pause/snooze is respected even for ordinary items.
        return {"send": False, "message": "", "reason": "snoozed", "urgent": urgent}

    item = {"kind": proposal.kind, "ref": proposal.key, "subject": proposal.message, "message": proposal.message}
    if preference_blocks(item, prefs, now):
        return {"send": False, "message": "", "reason": "explicit preference", "urgent": urgent}

    # Dedupe first: an already-emitted event/version/phase is suppressed
    # regardless of cooldown/cap and regardless of whether its expiry lapsed
    # (a lapsed expiry must not re-open the same event).
    cand = _candidate_for(packet, proposal.key)
    sig = candidate_signature({
        "kind": proposal.kind,
        "ref": proposal.key,
        "version": str(cand.get("version", "")),
        "phase": _phase_for(cand),
    })
    seen = state.get("items", {})
    prior = seen.get(sig)
    if prior and (prior.get("emitted") or prior.get("delivered")):
        return {"send": False, "message": "", "reason": "duplicate", "urgent": urgent}

    # A genuinely DISTINCT important event (different signature) must not be
    # suppressed by the ordinary cooldown merely because another alert was sent
    # recently. Urgent events bypass cooldown; ordinary items still respect it.
    if not requested_reminder and not urgent:
        if cooldown_active(state, now, int(policy.get("cooldown_minutes", 30))):
            return {"send": False, "message": "", "reason": "cooldown", "urgent": False}
    if not requested_reminder and _over_cap(state, policy, now, proposal.urgency):
        return {"send": False, "message": "", "reason": "daily cap", "urgent": urgent}

    return {"send": True, "message": proposal.message, "reason": "ok", "urgent": urgent, "signature": sig}


def _candidate_for(packet: dict[str, Any], key: str) -> dict[str, Any]:
    for cand in packet.get("candidates", []):
        if cand.get("ref") == key:
            return cand
    return {}


def _phase_for(cand: dict[str, Any]) -> str:
    """Stable time phase (date + hour) of a source object; '' when unknown.

    A changed phase (e.g. the same meeting moving to a new hour) is a distinct
    event for dedupe, so one timely approaching reminder can still be emitted.
    """
    raw = str(cand.get("date") or cand.get("start") or "")
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return f"{dt.date().isoformat()}T{dt.hour:02d}"


def _expiry_ok(proposal: EvaluatorResult, packet: dict[str, Any], now: Any) -> tuple[bool, str]:
    """Reject malformed, expired, or source-outliving expiry proposals."""
    raw = str(proposal.expiry or "").strip()
    if not raw:
        return True, ""
    try:
        expiry = datetime.fromisoformat(raw)
    except ValueError:
        return False, "malformed expiry"
    if expiry.tzinfo is None:
        return False, "naive expiry"
    if expiry <= now:
        return False, "expired"
    source_time = _source_time(_candidate_for(packet, proposal.key), now)
    if source_time is not None and expiry > source_time + timedelta(minutes=1):
        return False, "expiry beyond source time"
    return True, ""


def _source_time(cand: dict[str, Any], now: Any) -> datetime | None:
    raw = str(cand.get("date") or cand.get("start") or "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return dt


def _urgent_substantiated(
    proposal: EvaluatorResult, packet: dict[str, Any], now: Any, policy: dict[str, Any]
) -> tuple[bool, str]:
    """Urgency needs a real consequence, confidence, and a source-backed time bound.

    A bare evidence reference, the model's word "urgent", or a plain due date is
    never sufficient. The deadline must be tz-aware, in the future, within the
    configured bound, grounded in a concrete (non-all-day) evidence time, and
    backed by fresh healthy coverage for the item's source.
    """
    if not proposal.evidence:
        return False, "urgent without evidence"
    # Public feed posts are tentative public evidence and are never urgent, even
    # if the model labels them so.
    if proposal.kind == "feed":
        return False, "feed item never urgent"
    if not str(proposal.consequence).strip():
        return False, "urgent without consequence"
    if str(proposal.confidence) not in {"medium", "high"}:
        return False, "urgent without confidence"
    raw_deadline = str(proposal.deadline or "").strip()
    if not raw_deadline:
        return False, "urgent without deadline"
    try:
        deadline = datetime.fromisoformat(raw_deadline)
    except ValueError:
        return False, "urgent malformed deadline"
    if deadline.tzinfo is None:
        return False, "urgent naive deadline"
    if deadline <= now:
        return False, "urgent deadline not future"
    bound = now + timedelta(minutes=int(policy.get("urgent_max_minutes", 60)))
    if deadline > bound:
        return False, "urgent deadline beyond bound"

    # Source-backed time bound: at least one cited evidence item must carry a
    # concrete clock time inside the bound (all-day/date-only never counts).
    source_bound = False
    for ref in proposal.evidence:
        cand = _candidate_for(packet, ref)
        if cand.get("all_day"):
            continue
        t = _source_time(cand, now)
        if t is not None and now < t <= bound:
            source_bound = True
            break
    if not source_bound:
        return False, "urgent without source-backed time bound"

    if not _fresh_healthy_coverage(packet, proposal.key, now, policy):
        return False, "urgent without fresh healthy coverage"
    return True, "urgent"


def _fresh_healthy_coverage(
    packet: dict[str, Any], key: str, now: Any, policy: dict[str, Any]
) -> bool:
    source = str(_candidate_for(packet, key).get("source", ""))
    if not source:
        return False
    cov = packet.get("coverage", {}).get(source)
    if not isinstance(cov, dict) or cov.get("status") != "ok":
        return False
    checked = str(cov.get("checked_at", ""))
    if not checked:
        return False
    try:
        dt = datetime.fromisoformat(checked)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    age = now - dt
    return timedelta(0) <= age <= timedelta(minutes=int(policy.get("stale_after_minutes", 45)))


def _version_for(packet: dict[str, Any], key: str) -> str:
    for cand in packet.get("candidates", []):
        if cand.get("ref") == key:
            return str(cand.get("version", ""))
    return ""


def _expired(prior: dict[str, Any], now: Any) -> bool:
    """True when a stored expiry is at/before ``now`` (a datetime)."""
    expiry = prior.get("expiry")
    if not expiry:
        return False
    try:
        reference = now.now() if hasattr(now, "now") else now
        return parse_iso(expiry, reference) <= reference
    except ValueError:
        return False


def delivered_today(state: dict[str, Any], policy: dict[str, Any], now: Any) -> int:
    """Count ordinary delivery rows whose stored timestamp is today locally."""
    today = local_day(now, policy)
    return sum(
        1 for row in state.get("delivered", [])
        if stored_local_day(row.get("at"), policy, now) == today
    )


def _over_cap(state: dict[str, Any], policy: dict[str, Any], now: Any, urgency: str) -> bool:
    if urgency == "urgent":
        return False  # urgency bypasses the ordinary cap
    cap = daily_normal_cap(policy)
    if cap is None:
        # Unset means no deterministic daily-count ceiling. Quiet hours,
        # cooldown, dedupe, approvals and urgent/reminder exceptions still apply.
        return False
    return delivered_today(state, policy, now) >= cap


def record_delivery(state: dict[str, Any], decision: dict[str, Any], packet: dict[str, Any], clock: Any, policy: dict[str, Any]) -> dict[str, Any]:
    now = clock.now()
    sig = decision.get("signature", "unknown")
    items = state.setdefault("items", {})
    items[sig] = {
        "first_seen": now.isoformat(),
        # The bridge writes stdout; the native scheduler owns delivery, so we
        # only record an EMISSION, never a confirmed delivery. A future run may
        # reconcile against native run records, but we must not advertise a
        # successful delivered state here.
        "emitted": True,
        "status": "emitted",
        "reconciled": False,
        "kind": decision.get("kind", ""),
        "key": decision.get("key", ""),
        "expiry": decision.get("expiry", ""),
    }
    state.setdefault("delivered", []).append({"at": now.isoformat(), "signature": sig, "urgent": bool(decision.get("urgent")), "status": "emitted"})
    state["last_delivery_at"] = now.isoformat()
    return state
