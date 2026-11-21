"""Configured-only portal model routing through the native auxiliary slot.

The plugin never stores credentials or calls a raw SDK. It registers the existing
``proactive_secretary`` auxiliary key, derives defaults from its own native
settings, and reports the effective route honestly. Strict no-escalation routing
requires an explicit host capability; on hosts without it model operation is
reported unsupported and must not be invoked.

Capability is *only* the explicit ``fallback_policy`` keyword. A host that
swallows ``**kwargs`` does not prove enforcement and is treated as unsupported.
The plugin never imports private auxiliary routing helpers and never mutates an
operator's native auxiliary pin.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Mapping

AUX_KEY = "proactive_secretary"
SETTING_PRIMARY_PROVIDER = "primary_provider"
SETTING_PRIMARY_MODEL = "primary_model"
SETTING_FALLBACK_PROVIDER = "fallback_provider"
SETTING_FALLBACK_MODEL = "fallback_model"
ROUTE_SETTING_KEYS = (
    SETTING_PRIMARY_PROVIDER,
    SETTING_PRIMARY_MODEL,
    SETTING_FALLBACK_PROVIDER,
    SETTING_FALLBACK_MODEL,
)
STRICT_POLICY = "configured_only"
_MAX_ROUTE_TEXT = 160
_ROUTE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")


class ModelRouteUnsupported(RuntimeError):
    """The host cannot guarantee configured-only recovery."""


def _clean_route_text(value: Any, key: str) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if not isinstance(value, str):
        return "", f"{key} must be a string"
    text = value.strip()
    if not text:
        return "", None
    if len(text) > _MAX_ROUTE_TEXT or not _ROUTE_TEXT.fullmatch(text):
        return "", (f"{key} must be a bounded provider/model identifier "
                    f"(letters, digits and ._:/@+-; max {_MAX_ROUTE_TEXT})")
    return text, None


def validate_route_settings(settings: Mapping[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """Validate the four own-task route fields.

    Primary and fallback are both-or-none pairs. A fallback without a primary is
    blocked (there is no implicit main/global lane). Every value is a bounded
    string; credential-shaped input is not accepted anywhere in this surface.
    """
    raw = dict(settings or {})
    clean = {key: "" for key in ROUTE_SETTING_KEYS}
    errors: list[str] = []
    for key in ROUTE_SETTING_KEYS:
        value, error = _clean_route_text(raw.get(key), key)
        clean[key] = value
        if error:
            errors.append(error)
    primary = bool(clean[SETTING_PRIMARY_PROVIDER] or clean[SETTING_PRIMARY_MODEL])
    fallback = bool(clean[SETTING_FALLBACK_PROVIDER] or clean[SETTING_FALLBACK_MODEL])
    if bool(clean[SETTING_PRIMARY_PROVIDER]) != bool(clean[SETTING_PRIMARY_MODEL]):
        errors.append("primary_provider and primary_model must be set together or both left empty")
    if bool(clean[SETTING_FALLBACK_PROVIDER]) != bool(clean[SETTING_FALLBACK_MODEL]):
        errors.append("fallback_provider and fallback_model must be set together or both left empty")
    if fallback and not primary:
        errors.append("a fallback route requires an assigned primary route")
    return clean, errors


def read_route_settings(ctx: Any) -> dict[str, str]:
    settings = {key: "" for key in ROUTE_SETTING_KEYS}
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return settings
    for key in settings:
        try:
            value = getter(key, "")
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, str):
            settings[key] = value.strip()
    return settings


def build_defaults(settings: Mapping[str, Any]) -> dict[str, Any]:
    clean, errors = validate_route_settings(settings)
    if errors:
        # An invalid pair must not become a half-configured native default.
        return {}
    provider = clean[SETTING_PRIMARY_PROVIDER]
    model = clean[SETTING_PRIMARY_MODEL]
    fb_provider = clean[SETTING_FALLBACK_PROVIDER]
    fb_model = clean[SETTING_FALLBACK_MODEL]
    defaults: dict[str, Any] = {}
    if provider:
        defaults["provider"] = provider
    if model:
        defaults["model"] = model
    if fb_provider and fb_model:
        defaults["fallback_chain"] = [{"provider": fb_provider, "model": fb_model}]
    return defaults


def supports_strict_policy(register_aux: Any) -> bool:
    """True only for an explicit native ``fallback_policy`` keyword.

    ``**kwargs`` is deliberately NOT capability proof: a wrapper that ignores
    the keyword would otherwise be misreported as strict.
    """
    if not callable(register_aux):
        return False
    try:
        signature = inspect.signature(register_aux)
    except (TypeError, ValueError):
        return False
    parameter = signature.parameters.get("fallback_policy")
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _sanitize_auxiliary(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in ("provider", "model"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if isinstance(block.get("base_url"), str) and block["base_url"].strip():
        # Presence only: never echo an endpoint or credential-bearing URL.
        out["base_url_configured"] = True
    chain = block.get("fallback_chain")
    if isinstance(chain, list):
        clean_chain = []
        for entry in chain:
            if not isinstance(entry, Mapping):
                continue
            item = {}
            for key in ("provider", "model"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value.strip()
            clean_chain.append(item)
        out["fallback_chain"] = clean_chain
    return out


def _read_public_auxiliary() -> dict[str, Any] | None:
    """Read only this task's operator block through the public config reader.

    No private routing helper is imported. The result is sanitized: provider,
    model, a base-url presence flag and fallback entries only; no secret, raw
    endpoint or unrelated task is returned. A reader failure stays unverifiable
    (``None``), never a claimed explicit pin.
    """
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    except Exception:  # noqa: BLE001 - no public config reader / no profile
        return None
    auxiliary = config.get("auxiliary") if isinstance(config, Mapping) else None
    if not isinstance(auxiliary, Mapping):
        return None
    return _sanitize_auxiliary(auxiliary.get(AUX_KEY))


def read_explicit_auxiliary(reader: Any = None) -> dict[str, Any] | None:
    if reader is not None:
        try:
            return _sanitize_auxiliary(reader(AUX_KEY))
        except Exception:  # noqa: BLE001
            return None
    return _read_public_auxiliary()


def _pinned(block: Mapping[str, Any]) -> bool:
    provider = str(block.get("provider") or "").strip().lower()
    chain = block.get("fallback_chain")
    return (provider not in ("", "auto")
            or bool(str(block.get("model") or "").strip())
            or bool(block.get("base_url_configured"))
            or bool(isinstance(chain, list) and chain))


def _effective_block(defaults: Mapping[str, Any], explicit: Mapping[str, Any] | None) -> dict[str, Any]:
    block: dict[str, Any] = {}
    if defaults:
        block.update(defaults)
    if explicit:
        for key in ("provider", "model"):
            value = str(explicit.get(key) or "").strip()
            if value and value.lower() != "auto":
                block[key] = value
        if "fallback_chain" in explicit:
            block["fallback_chain"] = explicit["fallback_chain"]
    return block


def _route_blockers(block: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    provider = str(block.get("provider") or "").strip()
    model = str(block.get("model") or "").strip()
    if bool(provider) != bool(model):
        blockers.append("effective primary must set provider and model together")
    chain = block.get("fallback_chain")
    entries = list(chain) if isinstance(chain, list) else []
    if len(entries) > 1:
        blockers.append(
            "operator auxiliary fallback_chain has extra lanes; the portal allows "
            "exactly one assigned fallback. Correct the native auxiliary pin explicitly.")
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            blockers.append(f"effective fallback lane {index} is malformed")
            continue
        if bool(str(entry.get("provider") or "").strip()) != bool(str(entry.get("model") or "").strip()):
            blockers.append(f"effective fallback lane {index} must set provider and model together")
    if entries and not (provider and model):
        blockers.append("effective fallback exists without an assigned primary")
    return blockers


def effective_route_status(ctx: Any = None, *, settings: Mapping[str, Any] | None = None,
                           auxiliary_reader: Any = None, strict_active: bool = False) -> dict[str, Any]:
    raw_settings = dict(settings) if settings is not None else read_route_settings(ctx)
    clean, setting_errors = validate_route_settings(raw_settings)
    defaults = build_defaults(clean)
    explicit = read_explicit_auxiliary(auxiliary_reader)
    explicit_pinned = bool(explicit and _pinned(explicit))
    source = ("explicit_auxiliary" if explicit_pinned
              else ("plugin_defaults" if defaults else "chat_model_inheritance"))
    block = _effective_block(defaults, explicit if explicit_pinned else None)
    route_blockers = list(setting_errors) + _route_blockers(block)
    primary_provider = str(block.get("provider") or "")
    primary_model = str(block.get("model") or "")
    primary = {
        "provider": primary_provider,
        "model": primary_model,
        "source": (source if (primary_provider or primary_model)
                   else "chat_model_inheritance"),
    }
    fallback = None
    chain = block.get("fallback_chain")
    if isinstance(chain, list) and chain and isinstance(chain[0], Mapping):
        fallback = {
            "provider": str(chain[0].get("provider") or ""),
            "model": str(chain[0].get("model") or ""),
            "source": "explicit_auxiliary" if explicit_pinned else "plugin_defaults",
            "additional_entries": max(0, len(chain) - 1),
        }
    if primary["source"] == "chat_model_inheritance" and not primary_provider and not primary_model:
        primary["note"] = ("No portal primary is configured; initial chat-model inheritance is "
                           "the documented fallback, not an unrestricted recovery ladder.")
    warnings: list[str] = []
    if not strict_active:
        warnings.append("configured-only model operation unsupported: host lacks "
                        "register_auxiliary_task(fallback_policy=\"configured_only\")")
    if explicit_pinned:
        warnings.append("explicit native auxiliary pin wins over plugin settings; "
                        "the plugin never mutates it")
    if route_blockers:
        warnings.append("effective portal route is blocked: " + "; ".join(route_blockers))
    return {
        "auxiliary_key": AUX_KEY,
        "primary": primary,
        "fallback": fallback,
        "route_configured": bool(primary_provider or primary_model or fallback),
        "blockers": route_blockers,
        "strict_policy": STRICT_POLICY if strict_active else "unsupported",
        "strict_active": bool(strict_active),
        "configured_model_calls_allowed": bool(strict_active and not route_blockers),
        "freshness": "next portal process or native plugin reload",
        "reload_required": True,
        "warnings": warnings,
    }


def _bind_runtime(ctx: Any, status: Mapping[str, Any], *, error: str = "") -> None:
    """Bind this context's dispatch guard; failure to bind stays fail-closed."""
    try:
        from proactive_pilot.native_runtime import bind_model_lane
    except Exception:  # noqa: BLE001 - runtime package unavailable in this process
        return
    allowed = bool(status.get("configured_model_calls_allowed"))
    reason = error or ("configured-only lane ready" if allowed else
                       "configured-only model lane unavailable")
    bind_model_lane(ctx, allowed=allowed, reason=reason, status=dict(status))


