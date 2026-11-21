"""Typed configuration-adjustment PROPOSALS.

A proposal is never a configuration write. The plugin may identify a concrete
operational issue from bounded evidence, then suggest one exact, typed
before/after change with a reason and expiry. Configuration stays unchanged
until the owner approves it through the existing foreground validated writer
(backup + explicit confirmation). No arbitrary key, permission, scope, model,
budget, pace or credential write is executed here.

Proposals share the SAME ordinary attention envelope as alerts and replacement
notices: the five-per-day cap, cooldown, quiet hours and pause are enforced by
the caller (:func:`proactive_pilot.gates.notice_allowed`). There is no separate
allowance. Identical unchanged proposals are deduplicated by a stable id, and a
presented/dismissed/expired change is tombstoned so later evidence churn cannot
re-arm it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .source_health import classify_failure

FILE = "config_proposals.json"
SCHEMA = 1
DEFAULT_TTL_HOURS = 168
MAX_PROPOSALS = 32
MAX_TOMBSTONES = 128

# --- Typed settings -------------------------------------------------------
# The ONLY fields a proposal may name. Nested/section paths are deliberately not
# accepted, so no arbitrary configuration key can ever be proposed or executed.
ALLOWED_KEYS: dict[str, dict[str, Any]] = {
    "feed_cadence_minutes": {
        "type": int, "minimum": 5, "maximum": 1440,
        "settings_path": "advanced.legacy_policy.feed_cadence_minutes",
    },
    "adaptive_cadence_minutes": {
        "type": int, "minimum": 5, "maximum": 1440,
        "settings_path": "advanced.legacy_policy.adaptive_cadence_minutes",
    },
    "cooldown_minutes": {
        "type": int, "minimum": 5, "maximum": 1440,
        "settings_path": "notification.cooldown_minutes",
    },
}
_TRANSIENT_CLASSES = {"rate_limited", "retryable"}


# --- Shared time/value helpers --------------------------------------------
def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_iso(value: Any, now: datetime) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def _safe_int(value: Any, default: int = 0) -> int:
    if type(value) is bool:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# --- Registry lifecycle ---------------------------------------------------
def _ensure(registry: dict[str, Any]) -> None:
    if not isinstance(registry.get("proposals"), dict):
        registry["proposals"] = {}
    if not isinstance(registry.get("tombstones"), dict):
        registry["tombstones"] = {}


def empty_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": "", "proposals": {}, "tombstones": {}}


def load_registry(store: Any) -> dict[str, Any]:
    data = store.load(FILE, empty_registry())
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), dict):
        return empty_registry()
    _ensure(data)
    return data


def save_registry(store: Any, registry: dict[str, Any]) -> None:
    store.save(FILE, registry)


def _snapshot(registry: dict[str, Any]) -> str:
    return json.dumps(registry, sort_keys=True, default=str)


def _save_if_changed(store: Any, registry: dict[str, Any], before: str,
                     now: datetime) -> bool:
    """Write only a real semantic/state change (never an empty no-op rewrite)."""
    if _snapshot(registry) == before:
        return False
    registry["updated_at"] = now.isoformat()
    save_registry(store, registry)
    return True


def _load_current(store: Any, now: datetime) -> tuple[dict[str, Any], str]:
    """Load a registry, snapshot it, and expire obsolete pending proposals."""
    registry = load_registry(store)
    before = _snapshot(registry)
    _expire(registry, now)
    return registry, before


def _validate_change(key: str, before: Any, after: Any) -> None:
    spec = ALLOWED_KEYS.get(str(key))
    if spec is None:
        raise ValueError(f"configuration key {key!r} is not proposal-eligible")
    for value in (before, after):
        if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
            raise ValueError(f"invalid typed value for {key!r}")
    if before == after:
        raise ValueError("proposal is unchanged")


def _stable_id(key: str, before: Any, after: Any, evidence: str = "") -> str:
    # Identity is the exact typed change, NOT the ever-growing evidence count:
    # an unchanged proposal must remain one proposal.
    raw = json.dumps([key, before, after], sort_keys=True)
    return "cfg-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _settings_path(key: str) -> str:
    spec = ALLOWED_KEYS.get(str(key)) or {}
    return str(spec.get("settings_path") or key)


def _policy_value(policy: Any, key: str) -> Any:
    if not isinstance(policy, dict):
        return None
    # The loaded effective policy is flat for these keys; a raw user config may
    # carry the nested settings path. Try the flat key first, then the schema path.
    if key in policy:
        return policy[key]
    path = _settings_path(key)
    value: Any = policy
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _settings_patch(key: str, after: Any) -> dict[str, Any]:
    """Actual validated settings-writer location, matching userconfig schema."""
    if key == "cooldown_minutes":
        return {"notification": {"cooldown_minutes": after}}
    return {"advanced": {"legacy_policy": {key: after}}}


def _remember_tombstone(registry: dict[str, Any], proposal_id: str,
                        reason: str, now: datetime) -> None:
    _ensure(registry)
    tombstones = registry["tombstones"]
    if proposal_id not in tombstones:
        tombstones[proposal_id] = {"at": now.isoformat(), "reason": str(reason)}
    ordered = sorted(
        tombstones.items(),
        key=lambda kv: str((kv[1] or {}).get("at") if isinstance(kv[1], dict) else ""),
        reverse=True,
    )
    registry["tombstones"] = dict(ordered[:MAX_TOMBSTONES])


def _close(registry: dict[str, Any], proposal: dict[str, Any], status: str,
           reason: str, now: datetime) -> None:
    proposal["status"] = status
    proposal["closed_at"] = now.isoformat()
    proposal["close_reason"] = str(reason)[:300]
    if status in {"dismissed", "expired"}:
        _remember_tombstone(registry, str(proposal.get("id") or ""), status, now)


def _expire(registry: dict[str, Any], now: datetime) -> None:
    for proposal in list(registry.get("proposals", {}).values()):
        if not isinstance(proposal, dict) or str(proposal.get("status")) != "pending":
            continue
        expires = _parse_iso(proposal.get("expires_at"), now)
        if expires is None or expires <= now:
            _close(registry, proposal, "expired", "proposal expired before approval", now)


# --- Source relevance -----------------------------------------------------
def _failure_threshold(policy: dict[str, Any]) -> int:
    return max(1, _safe_int(policy.get("source_replacement_failure_threshold", 3), 3))


def _source_view(public_registry: dict[str, Any] | None,
                 policy: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Normalize public sources, preserving absent-vs-malformed distinction."""
    registry = public_registry if isinstance(public_registry, dict) else None
    sources = registry.get("sources") if registry is not None else None
    if not isinstance(sources, dict):
        sources = {} if registry is not None else None
    return sources, _failure_threshold(policy)


