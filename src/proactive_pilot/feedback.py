"""Schema-validated feedback -> preference updates.

The user (or the normal assistant on their behalf) records feedback here. This
module never touches sources, models, or the network: it only mutates the local
preferences artifact atomically, under an owner-only single-writer lock.

Design rules (brief):
- Explicit rules may suppress; inferred rules are advisory only.
- Conditional explicit rules (e.g. "career updates unless exceptionally
  fitting") are recorded but NEVER blanket-suppress; the evaluator decides fit.
- "not today" is a scoped, temporary snooze, never a durable suppression.
- Pause/resume is global and reversible.
- Bad input fails closed: nothing is written and a non-zero exit is returned.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .state import _atomic_write, file_lock

PREF_SCHEMA = 1
VALID_KINDS = {"calendar", "tasks", "mail", "feed", "obligation", "any"}
VALID_CONFIDENCE = {"low", "medium", "high"}
BLANKET_MATCHES = {"", "*", "all", "everything", "any"}
SNOOZE_WORDS = {"not today", "later", "not now"}

ACTIONS = {
    "add-explicit", "add-inferred", "snooze", "pause", "resume", "forget", "list",
}


class FeedbackError(ValueError):
    """Invalid feedback action; caller must fail closed."""


def empty_preferences() -> dict[str, Any]:
    return {
        "schema": PREF_SCHEMA,
        "version": 0,
        "updated_at": "",
        "explicit": [],
        "inferred": [],
        "snoozes": [],
        "paused": {"active": False},
    }


def normalize(prefs: dict[str, Any] | None) -> dict[str, Any]:
    base = empty_preferences()
    if not isinstance(prefs, dict):
        return base
    base.update({k: v for k, v in prefs.items() if k in base})
    for key in ("explicit", "inferred", "snoozes"):
        if not isinstance(base.get(key), list):
            base[key] = []
    if not isinstance(base.get("paused"), dict):
        base["paused"] = {"active": False}
    if not isinstance(base.get("version"), int):
        base["version"] = 0
    return base


def _require(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise FeedbackError(f"{name} is required")
    return text


def _parse_until(value: Any, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise FeedbackError(f"invalid until timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise FeedbackError("until must be timezone-aware")
    if parsed <= now:
        raise FeedbackError("until must be in the future")
    return parsed


def _new_id(action: dict[str, Any], now: datetime) -> str:
    raw = json.dumps(action, sort_keys=True) + now.isoformat()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def next_local_day(now: datetime) -> datetime:
    """Start of the next local day (used by the 'not today' snooze)."""
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def validate_action(action: dict[str, Any], now: datetime) -> None:
    name = action.get("action")
    if name not in ACTIONS:
        raise FeedbackError(f"unknown feedback action: {name!r}")
    if name == "list":
        return
    if name == "resume":
        return
    if name == "add-explicit":
        kind = _require(action.get("kind"), "kind")
        if kind not in VALID_KINDS:
            raise FeedbackError(f"invalid kind: {kind!r}")
        match = str(action.get("match") or "").lower().strip()
        if match in BLANKET_MATCHES:
            raise FeedbackError("refusing a blanket match; scope it to an explicit term")
        if match in SNOOZE_WORDS:
            raise FeedbackError("'not today'/'later' must use the snooze action, not suppression")
        _require(action.get("scope"), "scope")
        _require(action.get("provenance"), "provenance")
        if action.get("expires_at"):
            _parse_until(action["expires_at"], now)
        return
    if name == "add-inferred":
        kind = _require(action.get("kind"), "kind")
        if kind not in VALID_KINDS:
            raise FeedbackError(f"invalid kind: {kind!r}")
        match = str(action.get("match") or "").lower().strip()
        if match in BLANKET_MATCHES:
            raise FeedbackError("refusing a blanket inferred match")
        _require(action.get("provenance"), "provenance")
        if action.get("confidence") and action["confidence"] not in VALID_CONFIDENCE:
            raise FeedbackError("confidence must be low|medium|high")
        return
    if name == "snooze":
        if action.get("minutes") is not None:
            try:
                minutes = int(action["minutes"])
            except (TypeError, ValueError) as exc:
                raise FeedbackError("minutes must be an integer") from exc
            if minutes <= 0 or minutes > 60 * 24 * 14:
                raise FeedbackError("minutes out of range (1..20160)")
        else:
            _parse_until(action.get("until"), now)
        _require(action.get("scope"), "scope")
        _require(action.get("provenance"), "provenance")
        return
    if name == "pause":
        if action.get("until"):
            _parse_until(action["until"], now)
        _require(action.get("provenance"), "provenance")
        return
    if name == "forget":
        _require(action.get("id"), "id")
        return
    raise FeedbackError(f"unhandled action: {name!r}")


def apply_action(prefs: dict[str, Any], action: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return a new prefs dict with the validated action applied."""
    validate_action(action, now)
    result = normalize(json.loads(json.dumps(prefs)))
    name = action["action"]
    rid = _new_id(action, now)
    stamp = now.isoformat()
    if name == "list":
        return result
    if name == "add-explicit":
        result["explicit"].append({
            "id": rid,
            "kind": action["kind"],
            "match": str(action["match"]).lower().strip(),
            "suppress": bool(action.get("suppress", True)),
            "conditional": bool(action.get("conditional", False)),
            "exception": str(action.get("exception") or "")[:200],
            "scope": str(action["scope"])[:200],
            "provenance": str(action["provenance"])[:200],
            "created_at": stamp,
            "expires_at": action.get("expires_at") or "",
        })
    elif name == "add-inferred":
        result["inferred"].append({
            "id": rid,
            "kind": action["kind"],
            "match": str(action["match"]).lower().strip(),
            "note": str(action.get("note") or "")[:200],
            "confidence": action.get("confidence") or "low",
            "scope": str(action.get("scope") or "advisory")[:200],
            "provenance": str(action.get("provenance") or "inferred")[:200],
            "created_at": stamp,
        })
    elif name == "snooze":
        if action.get("minutes") is not None:
            until = (now + timedelta(minutes=int(action["minutes"]))).isoformat()
        else:
            until = _parse_until(action["until"], now).isoformat()
        target_keys = []
        for value in action.get("target_keys") or []:
            text = str(value or "").strip()
            if text and text not in target_keys and len(target_keys) < 8:
                target_keys.append(text[:240])
        result["snoozes"].append({
            "id": rid,
            "until": until,
            "kind": str(action.get("kind") or ""),
            "match": str(action.get("match") or "").lower().strip(),
            "scope": str(action["scope"])[:200],
            "target_id": str(action.get("target_id") or "")[:240],
            "target_keys": target_keys,
            "key": str(action.get("key") or "")[:128],
            "provenance": str(action["provenance"])[:200],
            "created_at": stamp,
        })
    elif name == "pause":
        result["paused"] = {
            "active": True,
            "until": action.get("until") or "",
            "scope": str(action.get("scope") or "global")[:200],
            "provenance": str(action["provenance"])[:200],
            "created_at": stamp,
        }
    elif name == "resume":
        result["paused"] = {"active": False, "provenance": str(action.get("provenance") or "user")[:200]}
    elif name == "forget":
        target = str(action["id"])
        found = False
        for key in ("explicit", "inferred", "snoozes"):
            kept = [r for r in result[key] if str(r.get("id")) != target]
            if len(kept) != len(result[key]):
                found = True
            result[key] = kept
        if not found:
            raise FeedbackError(f"no preference with id {target!r}")
    return result


