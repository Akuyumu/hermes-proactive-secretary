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
    "is finished (e.g. exams are over, a sheet is no longer needed). Closing is "
    "temporary by default and never cancels a requested reminder."
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["close", "list", "reopen"]},
        "topics": {"type": "array", "items": {"type": "string"},
                   "description": "Topic labels exactly as the owner names them"},
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "Stable ids (event, follow-up, observation) to close"},
        "until": {"type": "string",
                  "description": "ISO date/time or a number of days; default 7 days"},
        "permanent": {"type": "boolean",
                      "description": "Only when the owner explicitly wants it permanent"},
        "id": {"type": "string", "description": "Entry id for reopen"},
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
_CONFIRM_SUFFIX = ("; ask the owner to confirm in their own words and call again "
                   "with that reply as evidence_quote")


def _ensure_src() -> None:
    src = str(Path(paths.plugin_root()) / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def dispatch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public entry: reads only. Mutations must go through tool_handler, which
    proves the owner's turn; this surface never applies one."""
    body = dict(payload or {})
    if str(body.get("action") or "list").strip().lower() in ("close", "reopen"):
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
                         host_session_id: Any = None) -> tuple[bool, str, str]:
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
        if _CONDITIONAL_TURN.search(sentence):
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
        return False, "confirmation_required", (
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
            return False, "confirmation_required", (
                "the owner's words do not clearly close that topic" + _CONFIRM_SUFFIX)
        topics = payload.get("topics") or []
        labels = [str(label).strip() for label in topics if str(label).strip()]
        if topics:
            bound = any(
                _whole_word(label, plural=True).search(action_sentence)
                and not _label_excluded(label)
                and _whole_word(label, plural=True).search(quote)
                for label in labels if len(label) >= 3)
            if not bound:
                return False, "confirmation_required", (
                    "the owner's words do not name the topic to close" + _CONFIRM_SUFFIX)
    elif action == "reopen":
        if not _REOPEN_CUE.search(action_sentence):
            return False, "confirmation_required", (
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
            return False, "confirmation_required", (
                "the owner's words do not say the change is permanent" + _CONFIRM_SUFFIX)
        permanent_ok = False
        for match in _PERMANENT.finditer(action_sentence):
            before = re.findall(r"[\w'\u2019]+", action_sentence[:match.start()])
            if not _NEGATION_ANY.search(" ".join(before[-3:])):
                permanent_ok = True
                break
        if not permanent_ok:
            return False, "confirmation_required", (
                "the owner's words do not say the change is permanent" + _CONFIRM_SUFFIX)
    return True, "", ""


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, session_id: Any = None,
                task_id: Any = None, **_kwargs: Any) -> str:
        body = args or {}
        action = str(body.get("action") or "list").strip().lower()
        try:
            if action != "list":
                ok, status, why = _foreground_authority(
                    ctx, body, host_session_id=session_id)
                if not ok:
                    result = {"ok": False, "status": status, "error": why}
                    if status == "confirmation_required":
                        result["ask"] = (
                            "Ask the owner to confirm this exact change in their own "
                            "words, then call again with their reply as evidence_quote.")
                    return json.dumps(result, ensure_ascii=False, sort_keys=True)
                return json.dumps(_apply_mutation(body),
                                  ensure_ascii=False, sort_keys=True)
            return json.dumps(dispatch(body), ensure_ascii=False, sort_keys=True)
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
