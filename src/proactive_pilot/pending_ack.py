"""Durable proposals that wait for one explicit owner confirmation.

A proposal is not an acknowledgement: it stores only the exact effect the
owner will be asked to confirm. Confirmation consumes the stored row once and
keeps a bounded receipt, so a retried turn returns the same result instead of
applying a second effect.

The same owner-only artifact carries durable stop/done dispositions. They are
exposed through :func:`dispositions` so the follow-through seeder can honour an
owner stop that happened before a reminder's follow-up row existed. This module
does not read the reminder registry, send anything, call a model or touch the
network.
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .clock import Clock
from .state import CorruptStateError, StateStore

STORE = "pending_ack.json"
LOCK = "pending_ack.lock"
SCHEMA = 1
MAX_ROWS = 20
DEFAULT_TTL_SECONDS = 10 * 60
MAX_ID_CHARS = 80
MAX_CONVERSATION_CHARS = 200
MAX_OWNER_CHARS = 200
MAX_TARGET_ID_CHARS = 240
MAX_LABEL_CHARS = 240
MAX_QUESTION_CHARS = 480
MAX_KIND_CHARS = 64
MAX_SCOPE_CHARS = 240
MAX_TURN_CHARS = 128
MAX_DISPOSITIONS = 200

RECEIPT_FIELDS = frozenset({
    "ok", "status", "action", "minutes", "until", "target_label", "effect",
    "note", "no_further_followup", "confirmed_ref", "turn_id",
})
DISPOSITION_FIELDS = frozenset({
    "action", "target_id", "target_label", "at", "turn_id", "pending_id",
    "note", "source",
})
_EFFECT_FIELDS = frozenset({"action", "minutes", "until", "kind", "scope"})
_ROW_FIELDS = frozenset({
    "id", "conversation", "owner", "target_id", "target_label", "effect",
    "question", "created_at", "expires_at", "consumed_at", "receipt", "turn_id",
})
_ACTIONS = frozenset({"done", "snooze", "stop"})


class PendingAckError(ValueError):
    """A pending acknowledgement cannot be read or written safely."""


def empty_store() -> dict[str, Any]:
    return {"schema": SCHEMA, "version": 0, "updated_at": "", "rows": [],
            "dispositions": {}}


def _coerce_store(value: Any = None) -> StateStore:
    if all(hasattr(value, name) for name in ("load", "save", "lock", "root", "clock")):
        return value
    if value is None:
        try:
            from .. import paths
        except ImportError:  # pragma: no cover - direct module execution
            import paths  # type: ignore[no-redef]
        return StateStore(Path(paths.state_dir()), Clock())
    return StateStore(Path(value), Clock())


def _now(store: Any, now: datetime | None) -> datetime:
    return now if now is not None else store.clock.now()


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PendingAckError(f"{name} must be a string")
    text = " ".join(value.split())
    if required and not text:
        raise PendingAckError(f"{name} is required")
    if len(text) > limit:
        raise PendingAckError(f"{name} is too long (max {limit} chars)")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise PendingAckError(f"{name} contains control characters")
    return text


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PendingAckError(f"{name} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PendingAckError(f"{name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PendingAckError(f"{name} is not timezone-aware")
    return parsed


def _optional_iso(value: Any, name: str) -> str:
    if value in (None, ""):
        return ""
    _parse_iso(value, name)
    return str(value)


def _validate_effect(effect: Any) -> None:
    if not isinstance(effect, dict) or set(effect) - _EFFECT_FIELDS:
        raise PendingAckError("pending effect is malformed")
    action = effect.get("action")
    if action not in _ACTIONS:
        raise PendingAckError("pending effect action is invalid")
    _text(effect.get("kind", ""), "effect.kind", MAX_KIND_CHARS)
    _text(effect.get("scope", ""), "effect.scope", MAX_SCOPE_CHARS)
    if action == "snooze":
        minutes = effect.get("minutes")
        until = effect.get("until")
        if (minutes is None) == (until in (None, "")):
            raise PendingAckError("a snooze needs exactly one duration")
        if minutes is not None:
            if not isinstance(minutes, int) or isinstance(minutes, bool) \
                    or minutes <= 0 or minutes > 60 * 24 * 14:
                raise PendingAckError("pending snooze minutes are out of range")
        else:
            _parse_iso(until, "effect.until")
    else:
        if effect.get("minutes") is not None or effect.get("until") not in (None, ""):
            raise PendingAckError("this effect cannot carry a duration")


def _validate_receipt(receipt: Any) -> None:
    if not isinstance(receipt, dict):
        raise PendingAckError("pending receipt is malformed")
    if set(receipt) - RECEIPT_FIELDS:
        raise PendingAckError("pending receipt contains a non-allowlisted field")
    for key, value in receipt.items():
        if key == "effect":
            if not isinstance(value, dict) or set(value) - _EFFECT_FIELDS:
                raise PendingAckError("pending receipt effect is malformed")
        elif key == "minutes":
            if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise PendingAckError("pending receipt minutes are invalid")
        elif key == "no_further_followup":
            if not isinstance(value, bool):
                raise PendingAckError("pending receipt flag is invalid")
        elif key == "ok":
            if not isinstance(value, bool):
                raise PendingAckError("pending receipt ok flag is invalid")
        else:
            _text(value, f"receipt.{key}", 480)


def _validate_row(row: Any) -> None:
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        raise PendingAckError("pending row shape is invalid")
    _text(row.get("id"), "id", MAX_ID_CHARS, required=True)
    _text(row.get("conversation"), "conversation", MAX_CONVERSATION_CHARS,
          required=True)
    _text(row.get("owner"), "owner", MAX_OWNER_CHARS)
    _text(row.get("target_id"), "target_id", MAX_TARGET_ID_CHARS, required=True)
    _text(row.get("target_label"), "target_label", MAX_LABEL_CHARS, required=True)
    _validate_effect(row.get("effect"))
    _text(row.get("question"), "question", MAX_QUESTION_CHARS, required=True)
    _parse_iso(row.get("created_at"), "created_at")
    _parse_iso(row.get("expires_at"), "expires_at")
    _optional_iso(row.get("consumed_at"), "consumed_at")
    _validate_receipt(row.get("receipt", {}))
    _text(row.get("turn_id"), "turn_id", MAX_TURN_CHARS, required=True)


def _validate_disposition(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - DISPOSITION_FIELDS:
        raise PendingAckError("pending disposition is malformed")
    for key, item in value.items():
        if key == "no_further_followup":
            if not isinstance(item, bool):
                raise PendingAckError("pending disposition flag is invalid")
        else:
            _text(item, f"disposition.{key}", 480)
    _text(value.get("action"), "disposition.action", 32, required=True)


def _validate_store(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise PendingAckError("pending_ack.json has an unsupported schema")
    if set(data) - {"schema", "version", "updated_at", "rows", "dispositions"}:
        raise PendingAckError("pending_ack.json has an unknown field")
    version = data.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PendingAckError("pending_ack.json has an invalid version")
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise PendingAckError("pending_ack.json rows are malformed or oversized")
    for row in rows:
        _validate_row(row)
    dispositions = data.get("dispositions", {})
    if not isinstance(dispositions, dict) or len(dispositions) > MAX_DISPOSITIONS:
        raise PendingAckError("pending_ack.json dispositions are malformed or oversized")
    for key, value in dispositions.items():
        _text(key, "disposition key", MAX_TARGET_ID_CHARS, required=True)
        _validate_disposition(value)
    if not isinstance(data.get("updated_at", ""), str):
        raise PendingAckError("pending_ack.json has an invalid timestamp")
    data.setdefault("dispositions", {})
    data.setdefault("updated_at", "")
    return data


def _load(store: Any) -> dict[str, Any]:
    try:
        data = store.load(STORE, empty_store(), strict=True)
    except CorruptStateError as exc:
        raise PendingAckError("pending acknowledgement state is corrupt") from exc
    return _validate_store(data)


@contextmanager
def _locked(store: Any):
    with store.lock(LOCK) as acquired:
        if not acquired:
            raise PendingAckError("pending acknowledgement store is locked")
        yield


def _save(store: Any, data: dict[str, Any], now: datetime) -> None:
    data["schema"] = SCHEMA
    data["version"] = int(data.get("version", 0)) + 1
    data["updated_at"] = now.isoformat()
    store.save(STORE, data)


def _new_id(data: dict[str, Any]) -> str:
    used = {str(row.get("id") or "") for row in data.get("rows", [])
            if isinstance(row, dict)}
    while True:
        candidate = "ack-" + secrets.token_hex(8)
        if candidate not in used:
            return candidate


def _row_expired(row: Mapping[str, Any], now: datetime) -> bool:
    try:
        return _parse_iso(row.get("expires_at"), "expires_at") <= now
    except PendingAckError:
        return True


def _row_consumed(row: Mapping[str, Any]) -> bool:
    return bool(str(row.get("consumed_at") or "").strip())


def _same_owner(row: Mapping[str, Any], conversation: str, owner: str) -> bool:
    return (str(row.get("conversation") or "") == conversation
            and str(row.get("owner") or "") == owner)


def rows(store: Any = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    target = _coerce_store(store)
    data = _load(target)
    return [dict(row) for row in data["rows"]]


def get(store: Any = None, row_id: Any = "", *,
        now: datetime | None = None) -> dict[str, Any] | None:
    wanted = str(row_id or "").strip()
    if not wanted:
        return None
    for row in rows(store, now=now):
        if str(row.get("id") or "") == wanted:
            return row
    return None


def live_for(store: Any = None, *, conversation: Any, owner: Any,
             now: datetime | None = None) -> list[dict[str, Any]]:
    target = _coerce_store(store)
    moment = _now(target, now)
    conversation_text = str(conversation or "")
    owner_text = str(owner or "")
    live = [row for row in rows(target, now=moment)
            if _same_owner(row, conversation_text, owner_text)
            and not _row_consumed(row) and not _row_expired(row, moment)]
    live.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return live


def find_pin(store: Any = None, *, conversation: Any, owner: Any,
             now: datetime | None = None) -> dict[str, Any] | None:
    """The newest unexpired, unconsumed proposal for this conversation/owner."""
    live = live_for(store, conversation=conversation, owner=owner, now=now)
    return live[0] if live else None


def lookup(store: Any = None, *, conversation: Any, owner: Any, turn_id: Any,
           now: datetime | None = None) -> tuple[str, dict[str, Any] | None]:
    """Classify the pending state for one current owner turn."""
    target = _coerce_store(store)
    moment = _now(target, now)
    conversation_text = str(conversation or "")
    owner_text = str(owner or "")
    wanted_turn = str(turn_id or "")
    database = _load(target)
    same = [dict(row) for row in database["rows"]
            if _same_owner(row, conversation_text, owner_text)]
    for row in reversed(same):
        if _row_consumed(row) and str((row.get("receipt") or {}).get("turn_id") or "") == wanted_turn:
            return "retry", row
    live = [row for row in same if not _row_consumed(row) and not _row_expired(row, moment)]
    live.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    if live:
        return ("ambiguous" if len(live) > 1 else "pending"), live[0]
    expired = [row for row in same if not _row_consumed(row) and _row_expired(row, moment)]
    if expired:
        expired.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return "expired", expired[0]
    consumed = [row for row in same if _row_consumed(row)]
    if consumed:
        consumed.sort(key=lambda row: str(row.get("consumed_at") or ""), reverse=True)
        return "consumed", consumed[0]
    return "missing", None


def create(
    *,
    conversation: Any,
    owner: Any,
    target_id: Any,
    target_label: Any,
    effect: Any,
    question: Any,
    turn_id: Any,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    store: Any = None,
) -> dict[str, Any]:
    """Store one bounded proposal. Writes no acknowledgement state."""
    target = _coerce_store(store)
    moment = _now(target, now)
    try:
        row = {
            "id": "",
            "conversation": _text(conversation, "conversation",
                                  MAX_CONVERSATION_CHARS, required=True),
            "owner": _text(owner, "owner", MAX_OWNER_CHARS),
            "target_id": _text(target_id, "target_id", MAX_TARGET_ID_CHARS,
                               required=True),
            "target_label": _text(target_label, "target_label", MAX_LABEL_CHARS,
                                  required=True),
            "effect": dict(effect or {}),
            "question": _text(question, "question", MAX_QUESTION_CHARS,
                              required=True),
            "created_at": moment.isoformat(),
            "expires_at": (moment + timedelta(
                seconds=max(1, min(int(ttl_seconds), DEFAULT_TTL_SECONDS)))).isoformat(),
            "consumed_at": "",
            "receipt": {},
            "turn_id": _text(turn_id, "turn_id", MAX_TURN_CHARS, required=True),
        }
        _validate_effect(row["effect"])
        with _locked(target):
            data = _load(target)
            row["id"] = _new_id(data)
            data["rows"] = [
                existing for existing in data["rows"]
                if not (_same_owner(existing, row["conversation"], row["owner"])
                        and str(existing.get("turn_id") or "") == row["turn_id"]
                        and not _row_consumed(existing))
            ]
            _validate_row(row)
            data["rows"].append(row)
            data["rows"] = data["rows"][-MAX_ROWS:]
            _save(target, data, moment)
    except (PendingAckError, CorruptStateError) as exc:
        return {"ok": False, "status": "refused",
                "reason": f"pending acknowledgement refused: {exc}"}
    return {"ok": True, "status": "pending", "pending": dict(row)}


def _receipt_only(value: Any, *, turn_id: str) -> dict[str, Any]:
    candidate = dict(value) if isinstance(value, Mapping) else {}
    receipt = {key: candidate[key] for key in RECEIPT_FIELDS if key in candidate}
    receipt["turn_id"] = turn_id
    if "effect" in receipt and isinstance(receipt["effect"], Mapping):
        receipt["effect"] = {key: receipt["effect"][key]
                             for key in _EFFECT_FIELDS if key in receipt["effect"]}
    _validate_receipt(receipt)
    return receipt


def commit(
    row_id: Any,
    apply_fn: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    conversation: Any,
    owner: Any,
    turn_id: Any,
    now: datetime | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Apply the stored effect exactly once under the pending-store lock."""
    target = _coerce_store(store)
    moment = _now(target, now)
    wanted = str(row_id or "").strip()
    conversation_text = str(conversation or "")
    owner_text = str(owner or "")
    turn_text = str(turn_id or "")
    try:
        with _locked(target):
            data = _load(target)
            row = next((item for item in data["rows"]
                        if str(item.get("id") or "") == wanted), None)
            if row is None:
                return {"ok": False, "status": "refused",
                        "reason": "that confirmation is no longer available"}
            if not _same_owner(row, conversation_text, owner_text):
                return {"ok": False, "status": "refused",
                        "reason": "that confirmation belongs to another conversation"}
            if _row_consumed(row):
                return {"ok": True, "status": "already_confirmed",
                        "receipt": dict(row.get("receipt") or {}),
                        "pending": dict(row)}
            if _row_expired(row, moment):
                return {"ok": False, "status": "refused",
                        "reason": "that confirmation expired; ask again"}
            result = apply_fn(dict(row))
            deferred = result.pop("deferred_disposition", None)
            if not result.get("ok"):
                return result
            if isinstance(deferred, Mapping):
                key = _text(deferred.get("key"), "disposition key",
                            MAX_TARGET_ID_CHARS, required=True)
                record = dict(deferred.get("record") or {})
                record.setdefault("at", moment.isoformat())
                _validate_disposition(record)
                data["dispositions"][key] = record
                while len(data["dispositions"]) > MAX_DISPOSITIONS:
                    data["dispositions"].pop(next(iter(data["dispositions"])))
            receipt = _receipt_only(result.get("receipt") or result,
                                    turn_id=turn_text)
            row["consumed_at"] = moment.isoformat()
            row["receipt"] = receipt
            _save(target, data, moment)
            return {"ok": True, "status": "confirmed", "receipt": receipt,
                    "pending": dict(row)}
    except (PendingAckError, CorruptStateError) as exc:
        return {"ok": False, "status": "refused",
                "reason": f"pending acknowledgement refused: {exc}"}


