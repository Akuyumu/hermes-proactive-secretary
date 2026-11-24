"""Supported setup actions for the plugin settings page.

This module is the service side of ``desktop/plugin.js`` and
``dashboard/plugin_api.py``. It deliberately uses the SAME validated
configuration owner as onboarding, the native settings form and the scheduled
runners (:mod:`proactive_pilot.userconfig`). There is no second settings store.

Two actions are exposed:

``trigger``
    Non-destructive. Returns the foreground command to place in a chat. It never
    clears, saves or starts an interview, and it does not claim a conversation
    was launched by the host.

``reset``
    A two-step, owner-confirmed reset of interview progress and everyday
    preferences only. Account connections, source definitions, model routes and
    reminders are preserved. Reading permissions are turned OFF (never widened)
    so the next interview must re-confirm them. A private, owner-only backup is
    written before the first write; a failed backup means no writes.

The adapter contract is intentionally small. A foundation-provided backup or
projection helper may replace ``_write_backup``/``_load_effective`` by passing
``backup_writer``/``config_loader`` at construction; the reset semantics and
revision guard stay here. Nothing in this module is exposed to model-facing
onboarding tools.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from proactive_pilot import userconfig  # noqa: E402

PLUGIN_ID = userconfig.NATIVE_PLUGIN_ID
STATE_KEY = "onboarding_state"
CHALLENGE_KEY = "setup_reset_challenge"
CONFIRM_PHRASE = "RESET"
CHALLENGE_TTL_SECONDS = 600
RESET_REASON = "owner_confirmed_reset_and_onboard"

# The exact user-facing scope. Kept bounded and free of private values.
CLEARED_FIELDS = (
    "onboarding interview progress, answers, proposals and history",
    "everyday notification preferences (timezone, delivery target, quiet hours, daily cap, spacing, persona)",
    "public interests and discovery preferences",
    "reading permissions (mail, calendar, tasks, public feeds, obligations, private preparation)",
)
PRESERVED_FIELDS = (
    "account connections and aliases (mail, calendar, tasks)",
    "public source definitions and custom source URLs",
    "model routes",
    "reminders, reminder jobs and other plugin runtime state",
    "source health names, bounds, adapters and routed-retrieval configuration",
)


class SetupActionError(RuntimeError):
    """A supported setup action was refused. ``status_code`` is the HTTP hint."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> Any:
    """JSON-safe canonical form used only for revision hashes."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(child) for key, child in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _digest(value: Any) -> str:
    raw = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in config.items() if not str(key).startswith("_")}


_PATHS_LOCK = threading.RLock()
_PATHS_CACHE: dict[Path, Any] = {}


def _owner_paths() -> Any:
    """Load THIS plugin's root ``paths.py`` by absolute location.

    Importing a bare ``paths`` module (or the non-existent
    ``proactive_pilot.paths``) lets another installed plugin, a runner or a
    test inject a different module with the same name. The root module is the
    owner-bound resolver: it derives every location from
    :mod:`proactive_pilot.ownerpaths`, so the active request profile wins and a
    foreign profile never inherits the launch profile's data override.

    The module is deliberately registered under no global import name; only
    this file's cache refers to it. A copy from another plugin directory can
    therefore never satisfy or poison the lookup.
    """
    target = (_ROOT / "paths.py").resolve()
    if not target.is_file():
        raise RuntimeError("owner path resolver is missing")
    with _PATHS_LOCK:
        cached = _PATHS_CACHE.get(target)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(
            f"_hermes_proactive_secretary_owner_paths_{hashlib.sha256(str(target).encode('utf-8')).hexdigest()[:16]}",
            target,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("owner path resolver cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PATHS_CACHE[target] = module
        return module


def owner_data_root() -> Path:
    """Owner-only data root for the active profile (root resolver semantics)."""
    return Path(_owner_paths().data_root())


def owner_profile_home() -> Path:
    """Active profile home resolved by the host's context-local owner scope."""
    return Path(_owner_paths().hermes_home())


def _default_config_path() -> Path:
    return Path(_owner_paths().user_config_path())


def _default_preferences_path() -> Path:
    return Path(_owner_paths().preferences_path())


def _default_backup_root() -> Path:
    return owner_data_root()


def _read_preferences(path: Path) -> dict[str, Any]:
    from proactive_pilot.feedback import load_preferences_file
    return load_preferences_file(path, strict=True)


