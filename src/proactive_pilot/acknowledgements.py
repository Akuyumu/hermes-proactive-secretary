"""Short owner acknowledgements mapped onto the existing durable stores.

The words are deliberately few and exact:

* ``later <n>`` / ``later until <when>`` -> the existing feedback ``snooze``
  action (minutes or an explicit timezone-aware until);
* ``done`` -> close the notice topic through the existing guidance close path,
  or, when there is no topic, close the follow-up with the existing
  ``owner_confirmed`` reason;
* ``stop`` -> close the follow-up with the existing ``owner_stopped`` reason and
  close its topic. It is scoped to that item/topic and never becomes a global
  pause or a permanent blanket ban.

Anything else writes nothing and returns an explicit refusal reason. This
module only composes the stores; it does not add a state format, a scheduler,
a source read or a network call.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import feedback, guidance
from . import followups
from . import notice_voice
from . import pending_ack

_UNKNOWN_REASON = (
    "unrecognised acknowledgement; only 'later <n>', 'later until <when>', "
    "'done' and 'stop' are accepted, and nothing was changed"
)
_LATER_MINUTES = re.compile(
    r"^later\s+(\d{1,5})(?:\s*(min|mins|minute|minutes|h|hour|hours|d|day|days))?$",
    re.IGNORECASE,
)
_LATER_UNTIL = re.compile(r"^later\s+until\s+(.+)$", re.IGNORECASE)
_DONE_WORDS = frozenset({"done", "complete", "completed"})
_STOP_WORDS = frozenset({"stop"})


def _clean(text: Any) -> str:
    return " ".join(str(text or "").strip().strip(".!,").split())


def _minutes(value: str, unit: str | None) -> int:
    count = int(value)
    suffix = (unit or "minutes").lower()
    if suffix.startswith("h"):
        count *= 60
    elif suffix.startswith("d"):
        count *= 60 * 24
    if count <= 0 or count > 60 * 24 * 14:
        raise feedback.FeedbackError("later duration out of range (1..20160 minutes)")
    return count


def parse(text: Any, *, now: datetime) -> dict[str, Any]:
    """Return a typed acknowledgement action, or an explicit refusal."""
    cleaned = _clean(text)
    lowered = cleaned.lower()
    if lowered in _DONE_WORDS:
        return {"action": "done", "word": "done"}
    if lowered in _STOP_WORDS:
        return {"action": "stop", "word": "stop"}
    match = _LATER_MINUTES.match(cleaned)
    if match:
        try:
            minutes = _minutes(match.group(1), match.group(2))
        except feedback.FeedbackError as exc:
            return {"action": "refused", "reason": str(exc), "word": lowered}
        return {"action": "snooze", "minutes": minutes, "word": "later"}
    match = _LATER_UNTIL.match(cleaned)
    if match:
        try:
            until = feedback._parse_until(match.group(1), now)
        except feedback.FeedbackError as exc:
            return {"action": "refused", "reason": str(exc), "word": "later"}
        return {"action": "snooze", "until": until.isoformat(), "word": "later"}
    return {"action": "refused", "reason": _UNKNOWN_REASON, "word": lowered[:80]}


def apply(
    text: Any,
    *,
    now: datetime,
    preferences_path: Path | str | None = None,
    followup_store: Any = None,
    followup_id: str = "",
    topic: str = "",
    keys: Iterable[str] = (),
    guidance_root: Path | str | None = None,
    scope: str = "",
    provenance: str = "owner acknowledgement",
    item_kind: str = "",
    target_id: str = "",
    target_keys: Iterable[str] = (),
    key: str = "",
) -> dict[str, Any]:
    """Apply one acknowledgement to the existing stores, or refuse cleanly."""
    action = parse(text, now=now)
    kind = action["action"]
    if kind == "refused":
        return {"ok": False, "status": "refused", "reason": action["reason"]}

    if kind == "snooze":
        if preferences_path is None:
            return {"ok": False, "status": "refused",
                    "reason": "no preferences artifact is available for the snooze"}
        clean_target = str(target_id or "").strip()
        if clean_target:
            # The matcher is the pinned target id, never the model's scope or
            # the visible label. The label is only carried for display.
            match = clean_target.lower()
            display_scope = str(scope or target_id).strip()
        else:
            match = str(scope or topic or "").strip().lower()
            display_scope = str(scope or topic)
        if not match:
            return {"ok": False, "status": "refused",
                    "reason": ("later needs the words it applies to; pass scope with the "
                               "notice's own subject so the snooze is not global")}
        stored_keys: list[str] = []
        derived_keys = (target_gate_keys({"target_id": clean_target})
                        if clean_target else set())
        for value in (clean_target, *list(target_keys or ()), *derived_keys):
            text = str(value or "").strip()
            if text and text not in stored_keys and len(stored_keys) < 8:
                stored_keys.append(text[:240])
        request = {
            "action": "snooze",
            "kind": str(item_kind or "any"),
            "match": match[:200],
            "scope": display_scope[:200],
            "target_id": clean_target[:240],
            "target_keys": stored_keys,
            "key": str(key or "")[:128],
            "provenance": str(provenance)[:200],
        }
        if action.get("minutes") is not None:
            request["minutes"] = action["minutes"]
        else:
            request["until"] = action["until"]
        try:
            path = Path(preferences_path)
            before = feedback.load_preferences_file(path, strict=True)
            after = feedback.save_preferences_file(
                path, feedback.apply_action(before, request, now), now)
        except feedback.FeedbackError as exc:
            return {"ok": False, "status": "refused", "reason": str(exc),
                    "reason_code": "preferences_refused"}
        return {
            "ok": True,
            "status": "snoozed",
            "action": "snooze",
            "minutes": action.get("minutes"),
            "until": request.get("until", ""),
            "receipt": feedback.receipt_for_action(before, after, request, now),
        }

    clean_topic = str(topic or "").strip()
    clean_keys = [str(key).strip() for key in keys if str(key).strip()]

    if kind == "done":
        if clean_topic or clean_keys:
            if guidance_root is None:
                return {"ok": False, "status": "refused",
                        "reason": "no guidance store is available to close the topic"}
            result = guidance.close(
                Path(guidance_root), [clean_topic] if clean_topic else (),
                keys=clean_keys, now=now, source=f"owner: {provenance}"[:80])
            return {"ok": bool(result.get("ok")), "status": result.get("status", ""),
                    "action": "topic_closed", "guidance": result}
        if followup_store is None or not str(followup_id or "").strip():
            return {"ok": False, "status": "refused",
                    "reason": "done needs a topic or a follow-up id; nothing changed"}
        try:
            entry = followups.close(
                followup_store, followup_id, "owner_confirmed", now=now)
        except followups.FollowupError as exc:
            return {"ok": False, "status": "refused", "reason": str(exc)}
        return {"ok": True, "status": "owner_confirmed", "action": "followup_closed",
                "followup": entry}

    if kind == "stop":
        if not clean_topic and not clean_keys and not str(followup_id or "").strip():
            return {"ok": False, "status": "refused",
                    "reason": "stop needs a topic or a follow-up id; nothing changed"}
        receipt: dict[str, Any] = {"ok": True, "status": "stopped", "action": "stop"}
        if followup_store is not None and str(followup_id or "").strip():
            try:
                receipt["followup"] = followups.close(
                    followup_store, followup_id, "owner_stopped", now=now)
            except followups.FollowupError as exc:
                return {"ok": False, "status": "refused", "reason": str(exc)}
        if clean_topic or clean_keys:
            if guidance_root is None:
                return {"ok": False, "status": "refused",
                        "reason": "no guidance store is available to close the topic"}
            closed = guidance.close(
                Path(guidance_root), [clean_topic] if clean_topic else (),
                keys=clean_keys, now=now, source=f"owner: {provenance}"[:80])
            if not closed.get("ok"):
                return {"ok": False, "status": "refused",
                        "reason": str(closed.get("error") or "topic closure failed")}
            receipt["guidance"] = closed
            receipt["scoped_to"] = clean_topic or clean_keys
        return receipt

    return {"ok": False, "status": "refused", "reason": _UNKNOWN_REASON}


__all__ = [
    "parse", "apply", "turn_key", "proposal_problem", "is_bare_affirmative",
    "build_effect", "render_question", "target_candidates", "target_aliases",
    "target_gate_keys", "resolve_target", "resolve_question_answer",
    "apply_target", "apply_effect", "propose", "propose_target", "confirm",
]


_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "do it",
    "go ahead", "confirm",
})
_PROPOSAL_NEGATION = re.compile(
    r"\b(?:no|not|never|none|don't|dont|doesn't|does not|isn't|is not|won't|"
    r"will not|wouldn't|shouldn't|cannot|can't|cant|without|except|decline|"
    r"reject|refuse|nope|nah|disapprove)\b",
    re.IGNORECASE,
)
_PROPOSAL_CONDITIONAL = re.compile(
    r"\b(?:if|suppose|supposing|unless|what happens if|when i say)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PLUGIN_HANDLE = re.compile(
    r"\b(?:fu|resp|sec|cal|mail|task|thread|evt|msg|artifact|handle|"
    r"responsibility|reminder|followup|obs|cand)_[A-Za-z0-9][A-Za-z0-9_-]{3,}\b",
    re.IGNORECASE,
)
_TARGET_HANDLE = re.compile(
    r"\b(?:ack|notice|rem|reminder|followup|fu)[_-]"
    r"[A-Za-z0-9][A-Za-z0-9_-]{1,}\b",
    re.IGNORECASE,
)
_REPLY_KEYS = (
    "reply_to", "reply_to_id", "reply_to_message_id", "reply_to_ref",
    "quoted_message_id", "quoted_id", "quote_id", "in_reply_to",
    "replied_to_id", "replied_to_message_id", "reply_message_id",
)
_REPLY_PARENTS = ("reply", "quoted", "quote", "reply_to_message", "reply_context")
_REPLY_INNER = ("id", "message_id", "ref", "notice_id", "event_key")
_LABEL_FALLBACK = "the item you replied to"
_LABEL_MAX_CHARS = 60
_LABEL_QUOTED = re.compile(
    r"\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’")
_LABEL_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_LABEL_CLAUSE = re.compile(
    r"\s+(?:because|since|so\s+that|which|who|whom|whose|that|whereas|"
    r"although|though|while|when|if|unless)\b.*$",
    re.IGNORECASE,
)
_LABEL_SEPARATOR = re.compile(r"\s*(?:[;|]|[-–—]\s+)\s*")


def _bounded(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ValueError(f"{name} is required")
    if len(text) > limit:
        raise ValueError(f"{name} is too long (max {limit} chars)")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{name} contains control characters")
    return text


def turn_key(conversation: Any, owner: Any, turn: Mapping[str, Any]) -> str:
    """A stable id for the host-recorded turn, never for a model call."""
    raw = "|".join((
        str(conversation or ""),
        str(owner or ""),
        str(turn.get("at") or ""),
        str(turn.get("text") or ""),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def turn_ref(turn: Mapping[str, Any]) -> str:
    """The host's own turn reference, when it supplies one."""
    return str(turn.get("ref") or "").strip()[:128]


