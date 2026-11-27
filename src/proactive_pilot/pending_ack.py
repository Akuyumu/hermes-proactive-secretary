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
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .clock import Clock
from .state import CorruptStateError, StateStore

STORE = "pending_ack.json"
LOCK = "pending_ack.lock"
SCHEMA = 1
MAX_ROWS = 20
MAX_TURN_MARKS = 500
MAX_QUESTIONS = 20
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
MAX_TARGET_KEYS = 8
MAX_DISPOSITIONS = 200
MAX_QUESTION_LABELS = 3

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
    "id", "conversation", "owner", "target_id", "target_label", "target_keys",
    "effect", "question", "created_at", "expires_at", "consumed_at", "receipt",
    "turn_id", "key", "turn_ref", "turn_at", "intent_at", "intent_turn_id",
})
_TURN_FIELDS = frozenset({"mode", "at", "target_id", "key"})
_QUESTION_FIELDS = frozenset({
    "labels", "choices", "question", "at", "expires_at", "effect",
})
_CHOICE_FIELDS = frozenset({"position", "label", "target_id"})
_ACTIONS = frozenset({"done", "snooze", "stop"})
_STORE_FIELDS = frozenset({
    "schema", "version", "updated_at", "rows", "dispositions", "turns",
    "questions",
})


class PendingAckError(ValueError):
    """A pending acknowledgement cannot be read or written safely."""


def empty_store() -> dict[str, Any]:
    return {"schema": SCHEMA, "version": 0, "updated_at": "", "rows": [],
            "dispositions": {}, "turns": {}, "questions": {}}


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


def _upgrade_row(row: Any) -> Any:
    """Accept rows written before the turn/intent fields existed."""
    if isinstance(row, dict):
        for key in ("key", "turn_ref", "turn_at", "intent_at", "intent_turn_id"):
            row.setdefault(key, "")
        row.setdefault("target_keys", [])
    return row


def _validate_row(row: Any) -> None:
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        raise PendingAckError("pending row shape is invalid")
    _text(row.get("id"), "id", MAX_ID_CHARS, required=True)
    _text(row.get("conversation"), "conversation", MAX_CONVERSATION_CHARS,
          required=True)
    _text(row.get("owner"), "owner", MAX_OWNER_CHARS)
    _text(row.get("target_id"), "target_id", MAX_TARGET_ID_CHARS, required=True)
    _text(row.get("target_label"), "target_label", MAX_LABEL_CHARS, required=True)
    target_keys = row.get("target_keys")
    if not isinstance(target_keys, list) or len(target_keys) > MAX_TARGET_KEYS:
        raise PendingAckError("pending row target_keys are malformed")
    for key in target_keys:
        _text(key, "target_keys", MAX_TARGET_ID_CHARS, required=True)
    _validate_effect(row.get("effect"))
    _text(row.get("question"), "question", MAX_QUESTION_CHARS, required=True)
    _parse_iso(row.get("created_at"), "created_at")
    _parse_iso(row.get("expires_at"), "expires_at")
    _optional_iso(row.get("consumed_at"), "consumed_at")
    _validate_receipt(row.get("receipt", {}))
    _text(row.get("turn_id"), "turn_id", MAX_TURN_CHARS, required=True)
    _text(row.get("key"), "key", MAX_TURN_CHARS)
    _text(row.get("turn_ref"), "turn_ref", MAX_TURN_CHARS)
    _optional_iso(row.get("turn_at"), "turn_at")
    _optional_iso(row.get("intent_at"), "intent_at")
    _text(row.get("intent_turn_id"), "intent_turn_id", MAX_TURN_CHARS)


