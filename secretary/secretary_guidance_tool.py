"""Foreground tool: record and lift owner topic closures.

The owner's signal is often conversational — "just finished my exams", "I don't
need that cheat sheet any more" — so the model interprets it and flips this
switch. Enforcement is not the model's job: the deterministic store refuses a
matching notice at submit and again at emission.

Scope rules this tool keeps:
* closure applies to discretionary updates only, never to a requested reminder;
* closure is finite by default; permanence must be asked for;
* the write happens here, in the foreground, with the owner's words as the
  reason — a scheduled run may read the store but never write it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

try:  # package import
    from .. import paths
except ImportError:  # direct/standalone import
    import paths  # type: ignore

TOOL_NAME = "secretary_guidance"
TOOL_DESCRIPTION = (
    "Record or lift the owner's standing 'stop raising this' guidance so the "
    "notice lane obeys it deterministically. Use when the owner signals a topic "
    "is finished (e.g. exams are over, a sheet is no longer needed). Closing and "
    "reopening are two-step: first propose the exact change, then confirm only "
    "after the owner replies yes. Closing is temporary by default and never "
    "cancels a requested reminder."
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["close", "list", "reopen", "confirm", "cancel"],
                   "description": "close/reopen propose a change; confirm applies the shown ref"},
        "topics": {"type": "array", "items": {"type": "string"},
                   "description": "Topic labels exactly as the owner names them"},
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "Stable ids (event, follow-up, observation) to close"},
        "until": {"type": "string",
                  "description": "ISO date/time or a number of days; default 7 days"},
        "permanent": {"type": "boolean",
                      "description": "Only when the owner explicitly wants it permanent"},
        "id": {"type": "string", "description": "Entry id for reopen"},
        "ref": {"type": "string",
                "description": "The confirmation reference returned by close or reopen"},
        "reason": {"type": "string", "description": "The owner's own words"},
        "evidence_quote": {"type": "string",
                           "description": "Exact words from the owner's current message"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


TOOL_FUNCTION_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "parameters": TOOL_SCHEMA,
}


_ACTION_WORDS = re.compile(
    r"\b(?:close|stop|mute|drop|forget|hide|suppress|reopen|resume|undo)\b",
    re.IGNORECASE,
)
_NEGATION_ANY = re.compile(
    r"\b(?:no|not|never|don['\u2019]?t|do not|does not|"
    r"isn['\u2019]?t|won['\u2019]?t|will not|cannot|can['\u2019]?t|shouldn['\u2019]?t|"
    r"refuse|decline)\b",
    re.IGNORECASE,
)
_CONDITIONAL_TURN = re.compile(
    r"\b(?:if|unless|maybe|perhaps|possibly|might|"
    r"once|when|whenever|after|before|until|pending|wait|hold|depends?|"
    r"assuming|suppose|supposing|in\s+case|as\s+long\s+as|so\s+long\s+as|"
    r"provided)\b",
    re.IGNORECASE,
)
_CLOSE_CUE = re.compile(
    r"\b(?:done|finished|over|no longer|don['\u2019]?t need|do not need|"
    r"stop (?:notifying|sending|telling|showing)|close|drop|forget|mute)\b",
    re.IGNORECASE,
)
_REOPEN_CUE = re.compile(
    r"\b(?:reopen|un-?mute|resume|bring back|start again|undo)\b",
    re.IGNORECASE,
)
_PERMANENT = re.compile(
    r"\b(?:permanent|permanently|forever|always|indefinitely|for good)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE = re.compile(
    r"\b(?:yes|yeah|yep|confirm|confirmed|do it|please do|go ahead|ok(?:ay)?)\b",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|a)\s*"
    r"(day|week|month|year)s?\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NUMBER_WORDS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_CONFIRM_SUFFIX = ("; ask the owner to say it in their own words, then try again")
_PROPOSE_ASK = ("Show the owner this summary and ask them to reply with exactly the "
                "reply_with sentence. Anything else is refused. Then call action=confirm "
                "with this ref and their reply as evidence_quote.")


def _ensure_src() -> None:
    src = str(Path(paths.plugin_root()) / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _strip_duration_phrase(sentence: str) -> str:
    """Remove a stated duration before the conditional check.

    "until 2026-12-24" and "for 3 weeks" name how long the change lasts; they are
    not conditions. A bare "until" with no value stays a conditional.
    """
    text = sentence or ""
    text = re.sub(r"\buntil\s+\d{4}-\d{2}-\d{2}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:until|for)\s+\d+\s+(?:day|days|week|weeks|month|months)\b",
                  " ", text, flags=re.IGNORECASE)
    return text


def _requested_until(sentence: str) -> Any:
    """A duration the owner actually named, never a caller-supplied expiry."""
    match = _DURATION.search(sentence or "")
    if match:
        raw = match.group(1).lower()
        try:
            count = int(raw)
        except ValueError:
            count = _NUMBER_WORDS.get(raw)
        if count:
            return count * _UNIT_DAYS.get(match.group(2).lower(), 1)
    date = _ISO_DATE.search(sentence or "")
    return date.group(1) if date else None


def dispatch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public entry: reads only. Mutations must go through tool_handler, which
    proves the owner's turn; this surface never applies one."""
    body = dict(payload or {})
    if str(body.get("action") or "list").strip().lower() in (
            "close", "reopen", "confirm", "cancel"):
        return {"ok": False, "status": "refused",
                "error": "guidance changes require a verified owner turn"}
    return _apply_mutation(body)