def _find_source(sources: dict[str, Any], source_id: str) -> dict[str, Any] | None:
    direct = sources.get(source_id)
    if isinstance(direct, dict):
        return direct
    for rec in sources.values():
        if isinstance(rec, dict) and str(rec.get("id") or "") == source_id:
            return rec
    return None


def _inactive(rec: dict[str, Any]) -> bool:
    if rec.get("parked") or rec.get("disabled"):
        return True
    if rec.get("enabled") is False:
        return True
    maintenance = rec.get("maintenance")
    if isinstance(maintenance, dict):
        if str(maintenance.get("state") or "").strip().lower() in {
                "parked", "disabled", "off", "stopped"}:
            return True
    return False


def _inherits_global_cadence(rec: dict[str, Any]) -> bool:
    # A source with its own cadence field does not inherit the global default,
    # so changing feed_cadence_minutes would not affect it.
    return not rec.get("cadence_minutes")


def _reconcile(registry: dict[str, Any], policy: dict[str, Any],
               sources: dict[str, Any] | None, threshold: int,
               now: datetime) -> None:
    """Close pending proposals whose configured value or source issue changed."""
    for proposal in list(registry.get("proposals", {}).values()):
        if not isinstance(proposal, dict) or str(proposal.get("status")) != "pending":
            continue
        key = str(proposal.get("key") or "")
        if key not in ALLOWED_KEYS:
            _close(registry, proposal, "superseded",
                   "configuration key is no longer proposal-eligible", now)
            continue
        current = _policy_value(policy, key)
        before = proposal.get("before")
        if type(current) is not int or current != before:
            _close(registry, proposal, "superseded",
                   "configured value changed; this suggestion is obsolete", now)
            continue
        source_id = str(proposal.get("source_id") or "")
        if sources is None or not source_id:
            continue
        rec = _find_source(sources, source_id)
        if not isinstance(rec, dict):
            _close(registry, proposal, "withdrawn",
                   "the source this suggestion was based on is gone", now)
            continue
        if _inactive(rec):
            _close(registry, proposal, "withdrawn",
                   "the source is parked/disabled", now)
            continue
        failures = max(0, _safe_int(rec.get("failures", 0), 0))
        if failures < threshold:
            _close(registry, proposal, "withdrawn",
                   "the underlying source failure is resolved", now)
            continue
        reason = str(rec.get("stale_reason") or "")
        if classify_failure(reason) not in _TRANSIENT_CLASSES:
            _close(registry, proposal, "withdrawn",
                   "the underlying failure is no longer transient", now)
            continue
        if key == "feed_cadence_minutes" and not _inherits_global_cadence(rec):
            _close(registry, proposal, "withdrawn",
                   "the source has its own cadence; the global value is inapplicable", now)