def _validate_turn_mark(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - _TURN_FIELDS:
        raise PendingAckError("pending turn mark is malformed")
    _text(value.get("mode"), "turn.mode", 32, required=True)
    _parse_iso(value.get("at"), "turn.at")
    _text(value.get("target_id", ""), "turn.target_id", MAX_TARGET_ID_CHARS)
    _text(value.get("key", ""), "turn.key", MAX_TURN_CHARS)


def _upgrade_question(value: Any) -> Any:
    """Accept question rows written before the offered choices were persisted."""
    if isinstance(value, dict):
        if "choices" not in value:
            value["choices"] = [
                {"position": index + 1, "label": label}
                for index, label in enumerate(value.get("labels") or [])
            ]
    return value


def _validate_question(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - _QUESTION_FIELDS:
        raise PendingAckError("pending question is malformed")
    labels = value.get("labels")
    if not isinstance(labels, list) or len(labels) > MAX_QUESTION_LABELS:
        raise PendingAckError("pending question labels are malformed")
    for label in labels:
        _text(label, "question.label", MAX_LABEL_CHARS, required=True)
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) > MAX_QUESTION_LABELS:
        raise PendingAckError("pending question choices are malformed")
    positions: set[int] = set()
    for choice in choices:
        if (not isinstance(choice, dict)
                or set(choice) - _CHOICE_FIELDS):
            raise PendingAckError("pending question choice is malformed")
        position = choice.get("position")
        if (not isinstance(position, int) or isinstance(position, bool)
                or position < 1 or position in positions):
            raise PendingAckError("pending question choice position is invalid")
        positions.add(position)
        _text(choice.get("label"), "question.choice.label",
              MAX_LABEL_CHARS, required=True)
        _text(choice.get("target_id", ""), "question.choice.target_id",
              MAX_TARGET_ID_CHARS)
    _text(value.get("question"), "question", MAX_QUESTION_CHARS, required=True)
    _parse_iso(value.get("at"), "question.at")
    _parse_iso(value.get("expires_at"), "question.expires_at")
    effect = value.get("effect")
    if effect not in (None, {}):
        _validate_effect(effect)


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
    if set(data) - _STORE_FIELDS:
        raise PendingAckError("pending_ack.json has an unknown field")
    version = data.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PendingAckError("pending_ack.json has an invalid version")
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise PendingAckError("pending_ack.json rows are malformed or oversized")
    for row in rows:
        _validate_row(_upgrade_row(row))
    dispositions = data.get("dispositions", {})
    if not isinstance(dispositions, dict) or len(dispositions) > MAX_DISPOSITIONS:
        raise PendingAckError("pending_ack.json dispositions are malformed or oversized")
    for key, value in dispositions.items():
        _text(key, "disposition key", MAX_TARGET_ID_CHARS, required=True)
        _validate_disposition(value)
    turns = data.get("turns", {})
    if not isinstance(turns, dict) or len(turns) > MAX_TURN_MARKS:
        raise PendingAckError("pending_ack.json turns are malformed or oversized")
    for key, value in turns.items():
        _text(key, "turn key", MAX_TURN_CHARS, required=True)
        _validate_turn_mark(value)
    questions = data.get("questions", {})
    if not isinstance(questions, dict) or len(questions) > MAX_QUESTIONS:
        raise PendingAckError("pending_ack.json questions are malformed or oversized")
    for key, value in questions.items():
        _text(key, "question key", MAX_CONVERSATION_CHARS * 2, required=True)
        _validate_question(_upgrade_question(value))
    if not isinstance(data.get("updated_at", ""), str):
        raise PendingAckError("pending_ack.json has an invalid timestamp")
    data.setdefault("dispositions", {})
    data.setdefault("turns", {})
    data.setdefault("questions", {})
    data.setdefault("updated_at", "")
    return data


def _load(store: Any) -> dict[str, Any]:
    try:
        data = store.load(STORE, empty_store(), strict=True)
    except CorruptStateError as exc:
        raise PendingAckError("pending acknowledgement state is corrupt") from exc
    return _validate_store(data)


@contextmanager
def _locked(store: Any, *, wait_seconds: float = 1.5):
    """Take the pending-store lock, waiting briefly for a concurrent writer.

    Two confirmations racing for the same pending row must not both write. The
    loser either sees the winner's consumed row (and its receipt) or refuses
    cleanly; it never proceeds on a stale copy.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        with store.lock(LOCK) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise PendingAckError("pending acknowledgement store is locked")
        time.sleep(0.02)


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


def _question_key(conversation: Any, owner: Any) -> str:
    return (f"{str(conversation or '')}|{str(owner or '')}"
            [:MAX_CONVERSATION_CHARS * 2])


def rows(store: Any = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    target = _coerce_store(store)
    data = _load(target)
    return [deepcopy(row) for row in data["rows"]]


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
    """The one live proposal for this conversation/owner, or ``None``.

    More than one live row is ambiguous: callers must ask, never pick the
    newest.
    """
    live = live_for(store, conversation=conversation, owner=owner, now=now)
    return live[0] if len(live) == 1 else None


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
    target_keys: Any = (),
    turn_id: Any,
    turn_ref: Any = "",
    turn_at: Any = "",
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    store: Any = None,
    consume_question: bool = True,
) -> dict[str, Any]:
    """Store one bounded proposal. Writes no acknowledgement state.

    A conversation may have only one live proposal. The effect is copied once at
    creation and never rewritten; a second proposal while one is live refuses.
    When ``consume_question`` is true the clarification question this proposal
    answered is removed in the same locked write, so it cannot be reopened.
    """
    target = _coerce_store(store)
    moment = _now(target, now)
    clean_target_keys = []
    for value in list(target_keys or ())[:MAX_TARGET_KEYS]:
        text = _text(value, "target_keys", MAX_TARGET_ID_CHARS, required=True)
        if text not in clean_target_keys:
            clean_target_keys.append(text)
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
            "target_keys": clean_target_keys,
            "effect": deepcopy(dict(effect or {})),
            "question": _text(question, "question", MAX_QUESTION_CHARS,
                              required=True),
            "created_at": moment.isoformat(),
            "expires_at": (moment + timedelta(
                seconds=max(1, min(int(ttl_seconds), DEFAULT_TTL_SECONDS)))).isoformat(),
            "consumed_at": "",
            "receipt": {},
            "turn_id": _text(turn_id, "turn_id", MAX_TURN_CHARS, required=True),
            "key": "",
            "turn_ref": _text(turn_ref, "turn_ref", MAX_TURN_CHARS),
            "turn_at": _optional_iso(turn_at, "turn_at"),
            "intent_at": "",
            "intent_turn_id": "",
        }
        _validate_effect(row["effect"])
        with _locked(target):
            data = _load(target)
            live = [existing for existing in data["rows"]
                    if str(existing.get("conversation") or "")
                    == row["conversation"]
                    and not _row_consumed(existing)
                    and not _row_expired(existing, moment)]
            if live:
                return {"ok": False, "status": "refused",
                        "reason": ("a confirmation is already waiting; answer it "
                                   "before asking for another one"),
                        "reason_code": "proposal_live"}
            if row["turn_id"] in data["turns"]:
                return {"ok": False, "status": "refused",
                        "reason": "that owner turn was already used; nothing changed",
                        "reason_code": "turn_consumed"}
            row["id"] = _new_id(data)
            row["key"] = row["id"]
            _validate_row(row)
            data["rows"].append(row)
            data["rows"] = data["rows"][-MAX_ROWS:]
            data["turns"][row["turn_id"]] = {
                "mode": "propose",
                "at": moment.isoformat(),
                "target_id": row["target_id"],
                "key": row["key"],
            }
            while len(data["turns"]) > MAX_TURN_MARKS:
                data["turns"].pop(next(iter(data["turns"])))
            if consume_question:
                data["questions"].pop(
                    _question_key(row["conversation"], row["owner"]), None)
            _save(target, data, moment)
    except (PendingAckError, CorruptStateError) as exc:
        return {"ok": False, "status": "refused",
                "reason": f"pending acknowledgement refused: {exc}"}
    return {"ok": True, "status": "pending", "pending": deepcopy(row)}


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
                        "pending": deepcopy(row)}
            if _row_expired(row, moment):
                return {"ok": False, "status": "refused",
                        "reason": "that confirmation expired; ask again"}
            other = data["turns"].get(turn_text)
            if other is not None and str(other.get("key") or "") != str(
                    row.get("key") or row.get("id") or ""):
                return {"ok": False, "status": "refused",
                        "reason": "that owner turn was already used; nothing changed",
                        "reason_code": "turn_consumed"}
            # Persist the intent and its idempotency key before the effect. A
            # crash after the effect write leaves this row unconsumed but the
            # key discoverable in the effect store, so a retry reconciles.
            row["intent_at"] = moment.isoformat()
            row["intent_turn_id"] = turn_text
            _save(target, data, moment)
            result = apply_fn(deepcopy(row))
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
            data["turns"][turn_text] = {
                "mode": "confirm",
                "at": moment.isoformat(),
                "target_id": str(row.get("target_id") or ""),
                "key": str(row.get("key") or row.get("id") or ""),
            }
            while len(data["turns"]) > MAX_TURN_MARKS:
                data["turns"].pop(next(iter(data["turns"])))
            _save(target, data, moment)
            return {"ok": True, "status": "confirmed", "receipt": receipt,
                    "pending": deepcopy(row)}
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


def turn_used(store: Any = None, turn_id: Any = "",
              now: datetime | None = None) -> dict[str, Any] | None:
    """Return the mark for an already consumed/proposed owner turn, if any."""
    wanted = str(turn_id or "").strip()
    if not wanted:
        return None
    target = _coerce_store(store)
    database = _load(target)
    value = database.get("turns", {}).get(wanted)
    return dict(value) if isinstance(value, Mapping) else None


def mark_turn(store: Any = None, *, turn_id: Any, mode: str,
              target_id: Any = "", key: Any = "",
              now: datetime | None = None) -> dict[str, Any]:
    """Remember that one owner turn was consumed by an acknowledgement mode."""
    target = _coerce_store(store)
    moment = _now(target, now)
    clean_turn = _text(turn_id, "turn key", MAX_TURN_CHARS, required=True)
    row = {
        "mode": _text(mode, "turn.mode", 32, required=True),
        "at": moment.isoformat(),
        "target_id": _text(target_id, "turn.target_id", MAX_TARGET_ID_CHARS),
        "key": _text(key, "turn.key", MAX_TURN_CHARS),
    }
    _validate_turn_mark(row)
    try:
        with _locked(target):
            data = _load(target)
            data["turns"][clean_turn] = row
            while len(data["turns"]) > MAX_TURN_MARKS:
                data["turns"].pop(next(iter(data["turns"])))
            _save(target, data, moment)
    except (PendingAckError, CorruptStateError) as exc:
        raise PendingAckError(f"turn mark refused: {exc}") from exc
    return dict(row)


def record_question(*, conversation: Any, owner: Any, labels: Any,
                    question: Any, choices: Any = None, effect: Any = None,
                    now: datetime | None = None,
                    ttl_seconds: int = DEFAULT_TTL_SECONDS,
                    store: Any = None) -> dict[str, Any]:
    """Keep the exact choice list printed by one clarification question.

    The optional ``choices`` bind each printed label and its list position to the
    exact target id that was offered. The optional ``effect`` remembers the verb
    the owner already supplied, so a later turn that names one full label can be
    answered with the same exact confirmation question instead of an immediate
    write.
    """
    target = _coerce_store(store)
    moment = _now(target, now)
    conversation_text = _text(conversation, "conversation",
                              MAX_CONVERSATION_CHARS, required=True)
    owner_text = _text(owner, "owner", MAX_OWNER_CHARS)
    clean_labels = []
    for label in list(labels or [])[:MAX_QUESTION_LABELS]:
        text = _text(label, "question.label", MAX_LABEL_CHARS, required=True)
        if text not in clean_labels:
            clean_labels.append(text)
    clean_choices = []
    for raw in list(choices or [])[:MAX_QUESTION_LABELS]:
        if not isinstance(raw, Mapping):
            continue
        label = _text(raw.get("label"), "question.choice.label",
                      MAX_LABEL_CHARS, required=True)
        if label not in clean_labels:
            continue
        position = raw.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = clean_labels.index(label) + 1
        choice = {"position": position, "label": label}
        target_id = _text(raw.get("target_id", ""),
                          "question.choice.target_id", MAX_TARGET_ID_CHARS)
        if target_id:
            choice["target_id"] = target_id
        clean_choices.append(choice)
    if not clean_labels:
        clean_labels = [choice["label"] for choice in clean_choices]
    if not clean_labels:
        raise PendingAckError("question labels are required")
    if not clean_choices:
        clean_choices = [
            {"position": index + 1, "label": label}
            for index, label in enumerate(clean_labels)
        ]
    row = {
        "labels": clean_labels,
        "choices": clean_choices,
        "question": _text(question, "question", MAX_QUESTION_CHARS,
                          required=True),
        "at": moment.isoformat(),
        "expires_at": (moment + timedelta(
            seconds=max(1, min(int(ttl_seconds), DEFAULT_TTL_SECONDS)))).isoformat(),
    }
    if effect not in (None, {}, ""):
        row["effect"] = deepcopy(dict(effect))
    _validate_question(row)
    key = _question_key(conversation_text, owner_text)
    try:
        with _locked(target):
            data = _load(target)
            data["questions"][key] = row
            while len(data["questions"]) > MAX_QUESTIONS:
                data["questions"].pop(next(iter(data["questions"])))
            _save(target, data, moment)
    except (PendingAckError, CorruptStateError) as exc:
        raise PendingAckError(f"question refused: {exc}") from exc
    return dict(row)


def find_question(store: Any = None, *, conversation: Any, owner: Any,
                  now: datetime | None = None) -> dict[str, Any] | None:
    """The unexpired clarification question for this conversation, if any."""
    target = _coerce_store(store)
    moment = _now(target, now)
    key = _question_key(conversation, owner)
    database = _load(target)
    row = (database.get("questions") or {}).get(key)
    if not isinstance(row, Mapping):
        return None
    try:
        if _parse_iso(row.get("expires_at"), "expires_at") <= moment:
            return None
    except PendingAckError:
        return None
    return deepcopy(dict(row))


__all__ = [
    "STORE", "LOCK", "SCHEMA", "MAX_ROWS", "DEFAULT_TTL_SECONDS",
    "RECEIPT_FIELDS", "PendingAckError", "empty_store", "rows", "get",
    "live_for", "find_pin", "lookup", "create", "commit",
    "record_disposition", "dispositions", "turn_used", "mark_turn",
    "record_question", "find_question",
]