def _load_runtime_services(package_name: str) -> dict[str, Any]:
    """Locate the existing bounded status/onboarding projections, if present."""
    services: dict[str, Any] = {}
    if not package_name:
        return services
    try:
        import importlib
        services["status"] = importlib.import_module(f"{package_name}.secretary.secretary_status")
    except Exception as exc:  # noqa: BLE001 - status degrades, never invents
        services["status_error"] = type(exc).__name__
    try:
        import importlib
        services["cli"] = importlib.import_module(f"{package_name}.secretary.secretary_cli")
    except Exception as exc:  # noqa: BLE001
        services["cli_error"] = type(exc).__name__
    try:
        import importlib
        services["onboarding"] = importlib.import_module(f"{package_name}.secretary.secretary_onboarding")
    except Exception as exc:  # noqa: BLE001
        services["onboarding_error"] = type(exc).__name__
    return services


class SetupActions:
    """Profile-scoped setup status, trigger and confirmed reset."""

    def __init__(
        self,
        ctx: Any,
        *,
        package_name: str = "",
        config_path: str | Path | None = None,
        preferences_path: str | Path | None = None,
        backup_root: str | Path | None = None,
        config_loader: Callable[[], Any] | None = None,
        backup_writer: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int = CHALLENGE_TTL_SECONDS,
    ) -> None:
        self.ctx = ctx
        self.package_name = package_name
        self._config_path = Path(config_path) if config_path is not None else _default_config_path()
        self._preferences_path = (Path(preferences_path) if preferences_path is not None
                                  else _default_preferences_path())
        self._backup_root = Path(backup_root) if backup_root is not None else _default_backup_root()
        self._config_loader = config_loader
        self._backup_writer = backup_writer
        self._clock = clock or _now
        self._ttl = max(60, min(int(ttl_seconds), 1800))
        self._services = _load_runtime_services(package_name)

    # -- shared reads ----------------------------------------------------
    @property
    def state(self) -> Any:
        state = getattr(self.ctx, "state", None)
        if state is None:
            raise SetupActionError("durable_state_unavailable",
                                   "Owner-only setup state is unavailable.", status_code=503)
        return state

    def _load_effective(self) -> Any:
        if self._config_loader is not None:
            return self._config_loader()
        return userconfig.load_user_config(self._config_path, native=self.ctx)

    def _state_value(self, key: str) -> Any:
        return self.state.get(key)

    def _preferences(self) -> dict[str, Any]:
        return _read_preferences(self._preferences_path)

    def _revision(self, loaded: Any, onboarding: Any, preferences: Mapping[str, Any]) -> str:
        return _digest({
            "config": _public_config(loaded.config),
            "config_source": getattr(loaded, "source", ""),
            "config_present": bool(getattr(loaded, "present", False)),
            "onboarding": onboarding,
            "preferences": preferences,
        })

    # -- status ----------------------------------------------------------
    @staticmethod
    def _onboarding_label(status: str) -> str:
        return {
            "not_started": "Setup not started",
            "in_progress": "Setup in progress",
            "cancelled": "Setup paused",
            "pending_verification": "Needs delivery check",
            "complete": "Setup complete",
        }.get(status, "Setup status unavailable")

    @staticmethod
    def _source_key(status: str) -> str:
        return {
            "disabled": "off",
            "enabled-unscoped": "unconfigured",
            "configured-unverified": "unverified",
            "configured-stale": "stale",
            "verified-fresh": "fresh",
            "unavailable": "unavailable",
        }.get(status, "unverified")

    def _source_rows(self, config: Mapping[str, Any], truth: Any) -> dict[str, Any]:
        truth = truth if isinstance(truth, Mapping) else {}
        sources = config.get("sources") or {}
        scopes = {
            "mail": (config.get("mail") or {}).get("accounts") or [],
            "calendar": ((config.get("calendar") or {}).get("accounts")
                         or (config.get("calendar") or {}).get("calendars") or []),
            "tasks": ((config.get("tasks") or {}).get("accounts")
                      or (config.get("tasks") or {}).get("lists") or []),
            "public": (config.get("public_sources") or config.get("public_interests") or []),
            "obligations": (config.get("mail") or {}).get("accounts") or [],
        }
        enabled = {
            "mail": bool(sources.get("mail")),
            "calendar": bool(sources.get("calendar")),
            "tasks": bool(sources.get("tasks")),
            "public": bool(sources.get("public_feeds")),
            "obligations": bool(sources.get("obligations")),
        }
        out: dict[str, Any] = {}
        for name in ("mail", "calendar", "tasks", "public", "obligations"):
            raw = truth.get(name)
            scope = scopes.get(name) or []
            if isinstance(raw, Mapping):
                out[name] = {
                    "status": self._source_key(str(raw.get("status") or "")),
                    "available": raw.get("available") if isinstance(raw.get("available"), bool) else None,
                    "enabled": bool(enabled[name]),
                    "authorized": bool(raw.get("authorized", bool(scope))),
                    "verified": bool(raw.get("verified", False)),
                    "fresh": bool(raw.get("fresh", False)),
                    "scope_count": len(scope),
                    "last_success_at": str(raw.get("last_success_at") or "")[:64],
                    "remedy": str(raw.get("remedy") or "")[:180],
                }
                continue
            # No bounded truth available: never invent "connected".
            status = "off" if not enabled[name] else ("unconfigured" if not scope else "unverified")
            out[name] = {
                "status": status,
                "available": None,
                "enabled": bool(enabled[name]),
                "authorized": bool(scope),
                "verified": False,
                "fresh": False,
                "scope_count": len(scope),
                "last_success_at": "",
                "remedy": ("Status could not be read; no successful read is claimed."
                           if status != "off" else
                           "Turned off. Enable it from Agent settings and choose an explicit source."),
            }
        return out

    def _jobs(self) -> dict[str, Any]:
        status_mod = self._services.get("status")
        if status_mod is None:
            return {"available": False, "active_reminders": 0, "owned_jobs": 0,
                    "next_schedule": "", "error": self._services.get("status_error", "unavailable")}
        try:
            projection = status_mod.collect(self.ctx, probe_empty=False)
            jobs = projection.get("jobs") or {}
            return {
                "available": bool(jobs.get("available", False)),
                "active_reminders": int((projection.get("counts") or {}).get("active_reminders") or 0),
                "owned_jobs": int(len(projection.get("owned_jobs") or [])),
                "next_schedule": str(projection.get("next_schedule") or "")[:64],
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "active_reminders": 0, "owned_jobs": 0,
                    "next_schedule": "", "error": type(exc).__name__}

    def _truth(self) -> tuple[Any, str]:
        cli = self._services.get("cli")
        if cli is None:
            return {}, self._services.get("cli_error", "unavailable")
        try:
            return (cli.detect_capabilities() or {}).get("truth") or {}, ""
        except Exception as exc:  # noqa: BLE001
            return {}, type(exc).__name__

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "generated_at": _iso(time.time())}
        try:
            loaded = self._load_effective()
            config = loaded.config
            daily_cap = (config.get("notification") or {}).get("daily_normal_cap")
            result["config"] = {
                "present": bool(loaded.present),
                "source": str(loaded.source),
                "error": "",
                "timezone": str(config.get("timezone") or "")[:64],
                "delivery_configured": bool((config.get("notification") or {}).get("destination")),
                # An unset optional cap is reported honestly as null; an
                # explicit 0 or saved integer is never replaced by a hidden 0.
                "daily_normal_cap": daily_cap if type(daily_cap) is int else None,
                "quiet_start_hour": int((config.get("notification") or {}).get("quiet_start_hour") or 0),
                "quiet_end_hour": int((config.get("notification") or {}).get("quiet_end_hour") or 0),
            }
        except Exception as exc:  # noqa: BLE001 - report only the failure class
            config = userconfig.default_config()
            result["config"] = {"present": False, "source": "unavailable",
                                "error": type(exc).__name__}
        try:
            onboarding = self._state_value(STATE_KEY)
        except Exception as exc:  # noqa: BLE001
            onboarding = None
            result["state_error"] = type(exc).__name__
        onboarding = onboarding if isinstance(onboarding, Mapping) else {}
        board_status = str(onboarding.get("status") or "not_started")[:32]
        next_step = ""
        onboarding_mod = self._services.get("onboarding")
        if onboarding_mod is not None:
            try:
                next_step = str(onboarding_mod.next_step(dict(onboarding)) or "")[:48]
            except Exception:  # noqa: BLE001
                next_step = ""
        result["onboarding"] = {
            "status": board_status,
            "label": self._onboarding_label(board_status),
            "next_step": next_step,
            "complete": board_status in ("complete", "pending_verification"),
        }
        try:
            preferences = self._preferences()
            prefs_ok = True
        except Exception as exc:  # noqa: BLE001
            preferences = {}
            prefs_ok = False
            result["preferences_error"] = type(exc).__name__
        truth, truth_error = self._truth()
        sources = self._source_rows(config, truth)
        result["sources"] = sources
        result["source_status_error"] = truth_error
        result["permissions"] = {
            "reading_enabled": any(row.get("enabled") for row in sources.values()),
            "needs_reconfirmation": not any(row.get("enabled") for row in sources.values()),
            "paused": bool((preferences.get("paused") or {}).get("active")) if prefs_ok else None,
        }
        try:
            # Reuse the existing bounded route projection; the model module is
            # the same owner the runtime uses, never a second route store.
            import importlib
            from proactive_pilot.native_runtime import bound_model_lane_status
            route = dict(bound_model_lane_status(self.ctx) or {})
            if not route and self.package_name:
                models = importlib.import_module(f"{self.package_name}.secretary.secretary_models")
                route = dict(models.effective_route_status(self.ctx, strict_active=False) or {})
            primary = route.get("primary") if isinstance(route.get("primary"), Mapping) else {}
            fallback = route.get("fallback") if isinstance(route.get("fallback"), Mapping) else None
            result["model_routes"] = {
                "strict_active": bool(route.get("strict_active", False)),
                "configured_model_calls_allowed": bool(route.get("configured_model_calls_allowed", False)),
                "primary": {"provider": str(primary.get("provider") or "")[:80],
                            "model": str(primary.get("model") or "")[:80],
                            "source": str(primary.get("source") or "")[:40]},
                "fallback": ({"provider": str(fallback.get("provider") or "")[:80],
                              "model": str(fallback.get("model") or "")[:80]} if fallback else None),
                "blockers": [str(item)[:80] for item in (route.get("blockers") or [])[:5]],
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            result["model_routes"] = {"strict_active": False,
                                      "configured_model_calls_allowed": False,
                                      "primary": {}, "fallback": None,
                                      "blockers": [], "error": type(exc).__name__}
        result["jobs"] = self._jobs()
        result["trigger"] = {"command": "/secretary setup", "non_destructive": True}
        return result

    # -- bounded ordinary settings --------------------------------------
    @staticmethod
    def _settings_revision(loaded: Any) -> str:
        return _digest({"config": _public_config(loaded.config),
                        "source": getattr(loaded, "source", ""),
                        "present": bool(getattr(loaded, "present", False))})

    @staticmethod
    def _set_flat_value(config: dict[str, Any], key: str, value: Any) -> None:
        path = userconfig.NATIVE_FLAT_FIELDS[key]
        node = config
        for segment in path[:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = node[segment] = {}
            node = child
        node[path[-1]] = copy.deepcopy(value)

    @staticmethod
    def _validated_flat_patch(values: Any) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise SetupActionError("invalid_settings", "Settings must be an object.")
        unknown = sorted(set(values) - set(userconfig.NATIVE_FLAT_FIELDS))
        if unknown:
            raise SetupActionError("unknown_setting",
                                   f"Unknown setting: {unknown[0]}.", status_code=400)
        text_keys = {"timezone", "destination", "discovery_mode"}
        bool_keys = {"public_feeds", "obligations", "mail_enabled", "calendar_enabled",
                     "tasks_enabled", "discovery_privacy_review",
                     "discovery_inferred_goals", "preparation_enabled",
                     "preparation_source_consent"}
        int_keys = {"daily_normal_cap", "quiet_start_hour", "quiet_end_hour",
                    "cooldown_minutes", "collector_budget_seconds",
                    "max_candidates_per_kind", "feed_max_sources_per_run"}
        list_keys = {"public_interests", "public_sources", "preparation_scopes"}
        preparation_modes = {"", "review_only", "prepare_private", "prepare_and_approve"}
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key == "preparation_mode":
                if not isinstance(value, str) or value not in preparation_modes:
                    raise SetupActionError(
                        "invalid_setting",
                        "preparation_mode must be blank, review_only, prepare_private or prepare_and_approve.",
                    )
                clean[key] = value
            elif key in text_keys:
                if not isinstance(value, str):
                    raise SetupActionError("invalid_setting", f"{key} must be text.")
                clean[key] = value
            elif key in bool_keys:
                if type(value) is not bool:
                    raise SetupActionError("invalid_setting", f"{key} must be true or false.")
                clean[key] = value
            elif key in int_keys:
                if key == "daily_normal_cap":
                    # The optional cap is the one numeric control with an
                    # unset state. Delegate normalisation (blank -> None,
                    # digit text -> int, explicit 0 kept, bad values refused)
                    # to the canonical configuration validator.
                    try:
                        probe = userconfig.validate_config({
                            "schema": userconfig.CONFIG_SCHEMA,
                            "version": userconfig.CONFIG_VERSION,
                            "notification": {"daily_normal_cap": value},
                        })
                    except userconfig.ConfigError as exc:
                        raise SetupActionError(
                            "invalid_setting",
                            "daily_normal_cap must be a nonnegative whole number or blank.",
                        ) from exc
                    clean[key] = probe["notification"]["daily_normal_cap"]
                    continue
                if type(value) is not int:
                    raise SetupActionError("invalid_setting", f"{key} must be a whole number.")
                clean[key] = value
            elif key in list_keys:
                if not isinstance(value, list) or not all(isinstance(item, (str, Mapping)) for item in value):
                    raise SetupActionError("invalid_setting", f"{key} must be a list.")
                clean[key] = copy.deepcopy(value)
        return clean

    @staticmethod
    def _expanding_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
        expanding = []
        for key in ("mail_enabled", "calendar_enabled", "tasks_enabled", "public_feeds",
                    "obligations", "preparation_enabled", "preparation_source_consent"):
            if not bool(before.get(key)) and bool(after.get(key)):
                expanding.append(key)
        for key in ("public_interests", "public_sources", "preparation_scopes"):
            prior = {_digest(item) for item in (before.get(key) or [])}
            if any(_digest(item) not in prior for item in (after.get(key) or [])):
                expanding.append(key)
        prior_mode = before.get("preparation_mode") or ""
        next_mode = after.get("preparation_mode") or ""
        # Stepping down from prepare_and_approve to prepare_private only removes
        # the extra ask before consequential work; it does not widen permission,
        # so it needs no change confirmation. A legacy blank stays flagged.
        narrowing = prior_mode == "prepare_and_approve" and next_mode == "prepare_private"
        if (prior_mode != next_mode and not narrowing
                and next_mode in ("", "prepare_private", "prepare_and_approve")):
            expanding.append("preparation_mode")
        return expanding

    def settings(self) -> dict[str, Any]:
        loaded = self._load_effective()
        values = userconfig.native_flat_values(loaded.config)
        return {"ok": True, "revision": self._settings_revision(loaded),
                "present": bool(loaded.present), "source": str(loaded.source),
                "values": values}

    @staticmethod
    def _expansion_details(keys: list[str]) -> list[dict[str, str]]:
        labels = {
            "mail_enabled": ("Read configured mail", "Use mail accounts you already connected and scoped."),
            "calendar_enabled": ("Read configured calendars", "Use calendar accounts you already connected and scoped."),
            "tasks_enabled": ("Read configured task lists", "Use task lists you already connected and scoped."),
            "public_feeds": ("Read public sources", "Read only the public source entries already listed; this adds no source."),
            "obligations": ("Check mail for obligations", "Scan already-connected mail accounts for due payments and orders."),
            "preparation_enabled": ("Draft private preparation", "Draft for responsibilities you already accepted."),
            "preparation_source_consent": ("Let preparation use checked facts", "Use fresh facts from sources you already enabled; no new connector."),
            "public_interests": ("Add public topics", "Track the listed public topics; this does not connect an account."),
            "public_sources": ("Add public sources", "Read the listed public sources; this does not connect a private account."),
            "preparation_scopes": ("Add preparation scopes", "Preparation may use the additional scopes listed, only where already authorised."),
            "preparation_mode": ("Change autonomy mode", "Use the newly selected preparation mode; a legacy blank value could permit earlier configuration."),
        }
        return [{"key": key, "label": labels[key][0], "description": labels[key][1]}
                for key in keys if key in labels]

    def _prepared_settings(self, values: Any, revision: Any) -> tuple[Any, dict[str, Any],
                                                                    dict[str, Any], dict[str, Any],
                                                                    dict[str, Any], list[str]]:
        patch = self._validated_flat_patch(values)
        loaded = self._load_effective()
        if not isinstance(revision, str) or not revision:
            raise SetupActionError("revision_required",
                                   "Reload settings before saving so changes are not overwritten.",
                                   status_code=409)
        if not hmac.compare_digest(self._settings_revision(loaded), revision):
            raise SetupActionError("stale_settings",
                                   "Settings changed elsewhere. Reload and review the current values.",
                                   status_code=409)
        before = userconfig.native_flat_values(loaded.config)
        candidate = copy.deepcopy(loaded.config)
        for key, value in patch.items():
            self._set_flat_value(candidate, key, value)
        try:
            validated = userconfig.validate_config(candidate)
        except Exception as exc:  # noqa: BLE001 - keep the validator's message bounded
            raise SetupActionError("invalid_setting", str(exc)[:300], status_code=400) from exc
        after = userconfig.native_flat_values(validated)
        changed: dict[str, Any] = {}
        for key, value in patch.items():
            if key == "daily_normal_cap":
                # Compare the canonical form values: an explicit blank against
                # an already-unset cap is a true no-op, not a write.
                if (type(before.get(key)) is not type(after.get(key))
                        or before.get(key) != after.get(key)):
                    changed[key] = after.get(key)
                continue
            if type(before.get(key)) is not type(value) or before.get(key) != value:
                changed[key] = value
        expanding = self._expanding_keys(before, after) if changed else []
        return loaded, before, after, validated, changed, expanding

    def preview_settings(self, *, values: Any, revision: Any) -> dict[str, Any]:
        """Describe a proposed save without writing anything.

        Permission-widening changes are returned as a prospective list the
        owner must review and acknowledge before :meth:`update_settings` will
        apply them.
        """
        loaded, before, after, _validated, changed, expanding = self._prepared_settings(
            values, revision)
        return {
            "ok": True,
            "changed": bool(changed),
            "changed_fields": sorted(changed),
            "expanding": expanding,
            "expansion_details": self._expansion_details(expanding),
            "requires_confirmation": bool(expanding),
            "revision": self._settings_revision(loaded),
            "values": before,
            "prospective_values": after,
            "explanation": ("These changes widen reading or preparation permission. "
                            "They apply only to the sources and scopes you already connected; "
                            "onboarding will still confirm any remaining scope.") if expanding else "",
        }

    def update_settings(self, *, values: Any, revision: Any,
                        acknowledged_expanding: Any = None) -> dict[str, Any]:
        """Apply a small typed patch through the one validated config owner."""
        loaded, before, _after, validated, changed, expanding = self._prepared_settings(
            values, revision)
        if not changed:
            return {"ok": True, "changed": False, "revision": self._settings_revision(loaded),
                    "values": before, "changed_fields": [], "expanding": []}
        if expanding:
            acknowledged = (sorted({item for item in acknowledged_expanding
                                    if isinstance(item, str)}) if isinstance(acknowledged_expanding, list)
                            else [])
            if acknowledged != sorted(expanding):
                raise SetupActionError(
                    "permission_confirmation_required",
                    "This save widens reading or preparation permission. Review the prospective "
                    "expansion before it is applied; nothing was changed.",
                    status_code=409)

        from proactive_pilot import config_backup
        try:
            backup = config_backup.create_backup(
                self._backup_root, native=self.ctx, state=getattr(self.ctx, "state", None),
                reason="settings page update")
        except Exception as exc:  # noqa: BLE001 - no backup, no write
            raise SetupActionError("backup_failed",
                                   f"Settings backup failed; nothing was changed ({type(exc).__name__}).",
                                   status_code=503) from exc

        persisted = {key: copy.deepcopy(value) for key, value in validated.items()
                     if not str(key).startswith("_")}
        try:
            self.ctx.set_config(userconfig.NATIVE_SETTINGS_KEY, persisted)
            projected = userconfig.project_native_settings(self.ctx, validated)
        except Exception as exc:  # noqa: BLE001
            try:
                self.ctx.set_config(userconfig.NATIVE_SETTINGS_KEY, _public_config(loaded.config))
                userconfig.project_native_settings(self.ctx, loaded.config)
            except Exception:  # noqa: BLE001
                pass
            raise SetupActionError("settings_failed",
                                   "Settings could not be saved; the previous values were restored where possible.",
                                   status_code=500) from exc
        readback = self.settings()
        return {"ok": True, "changed": True, "revision": readback["revision"],
                "values": readback["values"], "changed_fields": sorted(changed),
                "projected_fields": sorted(projected),
                "expanding": expanding,
                "expansion_details": self._expansion_details(expanding),
                "backup_id": Path(backup["path"]).name,
                "explanation": ("These changes widen reading or preparation permission. "
                                "They apply only to the sources and scopes you already connected; "
                                "onboarding will still confirm any remaining scope.") if expanding else ""}

    # -- trigger ---------------------------------------------------------
    def trigger(self) -> dict[str, Any]:
        """Return a foreground continuation; never writes or starts a reset."""
        return {
            "ok": True,
            "changed": False,
            "command": "/secretary setup",
            "note": ("Nothing was cleared or started by the plugin. The desktop action places this "
                     "command in the composer for you to review and send; if it cannot, type it in a chat."),
        }

    # -- reset -----------------------------------------------------------
    def _reset_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        candidate = copy.deepcopy(dict(config))
        defaults = userconfig.default_config()
        candidate["timezone"] = defaults["timezone"]
        candidate["notification"] = copy.deepcopy(defaults["notification"])
        sources = candidate.setdefault("sources", {})
        for key in ("public_feeds", "obligations", "mail", "calendar", "tasks"):
            sources[key] = False
        candidate["public_interests"] = []
        candidate["adaptive"] = copy.deepcopy(defaults["adaptive"])
        candidate["preparation"] = copy.deepcopy(defaults["preparation"])
        advanced = candidate.setdefault("advanced", {})
        if isinstance(advanced, dict):
            # Reset defaults are a new baseline, not fresh user decisions. This
            # keeps the next interview asking while still using the same
            # validated config owner.
            advanced["onboarding_provenance"] = userconfig.neutral_init_marker(candidate)
        return userconfig.validate_config(candidate)

    def _reset_preferences(self, current: Mapping[str, Any]) -> dict[str, Any]:
        """Clear everyday feedback preferences but preserve the pause control.

        Preserving ``paused`` avoids silently resuming collection during a reset;
        clearing it would widen activity, not narrow it.
        """
        from proactive_pilot.feedback import empty_preferences
        fresh = empty_preferences()
        paused = current.get("paused") if isinstance(current.get("paused"), Mapping) else None
        if paused is not None:
            fresh["paused"] = copy.deepcopy(dict(paused))
        return fresh

    def prepare_reset(self) -> dict[str, Any]:
        loaded = self._load_effective()
        onboarding = self._state_value(STATE_KEY)
        preferences = self._preferences()
        issued = time.time()
        expires = issued + self._ttl
        challenge = secrets.token_urlsafe(32)
        record = {
            "schema": 1,
            "digest": _token_digest(challenge),
            "revision": self._revision(loaded, onboarding, preferences),
            "issued_at": _iso(issued),
            "expires_at": _iso(expires),
            "expires_epoch": expires,
            "used": False,
        }
        self.state.set(CHALLENGE_KEY, record)
        return {
            "ok": True,
            "action": "reset_and_onboard",
            "challenge": challenge,
            "expires_at": record["expires_at"],
            "phrase": CONFIRM_PHRASE,
            "explicit_required": True,
            "backup_before_write": True,
            "cleared": list(CLEARED_FIELDS),
            "preserved": list(PRESERVED_FIELDS),
            "warning": ("This clears interview progress and everyday preferences, turns reading "
                        "permissions off and starts setup again. Account connections, source "
                        "definitions, model routes and reminders are kept. A private backup is "
                        "made first, but the reset cannot be undone from this page."),
        }

    def _reset_lock_path(self) -> Path:
        return self._backup_root / "var" / "setup-reset.lock"

    def _validate_challenge(self, challenge: Any, phrase: Any, explicit: Any) -> str:
        if not isinstance(challenge, str) or not challenge:
            raise SetupActionError("challenge_required", "Reset confirmation challenge is missing.")
        if phrase != CONFIRM_PHRASE:
            raise SetupActionError("phrase_mismatch", f"Type {CONFIRM_PHRASE} exactly to confirm.")
        if explicit is not True:
            raise SetupActionError("explicit_confirmation_required",
                                   "Reset requires explicit confirmation.")
        return _token_digest(challenge)

    def _check_challenge(self, digest: str, revision: str) -> Mapping[str, Any]:
        """Validate without consuming, so refusal happens before any backup/write."""
        record = self._state_value(CHALLENGE_KEY)
        if not isinstance(record, Mapping) or record.get("used"):
            raise SetupActionError("challenge_invalid", "Reset confirmation is no longer valid.", status_code=409)
        if not hmac.compare_digest(str(record.get("digest") or ""), digest):
            raise SetupActionError("challenge_invalid", "Reset confirmation does not match.", status_code=409)
        if time.time() > float(record.get("expires_epoch") or 0):
            self.state.set(CHALLENGE_KEY, None)
            raise SetupActionError("challenge_expired", "Reset confirmation expired; prepare again.", status_code=409)
        if not hmac.compare_digest(str(record.get("revision") or ""), revision):
            self.state.set(CHALLENGE_KEY, None)
            raise SetupActionError("stale_confirmation",
                                   "Settings changed after the reset was prepared. Review and prepare again.",
                                   status_code=409)
        return record

    def _consume_challenge(self, digest: str, revision: str) -> None:
        self._check_challenge(digest, revision)
        self.state.set(CHALLENGE_KEY, None)

    def _write_backup(self, *, loaded: Any, onboarding: Any, preferences: Mapping[str, Any]) -> dict[str, Any]:
        if self._backup_writer is not None:
            return self._backup_writer(loaded=loaded, onboarding=onboarding,
                                       preferences=preferences, reason=RESET_REASON)
        root = self._backup_root / "backups"
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        directory = root / f"reset-{stamp}-{secrets.token_hex(4)}"
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        payloads = {
            "config.json": _public_config(loaded.config),
            "onboarding.json": {"present": onboarding is not None, "record": onboarding},
            "preferences.json": copy.deepcopy(dict(preferences)),
        }
        files: dict[str, dict[str, Any]] = {}
        try:
            for name, payload in payloads.items():
                data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                target = directory / name
                fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, target)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            manifest = {
                "schema": 1,
                "reason": RESET_REASON,
                "created_at": _iso(time.time()),
                "config_source": str(getattr(loaded, "source", "")),
                "files": files,
                "note": ("Owner-only backup of exactly this plugin's configuration, onboarding "
                         "state and feedback preferences. It contains no credentials, host "
                         "memories, reminders or other plugins' data."),
            }
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            target = directory / "manifest.json"
            fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception:
            # A partial backup must never be mistaken for a recoverable one.
            for child in directory.glob("*"):
                try:
                    child.unlink()
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        return {"id": directory.name, "path": str(directory),
                "files": files, "manifest_sha256": hashlib.sha256(raw).hexdigest()}

    def _rollback(self, *, original_config: Mapping[str, Any], original_onboarding: Any,
                  original_preferences: Mapping[str, Any], preferences_file_existed: bool) -> bool:
        ok = True
        try:
            persisted = {key: copy.deepcopy(value) for key, value in original_config.items()
                         if not str(key).startswith("_")}
            self.ctx.set_config(userconfig.NATIVE_SETTINGS_KEY, persisted)
            userconfig.project_native_settings(self.ctx, original_config)
        except Exception:  # noqa: BLE001
            ok = False
        try:
            if preferences_file_existed:
                from proactive_pilot.feedback import save_preferences_file
                save_preferences_file(self._preferences_path, dict(original_preferences), _now())
        except Exception:  # noqa: BLE001
            ok = False
        try:
            self.state.set(STATE_KEY, copy.deepcopy(original_onboarding))
        except Exception:  # noqa: BLE001
            ok = False
        return ok

    def commit_reset(self, *, challenge: Any, phrase: Any, explicit: Any) -> dict[str, Any]:
        digest = self._validate_challenge(challenge, phrase, explicit)
        from proactive_pilot.state import file_lock
        lock_path = self._reset_lock_path()
        with file_lock(lock_path) as acquired:
            if not acquired:
                raise SetupActionError("reset_busy", "Another reset is in progress.", status_code=409)
            loaded = self._load_effective()
            onboarding = self._state_value(STATE_KEY)
            preferences = self._preferences()
            revision = self._revision(loaded, onboarding, preferences)
            self._check_challenge(digest, revision)

            original_config = copy.deepcopy(loaded.config)
            reset_config = self._reset_config(loaded.config)
            reset_preferences = self._reset_preferences(preferences)
            preferences_existed = self._preferences_path.exists()

            # Backup failure raises here, before the challenge is consumed and
            # before any settings/state write.
            backup = self._write_backup(loaded=loaded, onboarding=onboarding,
                                        preferences=preferences)
            # Last-moment concurrency check: an edit that landed while the
            # backup was being written invalidates this confirmation.
            latest = self._load_effective()
            if self._revision(latest, self._state_value(STATE_KEY),
                              self._preferences()) != revision:
                self.state.set(CHALLENGE_KEY, None)
                raise SetupActionError("stale_confirmation",
                                       "Settings changed during confirmation. Nothing was reset.",
                                       status_code=409)
            self._consume_challenge(digest, revision)
            persisted = {key: copy.deepcopy(value) for key, value in reset_config.items()
                         if not str(key).startswith("_")}
            projected: list[str] = []
            try:
                self.ctx.set_config(userconfig.NATIVE_SETTINGS_KEY, persisted)
                projected = userconfig.project_native_settings(self.ctx, reset_config)
                from proactive_pilot.feedback import save_preferences_file
                save_preferences_file(self._preferences_path, reset_preferences, _now())
                self.state.set(STATE_KEY, None)
            except Exception as exc:  # noqa: BLE001 - restore before reporting
                rolled_back = self._rollback(
                    original_config=original_config,
                    original_onboarding=onboarding,
                    original_preferences=preferences,
                    preferences_file_existed=preferences_existed,
                )
                raise SetupActionError(
                    "reset_failed",
                    ("Reset could not finish. "
                     + ("The previous settings were restored." if rolled_back
                        else "Backup is available; restore it before retrying.")),
                    status_code=500,
                ) from exc

            return {
                "ok": True,
                "action": "reset_and_onboard",
                "changed": True,
                "backup_id": backup["id"],
                "backup_files": sorted(backup["files"]),
                "cleared": list(CLEARED_FIELDS),
                "preserved": list(PRESERVED_FIELDS),
                "projected_fields": sorted(projected),
                "permissions": {"reading_enabled": False, "needs_reconfirmation": True,
                                "paused": bool((reset_preferences.get("paused") or {}).get("active"))},
                "next": {"command": "/secretary setup",
                         "note": ("Reset complete. Reading permissions are off. Send /secretary setup "
                                  "in a chat to re-confirm them; reminders and model routes were kept.")},
            }


__all__ = [
    "CLEARED_FIELDS",
    "PRESERVED_FIELDS",
    "SetupActionError",
    "SetupActions",
]
