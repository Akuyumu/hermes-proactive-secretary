"""Bounded, owner-only follow-up lifecycle store.

A follow-up is an explicit request from the owner to keep an eye on something
during ordinary conversation. This module owns only the durable store and its
lifecycle; it does not schedule checks, send notices, call models or touch the
network. An empty store changes nothing about the portal.

Storage mirrors :mod:`proactive_pilot.watchstore`:
- one owner-only JSON artifact (dir 0700, file 0600) written atomically through
  :class:`~proactive_pilot.state.StateStore`;
- the shared non-blocking ``file_lock`` via ``StateStore.lock``;
- an explicit ``schema`` field and a monotonically increasing ``version``;
- ``CorruptStateError`` for unreadable or unsupported on-disk state.

Delivery uncertainty is deliberately not completion. Only a positive
``delivered`` receipt may close a one-shot; ``emitted``, ``attempted`` and
``unknown`` keep it awaiting and can never trigger a resend.

Active checks and unresolved one-shot deliveries have independent caps. Only
``active`` entries consume the check budget; only ``awaiting_delivery`` entries
consume the pending budget. At the pending cap a new dispatch is deferred before
any reservation or transport write, leaving the watch active for a later wake.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .clock import Clock
from .state import CorruptStateError, StateStore

SCHEMA = 1
STORE = "followups.json"
LOCK = "followups.lock"

MAX_TEXT_CHARS = 300
MAX_SOURCE_CHARS = 24
MAX_CHECK_HINT_CHARS = 200
MAX_EVIDENCE_QUOTE_CHARS = 600
MAX_SOURCE_REF_CHARS = 200
MAX_FINGERPRINT_CHARS = 120
MAX_EVENT_KEY_CHARS = 200
MAX_ATTEMPT_TOKEN_CHARS = 64
MAX_RECEIPT_REF_CHARS = 96
MAX_ACTIVE_FOLLOWUPS = 20
MAX_AWAITING_FOLLOWUPS = 20
MAX_PENDING_DELIVERIES = MAX_AWAITING_FOLLOWUPS
MAX_UNRESOLVED_DELIVERIES = MAX_AWAITING_FOLLOWUPS
MAX_PENDING_FOLLOWUPS = MAX_AWAITING_FOLLOWUPS
MAX_PENDING = MAX_AWAITING_FOLLOWUPS
MAX_EXPIRES_AT_CHARS = 64
DEFAULT_PORTAL_EXPIRY_DAYS = 14
MAX_EXPIRY_DAYS = 60
MAX_CREATES_PER_EXECUTION = 3
MAX_CLOSED = 20
MAX_DEDUPE = 60
MAX_SEEN = 50
MAX_RESERVATIONS = MAX_SEEN
DEFAULT_WAKE_ENTRIES = 15
MAX_WAKE_ENTRIES = 15

KINDS = frozenset({"ongoing", "one_shot"})
STATUSES = frozenset({"active", "awaiting_delivery", "done", "canceled", "closed"})
RESULTS = frozenset({"", "no_match", "match", "error", "blocked"})
CHECK_RESULTS = frozenset({"no_match", "match", "error", "blocked"})
NOTICE_STATUSES = frozenset({
    "", "emitted", "attempted", "unknown", "delivered", "owner_confirmed",
})
CLOSE_REASONS = frozenset({
    "", "notified", "owner_stopped", "owner_canceled", "owner_confirmed", "expired",
})
FILED_BY = frozenset({"owner", "agent"})
BLANKET_TEXTS = frozenset({"", "*", "all", "everything", "anything"})
NEEDS_CLARIFICATION_TEXT = (
    "The owner's check hint is missing; ask one short question about what to check."
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FollowupError(ValueError):
    """Invalid follow-up input or an impossible lifecycle transition."""


class FollowupCapacityError(FollowupError):
    """A bounded capacity limit was reached before any state change.

    ``capacity`` is ``"active"`` for a new check, ``"pending"`` for an
    unresolved one-shot delivery, or ``"execution"`` for the per-execution
    portal creation cap. Callers may defer the work explicitly; the exception
    never means a notice was sent or a watch was closed.
    """

    def __init__(self, message: str, *, capacity: str) -> None:
        super().__init__(message)
        self.capacity = capacity


def empty_store() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": 0,
        "updated_at": "",
        "entries": {},
        "dedupe": [],
    }


def _coerce_store(value: Any) -> StateStore:
    if all(hasattr(value, name) for name in ("load", "save", "lock", "root", "clock")):
        return value
    return StateStore(Path(value), Clock())


def _now(store: Any, now: datetime | None) -> datetime:
    return now if now is not None else store.clock.now()


@contextmanager
def _locked(store: Any) -> Iterator[None]:
    with store.lock(LOCK) as acquired:
        if not acquired:
            raise FollowupError("followup store is locked by another writer")
        yield


def _bounded(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FollowupError(f"{name} must be a string")
    text = " ".join(value.split())
    if required and not text:
        raise FollowupError(f"{name} is required")
    if len(text) > limit:
        raise FollowupError(f"{name} is too long (max {limit} chars)")
    if _CONTROL_RE.search(text):
        raise FollowupError(f"{name} contains control characters")
    return text


def _fingerprint(value: Any) -> str:
    return _bounded(value, "fingerprint", MAX_FINGERPRINT_CHARS)


def _clean_attempt_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_ATTEMPT_TOKEN_CHARS or _CONTROL_RE.search(text):
        return ""
    return text


def _mint_attempt_token() -> str:
    return secrets.token_hex(16)


def _corrupt(reason: str) -> CorruptStateError:
    return CorruptStateError(f"followups.json is invalid: {reason}")


def _stored_iso(entry: dict[str, Any], key: str) -> None:
    value = entry.get(key, "")
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise _corrupt(f"{key} has an invalid type")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise _corrupt(f"{key} is not an ISO timestamp") from None


def _validate_entry(followup_id: Any, entry: Any) -> None:
    if not isinstance(followup_id, str) or not followup_id:
        raise _corrupt("entry id is invalid")
    if not isinstance(entry, dict) or entry.get("id") != followup_id:
        raise _corrupt("entry identity is missing or mismatched")
    if entry.get("kind") not in KINDS:
        raise _corrupt("entry kind is invalid")
    if entry.get("status") not in STATUSES:
        raise _corrupt("entry status is invalid")
    text = entry.get("text")
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS \
            or _CONTROL_RE.search(text):
        raise _corrupt("entry text is not bounded/safe")
    if entry.get("last_result") not in RESULTS:
        raise _corrupt("entry last_result is invalid")
    if entry.get("notice_status") not in NOTICE_STATUSES:
        raise _corrupt("entry notice_status is invalid")
    if entry.get("close_reason") not in CLOSE_REASONS:
        raise _corrupt("entry close_reason is invalid")
    for key, limit in (("source", MAX_SOURCE_CHARS), ("check_hint", MAX_CHECK_HINT_CHARS),
                       ("evidence_quote", MAX_EVIDENCE_QUOTE_CHARS),
                       ("source_ref", MAX_SOURCE_REF_CHARS),
                       ("notice_event_key", MAX_EVENT_KEY_CHARS),
                       ("owner_confirmed_ref", MAX_RECEIPT_REF_CHARS),
                       ("expires_at", MAX_EXPIRES_AT_CHARS),
                       ("created_execution_id", MAX_EVENT_KEY_CHARS)):
        value = entry.get(key, "")
        if not isinstance(value, str) or len(value) > limit or _CONTROL_RE.search(value):
            raise _corrupt(f"entry {key} is not bounded/safe")
    filed_by = entry.get("filed_by", "")
    if not isinstance(filed_by, str) or (filed_by and filed_by not in FILED_BY):
        raise _corrupt("entry filed_by is invalid")
    for key in ("check_count", "notified_count"):
        value = entry.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _corrupt(f"entry {key} is invalid")
    seen = entry.get("seen", [])
    if not isinstance(seen, list) or len(seen) > MAX_SEEN:
        raise _corrupt("entry seen list is malformed or oversized")
    for item in seen:
        if not isinstance(item, str) or len(item) > MAX_FINGERPRINT_CHARS:
            raise _corrupt("entry seen list contains an invalid fingerprint")
    reservations = entry.get("notice_reservations", [])
    if not isinstance(reservations, list) or len(reservations) > MAX_RESERVATIONS:
        raise _corrupt("entry notice reservations are malformed or oversized")
    for item in reservations:
        if not isinstance(item, dict):
            raise _corrupt("entry notice reservation is malformed")
        event_key = item.get("event_key")
        fingerprint = item.get("fingerprint")
        if (not isinstance(event_key, str) or not event_key
                or len(event_key) > MAX_EVENT_KEY_CHARS
                or _CONTROL_RE.search(event_key)):
            raise _corrupt("entry notice reservation event key is invalid")
        if (not isinstance(fingerprint, str) or not fingerprint
                or len(fingerprint) > MAX_FINGERPRINT_CHARS
                or _CONTROL_RE.search(fingerprint)):
            raise _corrupt("entry notice reservation fingerprint is invalid")
        token = item.get("attempt_token", "")
        if token not in (None, ""):
            if (not isinstance(token, str)
                    or len(token) > MAX_ATTEMPT_TOKEN_CHARS
                    or _CONTROL_RE.search(token)):
                raise _corrupt("entry notice reservation attempt token is invalid")
    for key in ("created_at", "updated_at", "last_checked_at", "closed_at",
                "owner_confirmed_at", "expires_at"):
        _stored_iso(entry, key)


def _load(store: Any) -> dict[str, Any]:
    data = store.load(STORE, empty_store(), strict=True)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CorruptStateError("followups.json has an unsupported schema")
    version = data.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise _corrupt("version is invalid")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise _corrupt("entries are malformed")
    dedupe = data.get("dedupe", [])
    if not isinstance(dedupe, list) or len(dedupe) > MAX_DEDUPE:
        raise _corrupt("dedupe list is malformed or oversized")
    for item in dedupe:
        if not isinstance(item, str) or len(item) > MAX_EVENT_KEY_CHARS:
            raise _corrupt("dedupe list contains an invalid key")
    for followup_id, entry in entries.items():
        _validate_entry(followup_id, entry)
    data.setdefault("version", 0)
    data.setdefault("updated_at", "")
    data["dedupe"] = dedupe
    return data


def _save(store: Any, data: dict[str, Any], now: datetime) -> None:
    data["schema"] = SCHEMA
    data["version"] = int(data.get("version", 0)) + 1
    data["updated_at"] = now.isoformat()
    store.save(STORE, data)


def _readback(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, list) else value)
            for key, value in entry.items()}


def _find(data: dict[str, Any], followup_id: Any) -> dict[str, Any]:
    fid = str(followup_id or "")
    entry = data["entries"].get(fid)
    if not isinstance(entry, dict):
        raise FollowupError(f"no follow-up with id {followup_id!r}")
    return entry


def _new_id(data: dict[str, Any]) -> str:
    while True:
        fid = "fu_" + secrets.token_hex(8)
        if fid not in data["entries"]:
            return fid


def notice_event_key(followup_id: Any, fingerprint: Any = "") -> str:
    """Return the stable semantic event key for one follow-up notice.

    A one-shot follow-up has exactly one semantic event, so its key depends only
    on the durable follow-up id. That makes a retry after a restart detect the
    already-recorded event instead of creating a second notice. Ongoing watches
    include the bounded match fingerprint so a genuinely different match can be
    notified once without repeating an earlier one.
    """
    fid = _bounded(followup_id, "followup_id", 96, required=True)
    fp = _bounded(fingerprint, "fingerprint", MAX_FINGERPRINT_CHARS)
    if not fp:
        return f"followup-notice:{fid}"[:MAX_EVENT_KEY_CHARS]
    digest = hashlib.sha256(fp.encode("utf-8")).hexdigest()[:16]
    return f"followup-notice:{fid}:{digest}"[:MAX_EVENT_KEY_CHARS]


def _add_seen(entry: dict[str, Any], fingerprint: str) -> None:
    if not fingerprint:
        return
    seen = entry.setdefault("seen", [])
    if fingerprint in seen:
        return
    seen.append(fingerprint)
    if len(seen) > MAX_SEEN:
        del seen[: len(seen) - MAX_SEEN]


def _add_reservation(entry: dict[str, Any], event_key: str, fingerprint: str,
                     attempt_token: str) -> None:
    """Record the exact event/fingerprint attempt still awaiting rollback."""
    reservations = entry.setdefault("notice_reservations", [])
    attempt = {
        "event_key": event_key,
        "fingerprint": fingerprint,
        "attempt_token": attempt_token,
    }
    if attempt in reservations:
        return
    reservations.append(attempt)
    if len(reservations) > MAX_RESERVATIONS:
        del reservations[: len(reservations) - MAX_RESERVATIONS]


def _drop_reservation(entry: dict[str, Any], event_key: str,
                      fingerprint: str, attempt_token: str) -> bool:
    """Remove exactly one token-bound reservation; never a neighbouring attempt."""
    if not attempt_token:
        return False
    reservations = entry.get("notice_reservations")
    if not isinstance(reservations, list):
        return False
    for position, item in enumerate(reservations):
        if (isinstance(item, dict)
                and item.get("event_key") == event_key
                and item.get("fingerprint") == fingerprint
                and item.get("attempt_token") == attempt_token):
            del reservations[position]
            return True
    return False


def _reservation_holds_event_key(data: dict[str, Any], event_key: str) -> bool:
    for entry in data["entries"].values():
        for item in entry.get("notice_reservations") or []:
            if isinstance(item, dict) and item.get("event_key") == event_key:
                return True
    return False


def _reservation_holds_fingerprint(entry: dict[str, Any], fingerprint: str) -> bool:
    for item in entry.get("notice_reservations") or []:
        if isinstance(item, dict) and item.get("fingerprint") == fingerprint:
            return True
    return False


def _add_dedupe(data: dict[str, Any], value: Any) -> None:
    text = _bounded(value, "notice_event_key", MAX_EVENT_KEY_CHARS)
    if not text:
        return
    dedupe = data.setdefault("dedupe", [])
    if text not in dedupe:
        dedupe.append(text)
    if len(dedupe) > MAX_DEDUPE:
        del dedupe[: len(dedupe) - MAX_DEDUPE]


def _prune_closed(data: dict[str, Any]) -> None:
    closed = [
        (position, fid, entry)
        for position, (fid, entry) in enumerate(data["entries"].items())
        if entry.get("status") in {"done", "canceled"}
    ]
    if len(closed) <= MAX_CLOSED:
        return
    closed.sort(key=lambda item: (
        str(item[2].get("closed_at") or item[2].get("updated_at")
            or item[2].get("created_at") or ""),
        item[0],
    ))
    for _, fid, entry in closed[: len(closed) - MAX_CLOSED]:
        data["entries"].pop(fid, None)
        _add_dedupe(data, entry.get("notice_event_key", ""))


def _active_count(data: dict[str, Any]) -> int:
    return sum(1 for entry in data["entries"].values()
               if entry.get("status") == "active")


def _awaiting_count(data: dict[str, Any]) -> int:
    return sum(1 for entry in data["entries"].values()
               if entry.get("status") == "awaiting_delivery")


def _expiry_text(value: Any, stamp: datetime, *,
                 default_days: int | None = None,
                 max_days: int = MAX_EXPIRY_DAYS) -> str:
    """Validate an optional timezone-aware expiry, or mint the portal default."""
    if value is None:
        text = ""
    elif not isinstance(value, str):
        raise FollowupError("expires_at must be a string")
    else:
        text = value.strip()
    if not text:
        if default_days is None:
            return ""
        return (stamp + timedelta(days=default_days)).isoformat()
    if len(text) > MAX_EXPIRES_AT_CHARS or _CONTROL_RE.search(text):
        raise FollowupError("expires_at is too long or unsafe")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise FollowupError("expires_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FollowupError("expires_at must include a UTC offset")
    if parsed > stamp + timedelta(days=max_days):
        raise FollowupError(
            f"expires_at is too far in the future (max {max_days} days)")
    return text


def _expire_active_locked(data: dict[str, Any], stamp: datetime) -> list[str]:
    """Close active watches whose expiry has passed; never delete a record."""
    closed: list[str] = []
    for entry in data["entries"].values():
        if entry.get("status") != "active":
            continue
        expires = entry.get("expires_at") or ""
        if not isinstance(expires, str) or not expires:
            continue
        try:
            parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        if parsed < stamp:
            entry["status"] = "closed"
            entry["close_reason"] = "expired"
            entry["closed_at"] = stamp.isoformat()
            entry["updated_at"] = stamp.isoformat()
            closed.append(str(entry.get("id") or ""))
    return closed


def create(
    store: Any,
    text: Any = None,
    *,
    kind: str = "ongoing",
    source: Any = "",
    check_hint: Any = "",
    evidence_quote: Any = "",
    source_ref: Any = "",
    filed_by: Any = "owner",
    expires_at: Any = "",
    default_expiry_days: int | None = None,
    execution_id: Any = "",
    max_per_execution: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    kind_text = str(kind or "").strip().lower()
    if kind_text not in KINDS:
        raise FollowupError("kind must be ongoing or one_shot")
    clean_text = _bounded(text, "text", MAX_TEXT_CHARS, required=True)
    if clean_text.lower() in BLANKET_TEXTS:
        raise FollowupError("refusing a blanket follow-up text")
    clean_source = _bounded(source, "source", MAX_SOURCE_CHARS)
    clean_hint = _bounded(
        check_hint, "check_hint", MAX_CHECK_HINT_CHARS, required=True)
    clean_quote = _bounded(evidence_quote, "evidence_quote", MAX_EVIDENCE_QUOTE_CHARS)
    clean_ref = _bounded(source_ref, "source_ref", MAX_SOURCE_REF_CHARS)
    clean_filed_by = str(filed_by or "").strip().lower()
    if clean_filed_by not in FILED_BY:
        raise FollowupError("filed_by must be owner or agent")
    clean_execution = _bounded(
        execution_id, "execution_id", MAX_EVENT_KEY_CHARS)
    if (default_expiry_days is not None
            and (not isinstance(default_expiry_days, int)
                 or isinstance(default_expiry_days, bool)
                 or default_expiry_days < 0)):
        raise FollowupError("default_expiry_days must be a non-negative integer")

    target = _coerce_store(store)
    stamp = _now(target, now)
    clean_expiry = _expiry_text(
        expires_at, stamp, default_days=default_expiry_days)
    with _locked(target):
        data = _load(target)
        if _active_count(data) >= MAX_ACTIVE_FOLLOWUPS:
            raise FollowupCapacityError(
                f"active follow-up limit reached ({MAX_ACTIVE_FOLLOWUPS})",
                capacity="active",
            )
        if clean_execution and max_per_execution is not None:
            used = sum(
                1 for entry in data["entries"].values()
                if entry.get("created_execution_id") == clean_execution
            )
            if used >= max_per_execution:
                raise FollowupCapacityError(
                    (f"per-execution follow-up limit reached "
                     f"({max_per_execution})"),
                    capacity="execution",
                )
        fid = _new_id(data)
        entry = {
            "id": fid,
            "text": clean_text,
            "kind": kind_text,
            "status": "active",
            "created_at": stamp.isoformat(),
            "updated_at": stamp.isoformat(),
            "source": clean_source,
            "check_hint": clean_hint,
            "filed_by": clean_filed_by,
            "evidence_quote": clean_quote,
            "source_ref": clean_ref,
            "expires_at": clean_expiry,
            "created_execution_id": clean_execution,
            "last_checked_at": "",
            "last_result": "",
            "check_count": 0,
            "notified_count": 0,
            "seen": [],
            "notice_reservations": [],
            "notice_event_key": "",
            "notice_status": "",
            "owner_confirmed_ref": "",
            "owner_confirmed_at": "",
            "closed_at": "",
            "close_reason": "",
        }
        data["entries"][fid] = entry
        _save(target, data, stamp)
        return _readback(entry)


def record_check(
    store: Any,
    followup_id: Any,
    result: Any,
    fingerprint: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    result_text = str(result or "").strip().lower()
    if result_text not in CHECK_RESULTS:
        raise FollowupError("check result must be no_match|match|error|blocked")
    _fingerprint(fingerprint)

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)
        if entry["status"] != "active":
            raise FollowupError(f"cannot record a check while {entry['status']}")
        entry["last_checked_at"] = stamp.isoformat()
        entry["last_result"] = result_text
        entry["check_count"] = int(entry.get("check_count", 0)) + 1
        entry["updated_at"] = stamp.isoformat()
        _save(target, data, stamp)
        return _readback(entry)


def queue_notice(
    store: Any,
    followup_id: Any,
    event_key: Any,
    fingerprint: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    event_text = _bounded(event_key, "event_key", MAX_EVENT_KEY_CHARS, required=True)
    print_fp = _fingerprint(fingerprint) or event_text

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)
        kind = entry["kind"]
        if kind == "one_shot":
            if entry["status"] == "awaiting_delivery":
                raise FollowupError("notice is already queued; refusing a second send path")
            if entry["status"] != "active":
                raise FollowupError(f"cannot queue a notice while {entry['status']}")
        elif entry["status"] != "active":
            raise FollowupError(f"cannot queue a notice while {entry['status']}")
        if (kind == "one_shot"
                and _awaiting_count(data) >= MAX_AWAITING_FOLLOWUPS):
            raise FollowupCapacityError(
                (f"pending delivery limit reached ({MAX_AWAITING_FOLLOWUPS}); "
                 "the watch was deferred before dispatch and remains active"),
                capacity="pending",
            )
        if print_fp in (entry.get("seen") or []):
            raise FollowupError("match fingerprint was already notified")
        if event_text in (data.get("dedupe") or []):
            raise FollowupError("notice event was already emitted or retired")

        attempt_token = _mint_attempt_token()
        entry["notice_event_key"] = event_text
        if kind == "one_shot":
            entry["status"] = "awaiting_delivery"
        else:
            entry["notified_count"] = int(entry.get("notified_count", 0)) + 1
        _add_seen(entry, print_fp)
        _add_reservation(entry, event_text, print_fp, attempt_token)
        _add_dedupe(data, event_text)
        entry["updated_at"] = stamp.isoformat()
        _save(target, data, stamp)
        readback = _readback(entry)
        readback["attempt_token"] = attempt_token
        return readback


def release_notice(
    store: Any,
    followup_id: Any,
    event_key: Any,
    fingerprint: Any = None,
    attempt_token: Any = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Undo one not-yet-enqueued attempt, but only the exact token-bound one.

    This is used after the transport explicitly reports that it wrote nothing.
    Each reservation carries an opaque attempt token minted under the store lock
    by :func:`queue_notice`; the same stable event key can therefore be reserved
    again after a rejected attempt without a delayed rollback crossing into the
    new reservation.  A missing or non-matching token is a fail-closed no-op.
    It never restores a closed/stopped watch, never undoes an accepted/unknown
    notice state, and never removes another attempt's seen fingerprint, dedupe
    key or notify counter.
    """
    event_text = _bounded(event_key, "event_key", MAX_EVENT_KEY_CHARS, required=True)
    print_fp = _fingerprint(fingerprint) or event_text
    token_text = _clean_attempt_token(attempt_token)

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)

        if not token_text:
            return _readback(entry)

        if entry["kind"] == "one_shot":
            if entry.get("notice_event_key") != event_text:
                return _readback(entry)
            if entry["status"] != "awaiting_delivery":
                return _readback(entry)
            if entry.get("notice_status"):
                return _readback(entry)
            if not _drop_reservation(entry, event_text, print_fp, token_text):
                return _readback(entry)
            entry["status"] = "active"
            entry["notice_status"] = ""
            entry["notice_event_key"] = ""
        else:
            if entry["status"] != "active":
                return _readback(entry)
            if not _drop_reservation(entry, event_text, print_fp, token_text):
                return _readback(entry)
            entry["notified_count"] = max(
                0, int(entry.get("notified_count", 0)) - 1)
            remaining = entry.get("notice_reservations") or []
            if remaining and isinstance(remaining[-1], dict):
                entry["notice_event_key"] = str(
                    remaining[-1].get("event_key") or "")
            else:
                entry["notice_event_key"] = ""

        seen = entry.get("seen")
        if (isinstance(seen, list) and print_fp in seen
                and not _reservation_holds_fingerprint(entry, print_fp)):
            seen.remove(print_fp)
        if not _reservation_holds_event_key(data, event_text):
            data["dedupe"] = [
                key for key in data.get("dedupe", []) if key != event_text
            ]
        entry["updated_at"] = stamp.isoformat()
        _save(target, data, stamp)
        return _readback(entry)


