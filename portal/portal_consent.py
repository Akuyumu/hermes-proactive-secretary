"""Shared current-human consent helpers.

The model may describe a proposal, but only a real foreground human turn can
authorise it. These helpers read the turn the conversation hook stored, reject
quoted/reported/conditional/negated text, and require an affirmative reply that
is relevant to the exact action. They never read or invent host reply metadata.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

TURN_KEY = "responsibility_human_turn"
TURN_TTL_SECONDS = 30 * 60
MAX_TURNS = 24

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AFFIRMATIVE = re.compile(
    r"\b(?:yes|yeah|yep|yup|approve|approved|proceed|confirm|confirmed|ok|okay|"
    r"sure|go ahead|do it|accept|accepted|allow|allowed|grant|granted|save|"
    r"saved|apply|arm|enable|enabled)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|none|don't|dont|doesn't|does not|isn't|is not|won't|"
    r"will not|wouldn't|shouldn't|cannot|can't|cant|stop|cancel|decline|reject|"
    r"refuse|nope|nah|disapprove)\b", re.IGNORECASE)
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|maybe|perhaps|possibly|might|but|however|though|although|"
    r"except|only|wait|hold|pending|after|before|until|provided|depends?)\b",
    re.IGNORECASE)
_QUOTED = re.compile(r'["\u201c\u201d`]|^\s*>', re.MULTILINE)
_REPORTED = re.compile(
    r"\b(?:said|says|say|saying|wrote|writes|write|quoted?|quoting|according)\b",
    re.IGNORECASE)

_GRANT_ALIASES = {
    "mail": ("mail", "email", "inbox"),
    "calendar": ("calendar", "calendars"),
    "tasks": ("task", "tasks", "todo", "todos"),
    "public_sources": ("public", "public_sources", "public_feeds", "feeds", "web"),
    "memory": ("memory", "memories"),
    "discretionary": ("discretionary",),
}


class ConsentError(ValueError):
    """A model-supplied boolean or unrelated quote is not human authority."""


def clean(value: Any, limit: int = 2000) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return _CONTROL.sub("", text)[:limit]


def _facade(ctx: Any) -> Any:
    return getattr(ctx, "state", None) if ctx is not None else None


def _rows(ctx: Any) -> list[dict[str, Any]]:
    facade = _facade(ctx)
    if facade is None:
        return []
    try:
        raw = facade.get(TURN_KEY)
    except Exception as exc:  # noqa: BLE001 - unreadable authority denies
        raise ConsentError("human turn state could not be read") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConsentError("human turn state is corrupt")
    return [dict(row) for row in raw if isinstance(row, Mapping)]


def current_human_turn(ctx: Any, session_id: Any = "") -> dict[str, Any] | None:
    """Return the newest eligible human turn, or None when ambiguous/stale."""
    now = time.time()
    rows = [row for row in _rows(ctx)
            if float(row.get("at") or 0) >= now - TURN_TTL_SECONDS
            and clean(row.get("text"))]
    sid = clean(session_id, 200)
    if sid:
        rows = [row for row in rows if clean(row.get("session_id"), 200) == sid]
    if not rows:
        return None
    if not sid:
        sessions = {clean(row.get("session_id"), 200) for row in rows}
        if len(sessions) != 1:
            return None
    return max(rows, key=lambda row: float(row.get("at") or 0))


def quote_matches(turn: Mapping[str, Any] | None, quote: Any) -> bool:
    text = clean((turn or {}).get("text"), 4000)
    evidence = clean(quote)
    return bool(text and evidence and evidence in text)


def _reject_untrusted(text: str) -> None:
    if _QUOTED.search(text) or _REPORTED.search(text):
        raise ConsentError("quoted or reported text is not human consent")
    if _NEGATION.search(text):
        raise ConsentError("the current human turn negates or cancels this action")
    if _CONDITIONAL.search(text):
        raise ConsentError("a conditional or conflicting reply does not consent")
    if not _AFFIRMATIVE.search(text):
        raise ConsentError("the current human turn is not an explicit affirmative reply")


def positive_turn(ctx: Any, payload: Mapping[str, Any] | None, *,
                  purpose: str, words: tuple[str, ...] = (),
                  reference: str = "") -> dict[str, Any]:
    """Require an actual affirmative human turn relevant to the exact action."""
    payload = dict(payload or {})
    turn = current_human_turn(ctx, payload.get("session_id"))
    if turn is None:
        raise ConsentError(
            f"{purpose} requires a current eligible human conversation turn")
    quote = payload.get("evidence_quote")
    if clean(quote) and not quote_matches(turn, quote):
        raise ConsentError("evidence_quote is not present in the current human turn")
    text = clean(turn.get("text"))
    _reject_untrusted(text)
    lowered = text.lower()
    if reference and reference.lower() in lowered:
        return turn
    if words and any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
        return turn
    if not words and not reference:
        return turn
    raise ConsentError(
        f"{purpose} requires an answer about this exact current question, not a bare yes")


def require_grants(ctx: Any, payload: Mapping[str, Any] | None, grants: Mapping[str, Any],
                   *, state_review: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A positive grant must name its sources or match a server-owned review."""
    wanted = sorted(str(key) for key, value in grants.items() if value is True)
    if not wanted:
        return positive_turn(ctx, payload, purpose="grant change")
    turn = positive_turn(ctx, payload, purpose="grant change")
    text = clean(turn.get("text")).lower()
    missing = []
    for grant in wanted:
        aliases = _GRANT_ALIASES.get(grant, (grant.replace("_", " "), grant))
        if not any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases):
            missing.append(grant)
    if not missing:
        return turn
    review = dict(state_review or {})
    reviewed = {str(item) for item in (review.get("requested_grants") or [])}
    session = clean((payload or {}).get("session_id"), 200)
    if (review.get("kind") == "grants"
            and (not session or clean(review.get("session_id"), 200) == session)
            and set(wanted).issubset(reviewed) and reviewed):
        return turn
    raise ConsentError(
        "granting " + ", ".join(missing) + " requires naming that source explicitly; "
        "a bare yes cannot enable model-chosen permissions")


__all__ = ["TURN_KEY", "ConsentError", "clean", "current_human_turn",
           "quote_matches", "positive_turn", "require_grants"]
