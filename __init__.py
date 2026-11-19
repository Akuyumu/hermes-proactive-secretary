"""hermes-proactive-secretary plugin entry point.

``register(ctx)`` exposes the plugin surface only. It never reads private
sources, sends messages, arms jobs or mutates access at registration time.
"""
from __future__ import annotations

import sys
from pathlib import Path


# Sanitized diagnostics only; never a credential or raw provider output.
_MODEL_REGISTRATION: dict = {}
_CONVERSATION_REGISTRATION: dict = {}
_PORTAL_NOTICE_REGISTRATION: dict = {}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _setup_secretary_cli(subparser) -> None:
    from .secretary_cli import register_cli
    register_cli(subparser)


def _handle_secretary_cli(args):
    from .secretary_cli import secretary_command
    return secretary_command(args)


def _register_conversation(ctx) -> dict:
    from .secretary_context_refresh import register_context_tool
    from .secretary_conversation import register_conversation_surfaces, register_onboarding_tool
    from .secretary_followups import register_tool as register_followup_tool
    from .secretary_responsibilities import register_tool as register_responsibility_tool
    result = register_onboarding_tool(ctx)
    result.update(register_context_tool(ctx))
    result.update(register_responsibility_tool(ctx))
    result.update(register_followup_tool(ctx))
    result.update(register_conversation_surfaces(ctx))
    return result


def register(ctx) -> None:
    """Wire the plugin into Hermes. Called exactly once at load time."""
    root = _plugin_root()
    src = root / "src"
    if src.is_dir():
        src_str = str(src)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)

    from . import secretary_cli as _cli
    _cli.set_native_context(ctx)

    # Model routing is optional, but it is never silently replaced by an
    # unguarded/native lane when registration or import fails. The runtime guard
    # is bound to THIS host context and denies model dispatch until a real
    # configured-only contract is registered. Deterministic controls and
    # reminders do not depend on this block.
    try:
        from .secretary_models import register_configured_models
        _MODEL_REGISTRATION.update(register_configured_models(ctx))
    except Exception as exc:  # noqa: BLE001 - guard, never a permissive fallback
        _MODEL_REGISTRATION.update({
            "registered": False, "strict_active": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        try:
            from proactive_pilot.native_runtime import bind_model_lane
            bind_model_lane(
                ctx, allowed=False, status=dict(_MODEL_REGISTRATION),
                reason=f"secretary model registration failed: {type(exc).__name__}")
        except Exception:  # noqa: BLE001 - runtime import failure also stays closed
            pass

    skill = root / "skills" / "proactive-secretary" / "SKILL.md"
    if skill.is_file():
        try:
            ctx.register_skill("proactive-secretary", skill,
                               description="Proactive secretary setup, operation and limits.")
        except Exception:  # noqa: BLE001
            pass

    try:
        _CONVERSATION_REGISTRATION.update(_register_conversation(ctx))
    except Exception as exc:  # noqa: BLE001 - deterministic CLI must still load
        _CONVERSATION_REGISTRATION.update({
            "error": f"{type(exc).__name__}: {exc}"})

    # The proposal tool is optional: an old/fake host without native cron
    # execution context must still load the CLI and existing tools.
    try:
        from .secretary_portal_notices import register_tool as register_portal_notice_tool
        _PORTAL_NOTICE_REGISTRATION.update(register_portal_notice_tool(ctx))
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        _PORTAL_NOTICE_REGISTRATION.update({
            "portal_notice_tool": False,
            "portal_notice_tool_error": f"{type(exc).__name__}: {exc}",
        })

    ctx.register_cli_command(
        name="secretary",
        help="Proactive secretary: one-command setup, monitoring and reminders",
        setup_fn=_setup_secretary_cli,
        handler_fn=_handle_secretary_cli,
        description=("Proactive secretary over your own authorised mail, calendar and "
                     "tasks plus public sources, with native scheduled jobs."),
    )
