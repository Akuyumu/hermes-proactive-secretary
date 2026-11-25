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
        "session_id": {"type": "string",
                       "description": "Current chat session id; required to prove the owner's turn"},
        "evidence_quote": {"type": "string",
                           "description": "Exact words from the owner's current message"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _ensure_src() -> None:
    src = str(Path(paths.plugin_root()) / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def dispatch(payload: dict[str, Any] | None = None, *, authorized: bool = False) -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot import guidance

    body = dict(payload or {})
    action = str(body.get("action") or "list").strip().lower()
    root = Path(paths.data_root())
    reason = str(body.get("reason") or "").strip()[:200]
    if action == "list":
        return guidance.list_active(root)
    if action in ("close", "reopen") and not authorized:
        # The exported function is a call surface too: a mutation must carry the
        # tool's authority check, not just its arguments.
        return {"ok": False, "status": "refused",
                "error": "guidance changes require a verified owner turn"}
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


def _foreground_authority(ctx: Any, payload: dict[str, Any]) -> tuple[bool, str]:
    """Positive host-attested proof: a private human turn quoting the owner.

    A null execution, a TTY or an environment variable is not proof — delegated
    children of a scheduled run also report "not cron". The authoritative turn
    record the host keeps for a session is what we require, exactly as follow-ups
    and responsibilities do.
    """
    if ctx is None:
        return False, "a foreground conversation context is required"
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return False, "session_id is required to prove an owner turn"
    try:
        from .secretary_responsibilities import latest_turn, _quote_matches
        from .secretary_followups import _turn_is_private_human
    except Exception:  # noqa: BLE001 - unavailable authority proof means refuse
        return False, "owner authority could not be checked"
    try:
        turn = latest_turn(ctx, session_id=session_id)
    except Exception:  # noqa: BLE001
        return False, "owner authority could not be checked"
    if turn is None:
        return False, "no current human conversation turn for this session"
    if not _turn_is_private_human(turn):
        return False, "a background or shared turn cannot change guidance"
    if not _quote_matches(payload.get("evidence_quote"), turn):
        return False, "evidence_quote must quote the owner's current message"
    return True, ""


def tool_handler(ctx: Any = None):
    def handler(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        body = args or {}
        action = str(body.get("action") or "list").strip().lower()
        try:
            if action != "list":
                ok, why = _foreground_authority(ctx, body)
                if not ok:
                    return json.dumps({"ok": False, "status": "refused", "error": why},
                                      ensure_ascii=False, sort_keys=True)
                return json.dumps(dispatch(body, authorized=True),
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
        register(TOOL_NAME, "secretary", TOOL_SCHEMA, tool_handler(ctx),
                 description=TOOL_DESCRIPTION, emoji="")
        return {"guidance_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        return {"guidance_tool": False,
                "guidance_tool_error": f"{type(exc).__name__}: {exc}"}


__all__ = ["TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "dispatch", "tool_handler",
           "register_tool"]