def resolve_notice(
    store: Any,
    followup_id: Any,
    status: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    status_text = str(status or "").strip().lower()
    if status_text not in {"delivered", "emitted", "attempted", "unknown"}:
        raise FollowupError("notice status must be delivered|emitted|attempted|unknown")

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)
        if not entry.get("notice_event_key"):
            raise FollowupError("no queued notice to resolve")
        if entry["kind"] == "one_shot":
            if entry["status"] != "awaiting_delivery":
                raise FollowupError(f"cannot resolve a notice while {entry['status']}")
            entry["notice_status"] = status_text
            if status_text == "delivered":
                entry["status"] = "done"
                entry["close_reason"] = "notified"
                entry["closed_at"] = stamp.isoformat()
                entry["notified_count"] = max(int(entry.get("notified_count", 0)), 1)
                _prune_closed(data)
        else:
            if entry["status"] not in {"active", "awaiting_delivery"}:
                raise FollowupError(f"cannot resolve a notice while {entry['status']}")
            entry["notice_status"] = status_text
        entry["updated_at"] = stamp.isoformat()
        _save(target, data, stamp)
        return _readback(entry)


def confirm_owner_receipt(
    store: Any,
    followup_id: Any,
    event_key: Any,
    receipt_ref: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record an explicit owner receipt for one emitted-unconfirmed one-shot.

    The authority wrapper must have already bound ``receipt_ref`` to the real
    emitted-unconfirmed portal evidence for ``event_key`` and to the owner's
    current authenticated turn.  This function only persists that distinct
    outcome: it never claims transport delivery, never touches the portal queue
    or gates, and never resends.  Replaying the same reference is an idempotent
    read-back; a different reference cannot replace the recorded one.
    """
    event_text = _bounded(event_key, "event_key", MAX_EVENT_KEY_CHARS, required=True)
    ref_text = _bounded(
        receipt_ref, "owner_confirmed_ref", MAX_RECEIPT_REF_CHARS, required=True)

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)
        if entry["kind"] != "one_shot":
            raise FollowupError(
                "an owner notice receipt applies only to a one-shot follow-up")
        if entry.get("notice_event_key") != event_text:
            raise FollowupError("receipt does not match this follow-up's notice")
        if (entry["status"] == "done"
                and entry.get("close_reason") == "owner_confirmed"):
            if entry.get("owner_confirmed_ref") != ref_text:
                raise FollowupError(
                    "a different owner receipt reference is already recorded")
            return _readback(entry)
        if entry["status"] != "awaiting_delivery":
            raise FollowupError(
                f"cannot confirm an owner receipt while {entry['status']}")

        entry["notice_status"] = "owner_confirmed"
        entry["status"] = "done"
        entry["close_reason"] = "owner_confirmed"
        entry["owner_confirmed_ref"] = ref_text
        entry["owner_confirmed_at"] = stamp.isoformat()
        entry["closed_at"] = stamp.isoformat()
        entry["updated_at"] = stamp.isoformat()
        _prune_closed(data)
        _save(target, data, stamp)
        return _readback(entry)


def close(
    store: Any,
    followup_id: Any,
    reason: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    reason_text = str(reason or "").strip().lower()
    reason_text = {"stop": "owner_stopped", "cancel": "owner_canceled"}.get(
        reason_text, reason_text)
    if reason_text not in {"owner_stopped", "owner_canceled", "owner_confirmed"}:
        raise FollowupError(
            "close reason must be owner_stopped, owner_canceled or owner_confirmed")

    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        entry = _find(data, followup_id)
        if entry["status"] not in {"active", "awaiting_delivery"}:
            raise FollowupError(f"cannot close a follow-up while {entry['status']}")
        if reason_text in {"owner_stopped", "owner_confirmed"}:
            entry["status"] = "done"
        else:
            entry["status"] = "canceled"
        entry["close_reason"] = reason_text
        if reason_text == "owner_confirmed":
            entry["owner_confirmed_at"] = stamp.isoformat()
        entry["closed_at"] = stamp.isoformat()
        entry["updated_at"] = stamp.isoformat()
        _add_dedupe(data, entry.get("notice_event_key", ""))
        _prune_closed(data)
        _save(target, data, stamp)
        return _readback(entry)


def expire(
    store: Any,
    *,
    now: datetime | None = None,
    max_age_days: int = 30,
) -> list[str]:
    """Compatibility no-op: unresolved queued and active work is never expired."""
    _coerce_store(store)
    return []


def get(store: Any, followup_id: Any) -> dict[str, Any]:
    data = _load(_coerce_store(store))
    return _readback(_find(data, followup_id))


def _check_hint(entry: dict[str, Any]) -> str:
    hint = entry.get("check_hint", "")
    return hint if isinstance(hint, str) else ""


def _needs_clarification(entry: dict[str, Any]) -> bool:
    return (entry.get("status") in {"active", "awaiting_delivery"}
            and not _check_hint(entry).strip())


def _clarification_fields(entry: dict[str, Any]) -> dict[str, Any]:
    needs = _needs_clarification(entry)
    fields: dict[str, Any] = {"needs_clarification": needs}
    if needs:
        fields["clarification"] = NEEDS_CLARIFICATION_TEXT
    return fields


def _safe_entry(entry: dict[str, Any]) -> dict[str, Any]:
    row = {
        "id": entry["id"],
        "kind": entry["kind"],
        "status": entry["status"],
        "text": entry["text"],
        "check_hint": _check_hint(entry),
        "last_result": entry.get("last_result", ""),
        "last_checked_at": entry.get("last_checked_at", ""),
        "check_count": int(entry.get("check_count", 0)),
        "notified_count": int(entry.get("notified_count", 0)),
        "notice_status": entry.get("notice_status", ""),
        "created_at": entry.get("created_at", ""),
    }
    for key in ("filed_by", "expires_at", "close_reason",
                "evidence_quote", "source_ref"):
        value = entry.get(key, "")
        if value:
            row[key] = value
    row.update(_clarification_fields(entry))
    return row


def _sorted_rows(data: dict[str, Any], statuses: set[str],
                 max_entries: int | None) -> list[dict[str, Any]]:
    rows = [_safe_entry(entry) for entry in data["entries"].values()
            if entry.get("status") in statuses]
    rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    if max_entries is not None:
        rows = rows[: max(0, int(max_entries))]
    return rows


def _due_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Never-checked rows first, then least-recently checked, deterministically."""
    last = str(row.get("last_checked_at") or "")
    return (0 if not last else 1, last,
            str(row.get("created_at") or ""), str(row.get("id") or ""))


def active_entries(store: Any, *, max_entries: int | None = None) -> list[dict[str, Any]]:
    return _sorted_rows(_load(_coerce_store(store)), {"active"}, max_entries)


def awaiting_entries(store: Any, *, max_entries: int | None = None) -> list[dict[str, Any]]:
    return _sorted_rows(_load(_coerce_store(store)), {"awaiting_delivery"}, max_entries)


def wake_entries(store: Any, *, max_entries: int = DEFAULT_WAKE_ENTRIES) -> list[dict[str, Any]]:
    limit = min(max(0, int(max_entries)), MAX_WAKE_ENTRIES)
    rows = _sorted_rows(_load(_coerce_store(store)),
                        {"active", "awaiting_delivery"}, None)
    rows.sort(key=_due_key)
    return rows[:limit]


pending_entries = wake_entries


def _portal_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Bounded structured row for a portal run; never includes raw watch text."""
    row = {
        "id": entry["id"],
        "kind": entry["kind"],
        "status": entry["status"],
        "check_hint": _check_hint(entry),
        "last_result": entry.get("last_result", ""),
        "last_checked_at": entry.get("last_checked_at", ""),
        "check_count": int(entry.get("check_count", 0)),
        "notified_count": int(entry.get("notified_count", 0)),
        "created_at": entry.get("created_at", ""),
        "expires_at": entry.get("expires_at", ""),
    }
    row.update(_clarification_fields(entry))
    return row


def _reminder_window_end(rem: dict[str, Any], stamp: datetime) -> datetime | None:
    """The end of an emitted reminder's own window, if it can be read."""
    raw = str(rem.get("event_due") or rem.get("due") or rem.get("emitted_at") or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=stamp.tzinfo)
    try:
        grace = max(0, int(rem.get("late_grace_minutes") or 0))
    except (TypeError, ValueError):
        grace = 0
    return parsed + timedelta(minutes=grace)


def _seed_reminder_followthroughs_locked(store: Any, data: dict[str, Any],
                                         stamp: datetime) -> tuple[list[str], bool]:
    """Create at most one one-shot follow-up for each emitted reminder.

    Requested reminders live in their own registry. Once a reminder has been
    emitted and its own window has passed, the follow-up store gets exactly one
    one-shot entry for it. The bounded ``dedupe`` list keeps that true even
    after old closed entries are pruned. No sender, timer or new state file is
    introduced; the existing portal follow-up evaluation performs this read.
    """
    raw = store.load("reminders.json", {"reminders": {}})
    reminders = raw.get("reminders") if isinstance(raw, dict) else None
    if not isinstance(reminders, dict) or not reminders:
        return [], False
    created: list[str] = []
    touched = False
    existing_refs = {
        str(entry.get("source_ref") or "")
        for entry in data["entries"].values()
        if isinstance(entry, dict)
    }
    for rid, rem in sorted(reminders.items()):
        if not isinstance(rem, dict) or rem.get("status") != "emitted":
            continue
        key = f"reminder-followthrough:{rid}"[:MAX_EVENT_KEY_CHARS]
        if key in (data.get("dedupe") or []):
            continue
        source_ref = f"reminder:{rid}"[:MAX_SOURCE_REF_CHARS]
        if source_ref in existing_refs:
            if key not in (data.get("dedupe") or []):
                _add_dedupe(data, key)
                touched = True
            continue
        window_end = _reminder_window_end(rem, stamp)
        if window_end is not None and stamp <= window_end:
            continue
        if _active_count(data) >= MAX_ACTIVE_FOLLOWUPS:
            continue
        try:
            text = _bounded("Follow up: " + str(rem.get("text") or ""),
                            "text", MAX_TEXT_CHARS, required=True)
            hint = _bounded("Ask whether the owner acted on that reminder.",
                            "check_hint", MAX_CHECK_HINT_CHARS, required=True)
            expiry = _expiry_text("", stamp, default_days=DEFAULT_PORTAL_EXPIRY_DAYS)
        except FollowupError:
            continue
        fid = _new_id(data)
        entry = {
            "id": fid,
            "text": text,
            "kind": "one_shot",
            "status": "active",
            "created_at": stamp.isoformat(),
            "updated_at": stamp.isoformat(),
            "source": "reminder",
            "check_hint": hint,
            "filed_by": "agent",
            "evidence_quote": "",
            "source_ref": source_ref,
            "expires_at": expiry,
            "created_execution_id": "",
            "last_checked_at": "",
            "last_result": "",
            "check_count": 0,
            "notified_count": 0,
            "seen": [],
            "notice_reservations": [],
            "notice_event_key": "",
            "notice_status": "",
            "owner_confirmed_ref": "",
            "owner_confirmed_at": "",
            "closed_at": "",
            "close_reason": "",
        }
        data["entries"][fid] = entry
        _add_dedupe(data, key)
        existing_refs.add(source_ref)
        created.append(fid)
        touched = True
    return created, touched


def seed_from_reminders(store: Any, *, now: datetime | None = None) -> list[str]:
    """Seed one-shot follow-ups for emitted requested reminders (idempotent)."""
    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        created, touched = _seed_reminder_followthroughs_locked(target, data, stamp)
        if touched:
            _save(target, data, stamp)
        return created


def portal_rows(store: Any, *, max_entries: int = DEFAULT_WAKE_ENTRIES,
                now: datetime | None = None) -> list[dict[str, Any]]:
    """Close expired active watches, seed reminder follow-throughs, return due rows."""
    limit = min(max(0, int(max_entries)), MAX_WAKE_ENTRIES)
    target = _coerce_store(store)
    stamp = _now(target, now)
    with _locked(target):
        data = _load(target)
        changed = bool(_expire_active_locked(data, stamp))
        _created, seeded = _seed_reminder_followthroughs_locked(target, data, stamp)
        if seeded:
            changed = True
        if changed:
            _save(target, data, stamp)
        rows = [_portal_row(entry) for entry in data["entries"].values()
                if entry.get("status") == "active"]
    rows.sort(key=_due_key)
    return rows[:limit]


def summary(store: Any) -> dict[str, int]:
    data = _load(_coerce_store(store))
    counts = {status: 0 for status in STATUSES}
    for entry in data["entries"].values():
        status = str(entry.get("status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(data["entries"]),
        "active": counts["active"],
        "awaiting_delivery": counts["awaiting_delivery"],
        "done": counts["done"],
        "canceled": counts["canceled"],
        "closed": counts["closed"],
    }


def receipts(store: Any, *, max_entries: int = MAX_CLOSED) -> list[dict[str, Any]]:
    data = _load(_coerce_store(store))
    rows = []
    for entry in data["entries"].values():
        if entry.get("status") not in {"done", "canceled", "closed"}:
            continue
        rows.append({
            "id": entry["id"],
            "kind": entry["kind"],
            "status": entry["status"],
            "notice_status": entry.get("notice_status", ""),
            "close_reason": entry.get("close_reason", ""),
            "closed_at": entry.get("closed_at", ""),
            "created_at": entry.get("created_at", ""),
            "updated_at": entry.get("updated_at", ""),
            "notified_count": int(entry.get("notified_count", 0)),
        })
    rows.sort(key=lambda row: (str(row.get("closed_at") or ""), str(row.get("id") or "")),
              reverse=True)
    return rows[: max(0, int(max_entries))]


__all__ = [
    "SCHEMA", "STORE", "LOCK", "MAX_ACTIVE_FOLLOWUPS", "MAX_AWAITING_FOLLOWUPS",
    "MAX_EXPIRES_AT_CHARS", "DEFAULT_PORTAL_EXPIRY_DAYS", "MAX_EXPIRY_DAYS",
    "MAX_CREATES_PER_EXECUTION",
    "MAX_PENDING_DELIVERIES", "MAX_UNRESOLVED_DELIVERIES",
    "MAX_PENDING_FOLLOWUPS", "MAX_PENDING", "MAX_CLOSED", "MAX_DEDUPE",
    "MAX_SEEN", "MAX_TEXT_CHARS", "MAX_RECEIPT_REF_CHARS", "KINDS", "STATUSES",
    "RESULTS", "NOTICE_STATUSES", "CLOSE_REASONS", "NEEDS_CLARIFICATION_TEXT",
    "FollowupError", "FollowupCapacityError",
    "empty_store", "create", "record_check",
    "queue_notice", "release_notice", "resolve_notice", "confirm_owner_receipt",
    "close", "expire", "get", "seed_from_reminders",
    "active_entries",
    "awaiting_entries", "wake_entries", "pending_entries", "portal_rows",
    "notice_event_key", "summary", "receipts",
]
