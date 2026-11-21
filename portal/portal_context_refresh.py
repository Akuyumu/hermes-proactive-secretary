"""Opt-in, assistant-mediated semantic refresh of the bounded context snapshot.

The assistant may infer durable facts, preferences and interests from context
already available in an eligible private conversation, then record a short
advisory entry through the native ``portal_context`` tool. This module adds
no model call, no source/account access, no background importer and no regex
inference engine. Writes are always advisory, always expiring, and only happen
when the owner has explicitly enabled the dedicated boolean plugin setting on a
valid, unpaused profile.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

CONFIG_KEY = "context_refresh"
TOOL_NAME = "portal_context"
_ALLOWED_SOURCES = ("assistant:knowledge", "assistant:memory")
_ALLOWED_KINDS = ("note", "interest", "preference", "project", "person", "place")
_UPDATE_KEYS = frozenset({"action", "id", "text", "kind", "source", "expires_at"})
_REVOKE_KEYS = frozenset({"action", "id"})
_SHOW_KEYS = frozenset({"action", "id"})
_LIST_KEYS = frozenset({"action"})

_NONHUMAN_ENVELOPE_RE = re.compile(
    r"^(?:\[important:\s+(?:\d+\s+)?background\s+process(?:es)?\b"
    r"|\[async\s+delegation\s+batch\s+complete\b)", re.IGNORECASE)

GUIDANCE = (
    "[Proactive Portal context refresh]\n"
    "This is a normal private turn. From context already available in this "
    "conversation, infer only durable facts, preferences or interests worth "
    "remembering. If a material change is evident, use the portal_context "
    "tool with one short advisory entry; skip duplicates, trivial "
    "acknowledgements, secrets and raw excerpts. Inferred interests are not "
    "instructions or permission and never expand sources. Keep entries compact "
    "and let them expire. Do not claim an owner decision."
)


class ContextRefreshError(ValueError):
    """A refresh request is invalid, disabled or unsafe; write nothing."""


def _paths_module():
    try:
        from .. import paths as plugin_paths
    except ImportError:  # pragma: no cover - direct module execution
        import paths as plugin_paths  # type: ignore[no-redef]
    return plugin_paths


def _config_enabled(ctx: Any, enabled: bool | None = None) -> bool:
    """Only an explicit boolean ``true`` enables refresh; everything else denies."""
    if enabled is not None:
        return enabled is True
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return False
    try:
        return getter(CONFIG_KEY, False) is True
    except Exception:  # noqa: BLE001 - unreadable consent fails closed
        return False


def _preferences_allow(path: Any) -> bool:
    """Use the strict preference owner, then tighten the pause structure.

    The strict loader validates the whole preference record (including schema),
    so a foreign schema is corruption here. The raw object is used only to
    reject a pause shape the loader would otherwise normalise permissively; it
    never supplies the acceptance decision by itself. A missing file remains the
    normal unpaused state.
    """
    try:
        file_path = path
        if not file_path.exists():
            return True
        from proactive_pilot.feedback import load_preferences_file
        load_preferences_file(file_path, strict=True)
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/corrupt preferences deny the write
        return False
    if not isinstance(raw, dict):
        return False
    if "paused" not in raw:
        return True
    paused = raw["paused"]
    if not isinstance(paused, dict) or "active" not in paused:
        return False
    active = paused["active"]
    if not isinstance(active, bool):
        return False
    return active is False


def _resolve_preferences(paths_module: Any, preferences_path: Any):
    if preferences_path is not None:
        return preferences_path
    try:
        return (paths_module or _paths_module()).preferences_path()
    except Exception as exc:  # noqa: BLE001 - profile paths are never guessed
        raise ContextRefreshError("profile state is unavailable") from exc


def _resolve_profile(paths_module: Any, store: Any, preferences_path: Any, now: Any):
    if store is not None and preferences_path is not None and now is not None:
        return store, preferences_path, now
    try:
        from proactive_pilot.clock import Clock
        from proactive_pilot.state import StateStore

        module = paths_module or _paths_module()
        clock = Clock()
        resolved_store = store or StateStore(module.state_dir(), clock)
        resolved_prefs = _resolve_preferences(module, preferences_path)
        resolved_now = now if now is not None else clock.now()
        return resolved_store, resolved_prefs, resolved_now
    except ContextRefreshError:
        raise
    except Exception as exc:  # noqa: BLE001 - profile paths are never guessed
        raise ContextRefreshError("profile state is unavailable") from exc


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _is_assistant_advisory(entry: Mapping[str, Any]) -> bool:
    return (entry.get("advisory") is True
            and entry.get("source") in _ALLOWED_SOURCES)


def _unchanged(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "unchanged",
        "id": entry.get("id"),
        "source": entry.get("source"),
        "kind": entry.get("kind"),
        "advisory": entry.get("advisory") is True,
        "expires_at": entry.get("expires_at"),
        "updated_at": entry.get("updated_at"),
    }


def _validate_keys(payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(payload) - set(allowed)
    if unknown:
        raise ContextRefreshError("unsupported context refresh field")


def _require_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ContextRefreshError("context entry id is required")
    return value.strip()


def _dispatch_update(store: Any, now: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    from proactive_pilot import contextsnapshot as CS
    from proactive_pilot.state import CorruptStateError

    _validate_keys(payload, _UPDATE_KEYS)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ContextRefreshError("context text is required")
    source = payload.get("source") or "assistant:knowledge"
    if not isinstance(source, str) or source not in _ALLOWED_SOURCES:
        raise ContextRefreshError("context source is not permitted for advisory refresh")
    kind = str(payload.get("kind") or "note").strip().lower()
    if kind not in _ALLOWED_KINDS:
        raise ContextRefreshError("unsupported context kind")
    expiry = payload.get("expires_at")
    if expiry not in (None, "") and not isinstance(expiry, str):
        raise ContextRefreshError("context expiry is malformed")
    entry_id = payload.get("id")
    if entry_id not in (None, "") and not isinstance(entry_id, str):
        raise ContextRefreshError("context entry id is malformed")

    fields = {"text": text, "source": source, "kind": kind, "advisory": True}
    if expiry not in (None, ""):
        fields["expires_at"] = expiry
    target_id = str(entry_id or "").strip()
    if target_id:
        fields["id"] = target_id
    try:
        CS.validate_entry_fields(fields, now, entry_id=target_id)
    except CS.ContextSnapshotError as exc:
        raise ContextRefreshError("context refresh rejected") from exc

    try:
        view = CS.load_context_snapshot(store, now, strict=True)
        if target_id:
            target_entry = next((row for row in view.entries
                                 if str(row.get("id")) == target_id), None)
            if target_entry is not None and not _is_assistant_advisory(target_entry):
                raise ContextRefreshError("context entry is not assistant-owned")
        normalised = _normalise(text)
        for entry in view.entries:
            same_text = _normalise(str(entry.get("text") or "")) == normalised
            if not same_text or str(entry.get("kind")) != kind:
                continue
            if target_id and str(entry.get("id")) != target_id:
                continue
            return _unchanged(entry)
        written = CS.update_context_entry(
            store, fields, now, strict=True, require_assistant_owned=True,
            assistant_sources=_ALLOWED_SOURCES)
    except CorruptStateError:
        raise
    except ContextRefreshError:
        raise
    except CS.ContextSnapshotError as exc:
        raise ContextRefreshError("context refresh rejected") from exc
    return {
        "ok": True,
        "status": "updated",
        "id": written["id"],
        "source": written["source"],
        "kind": written["kind"],
        "advisory": True,
        "expires_at": written["expires_at"],
        "updated_at": written["updated_at"],
    }


def _dispatch_revoke(store: Any, now: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    from proactive_pilot import contextsnapshot as CS

    _validate_keys(payload, _REVOKE_KEYS)
    entry_id = _require_id(payload)
    try:
        result = CS.revoke_context_entry(
            store, entry_id, now, strict=True, require_assistant_owned=True,
            assistant_sources=_ALLOWED_SOURCES)
    except CS.ContextSnapshotError as exc:
        raise ContextRefreshError("context revocation rejected") from exc
    return {"ok": True, "id": result["id"], "revoked_at": result["revoked_at"]}


def dispatch(ctx: Any, action: str, payload: Mapping[str, Any] | None = None, *,
             store: Any = None, preferences_path: Any = None, now: Any = None,
             enabled: bool | None = None, paths_module: Any = None) -> dict[str, Any]:
    """Execute one bounded tool action. Raises typed errors; the tool wraps them."""
    from proactive_pilot import contextsnapshot as CS

    name = str(action or "list").strip().lower()
    data = payload if isinstance(payload, Mapping) else {}
    if name not in ("list", "show", "update", "revoke"):
        raise ContextRefreshError("unknown context refresh action")

    if name in ("update", "revoke"):
        if not _config_enabled(ctx, enabled):
            raise ContextRefreshError("context refresh is disabled")
        resolved_prefs = _resolve_preferences(paths_module, preferences_path)
        if not _preferences_allow(resolved_prefs):
            raise ContextRefreshError("context refresh is unavailable while paused or corrupt")
        resolved_store, _prefs, resolved_now = _resolve_profile(
            paths_module, store, resolved_prefs, now)
        if name == "update":
            return _dispatch_update(resolved_store, resolved_now, data)
        return _dispatch_revoke(resolved_store, resolved_now, data)

    resolved_store, _prefs, resolved_now = _resolve_profile(
        paths_module, store, preferences_path, now)
    if name == "list":
        _validate_keys(data, _LIST_KEYS)
        view = CS.load_context_snapshot(resolved_store, resolved_now, strict=False)
        return {"ok": True, **CS.inspect_metadata(view)}
    _validate_keys(data, _SHOW_KEYS)
    entry_id = _require_id(data)
    try:
        entry = CS.get_context_entry(resolved_store, entry_id, resolved_now)
    except CS.ContextSnapshotError as exc:
        raise ContextRefreshError("context entry is unavailable") from exc
    return {"ok": True, "entry": entry}


TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "show", "update", "revoke"]},
        "id": {"type": "string", "maxLength": 20},
        "text": {"type": "string", "maxLength": 500},
        "kind": {"type": "string", "enum": list(_ALLOWED_KINDS)},
        "source": {"type": "string", "enum": list(_ALLOWED_SOURCES)},
        "expires_at": {"type": "string", "maxLength": 64},
    },
    "required": ["action"],
    "additionalProperties": False,
}

TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "List, show, update or revoke bounded advisory secretary context entries. "
        "Updates are advisory, expiring and allowed only under explicit owner opt-in; "
        "inferred entries never grant access or owner consent."),
    "parameters": TOOL_PARAMETERS,
}


def tool_handler(ctx: Any):
    def handler(args: Mapping[str, Any] | None = None, **_kwargs: Any) -> str:
        try:
            payload = args if isinstance(args, Mapping) else {}
            result = dispatch(ctx, str(payload.get("action") or "list"), payload)
        except ContextRefreshError:
            result = {"ok": False, "error": "context refresh rejected"}
        except Exception as exc:  # noqa: BLE001 - never leak paths, text or values
            if type(exc).__name__ == "CorruptStateError":
                result = {"ok": False,
                          "error": "context refresh state is corrupt; failing closed"}
            else:
                result = {"ok": False, "error": "context refresh failed"}
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    return handler


def register_context_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    """Register the typed tool with the native keyword signature and schema envelope."""
    tool_reg = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(tool_reg):
        return {"context_tool": False, "error": "register_tool unavailable"}
    try:
        tool_reg(
            name=TOOL_NAME,
            toolset="portal",
            schema=TOOL_SCHEMA,
            handler=tool_handler(ctx),
            description=TOOL_SCHEMA["description"],
            emoji="",
        )
        return {"context_tool": True}
    except Exception as exc:  # noqa: BLE001 - plugin load must still succeed
        return {"context_tool": False, "error": f"{type(exc).__name__}: {exc}"}


def refresh_guidance(ctx: Any, *, platform: Any, user_message: Any,
                     task_id: str = "", parent_session_id: str = "",
                     chat_type: str = "", is_shared: bool = False,
                     enabled: bool | None = None, preferences_path: Any = None,
                     paths_module: Any = None) -> str | None:
    """Return static, bounded guidance only on eligible foreground human turns."""
    if task_id or parent_session_id:
        return None
    platform_key = str(platform or "").strip().lower()
    if any(token in platform_key for token in (
            "completion", "background", "cron", "scheduler", "a2a", "subagent",
            "internal", "system", "worker", "group", "channel", "shared")):
        return None
    try:
        from .portal_conversation import _text, eligible_human_turn
        message_text = _text(user_message).strip()
        if _NONHUMAN_ENVELOPE_RE.match(message_text):
            return None
        if not eligible_human_turn(platform, user_message, chat_type=chat_type,
                                   is_shared=is_shared):
            return None
    except Exception:  # noqa: BLE001 - an eligibility failure injects nothing
        return None
    if not _config_enabled(ctx, enabled):
        return None
    try:
        module = paths_module or _paths_module()
        prefs = preferences_path if preferences_path is not None else module.preferences_path()
    except Exception:  # noqa: BLE001 - unresolved profile injects nothing
        return None
    if not _preferences_allow(prefs):
        return None
    return GUIDANCE