def _apply_mutation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply an already-authorised mutation. Callers must have proved the owner's
    turn first; the only supported caller is tool_handler after its check."""
    _ensure_src()
    from proactive_pilot import guidance

    body = dict(payload or {})
    action = str(body.get("action") or "list").strip().lower()
    root = Path(paths.data_root())
    reason = str(body.get("reason") or "").strip()[:200]
    if action == "list":
        return guidance.list_active(root)
    if action == "reopen":
        entry_id = str(body.get("id") or "").strip()
        if not entry_id:
            return {"ok": False, "status": "refused", "error": "id is required"}
        return guidance.reopen(root, entry_id)
    if action != "close":
        return {"ok": False, "status": "refused", "error": f"unknown action {action!r}"}
    until: Any = body.get("until")
    if isinstance(until, str) and until.strip().isdigit():
        until = int(until.strip())
    result = guidance.close(
        root, body.get("topics") or [], keys=body.get("keys") or [],
        until=until, permanent=bool(body.get("permanent")),
        source=("owner: " + reason) if reason else "owner",
    )
    if result.get("ok"):
        result["note"] = ("Recorded. Matching discretionary notices are now refused "
                          "by the notice lane; requested reminders are unaffected.")
    return result


def _foreground_authority(ctx: Any, payload: dict[str, Any], *,
                         host_session_id: Any = None,
                         details: dict[str, Any] | None = None) -> tuple[bool, str, str]:
    """Positive host-attested proof bound to the requested change.

    A null execution, a TTY or an environment variable is not proof — delegated
    children of a scheduled run also report "not cron". The authoritative turn
    record the host keeps for a session is what we require, exactly as follow-ups
    and responsibilities do. The quote itself must name the action, target and
    duration being changed; when it does not, the caller must ask the owner to
    confirm in their own words.
    """
    if ctx is None:
        return False, "refused", "a foreground conversation context is required"
    # No task-id gate: Hermes mints an effective task id for every foreground
    # turn (agent/turn_context.py:641), so it identifies nothing. Foreground is
    # proved by the recorded turn below, which the pre_llm hook writes only for
    # an eligible human turn, plus the host-supplied session id.
    session_id = str(host_session_id or payload.get("session_id") or "").strip()
    if not session_id:
        return False, "refused", "session_id is required to prove an owner turn"
    try:
        from .secretary_responsibilities import latest_turn, _quote_matches
        from .secretary_followups import _turn_is_private_human
    except Exception:  # noqa: BLE001 - unavailable authority proof means refuse
        return False, "refused", "owner authority could not be checked"
    try:
        turn = latest_turn(ctx, session_id=session_id)
    except Exception:  # noqa: BLE001
        return False, "refused", "owner authority could not be checked"
    if turn is None:
        return False, "refused", "no current human conversation turn for this session"
    if not _turn_is_private_human(turn):
        return False, "refused", "a background or shared turn cannot change guidance"
    if not _quote_matches(payload.get("evidence_quote"), turn):
        return False, "refused", "evidence_quote must quote the owner's current message"

    quote = str(payload.get("evidence_quote") or "")
    action = str(payload.get("action") or "").strip().lower()
    turn_text = str(turn.get("text") or "")
    from .secretary_responsibilities import _QUOTED, _REPORTED

    sentences = re.split(r"(?<=[.!?;])\s+|\n+", turn_text)
    seen_candidate = False
    rejection = ""
    action_sentence = ""
    cursor = 0
    for sentence in sentences:
        found = _ACTION_WORDS.search(sentence)
        if not found:
            continue
        seen_candidate = True
        start = turn_text.find(sentence, cursor)
        if start < 0:
            start = cursor
        cursor = start + len(sentence)
        # The prefix is read from the whole host-recorded turn, so a negated
        # instruction cannot be laundered through a later "evidence" sentence.
        prefix = turn_text[:start + found.start()]
        if _NEGATION_ANY.search(prefix):
            rejection = rejection or "a negated instruction cannot change guidance"
            continue
        if sentence.rstrip().endswith("?"):
            rejection = rejection or "a question is not an instruction"
            continue
        if _CONDITIONAL_TURN.search(_strip_duration_phrase(sentence)):
            rejection = rejection or "a conditional turn cannot change guidance"
            continue
        if _QUOTED.search(sentence) or _REPORTED.search(sentence):
            rejection = rejection or "quoted or reported text cannot change guidance"
            continue
        if not action_sentence:
            action_sentence = sentence

    if not action_sentence:
        if seen_candidate and rejection:
            return False, "refused", rejection
        return False, "refused", (
            "the owner's words do not name the action to change" + _CONFIRM_SUFFIX)

    def _whole_word(value: str, *, plural: bool = False) -> re.Pattern[str]:
        suffix = r"(?:s)?" if plural else ""
        return re.compile(
            r"(?<![\w])" + re.escape(str(value).lower()) + suffix + r"(?![\w])",
            re.IGNORECASE)

    def _label_excluded(label: str) -> bool:
        stem = re.escape(label) + r"(?:s)?"
        return bool(
            re.search(r"\b(?:not|except|excluding|other\s+than|apart\s+from)\s+"
                      r"(?:the\s+)?" + stem + r"\b", action_sentence, re.IGNORECASE)
            or re.search(r"\b" + stem + r"\s+(?:not|no)\b",
                         action_sentence, re.IGNORECASE))

    if action == "close":
        if not _CLOSE_CUE.search(action_sentence):
            return False, "refused", (
                "the owner's words do not clearly close that topic" + _CONFIRM_SUFFIX)
        topics = payload.get("topics") or []
        labels = [str(label).strip() for label in topics if str(label).strip()]
        if topics:
            # Every requested label must be named: one authorised topic cannot
            # carry an extra one into the frozen payload.
            bound = bool(labels) and all(
                _whole_word(label, plural=True).search(action_sentence)
                and not _label_excluded(label)
                and _whole_word(label, plural=True).search(quote)
                for label in labels)
            if not bound:
                return False, "refused", (
                    "the owner's words do not name every topic to close" + _CONFIRM_SUFFIX)
    elif action == "reopen":
        if not _REOPEN_CUE.search(action_sentence):
            return False, "refused", (
                "the owner's words do not clearly reopen that topic" + _CONFIRM_SUFFIX)
        entry_id = str(payload.get("id") or "").strip()
        if not entry_id:
            return False, "refused", "id is required"
        try:
            _ensure_src()
            from proactive_pilot import guidance
            store = guidance.read_store(Path(paths.data_root()))
        except Exception:  # noqa: BLE001 - unavailable store means refuse
            return False, "refused", "guidance store could not be read"
        if not store.get("ok"):
            return False, "refused", store.get("error") or "guidance store could not be read"
        entry = next((row for row in store.get("entries") or []
                      if isinstance(row, dict) and row.get("id") == entry_id), None)
        if entry is None:
            return False, "refused", "unknown entry id"
        selectors = [str(label) for label in entry.get("labels") or []]
        selectors += [str(key) for key in entry.get("keys") or []]
        if not any(selector and _whole_word(selector).search(action_sentence)
                   for selector in selectors):
            return False, "refused", (
                "the owner's words do not name the entry being reopened")

    if bool(payload.get("permanent")):
        if re.search(r"\b(?:temporary|temporarily|for\s+now)\b",
                     action_sentence, re.IGNORECASE):
            return False, "refused", (
                "the owner's words do not say the change is permanent" + _CONFIRM_SUFFIX)
        permanent_ok = False
        for match in _PERMANENT.finditer(action_sentence):
            before = re.findall(r"[\w'\u2019]+", action_sentence[:match.start()])
            if not _NEGATION_ANY.search(" ".join(before[-3:])):
                permanent_ok = True
                break
        if not permanent_ok:
            return False, "refused", (
                "the owner's words do not say the change is permanent" + _CONFIRM_SUFFIX)
    if details is not None:
        details["action_sentence"] = action_sentence
    return True, "", ""


def _pending_source(payload: dict[str, Any]) -> str:
    reason = str(payload.get("reason") or "").strip()[:200]
    return ("owner: " + reason) if reason else "owner"


_AFFIRMATIVE_WORDS = frozenset({"yes", "yeah", "yep", "yup", "ok", "okay", "sure",
                               "confirm", "confirmed"})
_AFFIRMATIVE_FILLERS = frozenset({"yes", "yeah", "yep", "yup", "ok", "okay", "sure",
                                 "confirm", "confirmed", "do", "it", "please", "then",
                                 "go", "ahead", "that", "this", "the", "for", "now"})
_PAST_TENSE = re.compile(
    r"\b(?:did|already|yesterday|last\s+(?:week|month|night|time|year)|used\s+to|had|was|were)\b",
    re.IGNORECASE,
)


_QUESTION_START = re.compile(
    r"^\s*(?:should|shall|do|does|can|could|would|will|is|are|am|what|when|why|"
    r"how|which|who)\b",
    re.IGNORECASE,
)


def _normalise_reply(text: str) -> str:
    """Case, spacing and trailing punctuation do not matter; wording does.

    Question marks and quotation marks are kept: "yes, close exams?" and
    \"yes, close exams\" are not the sentence the owner was asked to send.
    """
    cleaned = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    return cleaned.rstrip("., ").strip()


def _standalone_affirmative(text: str) -> bool:
    """A short reply that only says yes.

    An affirmative word buried in an unrelated sentence ("yes I know, how is the
    weather") is not an approval of this change.
    """
    cleaned = re.sub(r"[^\w\s'-]", " ", str(text or "").lower())
    words = re.sub(r"\s+", " ", cleaned).strip().split()
    if not words:
        return False
    if " ".join(words) in {"do it", "go ahead", "please do", "please do it"}:
        return True
    if not any(word in _AFFIRMATIVE_WORDS for word in words[:2]):
        return False
    return all(word in _AFFIRMATIVE_FILLERS for word in words)


def _confirm(ctx: Any, payload: dict[str, Any], *,
             host_session_id: Any = None) -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot import guidance

    ref = str(payload.get("ref") or "").strip()
    session_id = str(host_session_id or "").strip()
    if not ref:
        return {"ok": False, "status": "refused", "error": "ref is required"}
    if not session_id:
        return {"ok": False, "status": "refused",
                "error": "session_id is required to confirm a change"}
    root = Path(paths.data_root())
    row = guidance.read_pending(root, ref)
    if row is None:
        return {"ok": False, "status": "refused",
                "error": "confirmation is unknown, expired or already used"}
    if str(row.get("session_id") or "") != session_id:
        return {"ok": False, "status": "refused",
                "error": "confirmation belongs to another session"}
    try:
        from .secretary_responsibilities import latest_turn, _quote_matches
        from .secretary_followups import _turn_is_private_human
    except Exception:  # noqa: BLE001 - unavailable authority proof means refuse
        return {"ok": False, "status": "refused",
                "error": "owner authority could not be checked"}
    try:
        turn = latest_turn(ctx, session_id=session_id)
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": "refused",
                "error": "owner authority could not be checked"}
    if turn is None:
        return {"ok": False, "status": "refused",
                "error": "no current human conversation turn for this session"}
    if not _turn_is_private_human(turn):
        return {"ok": False, "status": "refused",
                "error": "a background or shared turn cannot confirm guidance"}
    text = str(turn.get("text") or "")
    try:
        at = float(turn.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    try:
        created_epoch = float(row.get("created_epoch"))
    except (TypeError, ValueError):
        created = guidance._parse_iso(row.get("created_at"))
        created_epoch = created.timestamp() if created is not None else 0.0
    if at <= created_epoch:
        return {"ok": False, "status": "refused",
                "error": "the confirming reply is not newer than this request"}
    if not _quote_matches(payload.get("evidence_quote"), turn):
        return {"ok": False, "status": "refused",
                "error": "evidence_quote must quote the owner's confirming message"}
    quote = str(payload.get("evidence_quote") or "")
    expected = str(row.get("reply_with") or "").strip()
    if not expected:
        return {"ok": False, "status": "refused",
                "error": "this request has no confirmation wording; ask again"}
    # The owner replies with the exact sentence the proposal named. Anything else
    # -- a different topic, a narrower duration, an extra clause, a retraction or
    # a bare reference -- is not the confirmation that was asked for.
    if _normalise_reply(text) != _normalise_reply(expected):
        return {"ok": False, "status": "refused",
                "error": f'reply exactly: "{expected}"'}
    if _normalise_reply(quote) != _normalise_reply(expected):
        return {"ok": False, "status": "refused",
                "error": f'the quoted words must be exactly: "{expected}"'}
    pending_rows = guidance.pending_for_session(root, session_id)
    if not any(other.get("ref") == ref for other in pending_rows):
        return {"ok": False, "status": "refused",
                "error": "confirmation is unknown, expired or already used"}

    token = guidance.turn_token(session_id, at, text)
    if not guidance.consume_turn(root, token):
        return {"ok": False, "status": "refused",
                "error": "this turn already confirmed another change"}

    source = str(row.get("source") or "")
    reason = ""
    if source.startswith("owner: "):
        reason = source[len("owner: "):]
    elif source and source != "owner":
        reason = source
    apply_payload: dict[str, Any] = {
        "action": row.get("action"),
        "topics": row.get("topics") or [],
        "keys": row.get("keys") or [],
        "until": row.get("until"),
        "permanent": bool(row.get("permanent")),
        "reason": reason,
    }
    if str(row.get("action")) == "reopen":
        apply_payload["id"] = row.get("id")
    result = _apply_mutation(apply_payload)
    if result.get("ok"):
        guidance.consume_pending(root, ref)
    result["confirmed_ref"] = ref
    return result


def _cancel(payload: dict[str, Any], *, host_session_id: Any = None) -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot import guidance

    ref = str(payload.get("ref") or "").strip()
    session_id = str(host_session_id or "").strip()
    if not ref:
        return {"ok": False, "status": "refused", "error": "ref is required"}
    root = Path(paths.data_root())
    row = guidance.read_pending(root, ref)
    if row is None:
        return {"ok": False, "status": "refused",
                "error": "confirmation is unknown, expired or already used"}
    if str(row.get("session_id") or "") != session_id:
        return {"ok": False, "status": "refused",
                "error": "confirmation belongs to another session"}
    if not guidance.consume_pending(root, ref):
        return {"ok": False, "status": "refused",
                "error": "confirmation is unknown, expired or already used"}
    return {"ok": True, "status": "cancelled", "cancelled_ref": ref}


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, session_id: Any = None,
                task_id: Any = None, **_kwargs: Any) -> str:
        body = args or {}
        action = str(body.get("action") or "list").strip().lower()
        try:
            if action == "list":
                return json.dumps(dispatch(body), ensure_ascii=False, sort_keys=True)
            if action in ("close", "reopen"):
                details: dict[str, Any] = {}
                ok, status, why = _foreground_authority(
                    ctx, body, host_session_id=session_id, details=details)
                if not ok:
                    return json.dumps({"ok": False, "status": status, "error": why},
                                      ensure_ascii=False, sort_keys=True)
                _ensure_src()
                from proactive_pilot import guidance
                root = Path(paths.data_root())
                sid = str(session_id or body.get("session_id") or "").strip()
                if action == "reopen":
                    entry_id = str(body.get("id") or "").strip()
                    try:
                        store = guidance.read_store(root)
                    except Exception:  # noqa: BLE001
                        return json.dumps(
                            {"ok": False, "status": "refused",
                             "error": "guidance store could not be read"},
                            ensure_ascii=False, sort_keys=True)
                    if not store.get("ok"):
                        return json.dumps(
                            {"ok": False, "status": "refused",
                             "error": store.get("error") or "guidance store could not be read"},
                            ensure_ascii=False, sort_keys=True)
                    entry = next((row for row in store.get("entries") or []
                                  if isinstance(row, dict)
                                  and row.get("id") == entry_id), None)
                    if entry is None:
                        return json.dumps(
                            {"ok": False, "status": "refused",
                             "error": "unknown entry id"},
                            ensure_ascii=False, sort_keys=True)
                    pending = guidance.create_pending(
                        root, session_id=sid, action="reopen",
                        topics=entry.get("labels") or [], keys=entry.get("keys") or [],
                        until=None, permanent=False, source=_pending_source(body),
                        entry_id=entry_id)
                else:
                    permanent = bool(body.get("permanent"))
                    sentence = str(details.get("action_sentence") or "")
                    pending = guidance.create_pending(
                        root, session_id=sid, action="close",
                        topics=body.get("topics") or [], keys=body.get("keys") or [],
                        until=None if permanent else _requested_until(sentence),
                        permanent=permanent, source=_pending_source(body))
                if pending.get("ok") is False:
                    return json.dumps(pending, ensure_ascii=False, sort_keys=True)
                return json.dumps(
                    {"ok": False, "status": "confirmation_required",
                     "ref": pending["ref"], "summary": pending["summary"],
                     "reply_with": pending.get("reply_with", ""),
                     "ask": _PROPOSE_ASK},
                    ensure_ascii=False, sort_keys=True)
            if action == "confirm":
                return json.dumps(_confirm(ctx, body, host_session_id=session_id),
                                  ensure_ascii=False, sort_keys=True)
            if action == "cancel":
                return json.dumps(_cancel(body, host_session_id=session_id),
                                  ensure_ascii=False, sort_keys=True)
            return json.dumps({"ok": False, "status": "refused",
                               "error": f"unknown action {action!r}"},
                              ensure_ascii=False, sort_keys=True)
        except ValueError as exc:
            return json.dumps({"ok": False, "status": "refused", "error": str(exc)},
                              ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - never raise into the model loop
            return json.dumps({"ok": False,
                               "error": f"guidance failed ({type(exc).__name__})"},
                              ensure_ascii=False, sort_keys=True)
    return handler


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    register = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(register):
        return {"guidance_tool": False, "guidance_tool_error": "register_tool unavailable"}
    try:
        register(TOOL_NAME, "secretary", TOOL_FUNCTION_SCHEMA, tool_handler(ctx),
                 description=TOOL_DESCRIPTION, emoji="")
        return {"guidance_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        return {"guidance_tool": False,
                "guidance_tool_error": f"{type(exc).__name__}: {exc}"}


__all__ = ["TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "dispatch", "tool_handler",
           "register_tool"]
