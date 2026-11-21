"""Durable, model-free portal model-outage lifecycle.

One bounded incident record is shared by every model-dependent lane (evaluator,
planner, intake, preparation). It is deterministic metadata only: this module
never calls a model or network, and it never stores raw provider responses,
prompts or credentials.

Rules encoded here:

* assigned primary -> assigned fallback -> stop; no implicit third route;
* one fixed MODEL-FREE notice per genuine incident, only after BOTH routes are
  unavailable. Pending/attempted/delivered/unknown are the only notice states
  (there is no "read" state);
* repeated failures/restarts never create a second notice or reset the incident;
* backoff and Retry-After defer retries, but a blocked notice (pause/quiet/cap)
  stays pending exactly once;
* an ambiguous emission is ``unknown`` and is never blindly retried or claimed
  delivered;
* only a genuine successful call recovers the incident; a later failure starts a
  new incident with a fresh notice.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .state import CorruptStateError, StateStore

SCHEMA = 1
STORE = "model_health.json"
LOCK = "model_health.lock"

NOTICE_TEXT = (
    "No configured portal model is currently available. "
    "Model-dependent work is deferred and will retry later. "
    "Requested reminders continue."
)
NOTICE_STATUSES = frozenset({"", "pending", "attempted", "delivered", "unknown"})
FAILURE_KINDS = (
    "timeout", "rate_limit", "auth", "config", "budget", "unavailable",
    "network", "error",
)
ROLES = frozenset({"primary", "fallback"})
MAX_DIAGNOSTICS = 12
MAX_BACKOFF_SECONDS = 86400



AUX_STOPS = frozenset({
    "assigned_routes_exhausted", "no_assigned_route", "ambiguous_primary",
    "identity_unresolved",
})
AUX_RESULTS = frozenset({
    "success", "auth", "rate_limit", "timeout", "unavailable", "error",
    "quarantined", "skipped",
})


def validate_aux_route_outcome(value: Any) -> dict[str, Any] | None:
    """Validate the additive strict-host ``aux_route_outcome`` shape.

    Returns a bounded copy containing only documented metadata, or ``None`` when
    the value is absent/malformed. Raw messages, URLs, keys and provider text
    are never accepted or persisted. Callers must treat ``None`` as
    "unavailable", never as evidence of a particular route.
    """
    if not isinstance(value, dict):
        return None
    if str(value.get("policy") or "") != "configured_only":
        return None
    task = str(value.get("task") or "").strip()
    # Host schema v1 uses ``stop_reason``; the earlier draft used ``stop``.
    stop = str(value.get("stop_reason") or value.get("stop") or "").strip()
    if not task or len(task) > 80 or stop not in AUX_STOPS:
        return None
    # ``native_fallback_attempted`` is optional in the host schema, but if a
    # producer reports it, only the compliant value is accepted.
    if "native_fallback_attempted" in value and value.get(
            "native_fallback_attempted") is not False:
        return None
    attempts_raw = value.get("attempts")
    if not isinstance(attempts_raw, (list, tuple)) or len(attempts_raw) > 8:
        return None
    attempts: list[dict[str, str]] = []
    for item in attempts_raw:
        if not isinstance(item, dict):
            return None
        role = str(item.get("role") or "")
        result = str(item.get("result") or "")
        provider = str(item.get("provider") or "")
        model = str(item.get("model") or "")
        reason = str(item.get("reason") or "")
        if role not in ROLES or result not in AUX_RESULTS:
            return None
        if (len(provider) > 120 or len(model) > 160 or len(reason) > 80
                or any(ch in provider + model + reason for ch in "\n\r\x00")
                or any(token in (provider + model + reason).lower()
                       for token in ("://", "api_key", "secret=", "token="))):
            return None
        attempts.append({"role": role, "provider": provider[:120],
                         "model": model[:160], "result": result,
                         "reason": reason[:80]})
    primary = value.get("primary") or value.get("route")
    clean_primary: dict[str, str] = {}
    if isinstance(primary, dict):
        provider = str(primary.get("provider") or "")
        model = str(primary.get("model") or "")
        if len(provider) <= 120 and len(model) <= 160:
            clean_primary = {"provider": provider, "model": model}
    return {"policy": "configured_only", "task": task, "stop": stop,
            "native_fallback_attempted": False, "primary": clean_primary,
            "attempts": attempts}


def outcome_failure_kind(result: Any) -> str:
    """Map one documented attempt result to a bounded failure kind."""
    value = str(result or "").strip().lower()
    if value in {"auth", "rate_limit", "timeout", "budget", "network",
                 "unavailable", "error", "config"}:
        return value
    if value in {"quarantined", "skipped"}:
        return "unavailable"
    return "error"


class ModelHealthError(ValueError):
    """Invalid incident input/state; fail closed."""


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": 1,
        "updated_at": "",
        "incident": None,
        "last_success_at": "",
        "last_incident_id": "",
    }


def _now(store: StateStore, now: datetime | None) -> datetime:
    return now if now is not None else store.clock.now()


def _load(store: StateStore) -> dict[str, Any]:
    data = store.load(STORE, empty_state(), strict=True)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CorruptStateError("model_health.json has an unsupported schema")
    incident = data.get("incident")
    if incident is not None:
        if not isinstance(incident, dict):
            raise CorruptStateError("model_health incident is malformed")
        notice = incident.get("notice")
        if not isinstance(notice, dict) or notice.get("status") not in NOTICE_STATUSES:
            raise CorruptStateError("model_health notice state is malformed")
        if incident.get("status") not in {"open", "recovered"}:
            raise CorruptStateError("model_health incident status is malformed")
    return data


def _save(store: StateStore, data: dict[str, Any], now: datetime) -> None:
    data["schema"] = SCHEMA
    data["version"] = 1
    data["updated_at"] = now.isoformat()
    store.save(STORE, data)


def _locked(store: StateStore) -> Any:
    return store.lock(LOCK)


def _require_writer(acquired: bool) -> None:
    if not acquired:
        raise ModelHealthError("model health store is locked by another writer")


def sanitize_failure_kind(kind: Any, detail: Any = "") -> str:
    """Return one allowed diagnostic kind; never return/store raw detail."""
    text = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "rate_limited": "rate_limit", "429": "rate_limit", "quota": "rate_limit",
        "unauthorized": "auth", "forbidden": "auth", "401": "auth", "403": "auth",
        "configuration": "config", "missing_config": "config", "invalid_config": "config",
        "timed_out": "timeout", "deadline": "timeout",
        "billing": "budget", "insufficient_quota": "budget",
        "connection": "network", "dns": "network", "offline": "network",
        "not_available": "unavailable", "not_configured": "unavailable",
    }
    if text in FAILURE_KINDS:
        return text
    if text in aliases:
        return aliases[text]
    # Infer only from the caller's local detail, never persist that detail.
    try:
        haystack = str(detail or "")[:512].lower()
    except Exception:  # noqa: BLE001 - a hostile object cannot break diagnostics
        haystack = ""
    for needle, mapped in (
            ("timeout", "timeout"), ("timed out", "timeout"),
            ("rate limit", "rate_limit"), ("429", "rate_limit"),
            ("auth", "auth"), ("unauthor", "auth"), ("forbidden", "auth"),
            ("config", "config"), ("not configured", "unavailable"),
            ("budget", "budget"), ("quota", "budget"),
            ("network", "network"), ("connection", "network")):
        if needle in haystack:
            return mapped
    return "error"


def _clamp_retry(when: datetime, now: datetime) -> datetime:
    ceiling = now + timedelta(seconds=MAX_BACKOFF_SECONDS)
    if when < now:
        return now
    return min(when, ceiling)


def _parse_retry_after(value: Any, now: datetime) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = max(0, min(int(value), MAX_BACKOFF_SECONDS))
        except (OverflowError, ValueError):
            seconds = MAX_BACKOFF_SECONDS
        return now + timedelta(seconds=seconds)
    text = str(value).strip()[:128]
    try:
        seconds = max(0, min(int(float(text)), MAX_BACKOFF_SECONDS))
        return now + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _clamp_retry(parsed, now)
    except (TypeError, ValueError, OverflowError):
        return None


def _backoff(attempts: int, base_seconds: int, max_seconds: int) -> timedelta:
    base = max(1, min(int(base_seconds), MAX_BACKOFF_SECONDS))
    cap = max(base, min(int(max_seconds), MAX_BACKOFF_SECONDS))
    exponent = min(max(0, int(attempts) - 1), 20)
    return timedelta(seconds=min(cap, base * (2 ** exponent)))


def _new_incident(now: datetime) -> dict[str, Any]:
    raw = now.isoformat() + "|" + secrets.token_hex(8)
    return {
        "id": "outage_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20],
        "status": "open",
        "first_failure_at": now.isoformat(),
        "last_failure_at": now.isoformat(),
        "retry_after": "",
        "attempts": 0,
        "primary": {"attempted_at": "", "failure_at": "", "kind": "", "ok": None},
        "fallback": {"attempted_at": "", "failure_at": "", "kind": "", "ok": None},
        "diagnostics": [],
        "notice": {"status": "", "updated_at": "", "text": "",
                   "token": "", "claimed_at": "", "claimed_by": ""},
        "recovery": {"at": "", "success_role": ""},
        "config_signature": "",
    }


def _open_incident(data: dict[str, Any], now: datetime) -> dict[str, Any]:
    incident = data.get("incident")
    if not isinstance(incident, dict) or incident.get("status") != "open":
        incident = _new_incident(now)
        data["incident"] = incident
        data["last_incident_id"] = incident["id"]
    return incident


def _readback(data: dict[str, Any]) -> dict[str, Any]:
    incident = data.get("incident")
    if not isinstance(incident, dict):
        return {"incident": None, "notice": None, "retry_after": "",
                "last_success_at": data.get("last_success_at", "")}
    notice = dict(incident.get("notice") or {})
    # The one-shot claim token is never exposed by read-only status.
    notice.pop("token", None)
    notice.pop("claimed_by", None)
    return {
        "incident": {
            "id": incident.get("id", ""),
            "status": incident.get("status", ""),
            "first_failure_at": incident.get("first_failure_at", ""),
            "last_failure_at": incident.get("last_failure_at", ""),
            "retry_after": incident.get("retry_after", ""),
            "attempts": incident.get("attempts", 0),
            "primary": dict(incident.get("primary") or {}),
            "fallback": dict(incident.get("fallback") or {}),
            "diagnostics": [dict(row) for row in incident.get("diagnostics") or []],
            "recovery": dict(incident.get("recovery") or {}),
        },
        "notice": notice,
        "retry_after": incident.get("retry_after", ""),
        "last_success_at": data.get("last_success_at", ""),
    }


def record_attempt(
    store: StateStore,
    *,
    role: str,
    outcome: str,
    failure_kind: Any = "",
    detail: Any = "",
    retry_after: Any = None,
    now: datetime | None = None,
    base_backoff_seconds: int = 60,
    max_backoff_seconds: int = 3600,
) -> dict[str, Any]:
    """Record one assigned-route outcome. Never invokes a model/provider."""
    if role not in ROLES and not (outcome == "success" and role == "route"):
        raise ModelHealthError("role must be primary, fallback or a truthful route success")
    if outcome not in {"success", "failure"}:
        raise ModelHealthError("outcome must be success or failure")
    stamp = _now(store, now)
    with _locked(store) as acquired:
        _require_writer(acquired)
        data = _load(store)
        if outcome == "success":
            incident = data.get("incident")
            data["last_success_at"] = stamp.isoformat()
            if isinstance(incident, dict) and incident.get("status") != "recovered":
                incident["status"] = "recovered"
                incident["recovery"] = {"at": stamp.isoformat(), "success_role": role}
            _save(store, data, stamp)
            return _readback(data)

        incident = _open_incident(data, stamp)
        kind = sanitize_failure_kind(failure_kind, detail)
        route = incident.setdefault(role, {})
        route["attempted_at"] = stamp.isoformat()
        route["failure_at"] = stamp.isoformat()
        route["kind"] = kind
        route["ok"] = False
        incident["attempts"] = int(incident.get("attempts", 0)) + 1
        incident["last_failure_at"] = stamp.isoformat()

        backoff = _backoff(int(incident["attempts"]), base_backoff_seconds,
                           max_backoff_seconds)
        defer = stamp + backoff
        retry = _parse_retry_after(retry_after, stamp)
        if retry is not None and retry > defer:
            defer = retry
        incident["retry_after"] = defer.isoformat()

        diagnostics = list(incident.get("diagnostics") or [])
        diagnostics.append({"at": stamp.isoformat(), "role": role, "kind": kind})
        incident["diagnostics"] = diagnostics[-MAX_DIAGNOSTICS:]

        primary_failed = bool((incident.get("primary") or {}).get("failure_at"))
        fallback_failed = bool((incident.get("fallback") or {}).get("failure_at"))
        notice = incident.setdefault(
            "notice", {"status": "", "updated_at": "", "text": "",
                       "token": "", "claimed_at": "", "claimed_by": ""})
        for key in ("token", "claimed_at", "claimed_by"):
            notice.setdefault(key, "")
        if primary_failed and fallback_failed and not notice.get("status"):
            notice["status"] = "pending"
            notice["updated_at"] = stamp.isoformat()
            notice["text"] = NOTICE_TEXT

        _save(store, data, stamp)
        return _readback(data)


def recovery(
    store: StateStore,
    *,
    role: str = "route",
    now: datetime | None = None,
) -> dict[str, Any]:
    """A genuine successful call re-arms a later independent incident.

    The strict host deliberately emits no success metadata (including for a
    fallback success), so callers that only observe "the configured route
    returned" use the truthful role ``route`` rather than inventing a
    primary/fallback attribution.
    """
    return record_attempt(store, role=role, outcome="success", now=now)


def consume_terminal_outcome(
    store: StateStore,
    facts: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Feed one validated terminal-failure observation to the shared incident.

    This is the single consumer used by every model lane at the
    ``native_runtime.LlmRoute`` boundary. It records actual attempt results and
    arms the one fixed notice only for truthful assigned-route exhaustion. It
    never emits, never reads provider error strings and never invents a role.
    """
    clean = validate_aux_route_outcome(facts)
    if clean is None:
        return None
    stamp = _now(store, now)
    attempts = list(clean.get("attempts") or [])
    successes = {row["role"] for row in attempts if row["result"] == "success"}
    for role in ("primary", "fallback"):
        if role in successes:
            record_attempt(store, role=role, outcome="success", now=stamp)
    if successes:
        return status(store)
    if clean.get("stop") != "assigned_routes_exhausted":
        return status(store)
    by_role: dict[str, list[dict[str, Any]]] = {"primary": [], "fallback": []}
    for row in attempts:
        by_role.setdefault(row["role"], []).append(row)
    if not by_role.get("primary") or not by_role.get("fallback"):
        return status(store)
    for role in ("primary", "fallback"):
        last = by_role[role][-1]
        record_attempt(
            store, role=role, outcome="failure",
            failure_kind=outcome_failure_kind(last.get("result")), now=stamp)
    return status(store)


