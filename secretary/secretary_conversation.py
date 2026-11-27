"""Safe mobile/CLI conversation surfaces for the proactive secretary.

Everything here is bounded and additive: a slash command that starts or resumes
the foreground onboarding conversation, a one-per-profile first-use invitation,
a one-shot pending continuation, and narrow responsibility-reference context.
No privileged pre-auth hook, no forced session, no guessed DM and no persisted
onboarding mode.
"""
from __future__ import annotations

import json
import re
import time
import secrets
from types import SimpleNamespace
from typing import Any, Mapping

from .secretary_onboarding import (OnboardingError, OnboardingService, next_question,
                                   next_step)
from . import secretary_status

INVITATION_KEY = "onboarding_invitation"
INTERVIEW_KEY = "onboarding_interview"
PENDING_KEY = "onboarding_pending"
PENDING_TTL_SECONDS = 15 * 60
_NON_HUMAN = frozenset({
    "", "cron", "scheduler", "a2a", "agent", "subagent", "internal", "system",
    "worker", "background",
})
_REF = re.compile(r"\bsec-[a-z0-9]{6,}\b", re.IGNORECASE)
_RELEVANT = re.compile(
    r"\b(secretary|proactive|reminder|remind|alert|why this|not today|snooze|"
    r"stop these|standing scope|onboarding|setup)\b", re.IGNORECASE)
_DEICTIC = re.compile(
    r"\b(?:that|those|these|it|the one|the thing|you sent|you prepped|"
    r"you mentioned|earlier|before|add these|send me the)\b", re.IGNORECASE)


class ConversationError(ValueError):
    pass


def _facade(ctx: Any) -> Any:
    return getattr(ctx, "state", None)


def _get(ctx: Any, key: str, default: Any = None) -> Any:
    state = _facade(ctx)
    if state is None:
        return default
    try:
        return state.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _set(ctx: Any, key: str, value: Any) -> bool:
    state = _facade(ctx)
    if state is None:
        return False
    try:
        state.set(key, value)
        return True
    except Exception:  # noqa: BLE001
        return False


def _text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = []
        for part in user_message:
            if isinstance(part, Mapping) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return "" if user_message is None else str(user_message)


def eligible_human_turn(platform: Any, user_message: Any, *, task_id: str = "",
                        parent_session_id: str = "", chat_type: str = "",
                        sender_id: str = "", is_shared: bool = False) -> bool:
    platform_key = str(platform or "").strip().lower()
    if platform_key in _NON_HUMAN:
        return False
    if any(token in platform_key for token in ("cron", "a2a", "subagent", "internal")):
        return False
    if any(token in platform_key for token in ("group", "channel", "shared")):
        return False
    if is_shared or str(chat_type or "").strip().lower() in {"group", "channel", "supergroup"}:
        return False
    text = _text(user_message).strip()
    if not text or text.startswith("/"):
        return False
    return True


