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
                  "description": ("For 'later': distinctive words from the notice this answer "
                                  "applies to; the snooze matches on them and is never global")},
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


def _refusal(status: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": str(reason)[:300]}


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
    except Exception as exc:  # noqa: BLE001 - bounded refusal, never a traceback
        return _refusal("error", f"{type(exc).__name__}: {exc}")


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
    aliases = {alias.lower() for alias in acknowledgements.target_aliases(target)}
    for supplied in _model_targets(body):
        if supplied.lower() not in aliases:
            return None, _refusal(
                "refused",
                "the target the model named does not match the pinned item; nothing changed")
    return target, None


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, session_id: Any = None,
                task_id: Any = None, **_kwargs: Any) -> str:
        body = args or {}
        mode = str(body.get("mode") or "apply").strip().lower()
        if mode not in {"apply", "propose", "confirm"}:
            return json.dumps(_refusal("refused", f"unknown mode {mode!r}"),
                              ensure_ascii=False, sort_keys=True)
        text = str(body.get("text") or "").strip()
        if mode == "apply" and not text:
            return json.dumps(_refusal("refused", "no acknowledgement text was given"),
                              ensure_ascii=False, sort_keys=True)
        if str(task_id or "").strip() or str(_kwargs.get("parent_session_id") or "").strip():
            return json.dumps(
                _refusal("refused",
                         "a delegated run cannot apply, propose or confirm an acknowledgement"),
                ensure_ascii=False, sort_keys=True)
        execution = _current_execution(ctx)
        if execution is not None:
            return json.dumps(
                _refusal("refused", "a scheduled or delegated run cannot apply an acknowledgement"),
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
            return json.dumps(_refusal(status, why), ensure_ascii=False, sort_keys=True)

        foreign = _profile_refusal(ctx, turn)
        if foreign is not None:
            return json.dumps(foreign, ensure_ascii=False, sort_keys=True)

        _ensure_src()
        from proactive_pilot import acknowledgements, state
        from proactive_pilot.clock import Clock
        clock = Clock()
        now = clock.now()
        store = state.StateStore(paths.state_dir(), clock)
        spoken_text = str(turn.get("text") or "").strip()
        conversation = str(turn.get("session_id") or session_id
                           or body.get("session_id") or "").strip()
        owner = _owner_for(ctx, turn)

        if mode == "propose":
            target, refusal = _pinned_target(
                ctx, body, turn, conversation=conversation, owner=owner,
                store=store, now=now)
            if refusal is not None:
                return json.dumps(refusal, ensure_ascii=False, sort_keys=True)
            result = acknowledgements.propose(
                store=store, conversation=conversation, owner=owner, turn=turn,
                action=body.get("action"), quote=quote_raw, text=spoken_text,
                minutes=body.get("minutes"), until=body.get("until"),
                kind=body.get("kind"), scope=body.get("scope"),
                target_id="", now=now)
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        if mode == "confirm":
            result = acknowledgements.confirm(
                store=store, conversation=conversation, owner=owner, turn=turn,
                target_id=_model_targets(body),
                now=now, preferences_path=paths.preferences_path(),
                followup_store=store, guidance_root=Path(paths.data_root()))
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        # The action, the duration and the deadline all come from the whole
        # host-recorded owner message, never from the model's paraphrase.
        spoken = acknowledgements.parse(spoken_text, now=now)
        if spoken.get("action") == "refused":
            return json.dumps(_refusal("refused", str(spoken.get("reason") or "")),
                              ensure_ascii=False, sort_keys=True)
        declared = acknowledgements.parse(text, now=now)
        mismatch = declared.get("action") != spoken.get("action") or any(
            declared.get(field) != spoken.get(field) for field in ("minutes", "until"))
        if mismatch:
            return json.dumps(
                _refusal("refused", "the owner's words do not match that acknowledgement"),
                ensure_ascii=False, sort_keys=True)
        if spoken.get("action") == "snooze" and not str(body.get("scope") or "").strip():
            return json.dumps(
                _refusal("refused",
                         "later needs scope: the notice's own words, so the snooze is not global"),
                ensure_ascii=False, sort_keys=True)

        target, refusal = _pinned_target(
            ctx, body, turn, conversation=conversation, owner=owner,
            store=store, now=now)
        if refusal is not None:
            return json.dumps(refusal, ensure_ascii=False, sort_keys=True)

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
        if result.get("ok"):
            result["from_owner_turn"] = True
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

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