def find_records(prefs: dict[str, Any], record_id: str) -> list[dict[str, Any]]:
    """Exact read-back of every record with ``record_id`` (typed CLI readback)."""
    found: list[dict[str, Any]] = []
    for key in ("explicit", "inferred", "snoozes"):
        for record in prefs.get(key, []) or []:
            if isinstance(record, dict) and str(record.get("id")) == str(record_id):
                found.append({"collection": key, **record})
    return found


def receipt_for_action(before: dict[str, Any], after: dict[str, Any],
                       action: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The exact record an action created/affected, for the CLI readback receipt.

    Pure read of already-persisted state: never writes and never invents a record.
    """
    name = action.get("action")
    if name in {"list", "show", "noop"}:
        return {}
    if name in {"pause", "resume"}:
        return {"paused": dict(after.get("paused") or {})}
    rid = _new_id(action, now)
    for record in find_records(after, rid):
        return record
    return {}


def load_preferences_file(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Load preferences; in strict mode a corrupt file raises instead of resetting."""
    if not path.exists():
        return empty_preferences()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if strict:
            raise FeedbackError(f"preferences corrupt: {exc}") from exc
        return empty_preferences()
    if not isinstance(data, dict) or data.get("schema") not in (None, PREF_SCHEMA):
        if strict:
            raise FeedbackError("preferences schema mismatch")
        return empty_preferences()
    return normalize(data)


def save_preferences_file(path: Path, prefs: dict[str, Any], now: datetime, *, lock: bool = True) -> dict[str, Any]:
    """Atomically persist preferences under an owner-only single-writer lock."""
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    updated = normalize(prefs)
    updated["updated_at"] = now.isoformat()
    updated["version"] = int(updated.get("version", 0)) + 1
    if lock:
        with file_lock(lock_path) as acquired:
            if not acquired:
                raise FeedbackError("preferences are locked by another writer")
            _atomic_write(path, updated)
    else:
        _atomic_write(path, updated)
    return updated
