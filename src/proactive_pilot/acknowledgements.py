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
from datetime import datetime, timedelta
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
        match = str(scope or topic or "").strip().lower()
        if not match:
            return {"ok": False, "status": "refused",
                    "reason": ("later needs the words it applies to; pass scope with the "
                               "notice's own subject so the snooze is not global")}
        request = {
            "action": "snooze",
            "kind": str(item_kind or "any"),
            "match": match[:200],
            "scope": str(scope or topic)[:200],
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
            return {"ok": False, "status": "refused", "reason": str(exc)}
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
    "resolve_target", "apply_target", "apply_effect", "propose", "confirm",
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
_REPLY_KEYS = (
    "reply_to", "reply_to_id", "reply_to_message_id", "reply_to_ref",
    "quoted_message_id", "quoted_id", "quote_id", "in_reply_to",
    "replied_to_id", "replied_to_message_id", "reply_message_id",
)
_REPLY_PARENTS = ("reply", "quoted", "quote", "reply_to_message", "reply_context")
_REPLY_INNER = ("id", "message_id", "ref", "notice_id", "event_key")


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


def _safe_label(value: Any) -> str:
    text = notice_voice._notice_fragment(value, 180)
    text = _EMAIL.sub(" ", text)
    text = _PLUGIN_HANDLE.sub(" ", text)
    text = notice_voice._ABSOLUTE_PATH_RE.sub(" ", text)
    text = text.replace("?", " ")
    text = " ".join(text.split()).strip(" \"'“”‘’.,;:-")
    if len(text) > 140:
        text = text[:140].rstrip(" ,;:-") + "..."
    return text or "the item you replied to"


def _human_until(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")[:32]
    return parsed.strftime("%d %b %Y %H:%M")


def render_question(effect: Mapping[str, Any], target_label: Any) -> str:
    label = _safe_label(target_label)
    action = str(effect.get("action") or "")
    if action == "stop":
        question = f"Stop follow-ups for “{label}” only?"
    elif action == "done":
        question = f"Mark “{label}” as done?"
    elif effect.get("minutes") is not None:
        count = int(effect["minutes"])
        unit = "minute" if count == 1 else "minutes"
        question = f"Snooze “{label}” for {count} {unit}?"
    else:
        question = f"Snooze “{label}” until {_human_until(effect.get('until'))}?"
    ok, _rule = notice_voice.check_notice_text(question)
    if not ok:
        label = "the item you replied to"
        if action == "stop":
            question = f"Stop follow-ups for “{label}” only?"
        elif action == "done":
            question = f"Mark “{label}” as done?"
        else:
            question = "Snooze that item until the proposed time?"
    return question


def _candidate(target_id: str, target_type: str, label: Any, *,
               aliases: Iterable[Any] = (), at: Any = "",
               **extra: Any) -> dict[str, Any]:
    clean_aliases = {target_id}
    for alias in aliases:
        text = str(alias or "").strip()
        if text:
            clean_aliases.add(text)
    row = {
        "target_id": target_id,
        "target_type": target_type,
        "target_label": _safe_label(label),
        "aliases": clean_aliases,
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
    if not existing.get("target_label") and row.get("target_label"):
        existing["target_label"] = row["target_label"]


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
        if rid:
            aliases.extend([rid, f"reminder:{rid}"])
            reminder_to_followup[rid] = fid
        _add_candidate(found, _candidate(
            f"followup:{fid}", "followup", entry.get("text"), aliases=aliases,
            at=entry.get("created_at") or entry.get("updated_at"),
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
        target_followup = ""
        if event_key.startswith("followup-notice:"):
            target_followup = event_key[len("followup-notice:"):].split(":", 1)[0]
        if target_followup and f"followup:{target_followup}" in found:
            row = found[f"followup:{target_followup}"]
            row["aliases"] = set(row["aliases"]) | {
                alias for alias in (event_key, notice_id) if alias}
            continue
        candidate_id = notice_id or event_key
        if not candidate_id:
            continue
        _add_candidate(found, _candidate(
            f"notice:{candidate_id}", "notice", delivery.get("message"),
            aliases=[notice_id, event_key], at=delivery.get("at"),
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
    aliases.add(str(target.get("target_id") or ""))
    for key in ("followup_id", "reminder_id", "notice_id"):
        value = str(target.get(key) or "")
        if value:
            aliases.add(value)
    return {alias for alias in aliases if alias}


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
        **extra,
    }


_SELECTION_STOPWORDS = frozenset({
    "the", "this", "that", "these", "those", "about", "with", "from", "your",
    "you", "and", "for", "its", "it", "reminder", "reminders", "notice",
    "notices", "item", "items", "yes", "okay", "ok", "please", "stop",
    "done", "later", "snooze", "not", "dont", "don't",
})


def _label_terms(label: Any) -> list[str]:
    """Distinctive words from a visible label; used for owner selection only."""
    words = [w for w in re.split(r"[^0-9a-z]+", str(label or "").lower()) if len(w) >= 4]
    return [w for w in dict.fromkeys(words) if w not in _SELECTION_STOPWORDS]


def select_by_label(candidates: Iterable[Mapping[str, Any]], message: Any
                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The owner naming an item's own words selects it; ambiguity selects nothing.

    This is an owner-authored selection, not model authority: the words must be
    present in the message the owner actually sent, and exactly one item may
    own them. Two items sharing the words leave both in play.
    """
    text = " " + " ".join(str(message or "").lower().split()) + " "
    scored: list[tuple[int, dict[str, Any]]] = []
    for candidate in candidates:
        terms = _label_terms(candidate.get("target_label"))
        hits = sum(1 for term in terms if f" {term} " in text or f" {term}," in text)
        if hits:
            scored.append((hits, dict(candidate)))
    if not scored:
        return None, []
    best = max(hits for hits, _row in scored)
    winners = [row for hits, row in scored if hits == best]
    if len(winners) == 1:
        return winners[0], []
    return None, winners


def _no_pin_question(candidates: Iterable[Mapping[str, Any]]) -> str:
    labels: list[str] = []
    for candidate in candidates:
        label = _safe_label(candidate.get("target_label"))
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    if not labels:
        return "Which item do you mean? Reply to the notice you mean."
    if len(labels) == 1:
        return f"Which item do you mean: “{labels[0]}”?"
    joined = ", ".join(f"“{label}”" for label in labels[:-1])
    return f"Which item do you mean: {joined} or “{labels[-1]}”?"


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
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pin one target from host facts; the model's id is never authority."""
    try:
        candidates = target_candidates(store, now=now)
    except ValueError as exc:
        return None, {"ok": False, "status": "refused", "reason": str(exc)}

    replies = _reply_ids(turn)
    matches = _matches_reply(candidates, replies)
    distinct = {str(row.get("target_id") or ""): row for row in matches}
    if len(distinct) == 1:
        target = dict(next(iter(distinct.values())))
        target["aliases"] = target_aliases(target)
        problem = _model_target_problem(target, model_target)
        if problem:
            return None, {"ok": False, "status": "refused", "reason": problem}
        return target, None
    if len(distinct) > 1:
        return None, {
            "ok": False, "status": "needs_clarification",
            "reason": _no_pin_question(distinct.values()),
        }

    # No trusted reply: the owner may have named the item in their own words.
    # That selection is owner-authored and must name exactly one item.
    selected, ambiguous = select_by_label(candidates, message or turn.get("text"))
    if selected is not None:
        problem = _model_target_problem(selected, model_target)
        if problem:
            return None, {"ok": False, "status": "refused", "reason": problem}
        selected["aliases"] = target_aliases(selected)
        return selected, None
    if ambiguous:
        return None, {
            "ok": False, "status": "needs_clarification",
            "reason": _no_pin_question(ambiguous),
        }

    if allow_pending:
        try:
            pending = pending_ack.find_pin(
                store, conversation=conversation, owner=owner, now=now)
        except pending_ack.PendingAckError as exc:
            return None, {"ok": False, "status": "refused",
                          "reason": f"pending acknowledgement refused: {exc}"}
        if pending is not None:
            target = _target_from_pending(pending)
            problem = _model_target_problem(target, model_target)
            if problem:
                return None, {"ok": False, "status": "refused", "reason": problem}
            return target, None

    return None, {
        "ok": False, "status": "needs_clarification",
        "reason": _no_pin_question(candidates),
    }


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
        )
        return result

    if followup_id and followup_store is not None:
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
        else:
            result = {
                "ok": True,
                "status": "stopped" if kind == "stop" else "owner_confirmed",
                "action": "stop" if kind == "stop" else "topic_closed",
            }

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
            "note": ("no further follow-up for this item"
                     if kind == "stop" else "owner marked the item done"),
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
            except pending_ack.PendingAckError as exc:
                return {"ok": False, "status": "refused", "reason": str(exc)}
        result["disposition"] = disposition_key
        result["no_further_followup"] = True
        result["note"] = (
            f'No further follow-up will be raised for “{target_label}”.'
            if kind == "stop" else
            f'“{target_label}” is recorded as done; no further follow-up is needed.')
    return result


def _receipt_for(
    effect: Mapping[str, Any],
    target: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    turn_id: str,
) -> dict[str, Any]:
    label = _safe_label(target.get("target_label"))
    action = str(effect.get("action") or result.get("action") or "")
    receipt: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "status": str(result.get("status") or ""),
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
    elif action == "stop":
        receipt["note"] = (f'Stopped follow-ups for “{label}”. '
                           "No further follow-up will be raised for that item.")
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
    )
    result["receipt"] = _receipt_for(effect, target, result, turn_id=turn_id)
    return result


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
        return {"ok": False, "status": "refused", "reason": str(exc)}

    target, refusal = resolve_target(
        turn, store=store, conversation=conversation, owner=owner,
        model_target=target_id, message=message, now=now)
    if refusal is not None:
        return refusal
    question = render_question(effect, target.get("target_label"))
    created = pending_ack.create(
        conversation=conversation, owner=owner,
        target_id=target.get("target_id"),
        target_label=_safe_label(target.get("target_label")),
        effect=effect, question=question,
        turn_id=turn_key(conversation, owner, turn), now=now, store=store)
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
        "effect": effect,
        "target_label": _safe_label(target.get("target_label")),
        "note": "Nothing has been changed. Confirm with one bare yes before any effect.",
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
    """Commit the stored effect once for one bare affirmative owner turn."""
    text = str(turn.get("text") or "")
    if not is_bare_affirmative(text):
        return {"ok": False, "status": "refused",
                "reason": "confirmation needs one bare yes with no extra clause"}
    key = turn_key(conversation, owner, turn)
    try:
        state, row = pending_ack.lookup(
            store, conversation=conversation, owner=owner, turn_id=key, now=now)
    except pending_ack.PendingAckError as exc:
        return {"ok": False, "status": "refused",
                "reason": f"pending acknowledgement refused: {exc}"}
    if state == "retry" and row is not None:
        return {"ok": True, "status": "confirmed",
                "receipt": dict(row.get("receipt") or {}), "pending": row}
    if state == "consumed" and row is not None:
        return {"ok": False, "status": "already_done", "reason": "already done",
                "receipt": dict(row.get("receipt") or {})}
    if state == "expired":
        return {"ok": False, "status": "refused",
                "reason": "that confirmation expired; ask again"}
    if state == "ambiguous":
        return {"ok": False, "status": "refused",
                "reason": "more than one confirmation is waiting; which one do you mean?"}
    if state != "pending" or row is None:
        return {"ok": False, "status": "refused",
                "reason": "there is no confirmation waiting for this conversation"}

    target = _target_from_pending(row)
    supplied_targets = (target_id if isinstance(target_id, (list, tuple, set))
                        else [target_id])
    for supplied in supplied_targets:
        problem = _model_target_problem(target, supplied)
        if problem:
            return {"ok": False, "status": "refused", "reason": problem}
    effect = dict(row.get("effect") or {})
    pending_id = str(row.get("id") or "")

    def apply_stored(stored: Mapping[str, Any]) -> dict[str, Any]:
        return apply_effect(
            dict(stored.get("effect") or {}),
            _target_from_pending(stored),
            now=now, preferences_path=preferences_path,
            followup_store=followup_store, guidance_root=guidance_root,
            turn_id=key, pending_id=pending_id, pending_store=store,
            defer_disposition=True,
        )

    result = pending_ack.commit(
        pending_id, apply_stored, conversation=conversation, owner=owner,
        turn_id=key, now=now, store=store)
    if result.get("status") == "already_confirmed":
        receipt = dict(result.get("receipt") or {})
        if str(receipt.get("turn_id") or "") == key:
            return {"ok": True, "status": "confirmed", "receipt": receipt,
                    "pending": result.get("pending")}
        return {"ok": False, "status": "already_done", "reason": "already done",
                "receipt": receipt}
    if result.get("ok"):
        result.setdefault("effect", effect)
    return result
