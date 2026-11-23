"""Durable planner observation journal primitives (v1.1.2).

This module is deliberately isolated from the scheduler and run loop. It turns
already-normalised evaluator candidates (see :mod:`proactive_pilot.candidates`)
into a bounded, owner-local journal under a real :class:`StateStore`.

Design contract:
- Event identity excludes the revision so a changed revision replaces the
  latest pending revision of the *same* event.
- Version identity folds the existing normalised content ``version`` and time
  ``phase``; an older claim can never consume/acknowledge a newer revision.
- Empty input and exact duplicates never write.
- Persistence uses ``StateStore.lock`` / ``load(strict=True)`` / ``save`` with a
  versioned payload, UTC-aware timestamps and a scope check.
- Only minimum bounded references are stored; destinations, previews and raw
  source payloads are never accepted into the journal.
- Corruption, a busy lock and capacity overflow fail closed. Pending records are
  never evicted silently to make room.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from .state import CorruptStateError, StateStore

SCHEMA_VERSION = 1
JOURNAL_PREFIX = "planner_observations"
DEFAULT_MAX_RECORDS = 512
MAX_REFERENCE_CHARS = 200
MAX_ID_CHARS = 64


class JournalError(RuntimeError):
    """Base class for journal-specific refusal conditions."""


class JournalLockError(JournalError):
    """Another writer holds the journal lock; refuse rather than block."""


class JournalOverflowError(JournalError):
    """The bounded journal cannot accept a new event without eviction."""


class ObservationVersionError(JournalError):
    """The requested observation/version cannot be consumed."""


class ObservationNotFoundError(ObservationVersionError):
    """No journal record exists for the requested event identity."""


class StaleObservationError(ObservationVersionError):
    """The claim version is not the latest pending revision."""


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clean(value: Any, limit: int = MAX_REFERENCE_CHARS) -> str:
    return str(value or "")[:limit]


def event_id(candidate: dict[str, Any]) -> str:
    """Stable event identity from existing normalised evidence fields.

    Deliberately omits ``version`` and ``phase`` so a changed revision is the
    same event and can replace its own pending revision. ``subject`` is only a
    fallback when a source item has no explicit ``ref``; it is hashed, never
    persisted.
    """
    if not isinstance(candidate, dict):
        raise ValueError("observation candidate must be a mapping")
    kind = _clean(candidate.get("kind"), MAX_REFERENCE_CHARS)
    source = _clean(candidate.get("source"), MAX_REFERENCE_CHARS)
    if not kind or not source:
        raise ValueError("observation candidate requires non-empty kind and source")
    identity = _clean(candidate.get("ref"), MAX_REFERENCE_CHARS) or _clean(
        candidate.get("subject"), MAX_REFERENCE_CHARS
    )
    if not identity:
        raise ValueError(
            "observation candidate requires a stable ref or subject identity"
        )
    return _digest(f"{kind}|{source}|{identity}")


def version_id(candidate: dict[str, Any]) -> str:
    """Stable revision identity from normalised ``version`` plus ``phase``."""
    if not isinstance(candidate, dict):
        raise ValueError("observation candidate must be a mapping")
    version = _clean(candidate.get("version"), MAX_REFERENCE_CHARS)
    if not version:
        raise ValueError("observation candidate requires a normalised version")
    phase = _clean(candidate.get("phase"), MAX_REFERENCE_CHARS)
    return _digest(f"{version}|{phase}")


def _canonical_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorruptStateError(f"{field}: missing timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptStateError(f"{field}: invalid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorruptStateError(f"{field}: timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _bounded_str(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CorruptStateError(f"{field}: expected string")
    if len(value) > limit:
        raise CorruptStateError(f"{field}: exceeds {limit} characters")
    if required and not value:
        raise CorruptStateError(f"{field}: must not be empty")
    return value


class PlannerObservationJournal:
    """A bounded, scope-isolated observation journal for one ``StateStore``."""

    def __init__(
        self,
        store: StateStore,
        scope: str,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
    ):
        scope_text = _clean(scope, MAX_REFERENCE_CHARS)
        if not scope_text:
            raise ValueError("journal scope must be non-empty")
        if int(max_records) < 1:
            raise ValueError("max_records must be at least 1")
        self.store = store
        self.scope = scope_text
        self.max_records = int(max_records)
        suffix = hashlib.sha256(scope_text.encode("utf-8")).hexdigest()[:16]
        self._name = f"{JOURNAL_PREFIX}.{suffix}.json"
        self._lock_name = f"{self._name}.lock"

    @property
    def path(self) -> Any:
        return self.store.root / self._name

    def _empty(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "scope": self.scope, "records": {}}

    def _now(self) -> str:
        return self.store.clock.utc().astimezone(timezone.utc).isoformat()

    def _validate_record(self, observation_id: str, raw: Any) -> dict[str, Any]:
        if not isinstance(observation_id, str) or not observation_id:
            raise CorruptStateError("records: observation id must be a non-empty string")
        if len(observation_id) > MAX_ID_CHARS:
            raise CorruptStateError("records: observation id too long")
        if not isinstance(raw, dict):
            raise CorruptStateError(f"records[{observation_id}]: expected object")
        if raw.get("observation_id") != observation_id:
            raise CorruptStateError(f"records[{observation_id}]: id mismatch")
        version = _bounded_str(
            raw.get("version_id"), "version_id", MAX_ID_CHARS, required=True
        )
        kind = _bounded_str(raw.get("kind"), "kind", MAX_REFERENCE_CHARS, required=True)
        source = _bounded_str(
            raw.get("source"), "source", MAX_REFERENCE_CHARS, required=True
        )
        ref = _bounded_str(raw.get("ref"), "ref", MAX_REFERENCE_CHARS)
        phase = _bounded_str(raw.get("phase"), "phase", MAX_REFERENCE_CHARS)
        observed_at = _canonical_timestamp(raw.get("observed_at"), "observed_at")
        acknowledged = raw.get("acknowledged")
        if not isinstance(acknowledged, bool):
            raise CorruptStateError("acknowledged: expected boolean")
        acknowledged_at = raw.get("acknowledged_at")
        if acknowledged:
            acknowledged_at = _canonical_timestamp(
                acknowledged_at, "acknowledged_at"
            )
        elif acknowledged_at is not None:
            raise CorruptStateError("acknowledged_at: set while not acknowledged")
        return {
            "observation_id": observation_id,
            "version_id": version,
            "kind": kind,
            "source": source,
            "ref": ref,
            "phase": phase,
            "observed_at": observed_at,
            "acknowledged": acknowledged,
            "acknowledged_at": acknowledged_at,
        }

    def _load_locked(self) -> dict[str, Any]:
        raw = self.store.load(self._name, self._empty(), strict=True)
        if not isinstance(raw, dict):
            raise CorruptStateError(f"{self._name}: artifact is not an object")
        if raw.get("schema") != SCHEMA_VERSION:
            raise CorruptStateError(f"{self._name}: unsupported schema version")
        if raw.get("scope") != self.scope:
            raise CorruptStateError(f"{self._name}: scope mismatch")
        records = raw.get("records")
        if not isinstance(records, dict):
            raise CorruptStateError(f"{self._name}: records must be an object")
        if len(records) > self.max_records:
            raise JournalOverflowError(
                f"{self._name}: {len(records)} records exceed bound {self.max_records}"
            )
        return {
            "schema": SCHEMA_VERSION,
            "scope": self.scope,
            "records": {
                observation_id: self._validate_record(observation_id, record)
                for observation_id, record in records.items()
            },
        }

    def _save_locked(self, payload: dict[str, Any]) -> None:
        self.store.save(self._name, payload)

    def _prepare(self, candidate: dict[str, Any], observed_at: str) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ValueError("observation candidate must be a mapping")
        candidate_scope = candidate.get("scope")
        if candidate_scope is not None and str(candidate_scope) != self.scope:
            raise ValueError("observation candidate belongs to another scope")
        return {
            "observation_id": event_id(candidate),
            "version_id": version_id(candidate),
            "kind": _clean(candidate.get("kind"), MAX_REFERENCE_CHARS),
            "source": _clean(candidate.get("source"), MAX_REFERENCE_CHARS),
            "ref": _clean(candidate.get("ref"), MAX_REFERENCE_CHARS),
            "phase": _clean(candidate.get("phase"), MAX_REFERENCE_CHARS),
            "observed_at": observed_at,
            "acknowledged": False,
            "acknowledged_at": None,
        }

    def load(self) -> dict[str, Any]:
        """Return the complete validated payload (records are copies)."""
        with self.store.lock(self._lock_name) as acquired:
            if not acquired:
                raise JournalLockError("observation journal is locked")
            payload = self._load_locked()
        return {
            "schema": payload["schema"],
            "scope": payload["scope"],
            "records": {
                observation_id: dict(record)
                for observation_id, record in payload["records"].items()
            },
        }

    def record(self, candidates: Iterable[dict[str, Any]]) -> dict[str, int | bool]:
        """Record latest revisions. Empty or fully duplicate input never writes."""
        items = list(candidates)
        if not items:
            return {"wrote": False, "added": 0, "updated": 0, "unchanged": 0}

        observed_at = self._now()
        prepared = [self._prepare(candidate, observed_at) for candidate in items]

        with self.store.lock(self._lock_name) as acquired:
            if not acquired:
                raise JournalLockError("observation journal is locked")
            payload = self._load_locked()
            records = payload["records"]
            added = updated = unchanged = 0
            for incoming in prepared:
                observation_id = incoming["observation_id"]
                existing = records.get(observation_id)
                if existing is None:
                    if len(records) >= self.max_records:
                        raise JournalOverflowError(
                            f"{self._name}: capacity {self.max_records} reached"
                        )
                    records[observation_id] = incoming
                    added += 1
                elif existing["version_id"] == incoming["version_id"]:
                    unchanged += 1
                else:
                    records[observation_id] = incoming
                    updated += 1
            wrote = bool(added or updated)
            if wrote:
                self._save_locked(payload)
        return {
            "wrote": wrote,
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
        }

    def pending(self) -> list[dict[str, Any]]:
        """Return unacknowledged records, deterministically ordered."""
        with self.store.lock(self._lock_name) as acquired:
            if not acquired:
                raise JournalLockError("observation journal is locked")
            records = list(self._load_locked()["records"].values())
        pending = [dict(record) for record in records if not record["acknowledged"]]
        pending.sort(key=lambda record: (record["observed_at"], record["observation_id"]))
        return pending

    def get(self, observation_id: str) -> dict[str, Any]:
        with self.store.lock(self._lock_name) as acquired:
            if not acquired:
                raise JournalLockError("observation journal is locked")
            record = self._load_locked()["records"].get(observation_id)
        if record is None:
            raise ObservationNotFoundError(observation_id)
        return dict(record)

    def acknowledge(self, observation_id: str, version: str) -> dict[str, Any]:
        """Explicitly acknowledge the latest revision for one event.

        The supplied ``version`` must equal the current pending revision. An
        older claim is refused and never marks the newer revision as consumed.
        Repeating the same acknowledgement is idempotent and does not write.
        """
        with self.store.lock(self._lock_name) as acquired:
            if not acquired:
                raise JournalLockError("observation journal is locked")
            payload = self._load_locked()
            record = payload["records"].get(observation_id)
            if record is None:
                raise ObservationNotFoundError(observation_id)
            if record["version_id"] != version:
                raise StaleObservationError(
                    f"{observation_id}: claim version is not the latest revision"
                )
            if record["acknowledged"]:
                return dict(record)
            record["acknowledged"] = True
            record["acknowledged_at"] = self._now()
            self._save_locked(payload)
            return dict(record)

    def claim(self, observation_id: str, version: str) -> dict[str, Any]:
        """Consume a pending revision only when the claim version matches."""
        return self.acknowledge(observation_id, version)


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_MAX_RECORDS",
    "JournalError",
    "JournalLockError",
    "JournalOverflowError",
    "ObservationVersionError",
    "ObservationNotFoundError",
    "StaleObservationError",
    "PlannerObservationJournal",
    "event_id",
    "version_id",
]