def record_disposition(
    key: Any,
    record: Any,
    *,
    now: datetime | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Keep one durable stop/done disposition for a target (e.g. a reminder)."""
    target = _coerce_store(store)
    moment = _now(target, now)
    clean_key = _text(key, "disposition key", MAX_TARGET_ID_CHARS, required=True)
    row = dict(record or {})
    row.setdefault("at", moment.isoformat())
    _validate_disposition(row)
    try:
        with _locked(target):
            data = _load(target)
            data["dispositions"][clean_key] = row
            while len(data["dispositions"]) > MAX_DISPOSITIONS:
                data["dispositions"].pop(next(iter(data["dispositions"])))
            _save(target, data, moment)
    except (PendingAckError, CorruptStateError) as exc:
        raise PendingAckError(f"disposition refused: {exc}") from exc
    return dict(row)


def dispositions(store: Any = None, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Return the durable stop/done map exposed to the stage-B seeder."""
    target = _coerce_store(store)
    database = _load(target)
    return {str(key): dict(value)
            for key, value in (database.get("dispositions") or {}).items()}


__all__ = [
    "STORE", "LOCK", "SCHEMA", "MAX_ROWS", "DEFAULT_TTL_SECONDS",
    "RECEIPT_FIELDS", "PendingAckError", "empty_store", "rows", "get",
    "live_for", "find_pin", "lookup", "create", "commit",
    "record_disposition", "dispositions",
]