# --- Public lifecycle APIs ------------------------------------------------
def add_proposal(
    registry: dict[str, Any],
    *,
    key: str,
    before: Any,
    after: Any,
    reason: str,
    evidence: str,
    now: datetime | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    source_id: str = "",
    issue: str = "",
    observed_failures: int = 0,
) -> dict[str, Any]:
    """Add one typed proposal, deduplicating the exact unchanged suggestion."""
    _validate_change(key, before, after)
    now = _now(now)
    _ensure(registry)
    pid = _stable_id(key, before, after)
    existing = registry["proposals"].get(pid)
    if isinstance(existing, dict):
        status = str(existing.get("status") or "")
        if status == "pending":
            expires = _parse_iso(existing.get("expires_at"), now)
            if expires is None or expires <= now:
                _close(registry, existing, "expired",
                       "proposal expired before re-observation", now)
            elif existing.get("presented_at"):
                _remember_tombstone(registry, pid, "presented", now)
            return existing
        if status in {"presented", "approved", "handoff"} or existing.get("presented_at"):
            _remember_tombstone(registry, pid, status or "presented", now)
            return existing
        if status in {"dismissed", "expired"}:
            _remember_tombstone(registry, pid, status, now)
            return existing
    if pid in registry["tombstones"]:
        if isinstance(existing, dict):
            return existing
        return {
            "id": pid, "kind": "config_adjustment", "key": key,
            "before": before, "after": after, "status": "tombstoned",
            "reason": "an identical change was already presented/resolved",
            "settings_path": _settings_path(key),
        }
    proposal = {
        "id": pid,
        "kind": "config_adjustment",
        "key": key,
        "settings_path": _settings_path(key),
        "before": before,
        "after": after,
        "reason": str(reason)[:400],
        "evidence": str(evidence)[:300],
        "status": "pending",
        "risk": "low",
        "authority": "owner-approval-required; foreground-only",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=max(1, int(ttl_hours)))).isoformat(),
        "presented_at": "",
        "dismissed_at": "",
        "closed_at": "",
        "close_reason": "",
        "handoff_at": "",
        "source_id": str(source_id or ""),
        "issue": str(issue or ""),
        "observed_failures": max(0, _safe_int(observed_failures, 0)),
        "approval": "foreground_only; not applied by this proposal",
    }
    registry["proposals"][pid] = proposal
    ordered = sorted(registry["proposals"].items(),
                     key=lambda kv: str((kv[1] or {}).get("created_at") or "")
                     if isinstance(kv[1], dict) else "", reverse=True)
    registry["proposals"] = dict(ordered[:MAX_PROPOSALS])
    registry["updated_at"] = now.isoformat()
    return proposal