def _turn_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _turn_at_iso(turn: Mapping[str, Any], now: datetime) -> str:
    moment = _turn_datetime(turn.get("at"))
    return (moment or now).isoformat()


def proposal_problem(text: Any) -> str:
    """Why the whole owner message cannot authorise a proposal, or ``""``."""
    value = str(text or "").strip()
    if not value:
        return "the owner's current message is empty"
    if value.endswith("?"):
        return "a question is not an instruction"
    if _PROPOSAL_CONDITIONAL.search(value):
        return "a conditional turn is not an instruction"
    if _PROPOSAL_NEGATION.search(value):
        return "a negated turn is not an instruction"
    return ""


def is_bare_affirmative(text: Any) -> bool:
    value = " ".join(str(text or "").lower().split()).strip(" \t.,!;:\"'“”‘’")
    return value in _AFFIRMATIVES


def build_effect(
    action: Any,
    *,
    minutes: Any = None,
    until: Any = None,
    kind: Any = "",
    scope: Any = "",
    now: datetime,
) -> dict[str, Any]:
    """Validate the model's proposed effect without widening :func:`parse`."""
    word = str(action or "").strip().lower()
    if word in {"done", "complete", "completed"}:
        if minutes not in (None, "") or until not in (None, ""):
            raise ValueError("done does not take a duration")
        return {"action": "done", "kind": _bounded(kind, "kind", 64),
                "scope": _bounded(scope, "scope", 240)}
    if word == "stop":
        if minutes not in (None, "") or until not in (None, ""):
            raise ValueError("stop does not take a duration")
        return {"action": "stop", "kind": _bounded(kind, "kind", 64),
                "scope": _bounded(scope, "scope", 240)}
    if word not in {"later", "snooze"}:
        raise ValueError("action must be done, later or stop")
    if minutes not in (None, "") and until not in (None, ""):
        raise ValueError("later needs either minutes or an until time, not both")
    effect: dict[str, Any] = {"action": "snooze",
                              "kind": _bounded(kind, "kind", 64),
                              "scope": _bounded(scope, "scope", 240)}
    if minutes not in (None, ""):
        try:
            count = int(minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("minutes must be a whole number") from exc
        if isinstance(minutes, bool) or count <= 0 or count > 60 * 24 * 14:
            raise ValueError("later duration out of range (1..20160 minutes)")
        effect["minutes"] = count
        return effect
    if until in (None, ""):
        raise ValueError("later needs minutes or an until time")
    try:
        parsed = feedback._parse_until(str(until), now)
    except feedback.FeedbackError as exc:
        raise ValueError(str(exc)) from exc
    effect["until"] = parsed.isoformat()
    return effect


def _label_parts(value: Any) -> tuple[str, bool]:
    """Derive one public label from a headline-like field, never stored prose.

    The first headline/first sentence is used, quoted spans and trailing clauses
    are removed, then identifiers, handles, emails and paths are stripped. The
    result is capped at 60 characters. ``(fallback, False)`` means no safe label
    exists and the caller must have a trusted reply pin to proceed.
    """
    raw = str(value or "")
    text = raw.splitlines()[0] if raw.splitlines() else ""
    text = _LABEL_QUOTED.sub(" ", text)
    text = _LABEL_PAREN.sub(" ", text)
    text = _EMAIL.sub(" ", text)
    text = re.split(r"[.!?](?:\s|$)", text, maxsplit=1)[0]
    text = text.split(":", 1)[0]
    text = text.split(",", 1)[0]
    text = _LABEL_SEPARATOR.split(text, maxsplit=1)[0]
    text = _LABEL_CLAUSE.sub("", text)
    text = notice_voice._notice_fragment(text, 180)
    text = _EMAIL.sub(" ", text)
    text = _PLUGIN_HANDLE.sub(" ", text)
    text = _TARGET_HANDLE.sub(" ", text)
    text = notice_voice._ABSOLUTE_PATH_RE.sub(" ", text)
    text = text.replace("?", " ")
    text = " ".join(text.split()).strip(" .,;:-|")
    text = text.replace("\"", "").replace("'", "")
    text = text.replace("“", "").replace("”", "")
    text = text.replace("‘", "").replace("’", "")
    text = " ".join(text.split())
    if not text:
        return _LABEL_FALLBACK, False
    if len(text) > _LABEL_MAX_CHARS:
        text = text[:_LABEL_MAX_CHARS - 3].rstrip(" ,;:-") + "..."
    return text, True


def _safe_label(value: Any) -> str:
    return _label_parts(value)[0]


def _human_until(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("the proposed deadline cannot be rendered exactly") from exc
    if parsed.tzinfo is None:
        raise ValueError("the proposed deadline has no timezone")
    shape = "%d %b %Y %H:%M:%S" if parsed.second or parsed.microsecond else "%d %b %Y %H:%M"
    zone = parsed.strftime("%Z") or parsed.strftime("%z")
    return (parsed.strftime(shape) + (f" {zone}" if zone else "")).strip()


def _question_for(effect: Mapping[str, Any], label: str) -> str:
    action = str(effect.get("action") or "")
    if action == "stop":
        return f"Stop follow-ups for “{label}” only?"
    if action == "done":
        return f"Mark “{label}” as done?"
    if effect.get("minutes") is not None:
        count = int(effect["minutes"])
        unit = "minute" if count == 1 else "minutes"
        return f"Snooze “{label}” for {count} {unit}?"
    if not effect.get("until"):
        raise ValueError("the proposed snooze has no exact deadline")
    return f"Snooze “{label}” until {_human_until(effect.get('until'))}?"


def render_question(effect: Mapping[str, Any], target_label: Any) -> str:
    label = _safe_label(target_label)
    question = _question_for(effect, label)
    ok, _rule = notice_voice.check_notice_text(question)
    if not ok:
        question = _question_for(effect, "the item you replied to")
        ok, _rule = notice_voice.check_notice_text(question)
    if not ok:
        raise ValueError("the confirmation question cannot be rendered safely")
    return question


def _candidate(target_id: str, target_type: str, label: Any, *,
               aliases: Iterable[Any] = (), at: Any = "",
               delivery_keys: Iterable[Any] = (),
               **extra: Any) -> dict[str, Any]:
    clean_aliases = {target_id}
    for alias in aliases:
        text = str(alias or "").strip()
        if text:
            clean_aliases.add(text)
    clean_keys = set()
    for key in delivery_keys:
        text = str(key or "").strip()
        if text:
            clean_keys.add(text)
            clean_aliases.add(text)
    label_text, label_safe = _label_parts(label)
    row = {
        "target_id": target_id,
        "target_type": target_type,
        "target_label": label_text,
        "label_safe": label_safe,
        "aliases": clean_aliases,
        "delivery_keys": clean_keys,
        "at": str(at or ""),
    }
    row.update({key: value for key, value in extra.items() if value not in (None, "")})
    return row


def _add_candidate(found: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    existing = found.get(row["target_id"])
    if existing is None:
        found[row["target_id"]] = row
        return
    existing["aliases"] = set(existing.get("aliases") or ()) | set(row.get("aliases") or ())
    existing["delivery_keys"] = (
        set(existing.get("delivery_keys") or ())
        | set(row.get("delivery_keys") or ()))
    if not existing.get("target_label") and row.get("target_label"):
        existing["target_label"] = row["target_label"]
        existing["label_safe"] = row.get("label_safe", False)


def target_candidates(store: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Host-recorded delivered/active items the owner could have answered."""
    from . import deliveries

    found: dict[str, dict[str, Any]] = {}
    reminder_to_followup: dict[str, str] = {}
    try:
        entries = (followups.active_entries(store)
                   + followups.awaiting_entries(store))
    except Exception as exc:  # noqa: BLE001 - unreadable store must fail closed
        raise ValueError("the follow-up store could not be read") from exc
    for entry in entries:
        fid = str(entry.get("id") or "")
        if not fid:
            continue
        source_ref = str(entry.get("source_ref") or "")
        rid = source_ref[len("reminder:"):] if source_ref.startswith("reminder:") else ""
        aliases = [fid, f"followup:{fid}"]
        delivery_keys = set()
        try:
            base_event = followups.notice_event_key(fid)
        except Exception:  # noqa: BLE001 - malformed local id cannot be gated
            base_event = ""
        if base_event:
            delivery_keys.add("portal:" + base_event)
        queued_event = str(entry.get("notice_event_key") or "")
        if queued_event:
            delivery_keys.add("portal:" + queued_event)
        for reservation in entry.get("notice_reservations") or []:
            if isinstance(reservation, Mapping):
                event_key = str(reservation.get("event_key") or "")
                if event_key:
                    delivery_keys.add("portal:" + event_key)
        if rid:
            aliases.extend([rid, f"reminder:{rid}"])
            aliases.extend(delivery_keys)
            reminder_to_followup[rid] = fid
        _add_candidate(found, _candidate(
            f"followup:{fid}", "followup", entry.get("text"), aliases=aliases,
            at=entry.get("created_at") or entry.get("updated_at"),
            delivery_keys=delivery_keys,
            followup_id=fid, reminder_id=rid, kind=entry.get("kind")))

    try:
        raw = store.load("reminders.json", {"reminders": {}}, strict=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable registry fails closed
        raise ValueError("the reminder registry could not be read") from exc
    reminders = raw.get("reminders") if isinstance(raw, dict) else None
    if not isinstance(reminders, dict):
        raise ValueError("the reminder registry is malformed")
    for rid, reminder in reminders.items():
        if not isinstance(reminder, dict):
            continue
        if str(reminder.get("status") or "") in {"canceled", "retired", "rejected"}:
            continue
        key = str(rid)
        if key in reminder_to_followup:
            continue
        _add_candidate(found, _candidate(
            f"reminder:{key}", "reminder", reminder.get("text"),
            aliases=[key, f"reminder:{key}"], reminder_id=key,
            at=reminder.get("emitted_at") or reminder.get("created_at")))

    try:
        delivery_rows = deliveries.recent(store, limit=deliveries.MAX_ROWS,
                                          strict=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger fails closed
        raise ValueError("the delivery ledger could not be read") from exc
    for delivery in delivery_rows:
        event_key = str(delivery.get("event_key") or "")
        notice_id = str(delivery.get("notice_id") or "")
        delivery_keys = {"portal:" + event_key} if event_key else set()
        target_followup = ""
        if event_key.startswith("followup-notice:"):
            target_followup = event_key[len("followup-notice:"):].split(":", 1)[0]
        if target_followup and f"followup:{target_followup}" in found:
            row = found[f"followup:{target_followup}"]
            row["aliases"] = set(row["aliases"]) | {
                alias for alias in (event_key, notice_id, *delivery_keys) if alias}
            row["delivery_keys"] = (
                set(row.get("delivery_keys") or ()) | delivery_keys)
            continue
        candidate_id = notice_id or event_key
        if not candidate_id:
            continue
        _add_candidate(found, _candidate(
            f"notice:{candidate_id}", "notice", delivery.get("message"),
            aliases=[notice_id, event_key], at=delivery.get("at"),
            delivery_keys=delivery_keys,
            notice_id=candidate_id))

    rows = list(found.values())
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return rows


def _reply_ids(turn: Mapping[str, Any]) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in _REPLY_INNER:
                add(value.get(key))
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        if value is None or isinstance(value, bool):
            return
        text = str(value).strip()
        if text and len(text) <= 300 and text not in values:
            values.append(text)

    for key in _REPLY_KEYS:
        add(turn.get(key))
    for key in _REPLY_PARENTS:
        add(turn.get(key))
    return values


def target_aliases(target: Mapping[str, Any]) -> set[str]:
    aliases = {str(alias) for alias in target.get("aliases") or () if str(alias)}
    aliases.update(
        str(key) for key in target.get("delivery_keys") or () if str(key))
    aliases.add(str(target.get("target_id") or ""))
    for key in ("followup_id", "reminder_id", "notice_id"):
        value = str(target.get(key) or "")
        if value:
            aliases.add(value)
    return {alias for alias in aliases if alias}


def target_gate_keys(target: Mapping[str, Any]) -> set[str]:
    """Canonical keys the delivery gate may use for this exact item."""
    keys = {str(key) for key in target.get("delivery_keys") or () if str(key)}
    target_id = str(target.get("target_id") or "").strip()
    if target_id:
        keys.add(target_id)
    followup_id = str(target.get("followup_id") or "")
    if not followup_id and target_id.startswith("followup:"):
        followup_id = target_id[len("followup:"):]
    if followup_id:
        keys.add(f"followup:{followup_id}")
        try:
            event_key = followups.notice_event_key(followup_id)
        except Exception:  # noqa: BLE001 - a malformed id cannot be gated
            event_key = ""
        if event_key:
            keys.add("portal:" + event_key)
    reminder_id = str(target.get("reminder_id") or "")
    if not reminder_id and target_id.startswith("reminder:"):
        reminder_id = target_id[len("reminder:"):]
    if reminder_id:
        keys.add(f"reminder:{reminder_id}")
    notice_id = str(target.get("notice_id") or "")
    if not notice_id and target_id.startswith("notice:"):
        notice_id = target_id[len("notice:"):]
    if notice_id:
        keys.add(f"notice:{notice_id}")
    return {key for key in keys if key}


def _matches_reply(candidates: Iterable[Mapping[str, Any]], reply_ids: Iterable[str]):
    wanted = {str(value).strip().lower() for value in reply_ids if str(value).strip()}
    if not wanted:
        return []
    return [candidate for candidate in candidates
            if any(alias.lower() in wanted
                   for alias in target_aliases(candidate))]


def _target_from_pending(row: Mapping[str, Any]) -> dict[str, Any]:
    target_id = str(row.get("target_id") or "")
    target_type = "item"
    extra: dict[str, Any] = {}
    if target_id.startswith("followup:"):
        target_type = "followup"
        extra["followup_id"] = target_id[len("followup:"):]
    elif target_id.startswith("reminder:"):
        target_type = "reminder"
        extra["reminder_id"] = target_id[len("reminder:"):]
    elif target_id.startswith("notice:"):
        target_type = "notice"
        extra["notice_id"] = target_id[len("notice:"):]
    return {
        "target_id": target_id,
        "target_type": target_type,
        "target_label": _safe_label(row.get("target_label")),
        "aliases": {target_id},
        "delivery_keys": set(row.get("target_keys") or ()),
        "label_safe": _safe_label(row.get("target_label")) != _LABEL_FALLBACK,
        **extra,
    }


def _normalise_label(value: Any) -> str:
    text = re.sub(r"[^0-9a-z]+", " ", str(value or "").lower())
    return " ".join(text.split())


def _label_appears(label: Any, message: Any) -> bool:
    wanted = _normalise_label(label)
    if not wanted:
        return False
    haystack = f" {_normalise_label(message)} "
    return f" {wanted} " in haystack


def select_by_label(candidates: Iterable[Mapping[str, Any]], message: Any
                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The owner naming one item's full visible label selects exactly it.

    This is an owner-authored selection, not model authority. There is no
    scoring: a label either appears whole in the owner's message or it does
    not. Zero matches select nothing; two or more matches are ambiguous.
    """
    matched = [dict(candidate) for candidate in candidates
               if _label_appears(candidate.get("target_label"), message)]
    if len(matched) == 1:
        return matched[0], []
    if len(matched) > 1:
        return None, matched
    return None, []


def _question_choices(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    labels: list[str] = []
    for candidate in candidates:
        if candidate.get("label_safe") is False:
            # No safe public label exists; the owner must use a trusted reply
            # pin rather than being shown stored prose.
            continue
        label = _safe_label(candidate.get("target_label"))
        if not label or label in labels:
            continue
        target_id = str(candidate.get("target_id") or "")
        choice: dict[str, Any] = {
            "position": len(choices) + 1,
            "label": label,
        }
        if target_id:
            choice["target_id"] = target_id
        choices.append(choice)
        labels.append(label)
        if len(choices) >= 3:
            break
    return choices


def _question_labels(candidates: Iterable[Mapping[str, Any]]) -> list[str]:
    return [choice["label"] for choice in _question_choices(candidates)]


def _question_from_labels(labels: list[str]) -> str:
    if not labels:
        return "Which item do you mean? Reply to the notice you mean."
    if len(labels) == 1:
        return f"Which item do you mean: “{labels[0]}”?"
    joined = ", ".join(f"“{label}”" for label in labels[:-1])
    return f"Which item do you mean: {joined} or “{labels[-1]}”?"


def _no_pin_question(candidates: Iterable[Mapping[str, Any]]) -> str:
    return _question_from_labels(_question_labels(candidates))


def _clarify(candidates: Iterable[Mapping[str, Any]], code: str) -> dict[str, Any]:
    rows = list(candidates)
    labels = _question_labels(rows)
    choices = _question_choices(rows)
    return {
        "ok": False,
        "status": "needs_clarification",
        "reason": _question_from_labels(labels),
        "reason_code": code,
        "labels": labels,
        "choices": choices,
        "candidates": [dict(row) for row in rows],
    }


def _select_by_position(candidates: Iterable[Mapping[str, Any]],
                        labels: list[str], message: Any
                        ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    match = re.match(r"^\s*(\d{1,2})\s*[.)]?\s*$", str(message or ""))
    if not match or not labels:
        return None, []
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(labels):
        return None, []
    wanted = _normalise_label(labels[index])
    rows = [dict(candidate) for candidate in candidates
            if _normalise_label(_safe_label(candidate.get("target_label")))
            == wanted]
    if len(rows) == 1:
        return rows[0], []
    return None, rows


def _record_clarification(store: Any, *, conversation: Any, owner: Any,
                          labels: list[str], question: str,
                          now: datetime | None,
                          effect: Any = None,
                          choices: Iterable[Mapping[str, Any]] = ()) -> None:
    if not labels:
        return
    try:
        pending_ack.record_question(
            conversation=conversation, owner=owner, labels=labels,
            question=question, choices=list(choices or ()), effect=effect,
            now=now, store=store)
    except pending_ack.PendingAckError:
        pass


def _question_row_choices(question: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = question.get("choices")
    choices: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            label = _safe_label(item.get("label"))
            if not label:
                continue
            choice: dict[str, Any] = {
                "position": int(item.get("position") or len(choices) + 1),
                "label": label,
            }
            target_id = str(item.get("target_id") or "")
            if target_id:
                choice["target_id"] = target_id
            choices.append(choice)
    if choices:
        return choices
    return [
        {"position": index + 1, "label": _safe_label(label)}
        for index, label in enumerate(question.get("labels") or [])
        if _safe_label(label)
    ]


def _question_refusal(question: Mapping[str, Any], choices: list[dict[str, Any]],
                      code: str) -> dict[str, Any]:
    labels = [choice["label"] for choice in choices]
    text = str(question.get("question") or "").strip()
    if not text:
        text = _question_from_labels(labels)
    return {
        "ok": False,
        "status": "needs_clarification",
        "reason": text,
        "reason_code": code,
        "labels": labels,
        "choices": choices,
        "candidates": [],
    }


def _choice_target(choice: Mapping[str, Any],
                   candidates: Iterable[Mapping[str, Any]]
                   ) -> tuple[dict[str, Any] | None, str]:
    wanted_id = str(choice.get("target_id") or "")
    rows = [dict(candidate) for candidate in candidates]
    if wanted_id:
        for candidate in rows:
            if str(candidate.get("target_id") or "") == wanted_id:
                return candidate, ""
        return None, "choice_unavailable"
    wanted_label = _normalise_label(choice.get("label"))
    matching = [candidate for candidate in rows
                if _normalise_label(_safe_label(candidate.get("target_label")))
                == wanted_label]
    if len(matching) == 1:
        return matching[0], ""
    return None, ("choice_unavailable" if not matching else "ambiguous_position")


def resolve_question_answer(
    question: Mapping[str, Any],
    message: Any,
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve one owner message against exactly the choices a question offered."""
    choices = _question_row_choices(question)
    if not choices:
        return None, _question_refusal(question, choices, "not_offered")
    spoken = str(message or "").strip()
    chosen: dict[str, Any] | None = None
    position = re.match(r"^\s*(\d{1,2})\s*[.)]?\s*$", spoken)
    if position:
        index = int(position.group(1)) - 1
        if 0 <= index < len(choices):
            chosen = choices[index]
    else:
        wanted = _normalise_label(spoken)
        matches = [choice for choice in choices
                   if _normalise_label(choice.get("label")) == wanted]
        if len(matches) == 1:
            chosen = matches[0]
    if chosen is None:
        return None, _question_refusal(question, choices, "not_offered")
    target, problem = _choice_target(chosen, candidates)
    if target is None:
        return None, _question_refusal(question, choices, problem or "not_offered")
    target["pinned_by"] = "question"
    target["aliases"] = target_aliases(target)
    return target, None


def resolve_target(
    turn: Mapping[str, Any],
    *,
    store: Any,
    conversation: Any,
    owner: Any,
    model_target: Any = "",
    allow_pending: bool = True,
    message: Any = "",
    now: datetime | None = None,
    question_effect: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pin one target from host facts; the model's id is never authority."""
    try:
        candidates = target_candidates(store, now=now)
    except ValueError as exc:
        return None, {"ok": False, "status": "refused",
                      "reason": str(exc), "reason_code": "store_unreadable"}

    def selected_target(candidate: Mapping[str, Any]):
        target = dict(candidate)
        problem = _model_target_problem(target, model_target)
        if problem:
            return None, {"ok": False, "status": "refused", "reason": problem,
                          "reason_code": "target_mismatch"}
        target["aliases"] = target_aliases(target)
        return target, None

    spoken = message or turn.get("text")
    try:
        question = pending_ack.find_question(
            store, conversation=conversation, owner=owner, now=now)
    except pending_ack.PendingAckError:
        return None, {"ok": False, "status": "refused",
                      "reason": "the pending question could not be read",
                      "reason_code": "pending_unreadable"}
    if question is not None:
        # A live question owns the next owner message. Only its exact offered
        # choices are selectable; anything else re-asks and changes nothing.
        chosen, refusal = resolve_question_answer(question, spoken, candidates)
        if chosen is not None:
            return selected_target(chosen)
        return None, refusal

    replies = _reply_ids(turn)
    if replies:
        # An explicit host reply is authoritative. If it names no current item,
        # stop here: never fall through to a different target by label.
        matches = _matches_reply(candidates, replies)
        distinct = {str(row.get("target_id") or ""): row for row in matches}
        if len(distinct) == 1:
            source = dict(next(iter(distinct.values())))
            source["pinned_by"] = "reply"
            return selected_target(source)
        if len(distinct) > 1:
            refusal = _clarify(distinct.values(), "ambiguous_reply")
            _record_clarification(store, conversation=conversation, owner=owner,
                                  labels=refusal["labels"],
                                  question=refusal["reason"], now=now,
                                  effect=question_effect)
            return None, refusal
        refusal = _clarify(candidates, "unmatched_reply")
        _record_clarification(store, conversation=conversation, owner=owner,
                              labels=refusal["labels"],
                              question=refusal["reason"], now=now,
                              effect=question_effect)
        return None, refusal

    # No trusted reply: the owner may have named the item in their own words.
    # That selection is owner-authored and must name exactly one item.
    selected, ambiguous = select_by_label(candidates, spoken)
    if selected is not None:
        selected["pinned_by"] = "label"
        return selected_target(selected)
    if ambiguous:
        refusal = _clarify(ambiguous, "ambiguous_label")
        _record_clarification(store, conversation=conversation, owner=owner,
                              labels=refusal["labels"],
                              question=refusal["reason"], now=now,
                              effect=question_effect)
        return None, refusal

    if allow_pending:
        try:
            live = pending_ack.live_for(
                store, conversation=conversation, owner=owner, now=now)
        except pending_ack.PendingAckError as exc:
            return None, {"ok": False, "status": "refused",
                          "reason": "the pending acknowledgement could not be read",
                          "reason_code": "pending_unreadable"}
        if len(live) > 1:
            return None, {
                "ok": False, "status": "refused",
                "reason": ("more than one confirmation is waiting; say which item "
                           "you mean"),
                "reason_code": "ambiguous_pending",
            }
        if len(live) == 1:
            source = _target_from_pending(live[0])
            source["pinned_by"] = "pending"
            return selected_target(source)

    refusal = _clarify(candidates, "clarify_target")
    _record_clarification(store, conversation=conversation, owner=owner,
                          labels=refusal["labels"],
                          question=refusal["reason"], now=now,
                          effect=question_effect)
    return None, refusal


def _model_target_problem(target: Mapping[str, Any], model_target: Any) -> str:
    wanted = str(model_target or "").strip()
    if not wanted:
        return ""
    if wanted.lower() in {alias.lower() for alias in target_aliases(target)}:
        return ""
    return "the target the model named does not match the pinned item; nothing changed"


def _followup_for_reminder(store: Any, reminder_id: str) -> dict[str, Any] | None:
    if store is None:
        return None
    for entry in followups.active_entries(store) + followups.awaiting_entries(store):
        if str(entry.get("source_ref") or "") == f"reminder:{reminder_id}":
            return entry
    return None


def apply_target(
    text: Any,
    *,
    target: Mapping[str, Any],
    now: datetime,
    preferences_path: Path | str | None = None,
    followup_store: Any = None,
    guidance_root: Path | str | None = None,
    scope: str = "",
    item_kind: str = "",
    turn_id: str = "",
    pending_id: str = "",
    provenance: str = "owner acknowledgement",
    pending_store: Any = None,
    defer_disposition: bool = False,
    idempotency_key: str = "",
    consume_turn: bool = False,
) -> dict[str, Any]:
    """Apply one parsed acknowledgement to the already-pinned target."""
    action = parse(text, now=now)
    kind = action.get("action")
    if kind == "refused":
        return {"ok": False, "status": "refused", "reason": action["reason"]}

    target_id = str(target.get("target_id") or "")
    target_label = _safe_label(target.get("target_label"))
    followup_id = str(target.get("followup_id") or "")
    reminder_id = str(target.get("reminder_id") or "")
    target_type = str(target.get("target_type") or "")
    clean_scope = str(scope or target.get("scope") or "").strip()
    turn_store = pending_store if pending_store is not None else followup_store

    if consume_turn and turn_id and turn_store is not None:
        try:
            if pending_ack.turn_used(turn_store, turn_id, now=now) is not None:
                return {"ok": False, "status": "refused",
                        "reason": "that owner turn was already used; nothing changed",
                        "reason_code": "turn_consumed"}
        except pending_ack.PendingAckError:
            return {"ok": False, "status": "refused",
                    "reason": "the acknowledgement state could not be read",
                    "reason_code": "state_unreadable"}

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ok") and consume_turn and turn_id and turn_store is not None:
            pending_ack.mark_turn(
                turn_store, turn_id=turn_id, mode="apply",
                target_id=target_id, key=idempotency_key, now=now)
        return result

    if kind == "snooze":
        result = apply(
            text,
            now=now,
            preferences_path=preferences_path,
            followup_store=followup_store,
            followup_id=followup_id,
            guidance_root=guidance_root,
            scope=clean_scope or target_label,
            item_kind=str(item_kind or target.get("kind") or ""),
            provenance=provenance,
            target_id=target_id,
            target_keys=target_gate_keys(target),
            key=idempotency_key,
        )
        return finish(result)

    if followup_id:
        if followup_store is None:
            return {"ok": False, "status": "refused",
                    "reason": "no follow-up store is available",
                    "reason_code": "followup_unavailable"}
        result = apply(
            text,
            now=now,
            preferences_path=preferences_path,
            followup_store=followup_store,
            followup_id=followup_id,
            guidance_root=guidance_root,
            scope=clean_scope,
            item_kind=str(item_kind or target.get("kind") or ""),
            provenance=provenance,
        )
        if not result.get("ok"):
            return result
    else:
        existing = (_followup_for_reminder(followup_store, reminder_id)
                    if reminder_id else None)
        if existing is not None:
            result = apply(
                text,
                now=now,
                preferences_path=preferences_path,
                followup_store=followup_store,
                followup_id=str(existing.get("id") or ""),
                guidance_root=guidance_root,
                scope=clean_scope,
                item_kind=str(item_kind or target.get("kind") or ""),
                provenance=provenance,
            )
            if not result.get("ok"):
                return result
        elif reminder_id:
            # The disposition is recorded, but this stage does not yet wire the
            # follow-through seeder to honour it. Never claim an enforcement
            # that has not happened.
            result = {
                "ok": True,
                "status": "recorded",
                "action": "stop" if kind == "stop" else "done",
                "recorded": True,
            }
        else:
            return {"ok": False, "status": "refused",
                    "reason": ("this stage cannot close that notice; nothing changed"),
                    "reason_code": "notice_not_supported"}

    disposition_key = (reminder_id
                       or (target_id if target_type == "reminder" else ""))
    if disposition_key:
        record = {
            "action": "stop" if kind == "stop" else "done",
            "target_id": target_id,
            "target_label": target_label,
            "turn_id": str(turn_id or ""),
            "pending_id": str(pending_id or ""),
            "source": provenance[:80],
            "note": ("owner recorded a stop for this item; follow-through "
                     "wiring is pending"
                     if kind == "stop" else
                     "owner marked this item done; follow-through wiring is pending"),
        }
        if defer_disposition:
            result["deferred_disposition"] = {"key": disposition_key,
                                              "record": record}
        else:
            try:
                pending_ack.record_disposition(
                    disposition_key, record, now=now,
                    store=(pending_store if pending_store is not None
                           else followup_store))
            except pending_ack.PendingAckError:
                return {"ok": False, "status": "refused",
                        "reason": "the acknowledgement record could not be kept",
                        "reason_code": "state_unreadable"}
        result["disposition"] = disposition_key
        if result.get("recorded"):
            result["note"] = f'Recorded your reply for “{target_label}”.'
    return finish(result)


def _receipt_for(
    effect: Mapping[str, Any],
    target: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    turn_id: str,
) -> dict[str, Any]:
    label = _safe_label(target.get("target_label"))
    action = str(effect.get("action") or result.get("action") or "")
    recorded = bool(result.get("recorded")) or str(
        result.get("status") or "") == "recorded"
    receipt: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "status": "recorded" if recorded else str(result.get("status") or ""),
        "action": action,
        "target_label": label,
        "turn_id": turn_id,
    }
    if effect.get("minutes") is not None:
        receipt["minutes"] = int(effect["minutes"])
    if effect.get("until"):
        receipt["until"] = str(effect["until"])
    if result.get("no_further_followup"):
        receipt["no_further_followup"] = True
    if not result.get("ok"):
        receipt["note"] = f'Nothing changed for “{label}”.'
    elif recorded:
        receipt["note"] = f'Recorded your reply for “{label}”.'
    elif action == "stop":
        receipt["note"] = f'Stopped follow-ups for “{label}”.'
    elif action == "done":
        receipt["note"] = f'Marked “{label}” as done.'
    elif effect.get("minutes") is not None:
        count = int(effect["minutes"])
        unit = "minute" if count == 1 else "minutes"
        receipt["note"] = f'Snoozed “{label}” for {count} {unit}.'
    else:
        receipt["note"] = f'Snoozed “{label}” until {_human_until(effect.get("until"))}.'
    return receipt


def apply_effect(
    effect: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    now: datetime,
    preferences_path: Path | str | None = None,
    followup_store: Any = None,
    guidance_root: Path | str | None = None,
    turn_id: str = "",
    pending_id: str = "",
    pending_store: Any = None,
    defer_disposition: bool = False,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Apply the frozen effect exactly as the owner was asked to confirm it."""
    action = str(effect.get("action") or "")
    if action == "snooze":
        if effect.get("minutes") is not None:
            text = f"later {int(effect['minutes'])}"
        else:
            text = f"later until {effect.get('until')}"
    else:
        text = action
    result = apply_target(
        text, target=target, now=now, preferences_path=preferences_path,
        followup_store=followup_store, guidance_root=guidance_root,
        scope=str(effect.get("scope") or ""),
        item_kind=str(effect.get("kind") or ""),
        turn_id=turn_id, pending_id=pending_id,
        provenance="owner confirmation", pending_store=pending_store,
        defer_disposition=defer_disposition,
        idempotency_key=idempotency_key,
    )
    result["receipt"] = _receipt_for(effect, target, result, turn_id=turn_id)
    return result


def propose_target(
    target: Mapping[str, Any],
    effect: Mapping[str, Any],
    *,
    store: Any,
    conversation: Any,
    owner: Any,
    turn: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Store one proposal for an already-pinned target and parsed intent."""
    target_label = _safe_label(target.get("target_label"))
    label_safe = target.get("label_safe")
    if label_safe is None:
        label_safe = _label_parts(target.get("target_label"))[1]
    if not label_safe and str(target.get("pinned_by") or "") != "reply":
        return {
            "ok": False,
            "status": "refused",
            "reason": ("that item has no safe public label; reply to the notice "
                       "itself and try again"),
            "reason_code": "label_unavailable",
        }
    # Freeze the effect from server-side target facts. The model's scope may
    # narrow which item was chosen; it can never become the stored matcher.
    frozen = dict(effect)
    frozen["scope"] = target_label
    frozen["kind"] = str(target.get("kind") or "")[:64]
    try:
        question = render_question(frozen, target_label)
    except ValueError as exc:
        return {"ok": False, "status": "refused", "reason": str(exc),
                "reason_code": "deadline_unrenderable"}
    created = pending_ack.create(
        conversation=conversation, owner=owner,
        target_id=target.get("target_id"),
        target_label=target_label,
        target_keys=sorted(target_gate_keys(target)),
        effect=frozen, question=question,
        turn_id=turn_key(conversation, owner, turn),
        turn_ref=turn_ref(turn),
        turn_at=_turn_at_iso(turn, now),
        now=now, store=store)
    if not created.get("ok"):
        return created
    pending_row = created.get("pending") or {}
    return {
        "ok": True,
        "status": "confirmation_required",
        "id": pending_row.get("id", ""),
        "ref": pending_row.get("id", ""),
        "pending": pending_row,
        "question": question,
        "effect": frozen,
        "target_label": target_label,
        "note": "Nothing has been changed. Confirm with one bare yes before any effect.",
    }


def propose(
    *,
    store: Any,
    conversation: Any,
    owner: Any,
    turn: Mapping[str, Any],
    action: Any,
    quote: Any = "",
    text: Any = "",
    minutes: Any = None,
    until: Any = None,
    kind: Any = "",
    scope: Any = "",
    target_id: Any = "",
    now: datetime,
) -> dict[str, Any]:
    """Build and store one proposal; writes no acknowledgement state."""
    message = str(text if str(text or "").strip() else turn.get("text") or "")
    problem = proposal_problem(message)
    if problem:
        return {"ok": False, "status": "refused", "reason": problem}
    raw_quote = str(quote or "")
    wanted_quote = raw_quote.strip()
    if not wanted_quote or raw_quote not in message:
        return {"ok": False, "status": "refused",
                "reason": "quote must be a literal part of the owner's current message"}
    if (str(action or "").strip().lower() in {"later", "snooze"}
            and minutes in (None, "") and until in (None, "")):
        parsed = parse(message, now=now)
        if parsed.get("action") == "snooze":
            minutes = parsed.get("minutes")
            until = parsed.get("until")
    try:
        effect = build_effect(action, minutes=minutes, until=until,
                              kind=kind, scope=scope, now=now)
    except ValueError as exc:
        return {"ok": False, "status": "refused", "reason": str(exc),
                "reason_code": "effect_invalid"}

    target, refusal = resolve_target(
        turn, store=store, conversation=conversation, owner=owner,
        model_target=target_id, message=message, now=now,
        question_effect=effect)
    if refusal is not None:
        return refusal
    return propose_target(
        target, effect, store=store, conversation=conversation, owner=owner,
        turn=turn, now=now)


def _reconcile_applied(
    stored: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    preferences_path: Path | str | None,
    followup_store: Any,
    turn_id: str,
) -> dict[str, Any] | None:
    """Rebuild the receipt for an effect whose marker save was interrupted."""
    effect = dict(stored.get("effect") or {})
    action = str(effect.get("action") or "")
    key = str(stored.get("key") or stored.get("id") or "")
    if action == "snooze" and key and preferences_path is not None:
        try:
            prefs = feedback.load_preferences_file(Path(preferences_path), strict=True)
        except feedback.FeedbackError:
            return None
        for row in prefs.get("snoozes", []) or []:
            if str(row.get("key") or "") != key:
                continue
            result = {"ok": True, "status": "snoozed", "action": "snooze",
                      "minutes": effect.get("minutes"),
                      "until": effect.get("until", "")}
            result["receipt"] = _receipt_for(effect, target, result,
                                             turn_id=turn_id)
            return result
        return None
    if action in {"done", "stop"} and followup_store is not None:
        followup_id = str(target.get("followup_id") or "")
        if not followup_id:
            return None
        try:
            entry = followups.get(followup_store, followup_id)
        except Exception:  # noqa: BLE001 - unreadable state cannot reconcile
            return None
        expected = "owner_stopped" if action == "stop" else "owner_confirmed"
        if (str(entry.get("status") or "") != "done"
                or str(entry.get("close_reason") or "") != expected):
            return None
        result = {
            "ok": True,
            "status": "stopped" if action == "stop" else "owner_confirmed",
            "action": "stop" if action == "stop" else "followup_closed",
        }
        reminder_id = str(target.get("reminder_id") or "")
        if reminder_id:
            label = _safe_label(target.get("target_label"))
            record = {
                "action": "stop" if action == "stop" else "done",
                "target_id": str(target.get("target_id") or ""),
                "target_label": label,
                "turn_id": str(stored.get("intent_turn_id") or turn_id),
                "pending_id": str(stored.get("id") or ""),
                "source": "owner confirmation",
                "note": ("owner recorded a stop for this item; follow-through "
                         "wiring is pending" if action == "stop" else
                         "owner marked this item done; follow-through wiring is pending"),
            }
            result["deferred_disposition"] = {"key": reminder_id,
                                              "record": record}
            result["disposition"] = reminder_id
        result["receipt"] = _receipt_for(effect, target, result,
                                         turn_id=turn_id)
        return result
    return None


def _confirmed_response(receipt: Mapping[str, Any],
                        row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "confirmed",
        "receipt": dict(receipt),
        "pending": dict(row),
        "effect": dict(row.get("effect") or {}),
    }


def confirm(
    *,
    store: Any,
    conversation: Any,
    owner: Any,
    turn: Mapping[str, Any],
    target_id: Any = "",
    now: datetime,
    preferences_path: Path | str | None = None,
    followup_store: Any = None,
    guidance_root: Path | str | None = None,
) -> dict[str, Any]:
    """Commit the stored effect once for one later bare affirmative owner turn."""
    text = str(turn.get("text") or "")
    if not is_bare_affirmative(text):
        return {"ok": False, "status": "refused",
                "reason": "confirmation needs one bare yes with no extra clause",
                "reason_code": "not_affirmative"}
    key = turn_key(conversation, owner, turn)
    try:
        state, row = pending_ack.lookup(
            store, conversation=conversation, owner=owner, turn_id=key, now=now)
    except pending_ack.PendingAckError:
        return {"ok": False, "status": "refused",
                "reason": "the pending acknowledgement could not be read",
                "reason_code": "pending_unreadable"}
    if state == "retry" and row is not None:
        return _confirmed_response(row.get("receipt") or {}, row)
    if state == "consumed" and row is not None:
        return {"ok": False, "status": "already_done", "reason": "already done",
                "reason_code": "already_done",
                "receipt": dict(row.get("receipt") or {}),
                "effect": dict(row.get("effect") or {})}
    if state == "expired":
        return {"ok": False, "status": "refused",
                "reason": "that confirmation expired; ask again",
                "reason_code": "expired"}
    if state == "ambiguous":
        return {"ok": False, "status": "refused",
                "reason": "more than one confirmation is waiting; which one do you mean?",
                "reason_code": "ambiguous_pending"}
    if state != "pending" or row is None:
        return {"ok": False, "status": "refused",
                "reason": "there is no confirmation waiting for this conversation",
                "reason_code": "no_pending"}

    stored_turn = str(row.get("turn_id") or "")
    stored_ref = str(row.get("turn_ref") or "")
    current_ref = turn_ref(turn)
    if key == stored_turn or (stored_ref and current_ref
                              and stored_ref == current_ref):
        return {"ok": False, "status": "refused",
                "reason": "the message that proposed this cannot confirm it",
                "reason_code": "same_turn"}
    current_at = _turn_datetime(turn.get("at")) or now
    stored_at = (_turn_datetime(row.get("turn_at"))
                 or _turn_datetime(row.get("created_at")))
    if stored_at is not None and current_at <= stored_at:
        return {"ok": False, "status": "refused",
                "reason": "the confirmation must be a strictly later message",
                "reason_code": "not_later"}

    target = _target_from_pending(row)
    supplied_targets = (target_id if isinstance(target_id, (list, tuple, set))
                        else [target_id])
    for supplied in supplied_targets:
        problem = _model_target_problem(target, supplied)
        if problem:
            return {"ok": False, "status": "refused", "reason": problem,
                    "reason_code": "target_mismatch"}
    effect = dict(row.get("effect") or {})
    pending_id = str(row.get("id") or "")
    idempotency_key = str(row.get("key") or row.get("id") or "")

    def apply_stored(stored: Mapping[str, Any]) -> dict[str, Any]:
        target_row = _target_from_pending(stored)
        reconciled = _reconcile_applied(
            stored, target_row, preferences_path=preferences_path,
            followup_store=followup_store,
            turn_id=str(stored.get("intent_turn_id") or key))
        if reconciled is not None:
            return reconciled
        return apply_effect(
            dict(stored.get("effect") or {}),
            target_row,
            now=now, preferences_path=preferences_path,
            followup_store=followup_store, guidance_root=guidance_root,
            turn_id=key, pending_id=pending_id, pending_store=store,
            defer_disposition=True, idempotency_key=idempotency_key,
        )

    result = pending_ack.commit(
        pending_id, apply_stored, conversation=conversation, owner=owner,
        turn_id=key, now=now, store=store)
    if result.get("status") == "already_confirmed":
        receipt = dict(result.get("receipt") or {})
        if str(receipt.get("turn_id") or "") == key:
            return _confirmed_response(receipt, result.get("pending") or {})
        return {"ok": False, "status": "already_done", "reason": "already done",
                "reason_code": "already_done", "receipt": receipt,
                "effect": dict((result.get("pending") or {}).get("effect") or {})}
    if result.get("ok"):
        result.setdefault("effect", effect)
    return result
