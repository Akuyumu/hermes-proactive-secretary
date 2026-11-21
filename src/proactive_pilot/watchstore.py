"""Owner-only, atomic, versioned adaptive watch store.

A *watch* is an explicit, user-requested public-topic intent. Watches are the
ONLY thing that can cause adaptive public acquisition: nothing here ingests
conversations, mail, calendar or feed text. A watch is created/changed/stopped
only through the typed CLI (or the trusted assistant invoking that CLI on the
user's explicit behalf). Inferred/deterministic candidates are never written here
as live watches, and a fetched page can never mutate a watch.

Safeguards:
- The store is a single owner-only JSON artifact written atomically (0600 dir
  0700, temp file + ``os.replace``) under the shared non-blocking ``file_lock``.
- ``public_topic`` and include/exclude criteria are validated public-safe:
  emails, URLs, hosts, long numeric identifiers and sensitive markers are
  rejected. This is a structural allow/deny check, NOT a claim that a regex can
  prove the absence of all PII.
- Revision is explicit: a change bumps the watch ``version`` and clears the
  cached plan so the next planning pass supersedes the old queries.
- ``stop`` and ``expiry`` and an active snooze prevent any adaptive read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .publicfeeds import _EMAIL_RE, _LONGNUM_RE, _SENSITIVE_MARKERS, _URL_RE
from .state import CorruptStateError, file_lock

WATCH_SCHEMA = 1
WATCH_FILE = "adaptive_watches.json"
WATCH_LOCK_FILE = "adaptive_watches.lock"
MAX_WATCHES = 12
MAX_TOPIC_CHARS = 120
MAX_CRITERION_CHARS = 60
VALID_CONFIDENCE = {"low", "medium", "high"}
BLANKET_TOPICS = {"", "*", "all", "everything", "any", "anything"}
_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b", re.IGNORECASE)
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,39}$")


class WatchError(ValueError):
    """Invalid watch operation; the caller must fail closed (write nothing)."""


def empty_watchstore() -> dict[str, Any]:
    return {
        "schema": WATCH_SCHEMA,
        "version": 0,
        "updated_at": "",
        "cursor": 0,
        "watches": {},
        "feedback": [],
    }


_WATCH_STATUSES = {"active", "stopped"}
_WATCH_DATETIME_FIELDS = ("created_at", "updated_at", "expiry", "snoozed_until", "plan_at")
_WATCH_REQUIRED_TYPES: tuple[tuple[str, type], ...] = (
    ("id", str),
    ("public_topic", str),
    ("scope", str),
    ("version", int),
    ("status", str),
    ("include", list),
    ("exclude", list),
)


def validate_watch_record(watch_id: Any, watch: Any) -> None:
    """Structurally validate one persisted watch or raise ``CorruptStateError``.

    Malformed persisted records (wrong types, unknown status, unparseable
    timestamps) must fail closed rather than be silently treated as an empty or
    healthy watch. This is a schema/type check, not a PII guarantee.
    """
    if not isinstance(watch, dict):
        raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r} is not an object")
    if str(watch.get("id") or "") != str(watch_id):
        raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r} id mismatch")
    for field, expected in _WATCH_REQUIRED_TYPES:
        value = watch.get(field)
        if expected is int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r}.{field} not int")
        elif not isinstance(value, expected):
            raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r}.{field} not {expected.__name__}")
    if str(watch.get("status")) not in _WATCH_STATUSES:
        raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r} bad status")
    if str(watch.get("scope")) and not _SCOPE_RE.match(str(watch.get("scope"))):
        raise CorruptStateError(f"adaptive_watches.json: watch {watch_id!r} bad scope")
    for field in _WATCH_DATETIME_FIELDS:
        text = str(watch.get(field) or "")
        if not text:
            continue
        try:
            datetime.fromisoformat(text)
        except ValueError as exc:
            raise CorruptStateError(
                f"adaptive_watches.json: watch {watch_id!r}.{field} not a datetime"
            ) from exc


def load_watchstore(store: Any, *, strict: bool = False) -> dict[str, Any]:
    data = store.load(WATCH_FILE, empty_watchstore(), strict=strict)
    if (
        not isinstance(data, dict)
        or data.get("schema") != WATCH_SCHEMA
        or not isinstance(data.get("watches"), dict)
    ):
        if strict:
            raise CorruptStateError("adaptive_watches.json: schema mismatch")
        return empty_watchstore()
    if strict:
        for wid, watch in data.get("watches", {}).items():
            validate_watch_record(wid, watch)
    data.setdefault("cursor", 0)
    data.setdefault("feedback", [])
    return data


def save_watchstore(store: Any, data: dict[str, Any]) -> None:
    store.save(WATCH_FILE, data)


@contextmanager
def watch_lock(store: Any) -> Iterator[bool]:
    """Non-blocking single-writer lock for the watch store."""
    with file_lock(Path(store.root) / WATCH_LOCK_FILE) as acquired:
        yield acquired


# --------------------------------------------------------------------------- #
# Public-safe validation
# --------------------------------------------------------------------------- #


def _collapsed(value: Any) -> str:
    return " ".join(str(value or "").split())


def _reject_unsafe(text: str, *, label: str, max_chars: int) -> str:
    if not text:
        raise WatchError(f"{label} is required")
    if len(text) > max_chars:
        raise WatchError(f"{label} is too long (>{max_chars} chars)")
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise WatchError(f"{label} contains sensitive-looking data")
    if _EMAIL_RE.search(text) or _URL_RE.search(text) or _HOST_RE.search(text):
        raise WatchError(f"{label} must not contain emails, URLs or hosts")
    if _LONGNUM_RE.search(text):
        raise WatchError(f"{label} must not contain long numeric identifiers")
    return text


def validate_public_topic(topic: Any) -> str:
    """Return a validated, public-safe topic or raise ``WatchError``.

    This separates the validated public-safe topic from any local context: only
    a short generic phrase is accepted. No email, URL, host, long number or
    sensitive marker may pass, and a blanket ``*`` is refused.
    """
    text = _collapsed(topic)
    if text.lower() in BLANKET_TOPICS:
        raise WatchError("refusing a blanket public_topic")
    return _reject_unsafe(text, label="public_topic", max_chars=MAX_TOPIC_CHARS)


def validate_criteria(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        items = [part for part in re.split(r"[,\n;]+", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise WatchError("include/exclude must be a string or list")
    out: list[str] = []
    for item in items:
        text = _collapsed(item)
        if not text:
            continue
        out.append(_reject_unsafe(text, label="criterion", max_chars=MAX_CRITERION_CHARS))
    return out


def _validate_scope(scope: Any) -> str:
    text = _collapsed(scope) or "public"
    if not _SCOPE_RE.match(text):
        raise WatchError(f"invalid scope: {scope!r}")
    return text


def _validate_expiry(value: Any, now: datetime) -> str:
    text = _collapsed(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WatchError(f"invalid expiry timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise WatchError("expiry must be timezone-aware")
    if parsed <= now:
        raise WatchError("expiry must be in the future")
    return parsed.isoformat()


def _require_provenance(provenance: Any) -> str:
    text = _collapsed(provenance)
    if not text:
        raise WatchError("provenance is required (explicit trusted request only)")
    return text[:200]


def _new_id(topic: str, now: datetime) -> str:
    raw = f"{topic}|{now.isoformat()}"
    return "w_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _watch_is_expired(watch: dict[str, Any], now: datetime) -> bool:
    expiry = str(watch.get("expiry") or "")
    if not expiry:
        return False
    try:
        return datetime.fromisoformat(expiry) <= now
    except ValueError:
        return True  # malformed expiry fails closed (no reads)


def _watch_is_snoozed(watch: dict[str, Any], now: datetime) -> bool:
    until = str(watch.get("snoozed_until") or "")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > now
    except ValueError:
        return True  # malformed snooze fails closed


def watch_is_active(watch: dict[str, Any], now: datetime) -> bool:
    return (
        str(watch.get("status") or "") == "active"
        and not _watch_is_expired(watch, now)
        and not _watch_is_snoozed(watch, now)
    )


def watch_is_retired(watch: dict[str, Any], now: datetime) -> bool:
    """True when a watch is permanently retired (stopped or expired).

    A merely snoozed watch is NOT retired: snooze is reversible, so its evidence
    must be preserved. Retired watches must never have their evidence replayed.
    """
    if str(watch.get("status") or "") == "stopped":
        return True
    return _watch_is_expired(watch, now)


def active_watches(data: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    return [w for w in data.get("watches", {}).values() if watch_is_active(w, now)]


# --------------------------------------------------------------------------- #
# Lifecycle mutations (pure dict transforms; caller persists under lock)
# --------------------------------------------------------------------------- #


def add_watch(
    data: dict[str, Any],
    *,
    topic: Any,
    include: Any = None,
    exclude: Any = None,
    scope: Any = "public",
    provenance: Any = "",
    confidence: Any = "medium",
    expires_at: Any = "",
    now: datetime,
) -> dict[str, Any]:
    topic_text = validate_public_topic(topic)
    confidence_text = _collapsed(confidence) or "medium"
    if confidence_text not in VALID_CONFIDENCE:
        raise WatchError("confidence must be low|medium|high")
    if len(data.get("watches", {})) >= MAX_WATCHES:
        raise WatchError(f"watch limit reached ({MAX_WATCHES})")
    wid = _new_id(topic_text, now)
    watch = {
        "id": wid,
        "public_topic": topic_text,
        "include": validate_criteria(include),
        "exclude": validate_criteria(exclude),
        "scope": _validate_scope(scope),
        "confidence": confidence_text,
        "provenance": _require_provenance(provenance),
        "expiry": _validate_expiry(expires_at, now),
        "status": "active",
        "version": 1,
        "snoozed_until": "",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "plan": None,
        "plan_version": 0,
        "plan_at": "",
    }
    data.setdefault("watches", {})[wid] = watch
    data["updated_at"] = now.isoformat()
    data["version"] = int(data.get("version", 0)) + 1
    return watch


def change_watch(
    data: dict[str, Any],
    watch_id: str,
    *,
    topic: Any = None,
    include: Any = None,
    exclude: Any = None,
    scope: Any = None,
    confidence: Any = None,
    expires_at: Any = None,
    provenance: Any = None,
    now: datetime,
) -> dict[str, Any]:
    watch = data.get("watches", {}).get(str(watch_id))
    if not isinstance(watch, dict):
        raise WatchError(f"no watch with id {watch_id!r}")
    if topic is not None:
        watch["public_topic"] = validate_public_topic(topic)
    if include is not None:
        watch["include"] = validate_criteria(include)
    if exclude is not None:
        watch["exclude"] = validate_criteria(exclude)
    if scope is not None:
        watch["scope"] = _validate_scope(scope)
    if confidence is not None:
        confidence_text = _collapsed(confidence)
        if confidence_text not in VALID_CONFIDENCE:
            raise WatchError("confidence must be low|medium|high")
        watch["confidence"] = confidence_text
    if expires_at is not None:
        watch["expiry"] = _validate_expiry(expires_at, now)
    if provenance is not None:
        # Preserve the explicit trusted provenance of the latest revision.
        watch["provenance"] = _require_provenance(provenance)
    # An explicit revision supersedes the old plan: retire cached queries.
    watch["version"] = int(watch.get("version", 1)) + 1
    watch["plan"] = None
    watch["plan_version"] = 0
    watch["plan_at"] = ""
    watch["status"] = "active"
    watch["updated_at"] = now.isoformat()
    data["updated_at"] = now.isoformat()
    data["version"] = int(data.get("version", 0)) + 1
    return watch


def stop_watch(data: dict[str, Any], watch_id: str, *, now: datetime) -> dict[str, Any]:
    watch = data.get("watches", {}).get(str(watch_id))
    if not isinstance(watch, dict):
        raise WatchError(f"no watch with id {watch_id!r}")
    watch["status"] = "stopped"
    watch["plan"] = None
    watch["plan_version"] = 0
    watch["plan_at"] = ""
    watch["updated_at"] = now.isoformat()
    data["updated_at"] = now.isoformat()
    data["version"] = int(data.get("version", 0)) + 1
    return watch


def snooze_watch(
    data: dict[str, Any],
    watch_id: str,
    *,
    until: Any = "",
    minutes: int | None = None,
    now: datetime,
) -> dict[str, Any]:
    watch = data.get("watches", {}).get(str(watch_id))
    if not isinstance(watch, dict):
        raise WatchError(f"no watch with id {watch_id!r}")
    if minutes is not None:
        try:
            span = int(minutes)
        except (TypeError, ValueError) as exc:
            raise WatchError("minutes must be an integer") from exc
        if span <= 0 or span > 60 * 24 * 14:
            raise WatchError("minutes out of range (1..20160)")
        from datetime import timedelta

        watch["snoozed_until"] = (now + timedelta(minutes=span)).isoformat()
    else:
        watch["snoozed_until"] = _validate_expiry(until, now)
    watch["updated_at"] = now.isoformat()
    data["updated_at"] = now.isoformat()
    data["version"] = int(data.get("version", 0)) + 1
    return watch


def forget_watch(data: dict[str, Any], watch_id: str, *, now: datetime) -> dict[str, Any]:
    wid = str(watch_id)
    if wid not in data.get("watches", {}):
        raise WatchError(f"no watch with id {watch_id!r}")
    removed = data["watches"].pop(wid)
    data["updated_at"] = now.isoformat()
    data["version"] = int(data.get("version", 0)) + 1
    return removed


def apply_action(store: Any, action: dict[str, Any], now: datetime, *, lock: bool = True) -> dict[str, Any]:
    """Load, mutate under the shared lock, persist. Fail closed on bad input."""
    name = str(action.get("action") or "")

    def _mutate(data: dict[str, Any]) -> Any:
        if name == "add":
            return add_watch(
                data,
                topic=action.get("public_topic"),
                include=action.get("include"),
                exclude=action.get("exclude"),
                scope=action.get("scope") or "public",
                provenance=action.get("provenance"),
                confidence=action.get("confidence") or "medium",
                expires_at=action.get("expires_at"),
                now=now,
            )
        if name == "change":
            kwargs: dict[str, Any] = {}
            if action.get("public_topic") not in (None, ""):
                kwargs["topic"] = action.get("public_topic")
            for key in ("include", "exclude", "scope", "confidence", "expires_at"):
                if action.get(key) not in (None, ""):
                    kwargs[key] = action.get(key)
            if action.get("provenance"):
                kwargs["provenance"] = action.get("provenance")
            return change_watch(data, action.get("id", ""), now=now, **kwargs)
        if name == "stop":
            return stop_watch(data, action.get("id", ""), now=now)
        if name == "snooze":
            return snooze_watch(data, action.get("id", ""), until=action.get("until", ""),
                                minutes=action.get("minutes"), now=now)
        if name == "forget":
            return forget_watch(data, action.get("id", ""), now=now)
        if name == "list":
            return None
        raise WatchError(f"unknown watch action: {name!r}")

    def _retire(action_name: str, result_watch: Any) -> None:
        # A retired watch must not leave stale adaptive evidence/cache behind
        # that a later run could replay. Removal happens only for stop/forget.
        if action_name not in {"stop", "forget"}:
            return
        wid = str((result_watch or {}).get("id") if isinstance(result_watch, dict)
                  else action.get("id") or "")
        if not wid:
            return
        try:
            from .adaptive import purge_watch_evidence
            purge_watch_evidence(store, wid)
        except Exception:  # noqa: BLE001 - evidence cleanup never blocks lifecycle
            pass

    if lock:
        with watch_lock(store) as acquired:
            if not acquired:
                raise WatchError("watch store is locked by another writer")
            data = load_watchstore(store, strict=True)
            result = _mutate(data)
            save_watchstore(store, data)
            _retire(name, result)
            return result or data
    data = load_watchstore(store, strict=True)
    result = _mutate(data)
    save_watchstore(store, data)
    _retire(name, result)
    return result or data


# --------------------------------------------------------------------------- #
# Typed CLI
# --------------------------------------------------------------------------- #


def watch_command(args: Any) -> int:
    """Typed watch lifecycle CLI used by ``main``. Writes on valid input only."""
    from .clock import Clock, SeededClock

    clock = SeededClock(args.seeded_clock) if getattr(args, "seeded_clock", None) else Clock()
    now = clock.now()
    from .state import StateStore

    store = StateStore(Path(args.state_dir), clock)
    action = str(getattr(args, "feedback", "") or "")
    try:
        if action == "watch-list":
            payload = load_watchstore(store)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if action == "watch-feedback":
            # Benign acknowledgements ("silent"/"read"/"thanks") are NOT learning
            # signals: they never write a preference or change acquisition.
            note = str(getattr(args, "note", "") or "").strip().lower()
            if note in {"silent", "read", "thanks", "thank you", "ok"}:
                print(json.dumps({"feedback": action, "status": "no-op",
                                  "reason": "acknowledgement is not a preference"}, indent=2))
                return 0
            # A real correction is applied as a query/topic revision below.
            raise WatchError("watch-feedback requires a correction (topic/criteria)")
        mapped = {
            "watch-add": "add",
            "watch-change": "change",
            "watch-stop": "stop",
            "watch-snooze": "snooze",
            "watch-forget": "forget",
        }
        if action not in mapped:
            raise WatchError(f"unknown watch action: {action!r}")
        result = apply_action(store, {
            "action": mapped[action],
            "id": getattr(args, "id", ""),
            "public_topic": getattr(args, "watch_topic", ""),
            "include": getattr(args, "include", ""),
            "exclude": getattr(args, "exclude", ""),
            "scope": getattr(args, "scope", ""),
            "provenance": getattr(args, "provenance", ""),
            "confidence": getattr(args, "confidence", ""),
            "expires_at": getattr(args, "expires_at", ""),
            "until": getattr(args, "until", ""),
            "minutes": getattr(args, "minutes", None),
        }, now)
    except WatchError as exc:
        print(f"proactive-secretary watch rejected: {exc}", file=sys.stderr)
        return 2
    except CorruptStateError as exc:
        print(f"proactive-secretary watch state corrupt, failing closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "watch": action,
        "status": "saved",
        "id": result.get("id"),
        "version": result.get("version"),
        "updated_at": result.get("updated_at"),
    }, indent=2))
    return 0