def observe(
    store: Any,
    policy: dict[str, Any],
    *,
    public_registry: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Create at most one evidence-backed proposal from bounded source evidence."""
    now = _now(now)
    if not isinstance(policy, dict):
        policy = {}
    registry, before_state = _load_current(store, now)
    sources, threshold = _source_view(public_registry, policy)
    current = _safe_int(policy.get("feed_cadence_minutes", 60), 60)
    current = max(1, current)

    candidates: list[tuple[str, str, int]] = []
    if isinstance(sources, dict):
        for rid, rec in sources.items():
            if not isinstance(rec, dict) or _inactive(rec):
                continue
            failures = max(0, _safe_int(rec.get("failures", 0), 0))
            if failures < threshold:
                continue
            reason = str(rec.get("stale_reason") or "")
            if classify_failure(reason) not in _TRANSIENT_CLASSES:
                continue
            if not _inherits_global_cadence(rec):
                continue
            candidates.append((str(rec.get("id") or rid), reason, failures))

    _reconcile(registry, policy, sources, threshold, now)
    created: list[dict[str, Any]] = []
    if candidates:
        after = min(int(ALLOWED_KEYS["feed_cadence_minutes"]["maximum"]),
                    max(current + 15, current * 2))
        if after != current:
            source_id, reason, failures = sorted(candidates)[0]
            pid = _stable_id("feed_cadence_minutes", current, after)
            existing_before = registry["proposals"].get(pid)
            proposal = add_proposal(
                registry, key="feed_cadence_minutes", before=current, after=after,
                reason=("Repeated transient/rate-limited public reads at the current "
                        "cadence; slowing the ordinary feed cadence reduces provider "
                        "churn without changing access."),
                evidence=f"source {source_id}: {failures} consecutive failures ({reason[:100]})",
                now=now, ttl_hours=int(policy.get("config_proposal_ttl_hours",
                                                  DEFAULT_TTL_HOURS)),
                source_id=source_id, issue="repeated_transient_failures",
                observed_failures=failures,
            )
            fresh_slot = (
                not isinstance(existing_before, dict)
                or str(existing_before.get("status")) in {"superseded", "withdrawn"}
            )
            if fresh_slot and str(proposal.get("status")) == "pending":
                created.append(proposal)
    _save_if_changed(store, registry, before_state, now)
    return created


def pending_notice(
    store: Any,
    now: datetime | None = None,
    *,
    policy: dict[str, Any] | None = None,
    public_registry: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Return ``(proposal_id, message)`` for one relevant unpresented proposal."""
    now = _now(now)
    registry, before_state = _load_current(store, now)
    if isinstance(policy, dict):
        sources, threshold = _source_view(public_registry, policy)
        _reconcile(registry, policy, sources, threshold, now)
    _save_if_changed(store, registry, before_state, now)
    for pid, proposal in sorted(registry.get("proposals", {}).items(),
                                key=lambda kv: str((kv[1] or {}).get("created_at") or "")
                                if isinstance(kv[1], dict) else ""):
        if not isinstance(proposal, dict) or str(proposal.get("status")) != "pending":
            continue
        if proposal.get("presented_at") or proposal.get("handoff_at"):
            continue
        message = (
            f"Configuration suggestion: change {proposal.get('key')} "
            f"(settings path {proposal.get('settings_path') or proposal.get('key')}) from "
            f"{proposal.get('before')} to {proposal.get('after')}. "
            f"{proposal.get('reason')} Nothing has been changed; approve exactly "
            f"this proposal in the foreground or reply 'dismiss {pid}'."
        )
        return pid, message
    return None


def show(store: Any, proposal_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Show exactly one proposal by id (expiring obsolete pending records first)."""
    now = _now(now)
    registry, before_state = _load_current(store, now)
    _save_if_changed(store, registry, before_state, now)
    proposal = registry.get("proposals", {}).get(str(proposal_id))
    return proposal if isinstance(proposal, dict) else None


def mark_presented(store: Any, proposal_id: str, now: datetime | None = None) -> None:
    now = _now(now)
    registry = load_registry(store)
    proposal = registry.get("proposals", {}).get(str(proposal_id))
    if isinstance(proposal, dict) and str(proposal.get("status")) == "pending":
        proposal["presented_at"] = now.isoformat()
        _remember_tombstone(registry, str(proposal_id), "presented", now)
        registry["updated_at"] = now.isoformat()
        save_registry(store, registry)


def dismiss(store: Any, proposal_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    registry = load_registry(store)
    proposal = registry.get("proposals", {}).get(str(proposal_id))
    if not isinstance(proposal, dict):
        raise ValueError(f"no configuration proposal with id {proposal_id!r}")
    proposal["status"] = "dismissed"
    proposal["dismissed_at"] = now.isoformat()
    _close(registry, proposal, "dismissed", "dismissed by the owner", now)
    registry["updated_at"] = now.isoformat()
    save_registry(store, registry)
    return proposal


def approval_handoff(
    store: Any,
    proposal_id: str,
    policy: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    public_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one exact proposal and return a change for the foreground writer.

    This never applies configuration. It checks the exact id, expiry and the
    current before-value (and, when supplied, that the source issue still
    exists), then returns the typed change the existing settings writer may use.
    """
    now = _now(now)
    if not isinstance(policy, dict):
        raise ValueError("current configuration is required for approval handoff")
    registry = load_registry(store)
    _expire(registry, now)
    proposal = registry.get("proposals", {}).get(str(proposal_id))
    if not isinstance(proposal, dict):
        raise ValueError(f"no configuration proposal with id {proposal_id!r}")
    if str(proposal.get("status")) != "pending":
        raise ValueError(f"proposal {proposal_id!r} is not pending")
    expires = _parse_iso(proposal.get("expires_at"), now)
    if expires is None or expires <= now:
        _close(registry, proposal, "expired", "proposal expired before approval", now)
        save_registry(store, registry)
        raise ValueError(f"proposal {proposal_id!r} has expired")
    if isinstance(public_registry, dict):
        sources = public_registry.get("sources")
        if isinstance(sources, dict):
            threshold = _failure_threshold(policy)
            _reconcile(registry, policy, sources, threshold, now)
            if str(proposal.get("status")) != "pending":
                save_registry(store, registry)
                raise ValueError(f"proposal {proposal_id!r} is no longer relevant")
    key = str(proposal.get("key") or "")
    before = proposal.get("before")
    after = proposal.get("after")
    current = _policy_value(policy, key)
    if type(current) is not int or current != before:
        _close(registry, proposal, "superseded",
               "configured value changed; this suggestion is obsolete", now)
        save_registry(store, registry)
        raise ValueError(f"proposal {proposal_id!r} no longer matches the current value")
    _validate_change(key, before, after)
    proposal["handoff_at"] = now.isoformat()
    proposal["handoff_status"] = "awaiting-foreground-writer"
    registry["updated_at"] = now.isoformat()
    save_registry(store, registry)
    return {
        "id": str(proposal_id),
        "key": key,
        "settings_path": str(proposal.get("settings_path") or _settings_path(key)),
        "before": before,
        "after": after,
        "change": {key: after},
        "settings_change": _settings_patch(key, after),
        "risk": str(proposal.get("risk") or "low"),
        "authority": str(proposal.get("authority")
                         or "owner-approval-required; foreground-only"),
        "approval_required": True,
        "applied": False,
        "proposal": proposal,
    }


# --- CLI ------------------------------------------------------------------
def command(args: Any) -> int:
    """CLI: list/show/handoff/dismiss typed proposals (never applies a change)."""
    import sys
    from pathlib import Path

    from .clock import Clock, SeededClock
    from .state import StateStore

    clock = SeededClock(args.seeded_clock) if getattr(args, "seeded_clock", None) else Clock()
    store = StateStore(Path(args.state_dir), clock)
    action = str(getattr(args, "feedback", "") or "")

    def emit(payload: Any) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True))

    def required_id() -> str | None:
        target = str(getattr(args, "id", "") or "")
        if target:
            return target
        print("proactive-secretary config proposal rejected: --id is required",
              file=sys.stderr)
        return None

    try:
        if action == "config-proposal-list":
            registry, before_state = _load_current(store, clock.now())
            _save_if_changed(store, registry, before_state, clock.now())
            emit(registry)
            return 0
        if action in {"config-proposal-show", "config-show"}:
            target = required_id()
            if target is None:
                return 2
            proposal = show(store, target, clock.now())
            if proposal is None:
                print(f"proactive-secretary config proposal rejected: no proposal {target!r}",
                      file=sys.stderr)
                return 2
            emit(proposal)
            return 0
        if action in {"config-proposal-dismiss", "config-dismiss"}:
            target = required_id()
            if target is None:
                return 2
            proposal = dismiss(store, target, clock.now())
            emit({"feedback": action, "status": "dismissed", "proposal": proposal})
            return 0
        if action in {"config-proposal-handoff", "config-proposal-approve",
                      "config-approve"}:
            target = required_id()
            if target is None:
                return 2
            policy = getattr(args, "policy", None)
            if not isinstance(policy, dict):
                from .policy import load_policy

                path = getattr(args, "policy_path", None)
                policy = load_policy(Path(path) if path else None)
            result = approval_handoff(
                store, target, policy, now=clock.now(),
                public_registry=getattr(args, "public_registry", None),
            )
            emit(result)
            return 0
    except ValueError as exc:
        print(f"proactive-secretary config proposal rejected: {exc}", file=sys.stderr)
        return 2
    print("proactive-secretary config proposal rejected: unknown action", file=sys.stderr)
    return 2