def _pending_record(ctx: Any, session_id: str = "", token: str = "") -> dict[str, Any] | None:
    raw = _get(ctx, PENDING_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        created = float(raw.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    if not created or time.time() - created > PENDING_TTL_SECONDS:
        _set(ctx, PENDING_KEY, None)
        return None
    record = dict(raw)
    bound = str(record.get("session_id") or "")
    caller = str(session_id or "")
    expected = str(record.get("token") or "")
    if bound:
        # Bound by the caller that created it; another session never consumes it.
        if bound != caller:
            return None
        return record
    # A slash handler has no host session key. Never let an unrelated session
    # silently consume the pending continuation: an unbound record requires the
    # explicit continuation token shown by the command.
    presented = str(token or "")
    if not expected or not secrets.compare_digest(expected, presented):
        return None
    record["session_id"] = caller
    return record


def set_pending(ctx: Any, *, action: str = "setup", session_id: str = "") -> dict[str, Any]:
    if _invitation_record(ctx) is None:
        _set(ctx, INVITATION_KEY, {"version": 1, "invited_at": time.time(),
                                   "status_at_invite": "explicit_command"})
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    record = {"action": action, "created_at": time.time(),
              "session_id": str(session_id or ""), "token": token,
              "consumed": False}
    _set(ctx, PENDING_KEY, record)
    return record


def clear_pending(ctx: Any) -> bool:
    return _set(ctx, PENDING_KEY, None)


def _invitation_record(ctx: Any) -> dict[str, Any] | None:
    raw = _get(ctx, INVITATION_KEY)
    return dict(raw) if isinstance(raw, Mapping) else None


def _interview_record(ctx: Any) -> dict[str, Any] | None:
    raw = _get(ctx, INTERVIEW_KEY)
    return dict(raw) if isinstance(raw, Mapping) else None


def _start_interview(ctx: Any, *, session_id: str = "") -> dict[str, Any] | None:
    record = {"started_at": time.time(), "session_id": str(session_id or ""),
              "version": 1}
    return record if _set(ctx, INTERVIEW_KEY, record) else None


def _invitation_context(service: OnboardingService, *, resuming: bool = False) -> str:
    state = service._read()
    status = state.get("status", "not_started")
    step = next_step(state)
    if resuming:
        lead = "The user asked to continue secretary setup."
    else:
        lead = ("This is the first eligible conversation on this profile and secretary setup "
                "is incomplete. The one next step is to set up what to watch. "
                "If the user declines, do not offer setup again in this conversation or later.")
    return (
        "[Proactive Secretary onboarding]\n"
        f"{lead} Current status: {status}; next step: {step}.\n"
        "Start the interview in this conversation now, conversationally. Ask exactly the next "
        "genuine question below, one question at a time. Handle setup through conversation "
        "rather than requiring settings edits or commands. Preserve anything already known. "
        "Ask only for missing or "
        "conflicting facts and genuine permissions; existing memory may be proposed as "
        "tentative facts but is never consent to expand access. Do not ask for credentials, "
        "do not claim the secretary is installed/active beyond what the tool reports, and do "
        "not create sources, grants or schedules. Use the secretary_onboarding tool for every "
        "answer. Never invent a grant from silence, thanks or an unrelated message. If the "
        "user's first message is an unrelated request, answer it first; do not hijack or "
        "replace it with onboarding.\n"
        f"Next question: {next_question(state)}"
    )


def _interview_context(ctx: Any, service: OnboardingService, *,
                       session_id: str = "") -> str | None:
    """Continue the active interview in its own session without a command.

    The profile sees at most one first-use invitation. A later ordinary turn in
    the same conversation still gets bounded steering until the interview is
    complete or cancelled; an unrelated session is never injected.
    """
    record = _interview_record(ctx)
    if record is None:
        return None
    bound = str(record.get("session_id") or "")
    if not bound and not session_id:
        # A slash handler without a real session key has no continuation
        # boundary; never inject the interview into an unrelated later turn.
        return None
    if bound and session_id and bound != session_id:
        return None
    state = service._read()
    if state.get("status") in ("ready", "pending_verification", "cancelled"):
        _set(ctx, INTERVIEW_KEY, None)
        return None
    step = next_step(state)
    return (
        "[Proactive Secretary onboarding continuation]\n"
        f"An onboarding interview is active for this conversation (status "
        f"{state.get('status', 'not_started')}; next step: {step}). Continue it naturally and "
        "ask exactly the next missing question. If the user asks something unrelated, answer "
        "it briefly first, then continue the interview without dropping it. Record the "
        "user's answer with the secretary_onboarding tool, preserving known fields. Never "
        "turn thanks, silence, an "
        "unrelated request or an assistant suggestion into a permission, commitment or "
        "destination confirmation. No external action, source read or schedule is allowed "
        "from this context alone.\n"
        f"Next question: {next_question(state)}"
    )


def invitation_context(ctx: Any, service: OnboardingService, *, platform: Any,
                       user_message: Any, task_id: str = "",
                       parent_session_id: str = "", chat_type: str = "",
                       sender_id: str = "", is_shared: bool = False,
                       session_id: str = "") -> str | None:
    if not eligible_human_turn(platform, user_message, task_id=task_id,
                               parent_session_id=parent_session_id,
                               chat_type=chat_type, sender_id=sender_id,
                               is_shared=is_shared):
        return None
    state = service._read()
    if state.get("status") in ("ready", "pending_verification"):
        return None
    if _facade(ctx) is None:
        return None
    if _invitation_record(ctx) is not None:
        return _interview_context(ctx, service, session_id=session_id)
    if not _set(ctx, INVITATION_KEY, {"version": 1, "invited_at": time.time(),
                                      "status_at_invite": state.get("status", "not_started")}):
        return None
    _start_interview(ctx, session_id=session_id)
    return _invitation_context(service)


def pending_context(ctx: Any, service: OnboardingService, *, platform: Any,
                    user_message: Any, session_id: str = "",
                    chat_type: str = "", is_shared: bool = False) -> str | None:
    if not eligible_human_turn(platform, user_message, chat_type=chat_type,
                               is_shared=is_shared):
        return None
    token_match = re.search(r"\bsec-cont-[A-Za-z0-9]{6,}\b", _text(user_message))
    pending = _pending_record(ctx, session_id,
                              token_match.group(0)[len("sec-cont-"):] if token_match else "")
    if pending is None:
        return None
    _set(ctx, PENDING_KEY, None)
    state = service._read()
    scope = f" session {str(session_id)[:48]}" if session_id else " this conversation"
    return (
        "[Proactive Secretary onboarding continuation]\n"
        f"The user's previous /secretary request is awaiting this ordinary reply in{scope}. "
        f"Status: {state.get('status', 'not_started')}; next step: {next_step(state)}. "
        "Continue the foreground onboarding workflow using the secretary_onboarding tool. "
        "Ask only missing or conflicting facts and genuine permissions. Do not ask for "
        "credentials, do not arm jobs, and do not treat this reply as a standing grant. "
        "If a later turn belongs to a different session, do not continue this interview there."
    )


def _alert_lines(ctx: Any, ref: str = "") -> list[str]:
    try:
        import sys
        from pathlib import Path
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot.alertrefs import alert_context
        return alert_context(getattr(ctx, "state", None), ref=ref, limit=2)
    except Exception:  # noqa: BLE001 - context is optional
        return []


def relevance_context(ctx: Any, *, user_message: Any, platform: Any,
                      chat_type: str = "", is_shared: bool = False) -> str | None:
    if not eligible_human_turn(platform, user_message, chat_type=chat_type,
                               is_shared=is_shared):
        return None
    text = _text(user_message)
    ref = _REF.search(text)
    if ref is None and not _RELEVANT.search(text):
        return None
    lines = [
        "[Proactive Secretary context]",
        "Preserve your ordinary assistant identity and answer the user's message directly.",
        "Do not infer quoted-alert metadata that the host did not provide; use an explicit "
        "opaque reference and ask one short clarification when a responsibility or alert is "
        "ambiguous.",
        "Acknowledgement, thanks or silence are not preferences. A temporary snooze is "
        "temporary and must not become permanent suppression. Do not turn temporary scope "
        "into a permanent ban without an explicit request.",
        "For why/not-today use only the bounded alert reference and evidence already present; "
        "for remind-tomorrow/stop-these use the typed existing feedback/reminder CLI APIs with "
        "the exact scope and report their readback. Do not widen sources or grants.",
        "Any external send, booking, purchase, publication or calendar mutation still requires "
        "the user's specific approval in the foreground.",
    ]
    ref_text = ref.group(0) if ref is not None else ""
    if ref_text:
        lines.append(f"Referenced opaque id: {ref_text}.")
    alerts = _alert_lines(ctx, ref_text)
    if alerts:
        lines.append("Bounded emitted-alert references (emission only; delivery unconfirmed):")
        lines.extend(f"- {line}" for line in alerts)
    elif ref_text:
        lines.append("No matching emitted alert reference was found; ask the user to clarify "
                     "which alert or responsibility they mean before acting.")
    return "\n".join(lines)


def _append_guidance(context: str, guidance: str | None) -> str:
    """Append optional refresh guidance while preserving every existing line."""
    if not guidance or guidance in context:
        return context
    if not context:
        return guidance
    return f"{context}\n\n{guidance}"


def delivery_continuity_context(
    ctx: Any, *, platform: Any, user_message: Any, session_id: str = "",
    task_id: str = "", parent_session_id: str = "", chat_type: str = "",
    is_shared: bool = False, is_first_turn: bool = False,
) -> str | None:
    """Content-free recency hint pointing at the on-demand delivery read surface.

    The host exposes no chat id/chat type at this hook and ``Platform.TELEGRAM``
    covers both DM and group. Therefore this line never contains notice text,
    topics, handles or destination ids: only a count, a timestamp and the CLI
    read surface. It is also strictly limited to eligible human turns, a row in
    the last 24h, and either a deictic message or the first turn of a session.
    """
    if not eligible_human_turn(
            platform, user_message, task_id=task_id,
            parent_session_id=parent_session_id, chat_type=chat_type,
            is_shared=is_shared):
        return None
    if not is_first_turn and _DEICTIC.search(_text(user_message)) is None:
        return None
    try:
        try:
            from .. import paths as plugin_paths
        except ImportError:  # pragma: no cover - direct module execution
            import paths as plugin_paths  # type: ignore[no-redef]
        import sys
        from datetime import datetime, timedelta
        from pathlib import Path
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot import deliveries
        from proactive_pilot.clock import Clock
        from proactive_pilot.state import StateStore
        store = StateStore(Path(plugin_paths.state_dir()), Clock())
        summary = deliveries.summary_since(store, within_hours=24)
        if not summary:
            return None
        count = int(summary.get("count") or 0)
        newest = str(summary.get("newest") or "")
        if count <= 0 or not newest:
            return None
        try:
            since = (datetime.fromisoformat(newest) - timedelta(days=1)).isoformat()
        except ValueError:
            since = newest
        return (
            f"Recent proactive notices in this profile: {count} delivered in the "
            f"last 24h (newest {newest}). If the user refers to something you do "
            "not remember saying, run: hermes secretary deliveries "
            f"--since {since} --limit 5"
        )
    except Exception:  # noqa: BLE001 - continuity is optional and content-free
        return None


def make_pre_llm_hook(ctx: Any, service: OnboardingService | None = None):
    service = service or OnboardingService(ctx)

    def pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
        platform = kwargs.get("platform", "")
        user_message = kwargs.get("user_message", "")
        session_id = str(kwargs.get("session_id") or "")
        chat_type = str(kwargs.get("chat_type") or "")
        is_shared = bool(kwargs.get("is_shared", False))
        is_first_turn = bool(kwargs.get("is_first_turn", False))
        # Always remember an eligible human turn, even when relevance or
        # onboarding context has higher display priority. Consent checks read
        # this trusted turn; they never rely on model reply metadata.
        try:
            from .secretary_responsibilities import responsibility_context
            responsibility = responsibility_context(
                ctx, platform=platform, user_message=user_message,
                session_id=session_id, chat_type=chat_type, is_shared=is_shared)
        except Exception:  # noqa: BLE001 - a capture hint never breaks a turn
            responsibility = None
        pending = pending_context(ctx, service, platform=platform, user_message=user_message,
                                  session_id=session_id, chat_type=chat_type,
                                  is_shared=is_shared)
        if pending is not None:
            # An actual pending onboarding continuation keeps display priority
            # and may defer the optional hint; its safety lines are unchanged.
            return {"context": pending}
        try:
            from .secretary_context_refresh import refresh_guidance
            guidance = refresh_guidance(
                ctx, platform=platform, user_message=user_message,
                task_id=str(kwargs.get("task_id") or ""),
                parent_session_id=str(kwargs.get("parent_session_id") or ""),
                chat_type=chat_type, is_shared=is_shared)
        except Exception:  # noqa: BLE001 - optional guidance never breaks a turn
            guidance = None
        continuity = delivery_continuity_context(
            ctx, platform=platform, user_message=user_message,
            session_id=session_id, task_id=str(kwargs.get("task_id") or ""),
            parent_session_id=str(kwargs.get("parent_session_id") or ""),
            chat_type=chat_type, is_shared=is_shared,
            is_first_turn=is_first_turn)
        if continuity:
            guidance = _append_guidance(guidance or "", continuity)
        relevant = relevance_context(ctx, user_message=user_message, platform=platform,
                                     chat_type=chat_type, is_shared=is_shared)
        if relevant is not None:
            return {"context": _append_guidance(relevant, guidance)}
        if responsibility is not None:
            return {"context": _append_guidance(responsibility, guidance)}
        invited = invitation_context(
            ctx, service, platform=platform, user_message=user_message,
            task_id=str(kwargs.get("task_id") or ""),
            parent_session_id=str(kwargs.get("parent_session_id") or ""),
            chat_type=chat_type, sender_id=str(kwargs.get("sender_id") or ""),
            is_shared=is_shared, session_id=session_id)
        if invited is not None:
            return {"context": _append_guidance(invited, guidance)}
        if guidance is not None:
            return {"context": guidance}
        return None

    return pre_llm_call


def _control_state_paths() -> tuple[Any, Any]:
    """Resolve only this profile's existing plugin paths (no global interceptor)."""
    try:
        from .. import paths as plugin_paths
    except ImportError:  # pragma: no cover - direct module execution
        import paths as plugin_paths  # type: ignore[no-redef]
    return plugin_paths


def _apply_control(action: str) -> dict[str, Any]:
    """Use the existing deterministic preference owner for pause/resume."""
    paths = _control_state_paths()
    import sys
    from pathlib import Path
    src = str(Path(__file__).resolve().parent.parent / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from proactive_pilot.clock import Clock
    from proactive_pilot.feedback import (apply_action, load_preferences_file,
                                          save_preferences_file)
    now = Clock().now()
    path = paths.preferences_path()
    prefs = load_preferences_file(path, strict=True)
    updated = apply_action(prefs, {"action": action,
                                   "provenance": f"secretary chat /secretary {action}"}, now)
    save_preferences_file(path, updated, now)
    return {"paused": bool((updated.get("paused") or {}).get("active"))}


def _owned_jobs_summary() -> dict[str, Any]:
    """Read our ownership record only; never polls the cron backend."""
    try:
        paths = _control_state_paths()
        from .. import cron as cron_mod
    except Exception:  # noqa: BLE001
        return {"available": False, "owned": []}
    try:
        ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
        jobs = ownership.get("jobs") if isinstance(ownership, dict) else ownership
        if isinstance(jobs, dict):
            rows = sorted(jobs)
        elif isinstance(jobs, list):
            rows = sorted(str(item.get("name") or item) for item in jobs
                          if isinstance(item, (dict, str)))
        else:
            rows = []
        return {"available": True, "owned": rows}
    except Exception:  # noqa: BLE001 - missing ownership is simply "none recorded"
        return {"available": False, "owned": []}


def _readiness_summary() -> str:
    jobs = _owned_jobs_summary()
    owned = jobs.get("owned") or []
    return ("No schedule is claimed active here. "
            f"Owned jobs recorded: {', '.join(owned) if owned else 'none'}. "
            "Delivery remains unconfirmed until an explicit native canary; run "
            "`hermes secretary status --json` for the current profile.")


def _disable_control() -> str:
    """Pause and remove ONLY plugin-owned jobs/runners (existing CLI owner)."""
    import contextlib
    import io
    paused = False
    try:
        paused = _apply_control("pause").get("paused", False)
    except Exception as exc:  # noqa: BLE001 - report honestly, never claim success
        return f"Secretary disable failed safely ({type(exc).__name__}); nothing was claimed."
    try:
        from . import secretary_cli as cli
        backend = cli._backend()
        if not backend.is_available():
            return ("Proactive Secretary paused (collection/delivery stopped). The native "
                    "cron backend is unavailable from chat, so owned jobs were NOT removed; "
                    "run `hermes secretary disable` in the foreground to remove them. "
                    + _readiness_summary())
        with contextlib.redirect_stdout(io.StringIO()):
            cli._remove_own_jobs()
            cli._remove_own_runners()
    except Exception as exc:  # noqa: BLE001 - paused but removal unconfirmed
        return ("Proactive Secretary paused, but owned job/runner removal could not be "
                f"confirmed ({type(exc).__name__}). Run `hermes secretary disable` in the "
                "foreground. " + _readiness_summary())
    return ("Secretary disabled for this profile: collection/delivery paused and only "
            "plugin-owned jobs/runners were removed. Data was preserved. "
            + _readiness_summary())


def command_text(ctx: Any, service: OnboardingService, raw_args: str) -> str:
    args = (raw_args or "").strip().lower()
    if args == "cancel-setup":
        try:
            result = service.cancel()
        except OnboardingError as exc:
            return f"Secretary setup could not be cancelled: {exc}"
        clear_pending(ctx)
        return ("Secretary setup cancelled. Saved progress is retained; run /secretary setup "
                "to resume. No jobs or grants were changed. Use /secretary pause to stop alerts.")
    if args in ("pause", "mute"):
        try:
            state = _apply_control("pause")
        except Exception as exc:  # noqa: BLE001
            return f"Secretary pause failed safely ({type(exc).__name__}); nothing was claimed."
        return ("Proactive Secretary paused for this profile: collection and delivery are "
                "stopped. Use /secretary resume when you want it to run again. "
                "Requested reminders stay registered but are also paused until you resume.")
    if args == "resume":
        try:
            state = _apply_control("resume")
        except Exception as exc:  # noqa: BLE001
            return f"Secretary resume failed safely ({type(exc).__name__}); nothing was claimed."
        return ("Proactive Secretary is no longer paused. " + _readiness_summary())
    if args == "disable":
        return _disable_control()
    if args in ("cancel", "stop", "quit"):
        try:
            result = service.cancel()
        except OnboardingError as exc:
            return f"Secretary setup could not be cancelled: {exc}"
        clear_pending(ctx)
        return ("Secretary setup cancelled. Saved progress is retained; run /secretary setup "
                "to resume. No jobs or grants were changed.")
    if args in ("status", "show"):
        try:
            projection = secretary_status.collect(ctx, service)
        except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
            return f"Secretary status is unavailable ({type(exc).__name__})."
        return secretary_status.render_chat(projection)
    if args in ("", "setup", "start", "onboard", "resume"):
        try:
            info = service.inspect()
        except OnboardingError as exc:
            return f"Secretary setup is unavailable: {exc}"
        if info["state"]["status"] == "pending_verification":
            return ("Secretary settings are saved, but delivery verification is still pending "
                    "(this is not a live-ready claim) and no job is armed yet. "
                    f"Next action: {info.get('next_question')} "
                    "Run /secretary status to inspect, or repeat /secretary setup to review "
                    "and change settings. No job, schedule or grant is changed by this command.")
        if info["state"]["status"] == "ready":
            return ("Secretary setup is already complete. Use /secretary status to inspect, or "
                    "run /secretary setup again to review and change settings. No job or grant "
                    "is changed by this command.")
        pending = set_pending(ctx, action="setup")
        return (
            "Start or resume secretary setup in this conversation. I will use only "
            "already-authorised foreground memory to propose facts, ask for missing or "
            "conflicting details and genuine permissions, then show a preview before anything "
            "is saved.\n"
            f"Next question: {info['next_question']}\n"
            f"Reply with your answer AND this continuation code together: "
            f"sec-cont-{pending['token']}\n"
            "This channel does not expose a verified session id to slash commands, so the "
            "code is required; it is a short-lived routing code, not a credential. "
            "No schedule, source grant or destination is activated by this command, and never "
            "send credentials in chat."
        )
    return ("Usage: /secretary setup | status | pause | resume | disable | cancel-setup | "
            "cancel. Setup is resumable and never arms jobs or expands access by itself. "
            "`cancel` remains an alias for cancelling setup; it does not pause alerts.")


def make_command_handler(ctx: Any, service: OnboardingService | None = None):
    service = service or OnboardingService(ctx)

    def handler(raw_args: str) -> str:
        try:
            return command_text(ctx, service, raw_args)
        except Exception as exc:  # noqa: BLE001
            return f"Secretary command failed safely ({type(exc).__name__})."

    return handler


def register_conversation_surfaces(ctx: Any, *, service: OnboardingService | None = None,
                                   register_command: Any = None,
                                   register_hook: Any = None) -> dict[str, Any]:
    service = service or OnboardingService(ctx)
    command_reg = register_command if register_command is not None else getattr(ctx, "register_command", None)
    hook_reg = register_hook if register_hook is not None else getattr(ctx, "register_hook", None)
    result = {"command": False, "hook": False, "injection_used": False}
    if callable(command_reg):
        try:
            command_reg("secretary", make_command_handler(ctx, service),
                        description="Start, resume, inspect or cancel proactive-secretary setup.",
                        args_hint="[setup|status|pause|resume|disable|cancel-setup|cancel]")
            result["command"] = True
        except Exception as exc:  # noqa: BLE001
            result["command_error"] = f"{type(exc).__name__}: {exc}"
    if callable(hook_reg):
        try:
            hook_reg("pre_llm_call", make_pre_llm_hook(ctx, service))
            result["hook"] = True
        except Exception as exc:  # noqa: BLE001
            result["hook_error"] = f"{type(exc).__name__}: {exc}"
    return result


def register_onboarding_tool(ctx: Any, *, service: OnboardingService | None = None,
                             register_tool: Any = None) -> dict[str, Any]:
    from .secretary_onboarding import TOOL_SCHEMA, tool_handler
    service = service or OnboardingService(ctx)
    tool_reg = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(tool_reg):
        return {"tool": False, "error": "register_tool unavailable"}
    try:
        description = ("Inspect, propose from foreground memory, validate, preview and confirm "
                       "resumable secretary onboarding. Never grants access on a model assertion.")
        tool_reg(
            "secretary_onboarding", "secretary",
            {"name": "secretary_onboarding", "description": description,
             "parameters": TOOL_SCHEMA},
            tool_handler(service),
            description=description,
            emoji="",
        )
        return {"tool": True}
    except Exception as exc:  # noqa: BLE001
        return {"tool": False, "error": f"{type(exc).__name__}: {exc}"}


def tool_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
