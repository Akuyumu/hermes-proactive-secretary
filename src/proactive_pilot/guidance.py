"""Deterministic owner-guidance closures for the notice lane.

A prompt can ask the wake to stop raising a topic; it cannot stop a changed
countdown or a fresh event key from being emitted. This module is the
deterministic half: a small owner-only register of closed topics, consulted at
the notice boundary before anything is queued or emitted.

Matching is exact — a normalised label equality or an exact key equality. Never
substring, token overlap or "looks similar": an unseen paraphrase is simply not
covered, and saying so is better than pretending otherwise.

A closure is finite by default and expires. Permanent suppression must be asked
for explicitly. Closing a topic removes discretionary updates only; it never
cancels an explicitly requested reminder and never grants authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 1
STATE_DIR = "guidance"
STATE_NAME = "topics.json"
DEFAULT_DAYS = 7
MAX_ENTRIES = 200
_NORMALISE = re.compile(r"[^a-z0-9]+")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalise(value: Any) -> str:
    """Case-folded, whitespace-collapsed form used for exact label comparison.

    Punctuation is preserved on purpose: stripping it makes ``C++`` and ``C#``
    collide, and dropping non-ASCII makes a non-English label unmatchable.
    """
    return " ".join(str(value or "").casefold().split())


def store_path(root: Any) -> Path:
    return Path(root) / STATE_DIR / STATE_NAME


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "entries": [], "revision": 0}


def read_store(root: Any) -> dict[str, Any]:
    if root is None:
        return {"ok": False, "error": "guidance store root is unavailable"}
    path = store_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**_empty(), "ok": True, "present": False}
    except (OSError, UnicodeError) as exc:
        return {"ok": False, "error": f"guidance store unreadable: {type(exc).__name__}"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "guidance store is not valid JSON"}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {"ok": False, "error": "guidance store has an unknown schema"}
    entries = data.get("entries")
    if not isinstance(entries, list):
        return {"ok": False, "error": "guidance store has no entry list"}
    try:
        revision = int(data.get("revision") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "guidance store has an invalid revision"}
    return {"ok": True, "present": True, "schema": SCHEMA, "entries": entries,
            "revision": revision}


def _write_store(root: Any, store: dict[str, Any]) -> None:
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"schema": SCHEMA, "entries": store["entries"],
               "revision": int(store.get("revision") or 0) + 1}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".guidance-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry_id(labels: Iterable[str], keys: Iterable[str]) -> str:
    raw = "|".join(sorted([normalise(x) for x in labels]) + sorted(str(k) for k in keys))
    return "gd-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _parse_until(until: Any, *, permanent: bool, now: datetime) -> tuple[str, str]:
    if permanent:
        return "", ""
    if until in (None, ""):
        return _iso(now + timedelta(days=DEFAULT_DAYS)), ""
    if isinstance(until, int) and not isinstance(until, bool):
        if until < 1 or until > 365:
            return "", "days must be between 1 and 365"
        return _iso(now + timedelta(days=until)), ""
    parsed = _parse_iso(until)
    if parsed is None:
        return "", "until must be an ISO date/time or a number of days"
    if parsed <= now:
        return "", "until must be in the future"
    return _iso(parsed), ""


def _entry_labels_norm(entry: dict[str, Any]) -> list[str]:
    """Normalise from the stored originals, never from a cached list.

    The cached ``labels_norm`` can be stale after a normalisation change; deriving
    it here keeps older closures matching their original spelling.
    """
    labels = entry.get("labels")
    if not isinstance(labels, list):
        return []
    return sorted({normalise(label) for label in labels if normalise(label)})


def _valid_entry(entry: Any) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("labels"), list)
            and isinstance(entry.get("keys", []), list)
            and entry.get("action") in ("close", "watch")
            and bool(entry.get("id")))


def _active(entry: dict[str, Any], now: datetime) -> bool:
    if entry.get("permanent"):
        return True
    until_raw = entry.get("until")
    if until_raw in (None, ""):
        return True
    parsed = _parse_iso(until_raw)
    if parsed is None:
        # A malformed expiry is not "forever"; treat it as inactive everywhere.
        return False
    return parsed > now


def list_active(root: Any, now: datetime | None = None) -> dict[str, Any]:
    store = read_store(root)
    if not store.get("ok"):
        return store
    moment = now or _now()
    active = [entry for entry in store["entries"]
              if _valid_entry(entry) and _active(entry, moment)]
    return {"ok": True, "present": store.get("present", False),
            "revision": int(store.get("revision") or 0), "entries": active,
            "count": len(active)}


def close(root: Any, labels: Iterable[str], *, keys: Iterable[str] = (),
          until: Any = None, permanent: bool = False, source: str = "",
          now: datetime | None = None) -> dict[str, Any]:
    """Close a topic. Labels are normalised; keys are matched exactly."""
    moment = now or _now()
    clean_labels = sorted({str(label).strip() for label in labels if str(label).strip()})
    clean_keys = sorted({str(key).strip() for key in keys if str(key).strip()})
    if not clean_labels and not clean_keys:
        return {"ok": False, "status": "refused",
                "error": "a topic label or at least one stable key is required"}
    if any(not normalise(label) for label in clean_labels):
        return {"ok": False, "status": "refused",
                "error": "a topic label is empty after normalising"}
    until_iso, error = _parse_until(until, permanent=permanent, now=moment)
    if error:
        return {"ok": False, "status": "refused", "error": error}
    store = read_store(root)
    if not store.get("ok"):
        return {"ok": False, "status": "refused", "error": store.get("error")}
    entry = {
        "id": _entry_id(clean_labels, clean_keys),
        "labels": clean_labels,
        "labels_norm": sorted({normalise(label) for label in clean_labels}),
        "keys": clean_keys,
        "action": "close",
        "scope": "discretionary",
        "since": _iso(moment),
        "until": until_iso,
        "permanent": bool(permanent),
        "source": str(source or "owner")[:80],
    }
    entries = [existing for existing in store["entries"]
               if not (isinstance(existing, dict) and existing.get("id") == entry["id"])]
    entries.append(entry)
    store["entries"] = entries[-MAX_ENTRIES:]
    try:
        _write_store(root, store)
    except OSError as exc:
        return {"ok": False, "status": "error",
                "error": f"guidance store write failed ({type(exc).__name__})"}
    return {"ok": True, "status": "closed", "entry": entry}


def reopen(root: Any, entry_id: str) -> dict[str, Any]:
    store = read_store(root)
    if not store.get("ok"):
        return {"ok": False, "status": "refused", "error": store.get("error")}
    kept = [entry for entry in store["entries"]
            if not (isinstance(entry, dict) and entry.get("id") == entry_id)]
    if len(kept) == len(store["entries"]):
        return {"ok": False, "status": "missing", "error": "no such guidance entry"}
    store["entries"] = kept
    _write_store(root, store)
    return {"ok": True, "status": "reopened", "id": entry_id}


def matching_entry(root: Any, *, labels: Iterable[Any] = (), keys: Iterable[Any] = (),
                   now: datetime | None = None) -> dict[str, Any] | None:
    """Return the active closure that matches exactly, or None."""
    moment = now or _now()
    wanted_labels = {normalise(label) for label in labels if normalise(label)}
    wanted_keys = {str(key).strip() for key in keys if str(key).strip()}
    if not wanted_labels and not wanted_keys:
        return None
    store = read_store(root)
    if not store.get("ok"):
        return None
    for entry in store.get("entries", []):
        if not _valid_entry(entry) or entry.get("action") != "close":
            continue
        if not _active(entry, moment):
            continue
        if wanted_keys and wanted_keys.intersection(entry.get("keys") or []):
            return entry
        if wanted_labels and wanted_labels.intersection(_entry_labels_norm(entry)):
            return entry
    return None


def suppressed(root: Any, *, labels: Iterable[Any] = (), keys: Iterable[Any] = (),
               now: datetime | None = None) -> str:
    """Empty when allowed, an entry id when closed, ``unavailable`` on read error.

    ``unavailable`` is deliberately distinguishable from "no match": the notice
    lane must fail closed rather than quietly treat a broken store as permission.
    """
    if root is None:
        return "unavailable"
    store = read_store(root)
    if not store.get("ok"):
        return "unavailable"
    entry = matching_entry(root, labels=labels, keys=keys, now=now)
    return str(entry.get("id") or "") if entry else ""
