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
_GUIDANCE_REGISTRATION: dict = {}
_ACKNOWLEDGE_REGISTRATION: dict = {}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _setup_secretary_cli(subparser) -> None:
    from .secretary.secretary_cli import register_cli
    register_cli(subparser)


def _handle_secretary_cli(args):
    from .secretary.secretary_cli import secretary_command
    return secretary_command(args)


def _skill_description(skill: Path) -> str:
    """Read the routing description declared in the skill's frontmatter."""
    try:
        text = skill.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "description":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def _register_conversation(ctx) -> dict:
    from .secretary.secretary_context_refresh import register_context_tool
    from .secretary.secretary_conversation import register_conversation_surfaces, register_onboarding_tool
    from .secretary.secretary_draft import register_tool as register_draft_tool
    from .secretary.secretary_followups import register_tool as register_followup_tool
    from .secretary.secretary_responsibilities import register_tool as register_responsibility_tool
    result = register_onboarding_tool(ctx)
    result.update(register_context_tool(ctx))
    result.update(register_responsibility_tool(ctx))
    result.update(register_followup_tool(ctx))
    result.update(register_draft_tool(ctx))
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

    # One-time, idempotent copy from the retired id's data root. This runs
    # before any store opens; a failure is only a diagnostic and the plugin
    # still starts with the new (empty) root.
    try:
        from proactive_pilot.data_migration import migrate_for_load
        migrate_for_load()
    except Exception as exc:  # noqa: BLE001 - migration must never block load
        print(f"proactive-secretary: data migration skipped ({type(exc).__name__})",
              file=sys.stderr)

    from .secretary import secretary_cli as _cli
    _cli.set_native_context(ctx)

    # Model routing is optional, but it is never silently replaced by an
    # unguarded/native lane when registration or import fails. The runtime guard
    # is bound to THIS host context and denies model dispatch until a real
    # configured-only contract is registered. Deterministic controls and
    # reminders do not depend on this block.
    try:
        from .secretary.secretary_models import register_configured_models
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
                               description=_skill_description(skill))
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
        from .secretary.secretary_portal_notices import register_tool as register_portal_notice_tool
        _PORTAL_NOTICE_REGISTRATION.update(register_portal_notice_tool(ctx))
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        _PORTAL_NOTICE_REGISTRATION.update({
            "portal_notice_tool": False,
            "portal_notice_tool_error": f"{type(exc).__name__}: {exc}",
        })

    # Owner guidance is deterministic state; the tool is optional so an older
    # host without this surface still loads everything else.
    try:
        from .secretary.secretary_guidance_tool import register_tool as register_guidance_tool
        _GUIDANCE_REGISTRATION.update(register_guidance_tool(ctx))
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        _GUIDANCE_REGISTRATION.update({
            "guidance_tool": False,
            "guidance_tool_error": f"{type(exc).__name__}: {exc}",
        })

    # Owner acknowledgements write the same stores as guidance and follow-ups;
    # the tool is optional so an older host still loads everything else.
    try:
        from .secretary.secretary_acknowledge_tool import register_tool as register_acknowledge_tool
        _ACKNOWLEDGE_REGISTRATION.update(register_acknowledge_tool(ctx))
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        _ACKNOWLEDGE_REGISTRATION.update({
            "acknowledge_tool": False,
            "acknowledge_tool_error": f"{type(exc).__name__}: {exc}",
        })

    ctx.register_cli_command(
        name="secretary",
        help="Proactive secretary: one-command setup, monitoring and reminders",
        setup_fn=_setup_secretary_cli,
        handler_fn=_handle_secretary_cli,
        description=("Proactive secretary over your own authorised mail, calendar and "
                     "tasks plus public sources, with native scheduled jobs."),
    )
