"""Foreground tool: apply one short owner acknowledgement to the existing stores.

The owner answers a notice the way people answer a person: "done", "later 20",
"stop". This tool records that answer in the stores that already exist — the
feedback preferences file (snooze), the guidance store (topic closure) and the
follow-up store (owner_confirmed / owner_stopped). It invents no state format,
no scheduler and no second config path.

Authority rules, identical to the guidance tool:
* only a host-attested foreground turn of the owning profile may write;
* a scheduled run, a delegated child or a replayed transcript cannot;
* the quoted words must be present in the owner's current message.

Anything unrecognised is refused with a reason and changes nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:  # package import
    from .. import paths
except ImportError:  # direct/standalone import
    import paths  # type: ignore

try:  # reuse the guidance tool's attested-foreground gate verbatim
    from .secretary_guidance_tool import _attested_owner_turn, _ensure_src
except ImportError:  # direct/standalone import
    from secretary_guidance_tool import _attested_owner_turn, _ensure_src  # type: ignore

TOOL_NAME = "secretary_acknowledge"
TOOL_DESCRIPTION = (
    "Apply, propose or confirm the owner's reply to a notice or reminder. "
    "mode=apply accepts the exact short answers 'done', 'later <n>', "
    "'later until <when>' and 'stop' from the whole recorded owner message; "
    "mode=propose interprets the owner's sentence but writes nothing except a "
    "short confirmation question; mode=confirm accepts only one bare yes for "
    "that exact proposal. The target is always pinned from the message the owner "
    "replied to (or an earlier proposal), never from a model-supplied id. "
    "A stop is scoped to that item only (never a global mute). Anything else is "
    "refused and changes nothing."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["apply", "propose", "confirm"],
                 "default": "apply",
                 "description": "apply the exact words, propose a confirmation, or confirm it"},
        "text": {"type": "string",
                 "description": "The owner's acknowledgement, e.g. 'done', 'later 20', 'stop'"},
        "action": {"type": "string", "enum": ["done", "later", "stop"],
                   "description": "For mode=propose: the interpreted action"},
        "quote": {"type": "string",
                  "description": "For mode=propose: exact words from the owner's current message"},
        "minutes": {"type": "integer",
                    "description": "For mode=propose with action=later: whole minutes"},
        "until": {"type": "string",
                  "description": "For mode=propose with action=later: timezone-aware ISO time"},
        "target_id": {"type": "string",
                      "description": "The item the model believes the owner answered; checked, never trusted"},
        "followup_id": {"type": "string",
                        "description": "Follow-up id the notice belonged to, when known"},
        "reminder_id": {"type": "string",
                        "description": "Reminder id the notice belonged to, when known"},
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "Stable ids (event, follow-up, observation) to close"},
        "scope": {"type": "string",
                  "description": ("Optional words that help narrow which item was meant; "
                                  "the stored matcher is always the server-pinned target id")},
        "kind": {"type": "string",
                 "description": "Optional item kind for a scoped snooze (default: any)"},
        "evidence_quote": {"type": "string",
                           "description": "Exact words from the owner's current message"},
    },
    "required": ["text", "evidence_quote"],
    "additionalProperties": False,
}

TOOL_FUNCTION_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "parameters": TOOL_SCHEMA,
}


def _refusal(status: str, reason: str, code: str = "") -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": str(reason)[:300],
            "reason_code": str(code or status)[:40]}


_PUBLIC_STATUS = {
    "ok": "ok",
    "applied": "ok",
    "snoozed": "snoozed",
    "recorded": "recorded",
    "stopped": "stopped",
    "owner_confirmed": "done",
    "topic_closed": "done",
    "followup_closed": "done",
    "confirmation_required": "confirmation_required",
    "confirmed": "confirmed",
    "needs_clarification": "needs_clarification",
    "refused": "refused",
    "already_done": "already_done",
}
_ACTION_WORDS = {"done", "later", "stop"}
_CODE_SENTENCES = {
    "unknown_mode": "That request mode is not available; nothing changed.",
    "no_text": "Give the acknowledgement words first; nothing changed.",
    "delegated": "A delegated run cannot change acknowledgements.",
    "scheduled": "A scheduled run cannot change acknowledgements.",
    "unattested": "Only a current private message from the owner can change acknowledgements.",
    "foreign_profile": "A turn from another profile cannot change acknowledgements.",
    "proposal_live": "A confirmation is already waiting; answer it before asking for another one.",
    "same_turn": "That message proposed the change; a later message must confirm it.",
    "not_later": "That message proposed the change; a later message must confirm it.",
    "turn_consumed": "That owner message was already used; nothing changed.",
    "not_affirmative": "Reply with one bare yes to confirm.",
    "already_done": "That confirmation is already done.",
    "expired": "That confirmation expired; ask me again.",
    "ambiguous_pending": "More than one confirmation is waiting; say which item you mean.",
    "no_pending": "There is no confirmation waiting for this conversation.",
    "target_mismatch": "That item does not match what the owner replied to; nothing changed.",
    "deadline_unrenderable": "The proposed deadline could not be rendered exactly; nothing changed.",
    "label_unavailable": "That item has no safe public label; reply to the notice itself and try again.",
    "notice_not_supported": "This stage cannot close that notice; nothing changed.",
    "quote_invalid": "The quoted words are not in the owner's current message.",
    "not_instruction": "That sentence cannot be proposed as an acknowledgement.",
    "effect_invalid": "That acknowledgement is not valid; nothing changed.",
    "unmatched_reply": "That reply does not point to a current item.",
    "ambiguous_reply": "That reply points to more than one item.",
    "ambiguous_label": "More than one item matches those words.",
    "ambiguous_position": "That choice matches more than one item.",
    "clarify_target": "Which item do you mean?",
    "no_candidate": "There is no current item to apply that to; nothing changed.",
    "store_unreadable": "The acknowledgement store could not be read; nothing changed.",
    "pending_unreadable": "The acknowledgement state could not be read; nothing changed.",
    "preferences_refused": "The snooze could not be saved; nothing changed.",
    "state_unreadable": "The acknowledgement state could not be read; nothing changed.",
    "followup_unavailable": "The follow-up store is unavailable; nothing changed.",
    "internal_error": "That acknowledgement could not be applied; nothing changed.",
    "unrecognised": "That acknowledgement was not recognised; nothing changed.",
    "refused": "That acknowledgement could not be applied; nothing changed.",
}
_SAFE_ID_RE = re.compile(
    r"\b(?:ack|fu|rem|reminder|followup|notice|conversation|resp|sec|cal|mail|"
    r"task|thread|evt|msg|artifact|handle|obs|cand)[_-][A-Za-z0-9][A-Za-z0-9_-]{1,}\b",
    re.IGNORECASE,
)


def _public_modules():
    _ensure_src()
    from proactive_pilot import acknowledgements as ack
    from proactive_pilot import notice_voice
    return ack, notice_voice


def _public_text(value: Any, fallback: str = "") -> str:
    try:
        ack, voice = _public_modules()
    except Exception:  # noqa: BLE001 - the shaper itself must stay safe
        text = " ".join(str(value or "").split())
        return text[:240] or fallback
    # Sanitise identifiers without the notice fragment's end-strip, which would
    # remove a closing curly quote and leave the sentence unbalanced.
    text = str(value or "")
    text = voice._CITATION.sub(" ", text)
    text = voice._URL.sub(" ", text)
    text = voice._REF_TOKEN.sub(" ", text)
    text = voice._NOTICE_ARTIFACT.sub(" ", text)
    text = voice._NOTICE_PATH.sub(" ", text)
    text = voice._NOTICE_LONG_TOKEN.sub(" ", text)
    text = voice._NOTICE_OPAQUE_ID.sub(" ", text)
    text = voice._NOTICE_HEX.sub(" ", text)
    text = voice._CONTROL.sub(" ", text)
    text = ack._EMAIL.sub(" ", text)
    text = ack._PLUGIN_HANDLE.sub(" ", text)
    text = voice._ABSOLUTE_PATH_RE.sub(" ", text)
    text = _SAFE_ID_RE.sub(" ", text)
    text = " ".join(text.replace("<", " ").replace(">", " ").split())
    text = text.strip(".,;:-| ")
    if len(text) > 200:
        text = text[:197].rstrip(" ,;:-") + "..."
    if not text:
        return fallback
    if text[-1:] not in ".?!":
        text += "."
    return text


def _public_label(value: Any) -> str:
    try:
        ack, _voice = _public_modules()
        text = ack._safe_label(value)
    except Exception:  # noqa: BLE001
        text = " ".join(str(value or "").split())[:60]
    text = _SAFE_ID_RE.sub(" ", text)
    text = " ".join(text.split()).strip(" \"'“”‘’.,;:-")
    return text[:60].rstrip(" ,;:-") or "the item you replied to"


def _public_deadline(values: Any) -> str:
    data = values if isinstance(values, dict) else {}
    if data.get("minutes") is not None:
        try:
            count = int(data["minutes"])
        except (TypeError, ValueError):
            return ""
        return f"{count} minute" if count == 1 else f"{count} minutes"
    until = data.get("until")
    if until:
        try:
            ack, _voice = _public_modules()
            return ack._human_until(until)
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _public_response(*, ok: bool, status: Any, action: Any = "",
                     label: Any = "", deadline: Any = "", message: Any = "",
                     reason_code: Any = "", reason_message: Any = "") -> dict[str, Any]:
    status_text = _PUBLIC_STATUS.get(str(status or "").strip().lower(), "refused")
    action_text = str(action or "").strip().lower()
    action_text = {
        "snooze": "later",
        "snoozed": "later",
        "followup_closed": "done",
        "topic_closed": "done",
        "owner_confirmed": "done",
    }.get(action_text, action_text)
    if action_text not in _ACTION_WORDS:
        action_text = ""
    sentence = _public_text(message or reason_message, "")
    body: dict[str, Any] = {
        "ok": bool(ok),
        "status": status_text,
        "action": action_text,
        "target_label": _public_label(label) if label else "",
        "deadline": _public_deadline(deadline) if isinstance(deadline, dict) else "",
        "message": sentence,
    }
    if not ok:
        code = re.sub(r"[^a-z0-9_]+", "_", str(reason_code or "refused").lower())
        code = code.strip("_")[:40] or "refused"
        fixed = _CODE_SENTENCES.get(code)
        if status_text == "needs_clarification" and sentence:
            body["message"] = sentence
        else:
            body["message"] = _public_text(fixed or _CODE_SENTENCES["refused"], "")
            sentence = body["message"]
        body["reason"] = {"code": code, "message": sentence}
    return body


def _public_refusal(code: str, status: str = "refused",
                    message: Any = "") -> dict[str, Any]:
    return _public_response(ok=False, status=status, reason_code=code,
                            reason_message=message)


def _infer_code(result: dict[str, Any]) -> str:
    code = str(result.get("reason_code") or "").strip()
    if code:
        return code
    reason = str(result.get("reason") or "").lower()
    if "literal part" in reason:
        return "quote_invalid"
    if "negat" in reason or "conditional" in reason or "question is not" in reason:
        return "not_instruction"
    if "bare yes" in reason:
        return "not_affirmative"
    if "already done" in reason:
        return "already_done"
    if "expired" in reason:
        return "expired"
    if "more than one" in reason:
        return "ambiguous_pending"
    if "no confirmation waiting" in reason:
        return "no_pending"
    if "does not match" in reason:
        return "target_mismatch"
    if "unrecognised acknowledgement" in reason:
        return "unrecognised"
    if "could not be read" in reason or "corrupt" in reason:
        return "store_unreadable"
    return "refused"


def _public_failure(result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "refused")
    code = _infer_code(result)
    message = result.get("reason") if status == "needs_clarification" else ""
    return _public_response(
        ok=False, status=status, reason_code=code, reason_message=message)


def _public_proposal(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return _public_failure(result)
    effect = dict(result.get("effect") or {})
    return _public_response(
        ok=True, status="confirmation_required", action=effect.get("action"),
        label=result.get("target_label"), deadline=effect,
        message=result.get("question"))


def _public_confirmation(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return _public_failure(result)
    receipt = dict(result.get("receipt") or {})
    effect = dict(result.get("effect") or {})
    deadline = effect or receipt
    return _public_response(
        ok=True, status=receipt.get("status") or "confirmed",
        action=receipt.get("action") or effect.get("action"),
        label=receipt.get("target_label"),
        deadline=deadline, message=receipt.get("note") or "Confirmed.")


def _public_application(result: dict[str, Any], *, target_label: Any,
                        effect: Any) -> dict[str, Any]:
    if not result.get("ok"):
        return _public_failure(result)
    receipt = dict(result.get("receipt") or {})
    action = receipt.get("action") or result.get("action") or effect.get("action")
    deadline = receipt or effect
    note = receipt.get("note") or result.get("note") or "Recorded your reply."
    return _public_response(
        ok=True, status=receipt.get("status") or result.get("status") or "ok",
        action=action, label=receipt.get("target_label") or target_label,
        deadline=deadline, message=note)


def _current_execution(ctx: Any) -> Any:
    """The live execution when this turn is scheduled/delegated, else None.

    An unreadable execution counts as not-foreground: a cached human turn must
    never authorise a write from a background run.
    """
    if ctx is None:
        return None
    getter = getattr(ctx, "current_cron_execution", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - unknown execution means refuse
        return object()


def _apply_unattested(payload: dict[str, Any] | None = None, *,
                      now: Any = None) -> dict[str, Any]:
    """Apply one acknowledgement. Private: the handler attests the turn first."""
    body = payload or {}
    _ensure_src()
    from proactive_pilot import acknowledgements, state
    from proactive_pilot.clock import Clock

    clock = Clock()
    when = now if now is not None else clock.now()
    store = state.StateStore(paths.state_dir(), clock)
    followup_id = str(body.get("followup_id") or "").strip()
    try:
        target_id = str(body.get("target_id") or "").strip()
        if target_id:
            target = {
                "target_id": target_id,
                "target_type": str(body.get("target_type") or "").strip(),
                "target_label": str(body.get("target_label") or "").strip(),
                "followup_id": followup_id,
                "reminder_id": str(body.get("reminder_id") or "").strip(),
                "notice_id": str(body.get("notice_id") or "").strip(),
                "kind": str(body.get("kind") or "").strip(),
            }
            return acknowledgements.apply_target(
                body.get("text"),
                target=target,
                now=when,
                preferences_path=paths.preferences_path(),
                followup_store=store,
                guidance_root=Path(paths.data_root()),
                scope=str(body.get("scope") or "").strip(),
                item_kind=str(body.get("kind") or "").strip(),
                turn_id=str(body.get("turn_id") or "").strip(),
                pending_store=store,
                idempotency_key=str(body.get("key")
                                    or body.get("turn_id") or "").strip(),
                consume_turn=True,
            )
        return acknowledgements.apply(
            body.get("text"),
            now=when,
            preferences_path=paths.preferences_path(),
            followup_store=store,
            followup_id=followup_id,
            topic=str(body.get("topic") or "").strip(),
            keys=[str(key) for key in (body.get("keys") or []) if str(key).strip()],
            guidance_root=Path(paths.data_root()),
            scope=str(body.get("scope") or "").strip(),
            item_kind=str(body.get("kind") or "").strip(),
        )
    except Exception:  # noqa: BLE001 - bounded refusal, never a traceback
        return _refusal("refused",
                        "the acknowledgement could not be applied; nothing changed",
                        "internal_error")


def _owner_for(ctx: Any, turn: dict[str, Any]) -> str:
    for key in ("owner", "owner_id", "sender_id", "user_id"):
        value = str(turn.get(key) or "").strip()
        if value:
            return value[:200]
    profile = getattr(ctx, "profile_name", "") if ctx is not None else ""
    return str(profile or "").strip()[:200]


def _profile_refusal(ctx: Any, turn: dict[str, Any]) -> dict[str, Any] | None:
    """A foreign-profile turn cannot propose or confirm in this profile."""
    if ctx is None:
        return None
    profile = getattr(ctx, "profile_name", "")
    if callable(profile):
        return _refusal("refused", "the host profile could not be verified")
    current = str(profile or "").strip()
    if not current:
        return None
    for key in ("profile", "profile_name"):
        bound = str(turn.get(key) or "").strip()
        if bound and bound != current:
            return _refusal("refused",
                            "a turn from another profile cannot change acknowledgements")
    return None


def _model_targets(body: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("target_id", "followup_id", "reminder_id", "notice_id"):
        value = str(body.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def _target_refusal(body: dict[str, Any],
                    target: dict[str, Any]) -> dict[str, Any] | None:
    _ensure_src()
    from proactive_pilot import acknowledgements

    aliases = {alias.lower() for alias in acknowledgements.target_aliases(target)}
    for supplied in _model_targets(body):
        if supplied.lower() not in aliases:
            return _refusal(
                "refused",
                "the target the model named does not match the pinned item; nothing changed",
                "target_mismatch")
    return None


def _pinned_target(ctx: Any, body: dict[str, Any], turn: dict[str, Any], *,
                   conversation: str, owner: str, store: Any,
                   now: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _ensure_src()
    from proactive_pilot import acknowledgements

    target, refusal = acknowledgements.resolve_target(
        turn, store=store, conversation=conversation, owner=owner,
        message=str(turn.get("text") or ""), now=now)
    if refusal is not None:
        return None, refusal
    problem = _target_refusal(body, target)
    if problem is not None:
        return None, problem
    return target, None


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, session_id: Any = None,
                task_id: Any = None, **_kwargs: Any) -> str:
        body = args or {}
        mode = str(body.get("mode") or "apply").strip().lower()
        if mode not in {"apply", "propose", "confirm"}:
            return json.dumps(_public_refusal("unknown_mode"),
                              ensure_ascii=False, sort_keys=True)
        text = str(body.get("text") or "").strip()
        if mode == "apply" and not text:
            return json.dumps(_public_refusal("no_text"),
                              ensure_ascii=False, sort_keys=True)
        if str(task_id or "").strip() or str(_kwargs.get("parent_session_id") or "").strip():
            return json.dumps(_public_refusal("delegated"),
                              ensure_ascii=False, sort_keys=True)
        execution = _current_execution(ctx)
        if execution is not None:
            return json.dumps(_public_refusal("scheduled"),
                              ensure_ascii=False, sort_keys=True)

        quote_raw = str(body.get("quote") or body.get("evidence_quote") or "")
        quote = quote_raw.strip()
        if mode == "confirm" and not quote:
            quote = text or "yes"
        attested = dict(body)
        attested["evidence_quote"] = quote
        ok, status, why, turn, _quote = _attested_owner_turn(
            ctx, attested, host_session_id=session_id)
        if not ok:
            return json.dumps(_public_refusal("unattested", status=status),
                              ensure_ascii=False, sort_keys=True)

        foreign = _profile_refusal(ctx, turn)
        if foreign is not None:
            return json.dumps(_public_refusal("foreign_profile"),
                              ensure_ascii=False, sort_keys=True)

        _ensure_src()
        from proactive_pilot import acknowledgements, pending_ack, state
        from proactive_pilot.clock import Clock
        clock = Clock()
        now = clock.now()
        store = state.StateStore(paths.state_dir(), clock)
        spoken_text = str(turn.get("text") or "").strip()
        conversation = str(turn.get("session_id") or session_id
                           or body.get("session_id") or "").strip()
        owner = _owner_for(ctx, turn)

        def remember_question(refusal: dict[str, Any],
                              effect: dict[str, Any]) -> None:
            if not effect or refusal.get("status") != "needs_clarification":
                return
            labels = list(refusal.get("labels") or [])
            question = str(refusal.get("reason") or "")
            if not labels or not question:
                return
            try:
                pending_ack.record_question(
                    conversation=conversation, owner=owner, labels=labels,
                    question=question,
                    choices=list(refusal.get("choices") or ()),
                    effect=effect, now=now, store=store)
            except pending_ack.PendingAckError:
                pass

        try:
            live_question = pending_ack.find_question(
                store, conversation=conversation, owner=owner, now=now)
        except pending_ack.PendingAckError:
            live_question = None
        if live_question is not None:
            # The next owner message answers the outstanding question; it is
            # never reinterpreted as a fresh command, whatever mode the model
            # asked for.
            try:
                candidates = acknowledgements.target_candidates(store, now=now)
            except ValueError:
                return json.dumps(_public_refusal("store_unreadable"),
                                  ensure_ascii=False, sort_keys=True)
            target, refusal = acknowledgements.resolve_question_answer(
                live_question, spoken_text, candidates)
            if target is None:
                return json.dumps(_public_failure(refusal),
                                  ensure_ascii=False, sort_keys=True)
            stored_effect = dict(live_question.get("effect") or {})
            if not stored_effect:
                return json.dumps(_public_failure({
                    "ok": False,
                    "status": "needs_clarification",
                    "reason": str(live_question.get("question") or ""),
                    "reason_code": "question_unbound",
                }), ensure_ascii=False, sort_keys=True)
            problem = _target_refusal(body, target)
            if problem is not None:
                return json.dumps(_public_failure(problem),
                                  ensure_ascii=False, sort_keys=True)
            result = acknowledgements.propose_target(
                target, stored_effect, store=store,
                conversation=conversation, owner=owner, turn=turn, now=now)
            return json.dumps(_public_proposal(result),
                              ensure_ascii=False, sort_keys=True)

        if mode == "propose":
            try:
                target, refusal = _pinned_target(
                    ctx, body, turn, conversation=conversation, owner=owner,
                    store=store, now=now)
                if refusal is not None:
                    return json.dumps(_public_failure(refusal),
                                      ensure_ascii=False, sort_keys=True)
                result = acknowledgements.propose(
                    store=store, conversation=conversation, owner=owner, turn=turn,
                    action=body.get("action"), quote=quote_raw, text=spoken_text,
                    minutes=body.get("minutes"), until=body.get("until"),
                    kind=body.get("kind"), scope=body.get("scope"),
                    target_id="", now=now)
                return json.dumps(_public_proposal(result),
                                  ensure_ascii=False, sort_keys=True)
            except (AssertionError, OSError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 - public errors never leak text
                return json.dumps(_public_refusal("internal_error"),
                                  ensure_ascii=False, sort_keys=True)

        if mode == "confirm":
            try:
                result = acknowledgements.confirm(
                    store=store, conversation=conversation, owner=owner, turn=turn,
                    target_id=_model_targets(body),
                    now=now, preferences_path=paths.preferences_path(),
                    followup_store=store, guidance_root=Path(paths.data_root()))
                return json.dumps(_public_confirmation(result),
                                  ensure_ascii=False, sort_keys=True)
            except (AssertionError, OSError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 - public errors never leak text
                return json.dumps(_public_refusal("internal_error"),
                                  ensure_ascii=False, sort_keys=True)

        try:
            live_rows = pending_ack.live_for(
                store, conversation=conversation, owner=owner, now=now)
        except pending_ack.PendingAckError:
            live_rows = []
        if len(live_rows) > 1:
            return json.dumps(_public_refusal("ambiguous_pending"),
                              ensure_ascii=False, sort_keys=True)
        if len(live_rows) == 1:
            # A live proposal owns the next message. A bare yes is its offered
            # confirmation; a command may only repeat that same frozen effect
            # (the pre-existing direct-apply path); anything else re-asks and
            # changes nothing.
            live_effect = dict(live_rows[0].get("effect") or {})
            spoken_action = acknowledgements.parse(spoken_text, now=now)
            repeats = (
                spoken_action.get("action") not in {None, "refused"}
                and spoken_action.get("action") == live_effect.get("action")
                and spoken_action.get("minutes") == live_effect.get("minutes")
                and spoken_action.get("until") == live_effect.get("until")
            )
            if acknowledgements.is_bare_affirmative(spoken_text):
                result = acknowledgements.confirm(
                    store=store, conversation=conversation, owner=owner,
                    turn=turn, target_id=_model_targets(body), now=now,
                    preferences_path=paths.preferences_path(),
                    followup_store=store, guidance_root=Path(paths.data_root()))
                return json.dumps(_public_confirmation(result),
                                  ensure_ascii=False, sort_keys=True)
            if not repeats:
                return json.dumps(_public_failure({
                    "ok": False,
                    "status": "needs_clarification",
                    "reason": str(live_rows[0].get("question") or ""),
                    "reason_code": "not_offered",
                }), ensure_ascii=False, sort_keys=True)

        # The action, the duration and the deadline all come from the whole
        # host-recorded owner message, never from the model's paraphrase.
        spoken = acknowledgements.parse(spoken_text, now=now)
        if spoken.get("action") == "refused":
            # A later turn may name the item it was asked about. The verb was
            # parsed from the earlier turn and kept with that question, so this
            # turn asks the same answerable question instead of writing.
            try:
                question = pending_ack.find_question(
                    store, conversation=conversation, owner=owner, now=now)
            except pending_ack.PendingAckError:
                question = None
            stored_effect = dict((question or {}).get("effect") or {})
            if not stored_effect:
                return json.dumps(
                    _public_refusal("unrecognised"), ensure_ascii=False,
                    sort_keys=True)
            target, refusal = _pinned_target(
                ctx, body, turn, conversation=conversation, owner=owner,
                store=store, now=now)
            if target is not None:
                result = acknowledgements.propose_target(
                    target, stored_effect, store=store,
                    conversation=conversation, owner=owner, turn=turn, now=now)
                return json.dumps(_public_proposal(result),
                                  ensure_ascii=False, sort_keys=True)
            if refusal is not None:
                remember_question(refusal, stored_effect)
            if refusal is not None and refusal.get("status") == "needs_clarification":
                return json.dumps(_public_failure(refusal),
                                  ensure_ascii=False, sort_keys=True)
            return json.dumps(
                _public_refusal("unrecognised"), ensure_ascii=False,
                sort_keys=True)
        declared = acknowledgements.parse(text, now=now)
        mismatch = declared.get("action") != spoken.get("action") or any(
            declared.get(field) != spoken.get(field) for field in ("minutes", "until"))
        if mismatch:
            return json.dumps(
                _public_refusal("not_instruction"), ensure_ascii=False,
                sort_keys=True)
        try:
            intent = acknowledgements.build_effect(
                spoken.get("action"), minutes=spoken.get("minutes"),
                until=spoken.get("until"), now=now)
        except ValueError:
            return json.dumps(
                _public_refusal("effect_invalid"), ensure_ascii=False,
                sort_keys=True)

        target, refusal = _pinned_target(
            ctx, body, turn, conversation=conversation, owner=owner,
            store=store, now=now)
        if refusal is not None:
            remember_question(refusal, intent)
            if refusal.get("reason_code") == "clarify_target":
                candidates = list(refusal.get("candidates") or [])
                if not candidates:
                    return json.dumps(
                        _public_refusal("no_candidate"), ensure_ascii=False,
                        sort_keys=True)
                if len(candidates) == 1:
                    problem = _target_refusal(body, candidates[0])
                    if problem is not None:
                        return json.dumps(_public_failure(problem),
                                          ensure_ascii=False, sort_keys=True)
                    result = acknowledgements.propose_target(
                        candidates[0], intent, store=store,
                        conversation=conversation, owner=owner, turn=turn,
                        now=now)
                    return json.dumps(_public_proposal(result),
                                      ensure_ascii=False, sort_keys=True)
            return json.dumps(_public_failure(refusal),
                              ensure_ascii=False, sort_keys=True)

        # Apply the owner's own words, not the model's payload.
        payload = dict(body)
        payload["text"] = spoken_text
        payload["target_id"] = target.get("target_id", "")
        payload["target_type"] = target.get("target_type", "")
        payload["target_label"] = target.get("target_label", "")
        payload["followup_id"] = target.get("followup_id", "")
        payload["reminder_id"] = target.get("reminder_id", "")
        payload["notice_id"] = target.get("notice_id", "")
        payload["turn_id"] = acknowledgements.turn_key(conversation, owner, turn)
        if spoken.get("action") == "snooze" and not str(payload.get("scope") or "").strip():
            payload["scope"] = str(target.get("target_label") or "").strip()
        result = _apply_unattested(payload)
        effect = dict(spoken)
        return json.dumps(
            _public_application(result, target_label=target.get("target_label"),
                                effect=effect),
            ensure_ascii=False, sort_keys=True)

    return handler


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    register = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(register):
        return {"acknowledge_tool": False,
                "acknowledge_tool_error": "register_tool unavailable"}
    try:
        register(TOOL_NAME, "secretary", TOOL_FUNCTION_SCHEMA, tool_handler(ctx),
                 description=TOOL_DESCRIPTION, emoji="")
        return {"acknowledge_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        return {"acknowledge_tool": False,
                "acknowledge_tool_error": f"{type(exc).__name__}: {exc}"}


__all__ = ["TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "tool_handler",
           "register_tool"]