def _attention(attention_fn: Callable[[], Any] | None) -> tuple[bool, str]:
    if not callable(attention_fn):
        return False, "attention gate required"
    try:
        verdict = attention_fn()
    except Exception as exc:  # noqa: BLE001 - failed gate fails closed/pending
        return False, f"attention gate unavailable: {type(exc).__name__}"
    allowed = verdict
    reason = "attention blocked"
    if isinstance(verdict, tuple):
        allowed = bool(verdict[0]) if verdict else False
        reason = str(verdict[1])[:160] if len(verdict) > 1 else reason
    return bool(allowed), reason


def claim_notice(
    store: StateStore,
    *,
    now: datetime | None = None,
    attention_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Atomically reserve the single fixed notice for one outage incident.

    Attention policy is evaluated first. Under the state lock exactly one caller
    can move ``pending -> attempted`` and receive a one-shot token bound to the
    incident id. Only that winner may emit ``NOTICE_TEXT``. Losers/blocked
    callers must not send. The token is never included in ``status()``.
    """
    stamp = _now(store, now)
    allowed, reason = _attention(attention_fn)
    if not allowed:
        return {"eligible": False, "reason": reason, "text": ""}
    with _locked(store) as acquired:
        _require_writer(acquired)
        data = _load(store)
        incident = data.get("incident")
        if not isinstance(incident, dict) or incident.get("status") != "open":
            return {"eligible": False, "reason": "no open outage incident", "text": ""}
        notice = incident.setdefault(
            "notice", {"status": "", "updated_at": "", "text": "",
                       "token": "", "claimed_at": "", "claimed_by": ""})
        status = notice.get("status", "")
        if status != "pending":
            return {"eligible": False, "reason": f"notice {status or 'not armed'}",
                    "incident_id": incident.get("id", ""), "text": ""}
        token = secrets.token_hex(16)
        notice["status"] = "attempted"
        notice["updated_at"] = stamp.isoformat()
        notice["text"] = NOTICE_TEXT
        notice["token"] = token
        notice["claimed_at"] = stamp.isoformat()
        notice["claimed_by"] = "claim_notice"
        _save(store, data, stamp)
        return {
            "eligible": True,
            "reason": "ok",
            "incident_id": incident.get("id", ""),
            "notice_token": token,
            "text": NOTICE_TEXT,
        }


# Backwards-compatible name with the corrected atomic semantics. It is a claim,
# not a non-mutating preview; callers must either emit once or mark unknown.
notice_decision = claim_notice


def _mark_notice(store: StateStore, status: str, *,
                 incident_id: Any, notice_token: Any,
                 now: datetime | None = None) -> dict[str, Any]:
    if status not in {"delivered", "unknown"}:
        raise ModelHealthError("only delivered/unknown can follow an attempted claim")
    stamp = _now(store, now)
    with _locked(store) as acquired:
        _require_writer(acquired)
        data = _load(store)
        incident = data.get("incident")
        if not isinstance(incident, dict) or incident.get("status") != "open":
            raise ModelHealthError("no open outage incident for this receipt")
        if not incident_id or str(incident.get("id", "")) != str(incident_id):
            raise ModelHealthError("notice receipt is for a stale incident")
        notice = incident.get("notice") or {}
        if notice.get("status") != "attempted":
            raise ModelHealthError("notice has no valid attempted claim")
        if not notice_token or not secrets.compare_digest(
                str(notice.get("token", "")), str(notice_token)):
            raise ModelHealthError("notice claim token is stale")
        notice["status"] = status
        notice["updated_at"] = stamp.isoformat()
        notice["token"] = ""
        _save(store, data, stamp)
        return _readback(data)


def mark_notice_delivered(store: StateStore, *, incident_id: Any, notice_token: Any,
                          now: datetime | None = None) -> dict[str, Any]:
    return _mark_notice(store, "delivered", incident_id=incident_id,
                        notice_token=notice_token, now=now)


def mark_notice_unknown(store: StateStore, *, incident_id: Any, notice_token: Any,
                        now: datetime | None = None) -> dict[str, Any]:
    """Ambiguous emission: terminal for this incident; never blindly retried."""
    return _mark_notice(store, "unknown", incident_id=incident_id,
                        notice_token=notice_token, now=now)


def probe_allowed(
    store: StateStore,
    *,
    now: datetime | None = None,
    config_signature: str = "",
) -> dict[str, Any]:
    """Backoff-aware probe permission; a config change allows a probe only.

    It never creates a new incident, resets an existing notice or duplicates the
    fixed notice.
    """
    stamp = _now(store, now)
    with _locked(store) as acquired:
        _require_writer(acquired)
        data = _load(store)
        incident = data.get("incident")
        if not isinstance(incident, dict) or incident.get("status") != "open":
            return {"allowed": True, "reason": "no open incident"}
        signature = str(config_signature or "")
        previous = str(incident.get("config_signature") or "")
        changed = bool(signature and signature != previous)
        if changed:
            incident["config_signature"] = signature
            _save(store, data, stamp)
            return {"allowed": True, "reason": "configuration changed; probe only"}
        retry = incident.get("retry_after")
        if retry:
            try:
                when = datetime.fromisoformat(str(retry))
            except ValueError:
                when = None
            if when is not None and when.tzinfo is not None and stamp < when:
                return {"allowed": False, "reason": "backoff", "retry_after": retry}
        return {"allowed": True, "reason": "backoff elapsed"}


def status(store: StateStore) -> dict[str, Any]:
    return _readback(_load(store))


__all__ = [
    "SCHEMA", "STORE", "NOTICE_TEXT", "NOTICE_STATUSES", "FAILURE_KINDS",
    "ModelHealthError", "empty_state", "sanitize_failure_kind", "record_attempt",
    "recovery", "consume_terminal_outcome", "claim_notice", "notice_decision",
    "mark_notice_delivered",
    "mark_notice_unknown", "probe_allowed", "status",
    "validate_aux_route_outcome", "outcome_failure_kind", "AUX_STOPS", "AUX_RESULTS",
]
