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

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import feedback, guidance
from . import followups

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
        request = {
            "action": "snooze",
            "scope": str(scope or topic or "acknowledged notice")[:200],
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


__all__ = ["parse", "apply"]
