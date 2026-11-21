"""Typed tool for owner-only pending-approval drafts.

The durable state is the draft directory tree in
:mod:`proactive_pilot.drafts`. This module is the thin authority seam:

* ``stage`` and ``list_pending`` are portal-only and require the same verified
  owned portal execution identity used by the notice and follow-up tools.
* ``list``, ``show``, ``approve`` and ``discard`` are foreground-only.
* Approval moves a directory and returns a local receipt. It never sends,
  schedules, purchases or otherwise performs the staged action.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Mapping

try:  # package import or direct/top-level test import
    from .. import paths
except ImportError:  # pragma: no cover - direct module execution
    import paths  # type: ignore[no-redef]

TOOL_NAME = "secretary_draft"
PORTAL_ACTIONS = frozenset({"stage", "list_pending"})
FOREGROUND_ACTIONS = frozenset({"list", "show", "approve", "discard"})
MAX_ID = 24
NOTICE_TOPIC = "Draft ready"
NOTICE_MAX_MESSAGE = 350
NOTICE_EXPIRY_HOURS = 6
NOTICE_SUFFIX = '. Say "show drafts" to review.'


def _refusal(status: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": str(reason)[:300]}


def _notice_enabled(ctx: Any) -> bool:
    try:
        from proactive_pilot.native_runtime import read_native_settings
    except ImportError:  # pragma: no cover - package layout is always present
        return True
    value = read_native_settings(ctx, "draft_notice", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _notice_message(manifest: Mapping[str, Any]) -> str:
    intent = str(manifest.get("intent") or "other")
    title = str(manifest.get("title") or "")
    to = str(manifest.get("to") or "")
    prefix = f"Drafted a {intent} — "
    to_text = f" for {to}" if to else ""
    room = NOTICE_MAX_MESSAGE - len(prefix) - len(NOTICE_SUFFIX)
    middle = (title + to_text)[:max(0, room)]
    return f"{prefix}{middle}{NOTICE_SUFFIX}"


def _notice_result(receipt: Any) -> dict[str, str]:
    if not isinstance(receipt, dict):
        return {"accepted": False, "status": "error",
                "reason": "notice returned an unexpected result", "notice_id": ""}
    accepted = bool(receipt.get("accepted"))
    notice_id = receipt.get("id") or receipt.get("notice_id") or ""
    return {
        "accepted": accepted,
        "status": str(receipt.get("status")
                      or ("accepted" if accepted else "refused"))[:80],
        "reason": str(receipt.get("reason") or "")[:300],
        "notice_id": str(notice_id or "")[:64],
    }


def _submit_draft_notice(ctx: Any, identity: Mapping[str, str],
                         manifest: Mapping[str, Any],
                         now: datetime) -> dict[str, str]:
    if not _notice_enabled(ctx):
        return {"accepted": False, "status": "disabled",
                "reason": "", "notice_id": ""}
    draft_id = str(manifest.get("id") or "")
    try:
        from proactive_pilot.clock import Clock
        from proactive_pilot import portal_notices
        from proactive_pilot.state import StateStore

        clock = Clock()
        store = StateStore(paths.state_dir(), clock)
        proposal = {
            "event_key": f"draft:{draft_id}",
            "topic": NOTICE_TOPIC,
            "message": _notice_message(manifest),
            "evidence_refs": [f"draft:{draft_id}"],
            "expiry": (now + timedelta(hours=NOTICE_EXPIRY_HOURS)).isoformat(),
        }
        receipt = portal_notices.submit_notice(
            store, clock, identity=dict(identity), proposal=proposal)
    except Exception as exc:  # noqa: BLE001 - the heads-up is best effort
        return {"accepted": False, "status": "error",
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                "notice_id": ""}
    return _notice_result(receipt)


def _cron_execution(ctx: Any) -> Any:
    getter = getattr(ctx, "current_cron_execution", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - an unreadable execution is not foreground
        return object()


def _require_foreground(ctx: Any) -> None:
    if ctx is None:
        raise ValueError("a foreground conversation context is required")
    if _cron_execution(ctx) is not None:
        raise ValueError("list/show/approve/discard are foreground-only")


def _portal_identity(ctx: Any) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    try:
        from .secretary_portal_notices import _host_identity
    except ImportError:  # pragma: no cover - direct module execution
        from secretary.secretary_portal_notices import _host_identity  # type: ignore[no-redef]
    return _host_identity(ctx)


def _drafts() -> Any:
    from proactive_pilot import drafts

    return drafts


def _now() -> datetime:
    return datetime.now().astimezone()


def _stage_usage(ctx: Any) -> dict[str, int]:
    usage = getattr(ctx, "_secretary_draft_stage_usage", None)
    if not isinstance(usage, dict):
        usage = {}
        try:
            setattr(ctx, "_secretary_draft_stage_usage", usage)
        except Exception:  # noqa: BLE001 - a host context may reject attributes
            pass
    return usage


def _portal_dispatch(action: str, args: Mapping[str, Any], ctx: Any,
                     now: datetime) -> dict[str, Any]:
    D = _drafts()
    identity, refusal = _portal_identity(ctx)
    if refusal is not None:
        return refusal
    swept = D.sweep_expired(paths.drafts_dir(), now=now)

    if action == "list_pending":
        return {
            "ok": True,
            "status": "ok",
            "drafts": D.list_pending(paths.drafts_dir()),
            "expired_count": len(swept),
        }

    execution_id = str(identity.get("execution_id") or "")
    usage = _stage_usage(ctx)
    used = int(usage.get(execution_id, 0))
    if used >= D.MAX_STAGES_PER_EXECUTION:
        return {
            "ok": False,
            "status": "capacity",
            "capacity": "execution",
            "code": "stage_limit",
            "reason": (f"per-execution draft limit reached "
                       f"({D.MAX_STAGES_PER_EXECUTION})"),
            "expired_count": len(swept),
        }
    try:
        manifest = D.stage(
            paths.drafts_dir(),
            intent=args.get("intent"),
            title=args.get("title"),
            body=args.get("body"),
            reason=args.get("reason"),
            subject=args.get("subject"),
            to=args.get("to"),
            evidence=args.get("evidence"),
            body_ref=args.get("body_ref") or D.DEFAULT_BODY_REF,
            expires_at=args.get("expires_at"),
            now=now,
        )
    except D.DraftDuplicateError as exc:
        return {"ok": False, "status": exc.status, "reason": str(exc),
                "expired_count": len(swept)}
    except D.DraftCapacityError as exc:
        return {"ok": False, "status": "capacity", "capacity": exc.capacity,
                "code": "pending_limit", "reason": str(exc),
                "expired_count": len(swept)}
    except D.DraftError as exc:
        return _refusal("invalid_args", str(exc))
    usage[execution_id] = used + 1
    return {
        "ok": True,
        "status": "staged",
        "draft": manifest,
        "executed": False,
        "expired_count": len(swept),
        "notice": _submit_draft_notice(ctx, identity, manifest, now),
        "note": ("The draft is pending owner approval. Staging never sends, "
                 "books, buys or otherwise performs the action."),
    }


def _foreground_dispatch(action: str, args: Mapping[str, Any], ctx: Any,
                         now: datetime) -> dict[str, Any]:
    del ctx
    D = _drafts()
    root = paths.drafts_dir()
    if action == "list":
        result = D.list_drafts(root, now=now)
        return {"ok": True, "status": "ok", **result}
    if action == "show":
        return D.show(root, args.get("draft_id"), now=now)
    if action == "approve":
        return D.approve(root, args.get("draft_id"), now=now)
    return D.discard(root, args.get("draft_id"), note=args.get("note"), now=now)


def dispatch(args: Mapping[str, Any], *, ctx: Any = None,
             now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        return _refusal("invalid_args", "tool arguments must be an object")
    action = str(args.get("action") or "").strip().lower()
    if action not in PORTAL_ACTIONS | FOREGROUND_ACTIONS:
        return _refusal("invalid_args", f"unknown draft action {action!r}")
    stamp = now or _now()
    try:
        if action in FOREGROUND_ACTIONS:
            _require_foreground(ctx)
            return _foreground_dispatch(action, args, ctx, stamp)
        return _portal_dispatch(action, args, ctx, stamp)
    except _drafts().DraftError as exc:
        return _refusal(getattr(exc, "status", "invalid"), str(exc))


def tool_handler(ctx: Any):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        try:
            return json.dumps(dispatch(args, ctx=ctx), ensure_ascii=False,
                              sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - bounded tool error, no raw state
            return json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                ensure_ascii=False, sort_keys=True,
            )

    return handler


TOOL_DESCRIPTION = (
    "Stage and review owner-approval drafts. A portal run may stage a pending "
    "draft for something it would send, book, buy or change, and list pending "
    "ids/titles; staging never executes anything. Only a foreground owner "
    "session may show the untrusted body, approve or discard. Approval moves "
    "the draft to approved/ and returns a local receipt with executed=false; it "
    "never sends, schedules, purchases or performs the action."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "stage", "list_pending", "list", "show", "approve", "discard",
            ],
        },
        "draft_id": {"type": "string", "maxLength": MAX_ID},
        "intent": {
            "type": "string",
            "enum": ["email", "message", "calendar", "purchase", "file", "other"],
        },
        "title": {"type": "string", "maxLength": 120},
        "subject": {"type": "string", "maxLength": 200},
        "to": {"type": "string", "maxLength": 200},
        "reason": {"type": "string", "maxLength": 600},
        "body": {"type": "string"},
        "body_ref": {"type": "string", "maxLength": 81},
        "evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": 240},
            "maxItems": 5,
        },
        "expires_at": {"type": "string", "format": "date-time", "maxLength": 64},
        "note": {"type": "string", "maxLength": 600},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    register = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(register):
        return {"draft_tool": False, "draft_tool_error": "register_tool unavailable"}
    try:
        register(
            TOOL_NAME,
            "secretary",
            TOOL_SCHEMA,
            tool_handler(ctx),
            description=TOOL_DESCRIPTION,
            emoji="",
        )
        return {"draft_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        return {"draft_tool": False,
                "draft_tool_error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "dispatch", "tool_handler",
    "register_tool",
]