def register_configured_models(ctx: Any, *, auxiliary_reader: Any = None) -> dict[str, Any]:
    settings = read_route_settings(ctx)
    clean, setting_errors = validate_route_settings(settings)
    defaults = build_defaults(clean)
    register_aux = getattr(ctx, "register_auxiliary_task", None)
    if not callable(register_aux):
        status = effective_route_status(
            ctx, settings=settings, auxiliary_reader=auxiliary_reader, strict_active=False)
        status["blockers"] = list(status.get("blockers") or []) + ["register_auxiliary_task unavailable"]
        status["configured_model_calls_allowed"] = False
        _bind_runtime(ctx, status, error="register_auxiliary_task unavailable")
        return {
            "registered": False,
            "strict_active": False,
            "strict_error": "register_auxiliary_task unavailable",
            "status": status,
            "error": "register_auxiliary_task unavailable",
        }
    kwargs: dict[str, Any] = {
        "display_name": "Proactive portal",
        "description": "Portal evaluator/planner/intake structured calls.",
    }
    if defaults:
        kwargs["defaults"] = defaults
    strict_supported = supports_strict_policy(register_aux)
    strict_active = False
    strict_error = ""
    registered = False
    registration_error = ""
    if strict_supported:
        try:
            register_aux(AUX_KEY, fallback_policy=STRICT_POLICY, **kwargs)
            registered = strict_active = True
        except Exception as exc:  # noqa: BLE001 - never fall back to an unguarded lane
            strict_error = f"{type(exc).__name__}: {exc}"
            registration_error = strict_error
    else:
        # Keep the task visible for deterministic controls, but this lane can
        # never dispatch model work: there is no explicit strict contract.
        try:
            register_aux(AUX_KEY, **kwargs)
            registered = True
        except Exception as exc:  # noqa: BLE001
            registration_error = f"{type(exc).__name__}: {exc}"
    status = effective_route_status(
        ctx, settings=settings, auxiliary_reader=auxiliary_reader,
        strict_active=strict_active)
    status["strict_supported"] = strict_supported
    status["registration_error"] = registration_error
    _bind_runtime(ctx, status, error=registration_error)
    return {
        "registered": registered,
        "strict_active": strict_active,
        "strict_supported": strict_supported,
        "strict_error": strict_error,
        "defaults": defaults,
        "status": status,
        "error": registration_error,
    }


def require_configured_model_lane(status: Mapping[str, Any]) -> None:
    if not status.get("strict_active") or status.get("blockers"):
        remedy = "; ".join(str(item) for item in status.get("blockers") or [])
        raise ModelRouteUnsupported(
            "Configured-only portal model routing is unavailable on this host or route"
            + (f": {remedy}" if remedy else "")
            + "; no model call was made.")
