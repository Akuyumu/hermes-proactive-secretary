"""Bounded, profile-local ASSISTANT-MEDIATED context snapshot.

This is the explicit delivery cache that lets the conversational assistant hand
the ordinary scheduled pipeline a *bounded* set of already-authorised context
entries (facts, preferences, names of the user's own projects, etc.) without the
pipeline ever scraping Hermes memory/USER/Hindsight/Library itself.

Hard guarantees:
- NOT a second memory backend and NOT an automatic importer. Entries are only
  ever written through the typed CLI/`update_context_entry` API by the assistant
  acting on an explicit user request, or by the opt-in assistant-mediated
  refresh tool (advisory entries only, explicit owner config, unpaused profile).
  Nothing here reads USER/MEMORY/Hindsight/Library, a credential, a connector,
  an allowlist or the network.
- Owner-only atomic/lock writes via :mod:`proactive_pilot.state` (0700 dir /
  0600 file). Revoked ids are tombstoned so a stale entry cannot be revived.
- Bounded: at most ``MAX_ENTRIES`` live entries, ``MAX_TEXT_CHARS`` per entry,
  ``MAX_TOTAL_CHARS`` in total, ``MAX_REVOKED`` tombstones and ``MAX_FILE_BYTES``
  on-disk (checked BEFORE parsing). Oversize/malformed content is refused, never
  truncated silently.
- Every entry carries provenance label, created/updated/expiry timestamps, a
  stable id and an advisory-vs-owner-declared distinction. The ``source`` label
  is a CLOSED, documented low-sensitivity enum (see ``VALID_SOURCES``) validated
  identically at write and read time, so an owner-specific account/path/request
  excerpt can never appear in text-free metadata.
- Expiry is MANDATORY and BOUNDED. The writer defaults a missing ``expires_at``
  to ``DEFAULT_TTL_DAYS`` and refuses anything past ``MAX_TTL_DAYS``; the reader
  independently re-enforces the same lifetime bound relative to ``updated_at``
  (so a legitimate refresh with an older, preserved ``created_at`` is accepted
  while a hand-tampered multi-year expiry is refused), requires
  ``created_at <= updated_at <= expires_at``, and refuses an ``updated_at``
  arbitrarily in the future. Expired entries are ignored normally (they are not
  a corruption).
- Injection/secret-bearing entries are denied at write time AND re-checked at
  READ time, so a hand-crafted file cannot smuggle an instruction past the
  boundary. The snapshot text is additionally treated as UNTRUSTED data
  everywhere downstream.
- The READ boundary is authoritative: exact shapes/types/kind/id/provenance and
  every timestamp are validated, a mix of one valid + one malformed entry marks
  the WHOLE snapshot invalid, and strict reads consistently raise a typed
  ``CorruptStateError``.
- Error/status text is generic and never echoes untrusted ids, values or text.
- Empty/malformed/missing/expired means UNAVAILABLE with a bounded health reason;
  consumers never fabricate a value, never refresh in the background and never
  run a tool loop.
- Raw snapshot text is never written to stderr/status/log diagnostics; those
  surfaces report metadata only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .state import CorruptStateError, file_lock

CONTEXT_FILE = "context_snapshot.json"
CONTEXT_LOCK_FILE = "context_snapshot.lock"
CONTEXT_SCHEMA = 1

MAX_ENTRIES = 12
MAX_TEXT_CHARS = 500
MAX_TOTAL_CHARS = 4000
MAX_SOURCE_CHARS = 64
MAX_REASON_CHARS = 120
MAX_REVOKED = 200
MAX_FILE_BYTES = 262_144

# Mandatory bounded expiry: a missing expiry gets this reasonable default, and no
# entry may live longer than the maximum lifetime (documented, enforced at both
# write and read time).
DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 30

# Fixed, documented, low-sensitivity provenance labels. These are the ONLY
# accepted ``source`` values at write AND read time. A free-text provenance could
# otherwise smuggle an owner name, account id, filesystem path or request excerpt
# into text-free metadata (``context-list``), so the label is a closed enum:
# generic, portable and never owner-specific.
VALID_SOURCES = (
    "user-stated",
    "assistant:memory",
    "assistant:knowledge",
    "owner-config",
    "synthetic-test",
)
DEFAULT_SOURCE = "assistant:knowledge"

# A stored entry may not be updated arbitrarily in the future relative to the
# read-time clock. A small tolerance absorbs ordinary process clock skew while
# still rejecting a hand-tampered far-future ``updated_at``.
FUTURE_TOLERANCE = timedelta(minutes=5)

VALID_KINDS = {"note", "interest", "preference", "project", "person", "place"}

STATUS_UNCONFIGURED = "unconfigured"
STATUS_AVAILABLE = "available"
STATUS_EXPIRED = "expired"
STATUS_INVALID = "invalid"

# Generic, text-free reasons. Never interpolate an untrusted id/value/text.
_INVALID_REASON = "snapshot is invalid (fail closed)"
_INVALID_REASON_TRUNC = "snapshot entry failed validation"
_REASON_EMPTY = "context snapshot is empty"
_REASON_EXPIRED = "all context entries have expired"

_ID_RE = re.compile(r"^cs_[0-9a-f]{12}$")

# Prompt/command injection markers: an assistant-mediated entry is data, so an
# entry that tries to look like an instruction is refused outright. Defensive
# detection patterns for untrusted retrieved text, not executable instructions.
_INJECTION_MARKERS_RE = re.compile(
    r"ignore previous|ignore all previous|disregard|system prompt|you are now"
    r"|assistant:|system:|tool_use|execute_code|rm -rf|curl |wget |bash -c"
    r"|sh -c|subprocess|\$\(|`|<script|/etc/(?:passwd|shadow|sudoers)"
    r"|authorized_keys",
    re.IGNORECASE,
)

# Credential/secret-shaped content is never a legitimate delivery-cache entry.
_SECRET_RE = re.compile(
    r"(password|passwd|passphrase|secret|api[_ -]?key|apikey|access[_ -]?token|"
    r"auth[_ -]?token|bearer\s+[a-z0-9._-]{8,}|private[_ -]?key|"
    r"client[_ -]?secret|otp|one[- ]time[- ](code|password)|"
    r"\b\d{13,19}\b)",
    re.IGNORECASE,
)


class ContextSnapshotError(ValueError):
    """Invalid snapshot mutation; the caller must fail closed (write nothing)."""


@dataclass
class SnapshotView:
    """Validated read result. ``entries`` are only the usable entries."""

    status: str
    reason: str = ""
    updated_at: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE and bool(self.entries)

    def metadata(self) -> dict[str, Any]:
        """Text-free health/metadata for status/diagnostics."""
        return {
            "status": self.status,
            "reason": self.reason[:MAX_REASON_CHARS],
            "updated_at": self.updated_at,
            "available": len(self.entries),
            "expired": len(self.expired),
        }

    def texts(self) -> list[str]:
        return [str(e.get("text") or "") for e in self.entries if e.get("text")]


def empty_context_snapshot() -> dict[str, Any]:
    return {
        "schema": CONTEXT_SCHEMA,
        "updated_at": "",
        "entries": {},
        # Durable tombstones for explicitly revoked ids so a replayed/stale write
        # can never revive a withdrawn entry.
        "revoked": {},
    }


def _now_aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: Any, exc_type: type[Exception]) -> datetime:
    """Parse an ISO timestamp to an aware UTC-normalised datetime.

    A naive stored value (no offset) is treated as UTC rather than compared
    against an aware clock (which would raise ``TypeError``). Errors are generic:
    the raw value is never echoed.
    """
    if not isinstance(value, str) or not value.strip():
        raise exc_type("timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (ValueError, TypeError) as exc:
        raise exc_type("timestamp is malformed") from exc
    return _now_aware(parsed)


def _content_denied(text: str) -> bool:
    if _INJECTION_MARKERS_RE.search(text):
        return True
    return bool(_SECRET_RE.search(text))


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ContextSnapshotError("text must be a string")
    if not value.strip():
        raise ContextSnapshotError("text is required")
    if len(value) > MAX_TEXT_CHARS:
        raise ContextSnapshotError(
            f"text exceeds {MAX_TEXT_CHARS} characters")
    if _has_control(value):
        raise ContextSnapshotError("text contains control characters")
    if _content_denied(value):
        raise ContextSnapshotError("text is not permitted")
    return value


def _clean_source(value: Any) -> str:
    """Validate the provenance label against the fixed low-sensitivity enum.

    The error is generic and never echoes the supplied value: an arbitrary
    free-text provenance (a name, account, path or request excerpt) must be
    rejected without leaking it into an error/status surface.
    """
    if not isinstance(value, str):
        raise ContextSnapshotError("source provenance label must be a string")
    source = value.strip()
    if not source:
        raise ContextSnapshotError("source provenance label is required")
    if len(source) > MAX_SOURCE_CHARS:
        raise ContextSnapshotError("source provenance label is unsupported")
    if _has_control(source):
        raise ContextSnapshotError("source provenance label is unsupported")
    if source not in VALID_SOURCES:
        raise ContextSnapshotError("source provenance label is unsupported")
    return source


def _entry_id(text: str, source: str, now: datetime) -> str:
    raw = f"{source}|{text}|{now.isoformat()}".encode("utf-8")
    return "cs_" + hashlib.sha256(raw).hexdigest()[:12]


def validate_entry_fields(fields: dict[str, Any], now: datetime,
                          *, entry_id: str = "") -> dict[str, Any]:
    """Validate one entry payload; raise on ANY violation (no partial write)."""
    if not isinstance(fields, dict):
        raise ContextSnapshotError("entry is not an object")
    text = _clean_text(fields.get("text"))
    source = _clean_source(fields.get("source"))
    kind = str(fields.get("kind") or "note").strip().lower()
    if kind not in VALID_KINDS:
        raise ContextSnapshotError("unsupported kind")
    advisory = fields.get("advisory", True)
    if not isinstance(advisory, bool):
        raise ContextSnapshotError("advisory flag must be boolean")
    raw_id = str(entry_id or fields.get("id") or "").strip()
    if raw_id and not _ID_RE.match(raw_id):
        raise ContextSnapshotError("entry id is malformed")
    created = (_parse_timestamp(fields.get("created_at"), ContextSnapshotError)
               if fields.get("created_at") not in (None, "")
               else _now_aware(now))
    now_aware = _now_aware(now)
    if created > now_aware:
        # ``updated_at`` is the write clock; ``created_at`` may be old (a refresh
        # preserves it) but never in the future.
        raise ContextSnapshotError("created_at must not be in the future")
    raw_expiry = fields.get("expires_at")
    if raw_expiry in (None, ""):
        expires = now_aware + timedelta(days=DEFAULT_TTL_DAYS)
    else:
        expires = _parse_timestamp(raw_expiry, ContextSnapshotError)
        if expires <= now_aware:
            raise ContextSnapshotError("expires_at must be in the future")
        if expires - now_aware > timedelta(days=MAX_TTL_DAYS):
            raise ContextSnapshotError("expires_at exceeds the maximum lifetime")
    return {
        "id": str(raw_id or _entry_id(text, source, now)),
        "text": text,
        "source": source,
        "kind": kind,
        "advisory": advisory,
        "created_at": created.isoformat(),
        "updated_at": now_aware.isoformat(),
        "expires_at": expires.isoformat(),
    }


def _validate_stored_entry(entry_id: Any, entry: Any, now: datetime) -> None:
    """Validate one stored entry at READ time; raise generic ``CorruptStateError``.

    Never interpolates the stored id/value/text into the error, and re-checks
    injection/secret content so a hand-edited file cannot bypass the write gate.
    The timestamp rules are re-enforced here because the WRITE gate alone cannot
    stop a hand-edited file from bypassing the writer's 30-day lifetime bound:

    - ``created_at <= updated_at <= expires_at`` (no impossible ordering);
    - ``updated_at`` is not arbitrarily in the future vs the read clock;
    - ``expires_at - updated_at <= MAX_TTL_DAYS`` (the same bounded lifetime the
      writer enforces, but measured from ``updated_at`` so a legitimate refresh
      that preserves an old ``created_at`` is still valid).
    """
    if not isinstance(entry, dict):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if str(entry.get("id") or "") != entry_id:
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if len(text) > MAX_TEXT_CHARS or _has_control(text) or _content_denied(text):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if not isinstance(entry.get("source"), str):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    source = entry["source"]
    if (not source.strip() or len(source) > MAX_SOURCE_CHARS
            or _has_control(source) or source not in VALID_SOURCES):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if entry.get("kind") not in VALID_KINDS:
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if not isinstance(entry.get("advisory"), bool):
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    parsed: dict[str, datetime] = {}
    for key in ("created_at", "updated_at", "expires_at"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CorruptStateError(_INVALID_REASON_TRUNC)
        try:
            parsed[key] = _now_aware(datetime.fromisoformat(value.strip()))
        except (ValueError, TypeError) as exc:
            raise CorruptStateError(_INVALID_REASON_TRUNC) from exc
    created, updated, expires = (parsed["created_at"], parsed["updated_at"],
                                 parsed["expires_at"])
    if created > updated or updated > expires:
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if updated > _now_aware(now) + FUTURE_TOLERANCE:
        raise CorruptStateError(_INVALID_REASON_TRUNC)
    if expires - updated > timedelta(days=MAX_TTL_DAYS):
        raise CorruptStateError(_INVALID_REASON_TRUNC)


def _guard_store_paths(store: Any) -> None:
    """Refuse a symlinked snapshot file/lock/directory/root for read AND write.

    A symlinked artifact (or lock) could redirect an owner-only write outside the
    active profile data root, and ``chmod`` would otherwise follow the link onto
    an unrelated target. We fail closed instead.
    """
    root = getattr(store, "root", None)
    if root is not None and Path(root).is_symlink():
        raise CorruptStateError(f"{CONTEXT_FILE}: refusing a symlinked state root")
    for name in (CONTEXT_FILE, CONTEXT_LOCK_FILE):
        if not hasattr(store, "_path"):
            return
        path = Path(store._path(name))
        if path.is_symlink():
            raise CorruptStateError(f"{CONTEXT_FILE}: refusing a symlinked path")
        if path.parent.is_symlink():
            raise CorruptStateError(f"{CONTEXT_FILE}: refusing a symlinked directory")


def _load_raw(store: Any, *, strict: bool, now: datetime) -> dict[str, Any]:
    """Bounded load + structural/tombstone validation (never unbounded parse).

    Raises ``CorruptStateError`` on any malformed shape. When ``strict`` is False
    the caller-supplied writer still fails closed (we return an empty snapshot so
    the caller can decide), but a corrupt tombstone is NEVER silently replaced.
    """
    try:
        _guard_store_paths(store)
    except CorruptStateError:
        if strict:
            raise
        return empty_context_snapshot()
    path = Path(store._path(CONTEXT_FILE)) if hasattr(store, "_path") else None
    if path is None or not path.exists():
        return empty_context_snapshot()
    try:
        size = path.stat().st_size
    except OSError as exc:
        if strict:
            raise CorruptStateError(f"{CONTEXT_FILE}: unreadable") from exc
        return empty_context_snapshot()
    if size > MAX_FILE_BYTES:
        raise CorruptStateError(f"{CONTEXT_FILE}: exceeds the file size cap")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if strict:
            raise CorruptStateError(f"{CONTEXT_FILE}: unreadable") from exc
        return empty_context_snapshot()
    if (not isinstance(data, dict) or data.get("schema") != CONTEXT_SCHEMA):
        raise CorruptStateError(f"{CONTEXT_FILE}: schema mismatch")
    entries = data.get("entries")
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise CorruptStateError(f"{CONTEXT_FILE}: invalid entry collection")
    revoked = data.get("revoked")
    if not isinstance(revoked, dict):
        # Refuse an invalid tombstone shape instead of quietly replacing it with
        # an empty map (which would resurrect a withdrawn id).
        raise CorruptStateError(f"{CONTEXT_FILE}: invalid tombstone shape")
    if len(revoked) > MAX_REVOKED:
        raise CorruptStateError(f"{CONTEXT_FILE}: tombstone cap exceeded")
    for rid, revoked_at in revoked.items():
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            raise CorruptStateError(f"{CONTEXT_FILE}: invalid tombstone")
        if not isinstance(revoked_at, str):
            raise CorruptStateError(f"{CONTEXT_FILE}: invalid tombstone")
        try:
            _now_aware(datetime.fromisoformat(revoked_at.strip()))
        except (ValueError, TypeError) as exc:
            raise CorruptStateError(f"{CONTEXT_FILE}: invalid tombstone") from exc
    if set(entries) & set(revoked):
        # A revoked id can never also be accepted live.
        raise CorruptStateError(f"{CONTEXT_FILE}: revoked id is also live")
    for entry_id, entry in entries.items():
        _validate_stored_entry(entry_id, entry, now)
    updated_at = data.get("updated_at", "")
    if not isinstance(updated_at, str):
        raise CorruptStateError(f"{CONTEXT_FILE}: invalid updated_at")
    if updated_at.strip():
        try:
            parsed_updated = _now_aware(datetime.fromisoformat(updated_at.strip()))
        except (ValueError, TypeError) as exc:
            raise CorruptStateError(f"{CONTEXT_FILE}: invalid updated_at") from exc
        if parsed_updated > _now_aware(now) + FUTURE_TOLERANCE:
            raise CorruptStateError(f"{CONTEXT_FILE}: invalid updated_at")
    return data


def load_context_snapshot(store: Any, now: datetime, *,
                          strict: bool = False) -> SnapshotView:
    """Load + validate the snapshot; never raises unless ``strict`` is set.

    A missing file is ``unconfigured``; a corrupt/invalid file is ``invalid``;
    all-expired is ``expired``. Any non-available status yields NO entries, so a
    consumer can never read a partial/fabricated value. In strict mode every
    invalid state raises a typed ``CorruptStateError`` instead.
    """
    path = Path(store._path(CONTEXT_FILE)) if hasattr(store, "_path") else None
    try:
        _guard_store_paths(store)
    except CorruptStateError:
        if strict:
            raise
        return SnapshotView(STATUS_INVALID, _INVALID_REASON)
    if path is None or not path.exists():
        return SnapshotView(STATUS_UNCONFIGURED, "no context snapshot configured")
    try:
        data = _load_raw(store, strict=True, now=now)
    except CorruptStateError as exc:
        if strict:
            raise
        return SnapshotView(STATUS_INVALID, _INVALID_REASON)
    now_aware = _now_aware(now)
    usable: list[dict[str, Any]] = []
    expired: list[str] = []
    try:
        for entry_id, entry in data.get("entries", {}).items():
            expiry = _now_aware(datetime.fromisoformat(str(entry["expires_at"]).strip()))
            if expiry <= now_aware:
                expired.append(str(entry_id))
                continue
            # Canonicalise every timestamp to aware ISO (a stored naive value is
            # read as UTC) so downstream consumers never see a naive timestamp.
            canonical = dict(entry)
            for key in ("created_at", "updated_at", "expires_at"):
                canonical[key] = _now_aware(
                    datetime.fromisoformat(str(entry[key]).strip())).isoformat()
            usable.append(canonical)
    except (CorruptStateError, ValueError, TypeError, KeyError) as exc:
        if strict:
            raise CorruptStateError(_INVALID_REASON) from exc
        return SnapshotView(STATUS_INVALID, _INVALID_REASON)
    if len(usable) > MAX_ENTRIES:
        if strict:
            raise CorruptStateError(f"{CONTEXT_FILE}: entry cap exceeded")
        return SnapshotView(STATUS_INVALID, "snapshot exceeds the entry cap")
    total_chars = 0
    for entry in usable:
        total_chars += len(str(entry.get("text") or ""))
    if total_chars > MAX_TOTAL_CHARS:
        if strict:
            raise CorruptStateError(f"{CONTEXT_FILE}: total size cap exceeded")
        return SnapshotView(STATUS_INVALID, "snapshot exceeds the total size cap")
    updated_at = str(data.get("updated_at") or "")
    if not usable:
        reason = _REASON_EXPIRED if expired else _REASON_EMPTY
        return SnapshotView(STATUS_EXPIRED, reason, updated_at, [], expired)
    return SnapshotView(STATUS_AVAILABLE, "", updated_at, usable, expired)


def context_texts(view: SnapshotView) -> list[str]:
    """Bounded advisory text list for the proposer/privacy boundary."""
    if not view.available:
        return []
    return view.texts()


def inspect_metadata(view: SnapshotView) -> dict[str, Any]:
    """Text-free summary; the only snapshot content allowed in diagnostics."""
    if view.status == STATUS_AVAILABLE:
        return {**view.metadata(),
                "entries": [{"id": e.get("id"), "source": e.get("source"),
                             "kind": e.get("kind"), "advisory": bool(e.get("advisory")),
                             "chars": len(str(e.get("text") or "")),
                             "expires_at": e.get("expires_at", "")}
                            for e in view.entries]}
    return view.metadata()


def _write(store: Any, data: dict[str, Any], now: datetime) -> None:
    # The writer enforces the SAME bounds the reader validates. A legitimate
    # operation must never persist a snapshot its own reader immediately rejects.
    entries = data.get("entries")
    revoked = data.get("revoked")
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ContextSnapshotError("refusing to write an over-cap entry collection")
    if not isinstance(revoked, dict) or len(revoked) > MAX_REVOKED:
        raise ContextSnapshotError("refusing to write an over-cap tombstone collection")
    data["updated_at"] = _now_aware(now).isoformat()
    store.save(CONTEXT_FILE, data)


ASSISTANT_ADVISORY_SOURCES = ("assistant:knowledge", "assistant:memory")


def _require_assistant_advisory_owner(existing: Any, allowed_sources: Any) -> None:
    """Trusted-call-site guard: only assistant-advisory entries may be changed.

    The check runs inside the caller's existing snapshot lock. It is never fed
    from model payload text; the new tool passes fixed parameters and the CLI
    keeps the default (no guard).
    """
    if (not isinstance(existing, dict) or existing.get("advisory") is not True
            or existing.get("source") not in allowed_sources):
        raise ContextSnapshotError("context entry is not assistant-owned")


def update_context_entry(store: Any, fields: dict[str, Any], now: datetime,
                         *, strict: bool = True,
                         require_assistant_owned: bool = False,
                         assistant_sources: Any = ASSISTANT_ADVISORY_SOURCES) -> dict[str, Any]:
    """Add or replace one entry under the owner-only lock (atomic).

    A revoked id can never be revived, an existing ``created_at`` is preserved
    across updates, and entry/total caps are enforced after the edit; the whole
    update is refused if it would exceed them. Optional trusted-call-site guard:
    when ``require_assistant_owned`` is set, an existing id may only be replaced
    if its stored advisory flag is true and its provenance is an allowed
    assistant source. Existing callers keep the unchanged default.
    """
    entry = validate_entry_fields(fields, now,
                                  entry_id=str(fields.get("id") or ""))
    _guard_store_paths(store)
    with file_lock(store._path(CONTEXT_LOCK_FILE)) as acquired:
        if not acquired:
            raise ContextSnapshotError("context snapshot is locked by another writer")
        data = _load_raw(store, strict=strict, now=now)
        if str(entry["id"]) in (data.get("revoked") or {}):
            raise ContextSnapshotError("entry id was revoked and cannot be revived")
        entries = dict(data.get("entries") or {})
        if require_assistant_owned:
            existing_owned = entries.get(str(entry["id"]))
            if existing_owned is not None:
                _require_assistant_advisory_owner(existing_owned, assistant_sources)
        # Reclaim EXPIRED entries before enforcing the live-entry cap. Expiry is
        # not revocation: dropping an expired record is the documented lifecycle
        # and does not create (or need) a tombstone.
        now_aware = _now_aware(now)
        for expired_id, expired_entry in list(entries.items()):
            try:
                expiry = _now_aware(datetime.fromisoformat(
                    str(expired_entry.get("expires_at") or "").strip()))
            except (ValueError, TypeError):
                continue
            if expiry <= now_aware:
                entries.pop(expired_id, None)
        existing = entries.get(str(entry["id"]))
        if isinstance(existing, dict) and str(existing.get("created_at") or "").strip():
            # Preserve the original creation time across updates.
            entry["created_at"] = str(existing["created_at"])
        entries[str(entry["id"])] = entry
        if len(entries) > MAX_ENTRIES:
            raise ContextSnapshotError(
                f"refusing to exceed {MAX_ENTRIES} live entries; revoke one first")
        total = sum(len(str(e.get("text") or "")) for e in entries.values())
        if total > MAX_TOTAL_CHARS:
            raise ContextSnapshotError(
                f"refusing to exceed the {MAX_TOTAL_CHARS}-character total cap")
        data["entries"] = entries
        _write(store, data, now)
    return entry


def revoke_context_entry(store: Any, entry_id: str, now: datetime,
                         *, strict: bool = True,
                         require_assistant_owned: bool = False,
                         assistant_sources: Any = ASSISTANT_ADVISORY_SOURCES) -> dict[str, Any]:
    """Remove one entry and tombstone its id (durable; no auto-recreate).

    The optional trusted-call-site ownership guard runs inside the same lock as
    the removal. The CLI keeps the unchanged default and remains the explicit
    owner-authority path.
    """
    target = str(entry_id or "").strip()
    if not _ID_RE.match(target):
        raise ContextSnapshotError("entry id is malformed")
    _guard_store_paths(store)
    with file_lock(store._path(CONTEXT_LOCK_FILE)) as acquired:
        if not acquired:
            raise ContextSnapshotError("context snapshot is locked by another writer")
        data = _load_raw(store, strict=strict, now=now)
        entries = dict(data.get("entries") or {})
        if target not in entries:
            raise ContextSnapshotError("no such context entry")
        if require_assistant_owned:
            _require_assistant_advisory_owner(entries[target], assistant_sources)
        entries.pop(target, None)
        data["entries"] = entries
        revoked = dict(data.get("revoked") or {})
        if target not in revoked and len(revoked) >= MAX_REVOKED:
            # Fail closed BEFORE mutating: the existing snapshot stays valid and
            # no previously withdrawn id is ever pruned/resurrected.
            raise ContextSnapshotError(
                f"revocation limit reached ({MAX_REVOKED}); existing revocations "
                "are retained so withdrawn context can never be revived")
        revoked[target] = _now_aware(now).isoformat()
        data["revoked"] = revoked
        _write(store, data, now)
    return {"id": target, "revoked_at": _now_aware(now).isoformat()}


def get_context_entry(store: Any, entry_id: str, now: datetime) -> dict[str, Any]:
    """Explicit content read-back for ONE entry (used only by ``context-show``)."""
    target = str(entry_id or "").strip()
    if not _ID_RE.match(target):
        raise ContextSnapshotError("entry id is malformed")
    view = load_context_snapshot(store, now, strict=True)
    if not view.available:
        raise ContextSnapshotError("snapshot unavailable")
    for entry in view.entries:
        if str(entry.get("id")) == target:
            return dict(entry)
    raise ContextSnapshotError("no such available context entry")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def context_command(args: Any) -> int:
    """``--feedback context-list|context-show|context-update|context-revoke``."""
    from .clock import Clock, SeededClock
    from .state import StateStore

    clock = SeededClock(args.seeded_clock) if getattr(args, "seeded_clock", None) else Clock()
    now = clock.now()
    store = StateStore(Path(args.state_dir), clock)
    action = str(getattr(args, "feedback", "") or "")

    try:
        if action == "context-list":
            view = load_context_snapshot(store, now, strict=False)
            print(json.dumps(inspect_metadata(view), ensure_ascii=False,
                             indent=2, sort_keys=True))
            return 0
        if action == "context-show":
            entry_id = str(getattr(args, "id", "") or "")
            entry = get_context_entry(store, entry_id, now)
            print(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if action == "context-update":
            text = str(getattr(args, "note", "") or "")
            if not text:
                print("proactive-portal context update rejected: --note <text> is required",
                      file=__import__("sys").stderr)
                return 2
            fields = {
                "id": str(getattr(args, "id", "") or ""),
                "text": text,
                "source": str(getattr(args, "provenance", "") or DEFAULT_SOURCE),
                "kind": str(getattr(args, "kind", "") or "note"),
                "advisory": not bool(getattr(args, "owner_declared", False)),
                "expires_at": str(getattr(args, "expires_at", "") or ""),
            }
            entry = update_context_entry(store, fields, now)
            print(json.dumps({k: entry[k] for k in
                              ("id", "source", "kind", "advisory", "expires_at",
                               "updated_at")}, indent=2))
            return 0
        if action == "context-revoke":
            entry_id = str(getattr(args, "id", "") or "")
            result = revoke_context_entry(store, entry_id, now)
            print(json.dumps(result, indent=2))
            return 0
    except ContextSnapshotError as exc:
        import sys as _sys
        print(f"proactive-portal context rejected: {exc}", file=_sys.stderr)
        return 2
    except CorruptStateError as exc:
        import sys as _sys
        print(f"proactive-portal context state corrupt, failing closed: {exc}",
              file=_sys.stderr)
        return 1
    import sys as _sys
    print(f"proactive-portal context rejected: unknown action", file=_sys.stderr)
    return 2
