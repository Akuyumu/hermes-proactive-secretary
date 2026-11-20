"""Resumable, validated onboarding state for the proactive secretary.

The foreground Hermes agent owns the conversation. This module only keeps typed,
profile-scoped state, validates proposed values, and returns exact readback. It
never reads sources, arms jobs, stores credentials, or treats model assertions as
user consent.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .secretary_models import (ROUTE_SETTING_KEYS, effective_route_status,
                               validate_route_settings)
from . import secretary_consent as consent

STATE_KEY = "onboarding_state"
SCHEMA = 1
# Canonical unset: no deterministic daily-count ceiling. It is never a hidden
# five-message quota and is never rendered as 0.
DEFAULT_ALLOWANCE = None
STEP_ORDER = ("basics", "destination", "allowance", "quiet", "autonomy",
              "preparation", "permissions", "review", "complete")
GRANT_KEYS = ("memory", "mail", "calendar", "tasks", "public_sources", "discretionary")
SOURCE_GRANT_KEYS = ("mail", "calendar", "tasks", "public_sources")
AUTONOMY_MODES = ("review_only", "prepare_private", "prepare_and_approve")
AUTONOMY_LABELS = {
    "review_only": "Review only: no autonomous preparation",
    "prepare_private": "Prepare privately: private reversible drafts inside chosen scopes",
    "prepare_and_approve": "Prepare and ask: private drafts plus a quick ask before effects",
}
PREP_SCOPES = ("conversation", "public", "mail", "calendar", "tasks", "documents", "memory")

_SAFE_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*$")
_SAFE_DELIVER = re.compile(r"^[A-Za-z0-9 _:@./+,-]{1,128}$")
_SAFE_LABEL = re.compile(r"^[^\n\r\t\x00]{1,160}$")
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "client_secret", "token",
    "access_token", "refresh_token", "password", "passwd", "credential",
    "credentials", "private_key", "authorization", "cookie", "session_key",
})


class _ArmArgs:
    """Minimal argparse-compatible namespace for the existing setup engine."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class OnboardingError(ValueError):
    """A proposed onboarding change is invalid or lacks explicit consent."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": 1,
        "status": "not_started",
        "current_step": "basics",
        "created_at": "",
        "updated_at": "",
        "completed_at": "",
        "facts": {},
        "proposals": [],
        "settings": {
            "timezone": "",
            "daily_allowance": DEFAULT_ALLOWANCE,
            "quiet_start_hour": 0,
            "quiet_end_hour": 8,
            "persona": "Hermes",
            "primary_provider": "",
            "primary_model": "",
            "fallback_provider": "",
            "fallback_model": "",
            "autonomy_mode": "",
            "preparation_scopes": [],
            "preparation_source_consent": False,
        },
        "grants": {key: False for key in GRANT_KEYS},
        "source_scope": {
            "mail_accounts": [],
            "calendar_accounts": [],
            "calendars": [],
            "task_lists": [],
            "public_interests": [],
        },
        "destination": {"target": "", "status": "unconfigured", "challenge": "",
                        "confirmed_at": ""},
        "capabilities": {},
        "history": [],
        "decided": [],
        "consent_review": {},
        # Last values actually written by an onboarding confirmation. This lets
        # a later read tell an applied value (safe to converge on the current
        # native form) from an unsaved in-progress edit (must be preserved).
        "applied": {},
    }


def _scan_credentials(value: Any, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            dotted = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.strip().lower() in _CREDENTIAL_KEYS and child not in (None, "", [], {}):
                return dotted
            found = _scan_credentials(child, dotted)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _scan_credentials(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _require_int(value: Any, name: str, low: int, high: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OnboardingError(f"{name} must be an integer")
    if value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"between {low} and {high}"
        raise OnboardingError(f"{name} must be {bound}")
    return value


def _optional_allowance(value: Any) -> int | None:
    """Validate the optional daily allowance with the canonical owner.

    Blank/``None`` means unset; explicit nonnegative integers (including 0) are
    preserved. Booleans, negatives, fractions and other non-numeric values are
    refused rather than guessed.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    import sys
    from pathlib import Path
    src = str(Path(__file__).resolve().parent.parent / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from proactive_pilot.userconfig import validate_config
    try:
        probe = validate_config({
            "schema": 1,
            "version": 1,
            "notification": {"daily_normal_cap": value},
        })
    except Exception as exc:  # noqa: BLE001 - report one bounded validation error
        raise OnboardingError(
            "daily_allowance must be a nonnegative integer or blank") from exc
    return probe["notification"]["daily_normal_cap"]


def _safe_string_list(value: Any, name: str, pattern: re.Pattern[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OnboardingError(f"{name} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item.strip()):
            raise OnboardingError(f"{name} contains an unsafe or empty entry")
        text = item.strip()
        if text not in out:
            out.append(text)
    return out


def _require_timezone(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OnboardingError("timezone must be a non-empty IANA timezone name")
    text = value.strip()
    if not _SAFE_TIMEZONE.fullmatch(text):
        raise OnboardingError("timezone is not a valid IANA timezone name")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OnboardingError(
            "timezone is not a known IANA timezone (e.g. UTC, Europe/London)") from exc
    return text


def validate_settings(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise OnboardingError("settings must be an object")
    found = _scan_credentials(patch)
    if found:
        raise OnboardingError(f"credential-shaped field {found!r} is not accepted")
    allowed = {"timezone", "persona", "daily_allowance", "quiet_start_hour",
               "quiet_end_hour", "autonomy_mode", "preparation_scopes",
               "preparation_source_consent", *ROUTE_SETTING_KEYS}
    unknown = set(patch) - allowed
    if unknown:
        raise OnboardingError(f"unknown setting keys: {', '.join(sorted(map(str, unknown)))}")
    out: dict[str, Any] = {}
    if "timezone" in patch:
        out["timezone"] = _require_timezone(patch["timezone"])
    if "persona" in patch:
        value = patch["persona"]
        if not isinstance(value, str) or not value.strip() or not _SAFE_LABEL.fullmatch(value.strip()):
            raise OnboardingError("persona must be a bounded non-empty string")
        out["persona"] = value.strip()[:64]
    if "daily_allowance" in patch:
        out["daily_allowance"] = _optional_allowance(patch["daily_allowance"])
    for key in ("quiet_start_hour", "quiet_end_hour"):
        if key in patch:
            out[key] = _require_int(patch[key], key, 0, 23)
    if "autonomy_mode" in patch:
        mode = patch["autonomy_mode"]
        if mode not in ("", *AUTONOMY_MODES):
            raise OnboardingError("autonomy_mode must be review_only, prepare_private "
                                  "or prepare_and_approve")
        out["autonomy_mode"] = mode
    if "preparation_scopes" in patch:
        scopes = _safe_string_list(patch["preparation_scopes"], "preparation_scopes",
                                   re.compile(r"^[a-z]{3,20}$"))
        unknown = [scope for scope in scopes if scope not in PREP_SCOPES]
        if unknown:
            raise OnboardingError("unsupported preparation scope: " + ", ".join(unknown))
        out["preparation_scopes"] = scopes
    if "preparation_source_consent" in patch:
        if not isinstance(patch["preparation_source_consent"], bool):
            raise OnboardingError("preparation_source_consent must be true or false")
        out["preparation_source_consent"] = patch["preparation_source_consent"]
    route_patch = {key: patch[key] for key in ROUTE_SETTING_KEYS if key in patch}
    if route_patch:
        clean, errors = validate_route_settings(route_patch)
        if errors:
            raise OnboardingError("; ".join(errors))
        out.update(clean)
    return out


def validate_grants(grants: Any, *, explicit_consent: bool) -> dict[str, Any]:
    if not isinstance(grants, Mapping):
        raise OnboardingError("grants must be an object")
    unknown = set(grants) - set(GRANT_KEYS)
    if unknown:
        raise OnboardingError(f"unknown grant keys: {', '.join(sorted(map(str, unknown)))}")
    clean: dict[str, bool] = {}
    for key, value in grants.items():
        if not isinstance(value, bool):
            raise OnboardingError(f"grant {key} must be true or false")
        clean[key] = value
    if any(clean.values()) and not explicit_consent:
        raise OnboardingError("granting a permission requires explicit_consent=true")
    return clean


def validate_destination(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _SAFE_DELIVER.fullmatch(value.strip()):
        raise OnboardingError("destination is not a safe deliver target")
    return value.strip()


def validate_fact(key: Any, value: Any) -> tuple[str, str]:
    if not isinstance(key, str) or not _SAFE_LABEL.fullmatch(key.strip()):
        raise OnboardingError("fact key must be a safe short label")
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value.strip()):
        raise OnboardingError("fact value must be a safe short string")
    return key.strip()[:80], value.strip()[:400]


def _merge_settings(state: dict[str, Any], patch: Any) -> None:
    state["settings"].update(validate_settings(patch))


def _merge_scope(state: dict[str, Any], patch: Any) -> None:
    if not isinstance(patch, Mapping):
        raise OnboardingError("source_scope must be an object")
    found = _scan_credentials(patch)
    if found:
        raise OnboardingError(f"credential-shaped field {found!r} is not accepted")
    rules = {
        "mail_accounts": re.compile(r"^[A-Za-z0-9._@+-]{1,64}$"),
        "calendar_accounts": re.compile(r"^[A-Za-z0-9._@+-]{1,64}$"),
        "calendars": re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$"),
        "task_lists": _SAFE_LABEL,
        "public_interests": _SAFE_LABEL,
    }
    for key, pattern in rules.items():
        if key in patch:
            state["source_scope"][key] = _safe_string_list(patch[key], key, pattern)
    unknown = set(patch) - set(rules)
    if unknown:
        raise OnboardingError(f"unknown source_scope keys: {', '.join(sorted(map(str, unknown)))}")


def next_step(state: Mapping[str, Any]) -> str:
    if state.get("status") == "pending_verification":
        # Settings are saved but delivery is NOT confirmed and jobs are NOT
        # armed. This is a distinct, actionable state, never "complete".
        return "delivery_verification"
    if state.get("status") == "ready":
        return "complete"
    settings = state.get("settings") or {}
    destination = state.get("destination") or {}
    decided = set(state.get("decided") or [])
    if not settings.get("timezone"):
        return "basics"
    if not destination.get("target"):
        return "destination"
    # Ask only genuine gaps. A field already decided in this or an existing
    # configuration is never re-asked (no duplicate biography).
    if "setting:daily_allowance" not in decided:
        return "allowance"
    if "setting:quiet_start_hour" not in decided or "setting:quiet_end_hour" not in decided:
        return "quiet"
    if "setting:autonomy_mode" not in decided or not settings.get("autonomy_mode"):
        return "autonomy"
    if (settings.get("autonomy_mode") != "review_only"
            and "setting:preparation_scopes" not in decided):
        return "preparation"
    if not any(key.startswith("grant:") for key in decided):
        return "permissions"
    return "review"


def next_question(state: Mapping[str, Any]) -> str:
    step = next_step(state)
    if step == "delivery_verification":
        target = str((state.get("destination") or {}).get("target") or "").strip()
        if target:
            return ("Settings are saved for " + target + ", but delivery is NOT confirmed and NOT verified and "
                    "jobs are not armed yet. If you explicitly confirm, I will run the "
                    "existing setup engine on your behalf to arm only the plugin-owned "
                    "no-agent jobs; no manual command is required. The foreground "
                    "equivalent is `hermes secretary setup --deliver " + target + "`. "
                    "Delivery remains unverified until an explicitly authorised native "
                    "canary.")
        return ("Settings are saved, but no delivery destination is confirmed and jobs are "
                "NOT armed. Choose one destination, then explicitly confirm and I will arm "
                "the plugin-owned jobs; no manual command is required.")
    if step == "basics":
        return ("Which timezone should the secretary use for quiet hours and daily limits? "
                "Give a standard IANA name such as UTC, Europe/London or America/New_York; "
                "a city name alone is not guessed.")
    if step == "destination":
        return ("Which single delivery destination should receive proactive messages? "
                "A destination is not treated as verified until confirmed.")
    if step == "allowance":
        return ("What daily discretionary message ceiling do you want? Any nonnegative "
                "integer; zero means no ordinary discretionary messages. It is a ceiling, "
                "never a target.")
    if step == "quiet":
        return ("Which local quiet hours should defer ordinary messages? Use 24-hour "
                "start/end hours; this is not a pause.")
    if step == "autonomy":
        return ("How much should the secretary do on its own? Choose review_only "
                "(no autonomous preparation), prepare_private (private reversible "
                "drafts inside scopes you allow), or prepare_and_approve (same, plus "
                "one quick ask before any consequential effect). It can never infer "
                "wider permissions or perform an external action without approval.")
    if step == "preparation":
        return ("Which standing scopes may private preparation read? Name only the "
                "ones you explicitly allow from conversation, public, mail, calendar, "
                "tasks, memory (documents are off unless you name them). I will not "
                "read a scope you have not granted, and cached facts still require "
                "separate consent.")
    if step == "permissions":
        return ("Which standing source permissions, if any, do you explicitly grant? "
                "Memory proposals are not grants, and no source is enabled without your "
                "typed consent.")
    if step == "review":
        return ("Review the preview and confirm. Permissions are only saved with an explicit "
                "typed grant; setup is saved as pending verification, and I can arm the "
                "owned jobs for you after you explicitly confirm — no manual command.")
    return "Onboarding has no further required questions."


class OnboardingService:
    """Profile-scoped state plus validation; no source, model or cron side effects."""

    def __init__(self, ctx: Any = None, *, state: Any = None, config_resolver: Any = None,
                 settings_writer: Any = None) -> None:
        self.ctx = ctx
        self._state_facade = state if state is not None else getattr(ctx, "state", None)
        self._config_resolver = config_resolver
        self._settings_writer = settings_writer

    def _read(self) -> dict[str, Any]:
        raw = None
        if self._state_facade is not None:
            try:
                raw = self._state_facade.get(STATE_KEY)
            except Exception as exc:  # noqa: BLE001
                raise OnboardingError(f"cannot read onboarding state ({type(exc).__name__})") from exc
        if not isinstance(raw, dict):
            state = default_state()
            self._seed_from_effective_config(state)
            return state
        state = default_state()
        for key in state:
            if key in raw:
                if isinstance(state[key], dict) and isinstance(raw[key], dict):
                    state[key].update(copy.deepcopy(raw[key]))
                else:
                    state[key] = copy.deepcopy(raw[key])
        self._seed_from_effective_config(state)
        return state

    @staticmethod
    def _known_fields(loaded: Any) -> set[str]:
        """Fields the user actually chose, never validation-filled factories.

        A real load carries raw-presence provenance. A legacy caller that
        constructs ``LoadedConfig`` directly has none; for that shape only
        non-neutral values are treated as evidence, so factories are not
        mistaken for choices.
        """
        provenance = getattr(loaded, "provenance", None)
        if isinstance(provenance, dict):
            return {str(item) for item in (provenance.get("fields") or [])
                    if isinstance(item, str)}
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent.parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.userconfig import (default_config,
                                                    onboarding_provenance,
                                                    raw_value)
            neutral = default_config()
            config = getattr(loaded, "config", {}) or {}
            if onboarding_provenance(config).get("mode") in (
                    "factory_neutral", "neutral_init"):
                return set()
            return {path for path in (
                "timezone", "notification.destination",
                "notification.daily_normal_cap", "notification.quiet_start_hour",
                "notification.quiet_end_hour", "sources.mail", "sources.calendar",
                "sources.tasks", "mail.accounts", "calendar.accounts",
                "calendar.calendars", "tasks.lists", "public_interests",
                "preparation.enabled", "preparation.scopes",
                "preparation.source_consent", "preparation.mode",
            ) if raw_value(config, path) != raw_value(neutral, path)}
        except Exception:  # noqa: BLE001 - no provenance is simply no evidence
            return set()

    def _seed_from_effective_config(self, state: dict[str, Any]) -> None:
        """Converge onboarding fields on the CURRENT effective configuration.

        The native/file configuration is the one runtime owner; onboarding state
        is a resumable snapshot, never a shadow preference store. An applied
        value can therefore be refreshed when a native form edit changes the
        effective configuration, while an unsaved in-progress edit is preserved.
        Fields the configuration does not mention (proposed facts, un-applied
        choices and separately granted scopes) are never invented, and
        generated-neutral factories are never mistaken for user decisions.
        """
        try:
            loaded = self._loaded_effective()
        except Exception:  # noqa: BLE001 - no config is simply no seed
            return
        if not loaded.present:
            return
        config = loaded.config
        known = self._known_fields(loaded)
        decided = set(state.get("decided") or [])
        applied = state.get("applied") if isinstance(state.get("applied"), dict) else {}
        applied_decided = set(applied.get("decided") or [])
        complete = state.get("status") in ("pending_verification", "ready")

        def _safe_to_refresh(marker: str, bucket: str, key: str, current: Any) -> bool:
            """True when state does not hold an unsaved edit for this field."""
            if marker not in decided:
                return True  # never presented as a choice: seed from config
            if not current and current != 0:
                return True  # blank state always accepts an explicit config value
            if not applied:
                # Legacy state has no applied snapshot: a completed setup is a
                # stale snapshot; an in-progress one may still be edited.
                return complete
            snapshot = applied.get(bucket) if isinstance(applied.get(bucket), dict) else {}
            return marker in applied_decided and snapshot.get(key) == current

        timezone_name = str(config.get("timezone") or "").strip()
        if timezone_name and "timezone" in known:
            current = state["settings"].get("timezone") or ""
            if _safe_to_refresh("setting:timezone", "settings", "timezone", current):
                state["settings"]["timezone"] = timezone_name
                decided.add("setting:timezone")

        notif = config.get("notification") or {}
        if "notification.destination" in known:
            target = str(notif.get("destination") or "").strip()
            destination = state.setdefault("destination", {})
            current = str(destination.get("target") or "")
            if _safe_to_refresh("destination", "destination", "target", current):
                if target != current:
                    destination.update({"target": target,
                                        "status": "unverified" if target else "unconfigured",
                                        "challenge": "", "confirmed_at": ""})
                elif not target:
                    destination["status"] = "unconfigured"
                elif destination.get("status") == "unconfigured":
                    destination["status"] = "unverified"
                decided.add("destination")

        for marker, field, key in (
                ("setting:daily_allowance", "daily_normal_cap", "daily_allowance"),
                ("setting:quiet_start_hour", "quiet_start_hour", "quiet_start_hour"),
                ("setting:quiet_end_hour", "quiet_end_hour", "quiet_end_hour")):
            if f"notification.{field}" not in known:
                continue
            current = state["settings"].get(key)
            if not _safe_to_refresh(marker, "settings", key, current):
                continue
            if key == "daily_allowance":
                # Omission is not a decision (the field is not in ``known``);
                # an explicit present blank clears, and an explicit existing
                # integer is adopted as-is.
                value = notif.get(field)
                state["settings"][key] = value if type(value) is int else None
            else:
                if notif.get(field) is None:
                    continue
                state["settings"][key] = int(notif[field])
            decided.add(marker)

        sources = config.get("sources") or {}
        # Onboarding's public grant is the typed public-feeds switch. The two
        # names are intentionally mapped here instead of being written as an
        # unknown ``sources.public_sources`` key that validation would ignore.
        for grant, flag in (("mail", "mail"), ("calendar", "calendar"),
                            ("tasks", "tasks"), ("public_sources", "public_feeds")):
            if f"sources.{flag}" in known:
                current = bool(state["grants"].get(grant))
                if _safe_to_refresh(f"grant:{grant}", "grants", grant, current):
                    state["grants"][grant] = bool(sources.get(flag))
                decided.add(f"grant:{grant}")

        preparation = config.get("preparation") or {}
        seeded_mode = str(preparation.get("mode") or "").strip().lower()
        if not seeded_mode and "preparation.enabled" in known:
            seeded_mode = ("prepare_private" if preparation.get("enabled")
                           else "review_only")
        if seeded_mode in AUTONOMY_MODES and (
                "preparation.mode" in known or "preparation.enabled" in known):
            current = state["settings"].get("autonomy_mode") or ""
            if _safe_to_refresh("setting:autonomy_mode", "settings", "autonomy_mode", current):
                state["settings"]["autonomy_mode"] = seeded_mode
            decided.add("setting:autonomy_mode")
        if "preparation.scopes" in known:
            current = list(state["settings"].get("preparation_scopes") or [])
            if _safe_to_refresh("setting:preparation_scopes", "settings",
                                "preparation_scopes", current):
                state["settings"]["preparation_scopes"] = list(
                    preparation.get("scopes") or [])
            decided.add("setting:preparation_scopes")
        if "preparation.source_consent" in known:
            current = bool(state["settings"].get("preparation_source_consent"))
            if _safe_to_refresh("setting:preparation_source_consent", "settings",
                                "preparation_source_consent", current):
                state["settings"]["preparation_source_consent"] = bool(
                    preparation.get("source_consent"))
            decided.add("setting:preparation_source_consent")

        scope = state["source_scope"]
        for field, section, key, path in (
                ("mail_accounts", "mail", "accounts", "mail.accounts"),
                ("calendar_accounts", "calendar", "accounts", "calendar.accounts"),
                ("calendars", "calendar", "calendars", "calendar.calendars"),
                ("task_lists", "tasks", "lists", "tasks.lists")):
            if path in known:
                current = list(scope.get(field) or [])
                if _safe_to_refresh(f"scope:{field}", "source_scope", field, current):
                    # Explicit-empty scope is a decision and must stay empty.
                    scope[field] = list((config.get(section) or {}).get(key) or [])
                decided.add(f"scope:{field}")
        if "public_interests" in known:
            current = list(scope.get("public_interests") or [])
            if _safe_to_refresh("scope:public_interests", "source_scope",
                                "public_interests", current):
                scope["public_interests"] = list(config.get("public_interests") or [])
            decided.add("scope:public_interests")

        # Existing native route controls are operator/user choices. Read only
        # this plugin's four declared route keys; absent keys are not guessed.
        try:
            getter = getattr(self.ctx, "get_config", None)
            if callable(getter):
                missing = object()
                for key in ROUTE_SETTING_KEYS:
                    value = getter(key, missing)
                    if value is not missing and isinstance(value, str):
                        current = state["settings"].get(key) or ""
                        if _safe_to_refresh(f"setting:{key}", "settings", key, current):
                            state["settings"][key] = value.strip()
                        decided.add(f"setting:{key}")
            else:
                from .secretary_models import read_route_settings
                for key, value in read_route_settings(self.ctx).items():
                    if value and not state["settings"].get(key):
                        state["settings"][key] = value
                        decided.add(f"setting:{key}")
        except Exception:  # noqa: BLE001 - routes remain unseeded, not guessed
            pass
        state["decided"] = sorted(decided)[-100:]

    def _write(self, state: dict[str, Any]) -> None:
        if self._state_facade is None:
            raise OnboardingError("durable profile state is unavailable")
        state["updated_at"] = _now()
        try:
            self._state_facade.set(STATE_KEY, state)
        except Exception as exc:  # noqa: BLE001
            raise OnboardingError(f"cannot persist onboarding state ({type(exc).__name__})") from exc

    def _effective_config(self) -> dict[str, Any]:
        if self._config_resolver is not None:
            try:
                loaded = self._config_resolver()
            except Exception as exc:  # noqa: BLE001
                return {"available": False, "error": type(exc).__name__}
            return {"available": True, "source": getattr(loaded, "source", "resolver"),
                    "present": bool(getattr(loaded, "present", False)),
                    "summary": loaded.summary() if hasattr(loaded, "summary") else dict(loaded)}
        if self.ctx is None:
            return {"available": False, "error": "no native context"}
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent.parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.userconfig import load_user_config
            loaded = load_user_config(native=self.ctx)
            return {"available": True, "source": loaded.source, "present": loaded.present,
                    "summary": loaded.summary()}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": type(exc).__name__}

    def _capabilities(self) -> dict[str, Any]:
        caps = {
            "slash_command": _has_callable(self.ctx, "register_command"),
            "typed_tool": _has_callable(self.ctx, "register_tool"),
            "durable_state": self._state_facade is not None,
            "pre_llm_context": _has_callable(self.ctx, "register_hook"),
            "gateway_injection": False,
            "destination_live_verification": False,
            "automatic_schedule_arming": False,
        }
        checker = getattr(self.ctx, "_gateway_injection_allowed", None) if self.ctx is not None else None
        if callable(checker):
            try:
                caps["gateway_injection"] = bool(checker())
            except Exception:  # noqa: BLE001
                caps["gateway_injection"] = False
        return caps

    def _model_routes(self) -> dict[str, Any]:
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent.parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.native_runtime import bound_model_lane_status
            bound = bound_model_lane_status(self.ctx)
            if bound is not None:
                return bound
            return effective_route_status(self.ctx, strict_active=False)
        except Exception as exc:  # noqa: BLE001
            return {"strict_active": False, "configured_model_calls_allowed": False,
                    "error": type(exc).__name__}

    def inspect(self) -> dict[str, Any]:
        state = self._read()
        return {
            "ok": True,
            "state": state,
            "next_step": next_step(state),
            "next_question": next_question(state),
            "effective_config": self._effective_config(),
            "model_routes": self._model_routes(),
            "capabilities": self._capabilities(),
            "readback": {"record": STATE_KEY, "status": state["status"],
                         "settings": copy.deepcopy(state["settings"]),
                         "grants": copy.deepcopy(state["grants"]),
                         "destination": copy.deepcopy(state["destination"])},
        }

    def propose_facts(self, facts: Any, *, provenance: str = "foreground_memory") -> dict[str, Any]:
        """Accept bounded foreground-memory facts without inventing a memory API.

        Duplicate known facts are skipped (no double biography). A difference
        from an existing fact is marked as a conflict from the data itself, not
        merely because a caller supplied ``conflict: true``.
        """
        if not isinstance(facts, list):
            raise OnboardingError("proposed facts must be a list")
        state = self._read()
        added = []
        skipped = []
        conflicts = []
        for item in facts:
            if not isinstance(item, Mapping):
                raise OnboardingError("each proposed fact must be an object")
            key, value = validate_fact(item.get("key"), item.get("value"))
            existing = (state.get("facts") or {}).get(key)
            if isinstance(existing, Mapping):
                if str(existing.get("value") or "") == value:
                    skipped.append(key)
                    continue
            duplicate = next((p for p in state["proposals"]
                              if p.get("key") == key and p.get("value") == value
                              and not p.get("accepted")), None)
            if duplicate is not None:
                skipped.append(key)
                continue
            previous_values = {
                str(p.get("value") or "") for p in state["proposals"]
                if p.get("key") == key and not p.get("accepted")
            }
            if isinstance(existing, Mapping):
                previous_values.add(str(existing.get("value") or ""))
            conflict = bool(item.get("conflict", False)) or bool(
                previous_values and value not in previous_values)
            proposal = {
                "key": key, "value": value,
                "provenance": str(item.get("provenance") or provenance)[:80],
                "confidence": str(item.get("confidence") or "unspecified")[:32],
                "freshness": str(item.get("freshness") or "unspecified")[:32],
                "conflict": conflict,
                "conflict_reason": ("existing_fact_differs" if conflict else ""),
                "accepted": False, "created_at": _now(),
            }
            state["proposals"].append(proposal)
            added.append(proposal)
            if conflict:
                conflicts.append(key)
        if state["status"] in ("not_started", "cancelled"):
            state["status"] = "in_progress"
            state["created_at"] = state["created_at"] or _now()
        self._write(state)
        return {"ok": True, "added": added, "skipped_existing": sorted(set(skipped)),
                "conflicts": sorted(set(conflicts)),
                "note": ("Proposals are not facts and grant no access. Conflicting facts "
                         "require explicit foreground resolution."),
                "readback": {"record": STATE_KEY, "proposal_count": len(state["proposals"]),
                             "status": state["status"]}}

    def accept_proposals(self, keys: Any) -> dict[str, Any]:
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise OnboardingError("accepted proposals must be a list of keys")
        state = self._read()
        accepted = []
        for proposal in state["proposals"]:
            if proposal["key"] in keys and not proposal.get("accepted"):
                state["facts"][proposal["key"]] = {
                    "value": proposal["value"], "provenance": proposal["provenance"],
                    "confidence": proposal["confidence"], "freshness": proposal["freshness"],
                    "updated_at": _now(),
                }
                proposal["accepted"] = True
                accepted.append(proposal["key"])
        self._write(state)
        return {"ok": True, "accepted": accepted,
                "readback": {"record": STATE_KEY, "facts": copy.deepcopy(state["facts"])}}

    def update(self, *, settings: Any = None, grants: Any = None, source_scope: Any = None,
               destination: Any = None, explicit_consent: bool = False,
               step: str | None = None) -> dict[str, Any]:
        state = self._read()
        changed: list[str] = []
        decided = set(state.get("decided") or [])
        if settings is not None:
            clean_settings = validate_settings(settings)
            candidate = dict(state["settings"])
            candidate.update(clean_settings)
            _clean_route, route_errors = validate_route_settings(candidate)
            if route_errors:
                raise OnboardingError("; ".join(route_errors))
            mode = candidate.get("autonomy_mode") or ""
            if mode in ("prepare_private", "prepare_and_approve") \
                    and not explicit_consent:
                raise OnboardingError(
                    "enabling private preparation requires explicit_consent=true")
            if any(candidate.get("preparation_scopes") or []) \
                    and not explicit_consent:
                raise OnboardingError(
                    "choosing preparation scopes requires explicit_consent=true")
            state["settings"].update(clean_settings)
            decided.update(f"setting:{key}" for key in clean_settings)
            changed.append("settings")
        if grants is not None:
            clean = validate_grants(grants, explicit_consent=explicit_consent)
            state["grants"].update(clean)
            decided.update(f"grant:{key}" for key in clean)
            changed.append("grants")
        if source_scope is not None:
            if not explicit_consent and any((state["grants"].get(key) for key in SOURCE_GRANT_KEYS)):
                raise OnboardingError("changing source scope requires explicit_consent=true")
            _merge_scope(state, source_scope)
            decided.update(f"scope:{key}" for key in source_scope)
            changed.append("source_scope")
        if destination is not None:
            target = validate_destination(destination)
            state["destination"]["target"] = target
            state["destination"]["status"] = "unverified" if target else "unconfigured"
            state["destination"]["challenge"] = ""
            state["destination"]["confirmed_at"] = ""
            decided.add("destination")
            changed.append("destination")
        state["decided"] = sorted(decided)[-100:]
        if step is not None:
            if step not in STEP_ORDER:
                raise OnboardingError(f"unknown onboarding step {step!r}")
            state["current_step"] = step
            changed.append("current_step")
        if state["status"] in ("not_started", "cancelled"):
            state["status"] = "in_progress"
            state["created_at"] = state["created_at"] or _now()
        state["current_step"] = next_step(state)
        state["history"].append({"at": _now(), "changed": changed})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "changed": changed,
                "readback": {"record": STATE_KEY, "settings": copy.deepcopy(state["settings"]),
                             "grants": copy.deepcopy(state["grants"]),
                             "source_scope": copy.deepcopy(state["source_scope"]),
                             "destination": copy.deepcopy(state["destination"]),
                             "next_step": state["current_step"]}}

    def request_destination_verification(self) -> dict[str, Any]:
        state = self._read()
        target = state["destination"].get("target")
        if not target:
            raise OnboardingError("set a destination before requesting verification")
        challenge = f"verify-{_now().replace(':', '').replace('-', '')[:15]}"
        state["destination"]["status"] = "pending"
        state["destination"]["challenge"] = challenge
        self._write(state)
        return {"ok": True, "status": "pending", "challenge": challenge,
                "note": ("No live delivery receipt is available to this plugin. A foreground "
                         "confirmation is recorded as user_confirmed, never as live-verified."),
                "readback": {"record": STATE_KEY, "destination": copy.deepcopy(state["destination"])}}

    def confirm_destination(self, challenge: Any, *, user_confirmed: bool = False) -> dict[str, Any]:
        state = self._read()
        expected = state["destination"].get("challenge")
        if not user_confirmed:
            raise OnboardingError("destination confirmation requires user_confirmed=true")
        if not expected or challenge != expected:
            raise OnboardingError("destination challenge does not match")
        state["destination"]["status"] = "user_confirmed"
        state["destination"]["confirmed_at"] = _now()
        state["destination"]["challenge"] = ""
        self._write(state)
        return {"ok": True, "status": "user_confirmed",
                "note": "Not a live delivery verification.",
                "readback": {"record": STATE_KEY, "destination": copy.deepcopy(state["destination"])}}

    def preview(self) -> dict[str, Any]:
        state = self._read()
        planned = {
            "timezone": state["settings"].get("timezone") or None,
            "notification.daily_normal_cap": state["settings"].get("daily_allowance"),
            "notification.quiet_start_hour": state["settings"].get("quiet_start_hour"),
            "notification.quiet_end_hour": state["settings"].get("quiet_end_hour"),
            "notification.destination": state["destination"].get("target") or None,
            "sources": {key: bool(state["grants"].get(key)) for key in SOURCE_GRANT_KEYS},
            "source_scope": copy.deepcopy(state["source_scope"]),
            "autonomy": {
                "mode": state["settings"].get("autonomy_mode") or None,
                "label": AUTONOMY_LABELS.get(state["settings"].get("autonomy_mode") or "", ""),
                "preparation_enabled": state["settings"].get("autonomy_mode")
                in ("prepare_private", "prepare_and_approve"),
                "preparation_scopes": list(state["settings"].get("preparation_scopes") or []),
                "preparation_source_consent": bool(
                    state["settings"].get("preparation_source_consent")),
            },
            "model_route": {key: state["settings"].get(key, "") for key in ROUTE_SETTING_KEYS},
        }
        blockers = []
        if not state["settings"].get("timezone"):
            blockers.append("timezone")
        if not state["destination"].get("target"):
            blockers.append("destination")
        mode = state["settings"].get("autonomy_mode") or "review_only"
        if mode in ("prepare_private", "prepare_and_approve") \
                and not state["settings"].get("preparation_scopes"):
            blockers.append("preparation_scopes")
        if "setting:autonomy_mode" not in set(state.get("decided") or []):
            blockers.append("autonomy_mode")
        prep_scopes = {str(item).lower() for item in
                       (state["settings"].get("preparation_scopes") or [])}
        granted = {key for key in SOURCE_GRANT_KEYS
                   if bool(state["grants"].get(key))}
        if mode in ("prepare_private", "prepare_and_approve"):
            # A preparation scope that names a private source is only usable
            # with that explicit source grant and current source consent.
            ungranted = sorted(prep_scopes & {"mail", "calendar", "tasks"} - granted)
            if ungranted:
                blockers.append("preparation_scope_grant: " + ",".join(ungranted))
            if (prep_scopes - {"conversation", "memory"}) and \
                    "setting:preparation_source_consent" not in set(state.get("decided") or []):
                blockers.append("preparation_source_consent")
        destination_status = state["destination"].get("status")
        verification = []
        if destination_status in ("pending", "unconfigured", "unverified", "user_confirmed"):
            verification.append("destination_verification_pending")
        _clean, route_errors = validate_route_settings(state["settings"])
        blockers.extend(f"model_route: {error}" for error in route_errors)
        return {"ok": True, "status": state["status"], "planned_effective_settings": planned,
                "blockers": blockers, "would_apply": not blockers,
                "pending_verification": verification,
                "destination_status": destination_status,
                "live_destination_verified": False,
                "note": ("Preview only: no settings, grants, jobs or sources were changed. "
                         "Destination verification is pending; onboarding never auto-arms jobs "
                         "or claims live delivery."),
                "readback": {"record": STATE_KEY, "planned": planned, "blockers": blockers}}

    def _config_ref(self) -> str:
        """Fingerprint the trusted effective configuration for apply/arm binding."""
        import hashlib
        try:
            loaded = self._loaded_effective()
            if not loaded.present:
                return ""
            raw = json.dumps(loaded.config, ensure_ascii=False, sort_keys=True,
                             default=str)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        except Exception:  # noqa: BLE001 - unverifiable configuration fails closed
            return ""

    @staticmethod
    def _review_fingerprint(state: Mapping[str, Any]) -> str:
        import hashlib
        relevant = {key: state.get(key) for key in (
            "settings", "grants", "source_scope", "destination", "decided")}
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _issue_review(self, state: dict[str, Any], *, kind: str) -> dict[str, Any]:
        import secrets
        review = {
            "ref": "sec-onb-" + secrets.token_hex(6),
            "kind": kind,
            "created_at": _now(),
            "fingerprint": self._review_fingerprint(state),
            "requested_grants": sorted(
                key for key, value in (state.get("grants") or {}).items() if value),
            "session_id": "",
        }
        state["consent_review"] = review
        return review

    def _require_review(self, state: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        """An explicitly supplied opaque review must match the current server state."""
        supplied = str(payload.get("review_ref") or "").strip()
        if not supplied:
            return
        review = state.get("consent_review") if isinstance(
            state.get("consent_review"), dict) else {}
        if (not review or supplied != str(review.get("ref") or "")
                or str(review.get("fingerprint") or "") != self._review_fingerprint(state)):
            raise OnboardingError(
                "the supplied review reference is not the current server-issued review")

    def _current_raw_user_config(self) -> dict[str, Any]:
        if self.ctx is None or not _has_callable(self.ctx, "get_config"):
            raise OnboardingError("native settings are unavailable")
        raw = self.ctx.get_config("user_config")
        if raw is None or raw == "":
            return {}
        if not isinstance(raw, dict):
            raise OnboardingError("native user_config is present but not an object")
        return copy.deepcopy(raw)

    def _loaded_effective(self) -> Any:
        if self._config_resolver is not None:
            try:
                return self._config_resolver()
            except Exception as exc:  # noqa: BLE001
                raise OnboardingError(
                    f"cannot resolve effective configuration ({type(exc).__name__})") from exc
        if self.ctx is None:
            raise OnboardingError("native settings are unavailable")
        import sys
        from pathlib import Path
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot.userconfig import load_user_config
        try:
            return load_user_config(native=self.ctx)
        except Exception as exc:  # noqa: BLE001
            raise OnboardingError(
                f"effective configuration is invalid ({type(exc).__name__})") from exc

    def _apply_settings(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Apply only decided fields through the one canonical config owner.

        The effective validated config (native opaque + flat form + defaults) is
        the base, so an onboarding edit preserves every existing account, alias,
        grant and cap. The merged record is validated once, written to the same
        native ``user_config`` value, then ordinary fields are projected through
        ``project_native_settings``. No cloned precedence exists here.
        """
        import sys
        from pathlib import Path
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot.userconfig import (native_flat_changes,
                                                project_native_settings,
                                                validate_config)

        loaded = self._loaded_effective()
        config = copy.deepcopy(loaded.config)
        settings = state["settings"]
        decided = set(state.get("decided") or [])
        # Record only fields this onboarding write actually chose. This keeps
        # generated-neutral factory values suppressed on later loads while real
        # explicit choices (including UTC/5/0/8) stay known.
        chosen_paths: set[str] = set()
        field_markers = {
            "setting:timezone": "timezone",
            "setting:daily_allowance": "notification.daily_normal_cap",
            "setting:quiet_start_hour": "notification.quiet_start_hour",
            "setting:quiet_end_hour": "notification.quiet_end_hour",
            "setting:autonomy_mode": "preparation.mode",
            "setting:preparation_scopes": "preparation.scopes",
            "setting:preparation_source_consent": "preparation.source_consent",
            "destination": "notification.destination",
            "scope:mail_accounts": "mail.accounts",
            "scope:calendar_accounts": "calendar.accounts",
            "scope:calendars": "calendar.calendars",
            "scope:task_lists": "tasks.lists",
            "scope:public_interests": "public_interests",
        }
        for marker, path in field_markers.items():
            if marker in decided:
                chosen_paths.add(path)
                if marker == "setting:autonomy_mode":
                    # The mode is the authority; enabled is only its legacy
                    # projection, so persist both as chosen fields.
                    chosen_paths.add("preparation.enabled")
        source_flags = {"mail": "mail", "calendar": "calendar",
                        "tasks": "tasks", "public_sources": "public_feeds"}
        for key in SOURCE_GRANT_KEYS:
            if f"grant:{key}" in decided:
                chosen_paths.add(f"sources.{source_flags[key]}")
        if chosen_paths:
            advanced = config.setdefault("advanced", {})
            if isinstance(advanced, dict):
                marker = advanced.get("onboarding_provenance")
                existing_marker = marker if isinstance(marker, dict) else None
                provenance = getattr(loaded, "provenance", None) or {}
                if existing_marker is not None:
                    marker = dict(existing_marker)
                elif provenance.get("mode") == "explicit":
                    marker = {"schema": 1, "neutral_init": False,
                              "known_fields": list(provenance.get("fields") or [])}
                else:
                    from proactive_pilot.userconfig import raw_value
                    marker = {
                        "schema": 1, "neutral_init": True, "known_fields": [],
                        "baseline": {path: copy.deepcopy(raw_value(config, path))
                                     for path in (
                                         "timezone", "notification.destination",
                                         "notification.daily_normal_cap",
                                         "notification.quiet_start_hour",
                                         "notification.quiet_end_hour",
                                         "sources.public_feeds", "sources.obligations",
                                         "sources.mail", "sources.calendar",
                                         "sources.tasks", "mail.accounts",
                                         "calendar.accounts", "calendar.calendars",
                                         "tasks.lists", "public_interests",
                                         "preparation.enabled", "preparation.scopes",
                                         "preparation.source_consent",
                                         "preparation.mode")},
                    }
                known = {str(item) for item in (marker.get("known_fields") or [])
                         if isinstance(item, str)}
                known.update(chosen_paths)
                marker["known_fields"] = sorted(known)[-100:]
                advanced["onboarding_provenance"] = marker
        notification = config.setdefault("notification", {})
        for key, field in (("daily_allowance", "daily_normal_cap"),
                           ("quiet_start_hour", "quiet_start_hour"),
                           ("quiet_end_hour", "quiet_end_hour")):
            if f"setting:{key}" in decided:
                notification[field] = settings[key]
        if "destination" in decided and state["destination"].get("target"):
            notification["destination"] = state["destination"]["target"]
        if "setting:timezone" in decided:
            config["timezone"] = settings["timezone"]
        if "setting:autonomy_mode" in decided:
            prep = config.setdefault("preparation", {})
            if not isinstance(prep, dict):
                raise OnboardingError("native preparation settings are malformed")
            mode = str(settings.get("autonomy_mode") or "review_only")
            if mode not in AUTONOMY_MODES:
                raise OnboardingError("autonomy_mode is not a supported mode")
            prep["mode"] = mode
            prep["enabled"] = mode in ("prepare_private", "prepare_and_approve")
        if "setting:preparation_scopes" in decided:
            prep = config.setdefault("preparation", {})
            if not isinstance(prep, dict):
                raise OnboardingError("native preparation settings are malformed")
            prep["scopes"] = list(settings.get("preparation_scopes") or [])
        if "setting:preparation_source_consent" in decided:
            prep = config.setdefault("preparation", {})
            if not isinstance(prep, dict):
                raise OnboardingError("native preparation settings are malformed")
            prep["source_consent"] = bool(settings.get("preparation_source_consent"))
        sources = config.setdefault("sources", {})
        for key in SOURCE_GRANT_KEYS:
            if f"grant:{key}" in decided:
                sources[source_flags[key]] = bool(state["grants"].get(key))
        scope = state["source_scope"]
        for section, field, decided_key, scope_key in (
                ("mail", "accounts", "scope:mail_accounts", "mail_accounts"),
                ("calendar", "accounts", "scope:calendar_accounts", "calendar_accounts"),
                ("calendar", "calendars", "scope:calendars", "calendars"),
                ("tasks", "lists", "scope:task_lists", "task_lists")):
            if decided_key in decided:
                node = config.setdefault(section, {})
                if not isinstance(node, dict):
                    raise OnboardingError(f"native {section} settings are malformed")
                node[field] = list(scope[scope_key])
        if "scope:public_interests" in decided:
            config["public_interests"] = list(scope["public_interests"])

        validated = validate_config(config)
        persisted = {key: value for key, value in validated.items()
                     if not str(key).startswith("_")}
        writer = self._settings_writer
        if writer is None and not _has_callable(self.ctx, "set_config"):
            raise OnboardingError("native settings writer is unavailable")

        def _same_user_config(value: Any) -> bool:
            if isinstance(value, Mapping):
                return dict(value) == persisted
            if isinstance(value, str) and value.strip():
                try:
                    return json.loads(value) == persisted
                except ValueError:
                    return False
            return False

        getter = getattr(self.ctx, "get_config", None)
        missing = object()
        current_user_config = getter("user_config", missing) if callable(getter) else missing
        user_config_changed = not (current_user_config is not missing
                                   and _same_user_config(current_user_config))
        projection_changes = (native_flat_changes(self.ctx, validated)
                              if _has_callable(self.ctx, "set_config") else {})
        route_writes: dict[str, str] = {}
        if callable(getter) and any(f"setting:{key}" in decided
                                    for key in ROUTE_SETTING_KEYS):
            for key in ROUTE_SETTING_KEYS:
                desired = str(settings.get(key) or "")
                current = getter(key, missing)
                if current is missing and not desired:
                    continue
                if current is not missing and isinstance(current, str) \
                        and current.strip() == desired:
                    continue
                route_writes[key] = desired

        projected: list[str] = []
        route_written: list[str] = []
        backup = None
        if user_config_changed or projection_changes or route_writes:
            try:
                from proactive_pilot import config_backup
                from .. import paths as plugin_paths
                backup = config_backup.create_backup(
                    plugin_paths.data_root(), native=self.ctx,
                    state=self._state_facade, reason="onboarding apply settings")
            except Exception as exc:  # noqa: BLE001 - never write without backup
                raise OnboardingError(
                    "settings backup failed; nothing was changed "
                    f"({type(exc).__name__})") from exc
            if user_config_changed:
                if writer is not None:
                    writer(copy.deepcopy(persisted))
                else:
                    self.ctx.set_config("user_config", persisted)
            if projection_changes:
                projected = project_native_settings(self.ctx, validated)
            for key, value in route_writes.items():
                self.ctx.set_config(key, value)
                route_written.append(key)

        after = {
            "record": (f"plugins.entries.{getattr(self.ctx, 'plugin_id', 'hermes-proactive-secretary')}"
                       ".settings.user_config"),
            "timezone": persisted.get("timezone"),
            "notification.daily_normal_cap": persisted["notification"]["daily_normal_cap"],
            "notification.destination": persisted["notification"].get("destination", ""),
            "mail_accounts": list(persisted["mail"]["accounts"]),
            "calendar_accounts": list(persisted["calendar"]["accounts"]),
            "task_lists": list(persisted["tasks"]["lists"]),
            "sources": copy.deepcopy(persisted.get("sources", {})),
            "preparation": copy.deepcopy(persisted.get("preparation", {})),
            "projected_native_fields": projected,
            "projected_model_fields": route_written,
            "effective_source": loaded.source,
            "backup": backup,
            "changed": bool(user_config_changed or projection_changes or route_writes),
        }
        return after

    def confirm(self, *, user_confirmed: bool = False, apply_settings: bool = False) -> dict[str, Any]:
        if not user_confirmed:
            raise OnboardingError("onboarding confirmation requires user_confirmed=true")
        state = self._read()
        if not state["settings"].get("autonomy_mode"):
            # A legacy/partial interview with no autonomy answer is a safe
            # no-op: review only. This invents no grant and enables no read.
            state["settings"]["autonomy_mode"] = "review_only"
            decided = set(state.get("decided") or [])
            decided.add("setting:autonomy_mode")
            state["decided"] = sorted(decided)[-100:]
            self._write(state)
        blockers = self.preview()["blockers"]
        if blockers:
            raise OnboardingError(f"onboarding is incomplete: {', '.join(blockers)}")
        config_readback = None
        config_ref = ""
        if apply_settings:
            config_readback = self._apply_settings(state)
            config_ref = self._config_ref()
            if not config_ref:
                raise OnboardingError(
                    "settings could not be read back; confirmation was not recorded")
            state["applied"] = {
                "at": _now(),
                "decided": sorted(state.get("decided") or []),
                "settings": copy.deepcopy(state["settings"]),
                "grants": copy.deepcopy(state["grants"]),
                "source_scope": copy.deepcopy(state["source_scope"]),
                "destination": (state.get("destination") or {}).get("target", ""),
                "config_ref": config_ref,
            }
            state["status"] = "pending_verification"
            state["current_step"] = "delivery_verification"
        else:
            # A boolean alone did not persist anything. Keep the draft distinct
            # from an applied/pending-verification setup so arming cannot follow.
            state["applied"] = {}
            state["status"] = "pending_settings"
            state["current_step"] = "delivery_verification"
        state["completed_at"] = _now()
        state["history"].append({"at": _now(), "changed": ["confirmed"],
                                 "applied_settings": bool(apply_settings)})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "status": state["status"],
                "saved": bool(apply_settings),
                "applied_settings": bool(apply_settings),
                "schedule_armed": False, "grants_changed_by_confirmation": False,
                "live_destination_verified": False,
                "note": ("Configuration is saved and status remains pending verification. "
                         "After your explicit confirmation I can run the existing setup "
                         "engine on your behalf to arm only the plugin-owned no-agent jobs; "
                         "no manual command is needed. Delivery stays unverified until an "
                         "explicit native canary."
                         if apply_settings else
                         "Nothing was saved or applied. The draft remains unapplied, so "
                         "jobs were not armed; apply the settings before confirming."),
                "config_readback": config_readback,
                "readback": {"record": STATE_KEY, "status": state["status"],
                             "settings": copy.deepcopy(state["settings"]),
                             "grants": copy.deepcopy(state["grants"]),
                             "destination": copy.deepcopy(state["destination"])}}

    def arm_confirmed(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        """Perform confirmed foreground setup/arming through the existing engine.

        The user asked the secretary to do this on their behalf; no manual CLI
        step is required. This is only reachable after explicit confirmation and
        only arms the same plugin-owned, no-agent jobs the CLI setup uses.
        """
        if not user_confirmed:
            raise OnboardingError("arming requires user_confirmed=true")
        state = self._read()
        if state.get("status") != "pending_verification":
            raise OnboardingError(
                "onboarding settings must be saved and confirmed before arming")
        applied = state.get("applied") if isinstance(state.get("applied"), dict) else {}
        stored_config_ref = str(applied.get("config_ref") or "")
        current_config_ref = self._config_ref()
        if not applied or not stored_config_ref or not current_config_ref:
            raise OnboardingError("arming requires a verified saved configuration")
        if stored_config_ref != current_config_ref:
            raise OnboardingError(
                "the configuration changed after confirmation; review and confirm again")
        if (applied.get("settings") != state.get("settings")
                or applied.get("grants") != state.get("grants")
                or applied.get("source_scope") != state.get("source_scope")
                or str(applied.get("destination") or "")
                != str((state.get("destination") or {}).get("target") or "")):
            raise OnboardingError(
                "the onboarding draft changed after it was applied; save and confirm again")
        target = str((state.get("destination") or {}).get("target") or "").strip()
        if not target or (state.get("destination") or {}).get("status") == "unconfigured":
            raise OnboardingError("a delivery destination is required before arming")
        preparation = state.get("settings") or {}
        args = _ArmArgs(
            deliver=target,
            no_cron=False,
            force=False,
            import_from=None,
            import_dry_run=False,
            fixture="healthy_empty",
            with_preparation=bool(preparation.get("autonomy_mode") in
                                  ("prepare_private", "prepare_and_approve")),
            json=True,
        )
        from . import secretary_cli
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                rc = secretary_cli.setup_command(args)
        except Exception as exc:  # noqa: BLE001 - keep pending, report honestly
            return {"ok": False, "status": state.get("status"),
                    "schedule_armed": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        output = buffer.getvalue().strip()
        try:
            result = json.loads(output)
        except Exception:  # noqa: BLE001 - tolerate a human/plain last line
            try:
                result = json.loads(output.splitlines()[-1])
            except Exception:  # noqa: BLE001
                result = {}
        jobs = result.get("jobs") if isinstance(result, dict) else None
        armed = bool(rc == 0 and isinstance(jobs, list) and jobs)
        if armed:
            state["status"] = "ready"
            state["history"].append({"at": _now(), "changed": ["armed"]})
            state["history"] = state["history"][-50:]
            self._write(state)
        return {"ok": armed, "status": state.get("status"),
                "schedule_armed": armed,
                "jobs": jobs if isinstance(jobs, list) else [],
                "setup_rc": rc,
                "error": ("" if armed else str(result.get("error") or "")
                          if isinstance(result, dict) else ""),
                "note": ("Confirmed setup ran through the existing setup engine; only "
                         "plugin-owned jobs were ensured. No new grants or targets."
                         if armed else
                         "Arming did not complete; status remains pending verification."),
                "readback": {"record": STATE_KEY, "status": state.get("status"),
                             "destination": copy.deepcopy(state.get("destination") or {})}}

    def cancel(self) -> dict[str, Any]:
        state = self._read()
        state["status"] = "cancelled"
        state["current_step"] = next_step(state)
        state["history"].append({"at": _now(), "changed": ["cancelled"]})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "status": "cancelled",
                "note": "Progress is retained and can be resumed with /secretary setup.",
                "readback": {"record": STATE_KEY, "status": state["status"],
                             "settings": copy.deepcopy(state["settings"])}}

    def _model_update_authority(self, payload: Mapping[str, Any],
                                state: Mapping[str, Any]) -> None:
        """Expanded agentic changes need a real human answer, not a model bool."""
        settings = payload.get("settings") if isinstance(payload.get("settings"), Mapping) else {}
        grants = payload.get("grants") if isinstance(payload.get("grants"), Mapping) else {}
        positive_grants = {key: value for key, value in grants.items() if value is True}
        expansion = bool(positive_grants) or any((
            "autonomy_mode" in settings,
            bool(settings.get("preparation_scopes")),
            settings.get("preparation_source_consent") is True,
        ))
        if not expansion:
            return
        review = state.get("consent_review") if isinstance(
            state.get("consent_review"), dict) else {}
        if positive_grants:
            consent.require_grants(self.ctx, payload, positive_grants,
                                   state_review=review)
            return
        consent.positive_turn(
            self.ctx, payload, purpose="expanding preparation/autonomy",
            words=("autonomy", "prepare", "preparation", "private", "scope", "scopes",
                   "consent", "permission", "permissions", "enable", "allow", "mode"))

    def _model_review_authority(self, payload: Mapping[str, Any], state: Mapping[str, Any],
                                *, purpose: str, words: tuple[str, ...],
                                require_review: bool = False) -> None:
        if not bool(payload.get("user_confirmed")):
            raise OnboardingError(f"{purpose} requires user_confirmed=true")
        review = state.get("consent_review") if isinstance(
            state.get("consent_review"), dict) else {}
        if require_review and not review:
            raise OnboardingError(
                f"{purpose} requires a current server-issued review; preview first")
        self._require_review(state, payload)
        consent.positive_turn(self.ctx, payload, purpose=purpose, words=words,
                              reference=str(review.get("ref") or ""))

    def dispatch(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        action = str(action or "inspect").strip().lower()
        if action == "inspect":
            return self.inspect()
        if action in ("propose", "propose_facts"):
            return self.propose_facts(payload.get("facts"), provenance=str(
                payload.get("provenance") or "foreground_memory"))
        if action in ("accept", "accept_proposals"):
            return self.accept_proposals(payload.get("keys"))
        if action == "update":
            state = self._read()
            self._model_update_authority(payload, state)
            result = self.update(settings=payload.get("settings"),
                                 grants=payload.get("grants"),
                                 source_scope=payload.get("source_scope"),
                                 destination=payload.get("destination"),
                                 explicit_consent=bool(payload.get("explicit_consent")),
                                 step=payload.get("step"))
            state = self._read()
            result["review_ref"] = self._issue_review(state, kind="update")["ref"]
            self._write(state)
            return result
        if action == "preview":
            result = self.preview()
            state = self._read()
            result["review_ref"] = self._issue_review(state, kind="review")["ref"]
            self._write(state)
            return result
        if action == "request_verification":
            return self.request_destination_verification()
        if action == "confirm_destination":
            return self.confirm_destination(payload.get("challenge"),
                                            user_confirmed=bool(payload.get("user_confirmed")))
        if action == "confirm":
            state = self._read()
            self._model_review_authority(
                payload, state, purpose="confirming onboarding",
                words=("confirm", "confirmed", "approve", "approved", "proceed",
                       "save", "saved", "apply", "finish", "complete", "setup",
                       "onboarding", "settings", "review", "done"),
                require_review=True)
            result = self.confirm(user_confirmed=True,
                                  apply_settings=bool(payload.get("apply_settings")))
            if result.get("status") == "pending_verification":
                state = self._read()
                result["review_ref"] = self._issue_review(state, kind="arm")["ref"]
                self._write(state)
            return result
        if action == "arm":
            state = self._read()
            self._model_review_authority(
                payload, state, purpose="arming jobs",
                words=("arm", "schedule", "scheduled", "start", "enable", "jobs",
                       "setup", "onboarding", "proceed", "confirm", "go ahead"),
                require_review=True)
            return self.arm_confirmed(user_confirmed=True)
        if action == "cancel":
            return self.cancel()
        raise OnboardingError(f"unknown onboarding action {action!r}")


def _has_callable(obj: Any, name: str) -> bool:
    return obj is not None and callable(getattr(obj, name, None))


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "inspect", "propose", "accept", "update", "preview",
            "request_verification", "confirm_destination", "confirm", "arm", "cancel",
        ]},
        "facts": {"type": "array", "items": {"type": "object"}},
        "keys": {"type": "array", "items": {"type": "string"}},
        "settings": {"type": "object"},
        "grants": {"type": "object"},
        "source_scope": {"type": "object"},
        "destination": {"type": "string"},
        "explicit_consent": {"type": "boolean"},
        "user_confirmed": {"type": "boolean"},
        "apply_settings": {"type": "boolean"},
        "challenge": {"type": "string"},
        "step": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def tool_handler(service: OnboardingService):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        import json
        try:
            return json.dumps(service.dispatch(str(args.get("action") or "inspect"), args),
                              ensure_ascii=False, sort_keys=True)
        except (OnboardingError, consent.ConsentError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"onboarding failed ({type(exc).__name__})"},
                              ensure_ascii=False, sort_keys=True)
    return handler


def is_complete(service: OnboardingService) -> bool:
    return service._read().get("status") == "pending_verification"
