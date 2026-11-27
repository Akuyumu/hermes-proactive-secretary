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
    from .secretary_guidance_tool import _ensure_src, _foreground_authority
except ImportError:  # direct/standalone import
    from secretary_guidance_tool import _ensure_src, _foreground_authority  # type: ignore

TOOL_NAME = "secretary_acknowledge"
TOOL_DESCRIPTION = (
    "Apply the owner's short reply to a notice or reminder: 'later <n>' / "
    "'later until <when>' snoozes, 'done' closes the topic or confirms the "
    "follow-up, 'stop' closes that item only (never a global mute). Use only "
    "when the owner's current message is exactly one of those answers; anything "
    "else is refused and changes nothing."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string",
                 "description": "The owner's acknowledgement, e.g. 'done', 'later 20', 'stop'"},
        "followup_id": {"type": "string",
                        "description": "Follow-up id the notice belonged to, when known"},
        "topic": {"type": "string",
                  "description": "Topic label of the notice, when the owner closed a topic"},
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "Stable ids (event, follow-up, observation) to close"},
        "scope": {"type": "string",
                  "description": "Bounded description of what the answer applies to"},
        "evidence_quote": {"type": "string",
                           "description": "Exact words from the owner's current message"},
    },
    "required": ["text"],
    "additionalProperties": False,
}

TOOL_FUNCTION_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "parameters": TOOL_SCHEMA,
}


def _refusal(status: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": str(reason)[:300]}


def dispatch(payload: dict[str, Any] | None = None, *,
             now: Any = None) -> dict[str, Any]:
    """Apply one acknowledgement without host authority (unit-test seam)."""
    body = payload or {}
    _ensure_src()
    from proactive_pilot import acknowledgements, followups, state
    from proactive_pilot.clock import Clock

    clock = Clock()
    when = now if now is not None else clock.now()
    store = state.StateStore(paths.state_dir(), clock)
    followup_id = str(body.get("followup_id") or "").strip()
    try:
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
        )
    except Exception as exc:  # noqa: BLE001 - bounded refusal, never a traceback
        return _refusal("error", f"{type(exc).__name__}: {exc}")


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, session_id: Any = None,
                task_id: Any = None, **_kwargs: Any) -> str:
        body = args or {}
        if not str(body.get("text") or "").strip():
            return json.dumps(_refusal("refused", "no acknowledgement text was given"),
                              ensure_ascii=False, sort_keys=True)
        details: dict[str, Any] = {}
        ok, status, why = _foreground_authority(
            ctx, body, host_session_id=session_id, details=details)
        if not ok:
            return json.dumps(_refusal(status, why), ensure_ascii=False, sort_keys=True)
        result = dispatch(body)
        if details:
            result = {**result, "authority": details}
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


__all__ = ["TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "dispatch", "tool_handler",
           "register_tool"]
