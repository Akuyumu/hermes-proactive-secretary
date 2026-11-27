"""Versioned, validated per-user configuration for the proactive secretary.

This module is the generic base configuration layer. It is deliberately
separate from:

* the shipped policy defaults (:mod:`proactive_pilot.policy`) -- those stay the
  neutral runtime defaults;
* learned preferences (:mod:`proactive_pilot.feedback`) -- mutable, revised by
  feedback, never configuration;
* durable runtime state (``var/``) -- mutable, never configuration.

Design rules:

* The configuration schema is versioned. Unknown, newer or corrupt versions
  fail CLOSED with an actionable remedy; a stale (older) version requires an
  explicit migration and is never rewritten silently.
* Credential VALUES are not configuration. Any credential-shaped key with a
  value is rejected, so no token/secret can be smuggled into the file.
* Nothing here reads the network, a connector, a credential store or a live
  profile. Native plugin settings are preferred through a duck-typed accessor
  (``PluginContext.get_config``) when supplied; otherwise an explicit file path
  is read.

The public API is intentionally small: :func:`default_config`,
:func:`validate_config`, :func:`load_user_config`, :func:`migrate_legacy`,
:func:`policy_overrides`, :func:`apply_user_config`.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_SCHEMA = 1
CONFIG_VERSION = 1
DEFAULT_TIMEZONE = "UTC"
# Optional ordinary-message ceiling. ``None`` is the canonical "unset" value:
# no deterministic daily-count ceiling is applied. It is never a hidden target
# or a silent five-message quota.
DEFAULT_DAILY_NORMAL_CAP: int | None = None
NATIVE_PLUGIN_ID = "hermes-proactive-secretary"
NATIVE_SETTINGS_KEY = "user_config"


class ConfigError(ValueError):
    """The user configuration is missing, corrupt, unknown or unsafe."""

    def __init__(self, message: str, *, remedy: str = "") -> None:
        self.remedy = remedy
        text = message if not remedy else f"{message} ({remedy})"
        super().__init__(text)


# Keys that must never carry a credential value in configuration. Matching is
# on the leaf key name, case-insensitively, at any depth. Credential POINTERS
# (e.g. an env var name) are also refused: this file is not a credential store.
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "client_secret", "clientsecret",
    "token", "access_token", "refresh_token", "id_token", "bearer",
    "password", "passwd", "pass", "credential", "credentials", "private_key",
    "authorization", "auth", "oauth", "cookie", "session_key",
})

# Destination is passed as a fixed ``--deliver`` argv token, so it is bounded to
# characters that cannot become a shell/argv injection.
_SAFE_DELIVER = re.compile(r"^[A-Za-z0-9 _:@./+,-]{1,128}$")
_SAFE_ACCOUNT = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
_SAFE_CALENDAR = re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$")
# Human-facing labels (task list names, public interests): bounded, no control
# characters, but spaces and ordinary punctuation are allowed.
_SAFE_LABEL = re.compile(r"^[^\n\r\t\x00]{1,160}$")
_SAFE_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*$")
# MCP server/tool names are identifiers, never an endpoint URL or a credential.
_SAFE_MCP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_SECTIONS = (
    "schema", "version", "timezone", "context_refresh", "notification",
    "sources", "mail", "calendar", "tasks", "public_interests",
    "public_sources", "adaptive", "bounds", "adapters", "retrieval", "health",
    "preparation", "advanced",
)

# Small documented standing-approval vocabulary for private preparation. Broad
# "public" consent never implies mail/calendar/tasks/documents/memory.
PREPARATION_SCOPES = frozenset({
    "conversation", "public", "mail", "calendar", "tasks", "documents", "memory",
})

# Health critical sources are named source ids like ``mail:formal`` or
# ``calendar:primary``. They are names only, never a credential.
_SAFE_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def default_config() -> dict[str, Any]:
    """A neutral, generic configuration: no owner identifiers, no accounts."""
    return {
        "schema": CONFIG_SCHEMA,
        "version": CONFIG_VERSION,
        "timezone": DEFAULT_TIMEZONE,
        "context_refresh": False,
        "notification": {
            "persona": "Hermes",
            "notify_address": "the user",
            "destination": "",
            "quiet_start_hour": 0,
            "quiet_end_hour": 8,
            "daily_normal_cap": DEFAULT_DAILY_NORMAL_CAP,
            "cooldown_minutes": 30,
        },
        "sources": {
            "public_feeds": False,
            "obligations": False,
            "mail": False,
            "calendar": False,
            "tasks": False,
        },
        "mail": {"accounts": [], "aliases": {}},
        "calendar": {"accounts": [], "calendars": []},
        "tasks": {"accounts": [], "lists": [], "adapter": ""},
        "public_interests": [],
        "public_sources": [],
        # Internal marker: neutral generic defaults deliberately suppress any
        # inherited seed feeds. Never persisted (leading underscore).
        "_explicit_public_sources": True,
        # Internal marker: a generated default never explicitly chose a daily
        # ceiling, so an explicit legacy value may still be preserved.
        "_explicit_daily_cap": False,
        "adaptive": {
            "mode": "disabled",
            "privacy_review": False,
            "inferred_goals": False,
        },
        # Per-source health: NEUTRAL by default. No source is critical until the
        # user explicitly names it here; mail/calendar are never implicit.
        "health": {"critical_sources": []},
        # Private preparation is opt-in and OFF by default. Enabling it does
        # not arm a job or grant any source scope.
        "preparation": {"enabled": False, "scopes": [], "source_consent": False,
                        "mode": ""},
        "bounds": {
            "collector_budget_seconds": 60,
            "max_candidates_per_kind": 60,
            "feed_max_sources_per_run": 2,
        },
        # Optional routed retrieval. Neutral/off by default; the server and tool
        # are NAMED here (never an endpoint or a credential). The native Hermes
        # OAuth connection and the operator's MCP allowlist stay host-owned.
        "retrieval": {
            "firecrawl_enabled": False,
            "firecrawl_consent": False,
            "server": "",
            "tool": "",
            "max_attempts_per_cycle": 1,
            "max_attempts_per_day": 10,
            "concurrency": 1,
            "timeout_seconds": 30,
            "cooldown_minutes": 60,
            "cache_ttl_minutes": 60,
        },
        "adapters": {
            "productivity_secretary": False,
            "gws_calendar": False,
        },
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping, got {type(value).__name__}",
                          remedy=f"fix the {name!r} section or delete user_config.json")
    return value


def _scan_credentials(value: Any, path: str = "") -> str | None:
    """Return the dotted path of the first credential-shaped key with a value."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.strip().lower() in _CREDENTIAL_KEYS and child not in (None, "", {}, []):
                return key_path
            found = _scan_credentials(child, key_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _scan_credentials(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _validate_str_list(raw: Any, name: str, pattern: re.Pattern[str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{name} must be a list", remedy=f"use a JSON array for {name!r}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} entries must be non-empty strings")
        text = item.strip()
        if not pattern.fullmatch(text):
            raise ConfigError(f"{name} entry {text!r} is not a safe explicit name")
        if text not in out:
            out.append(text)
    return out


def _bounded_int(raw: Any, name: str, low: int, high: int, default: int) -> int:
    if raw is None:
        return default
    if not _is_int(raw):
        raise ConfigError(f"{name} must be an integer")
    if not (low <= raw <= high):
        raise ConfigError(f"{name} must be between {low} and {high}, got {raw}")
    return raw


def _nonnegative_int(raw: Any, name: str, default: int) -> int:
    """Any nonnegative integer. There is no product ceiling/target of five."""
    if raw is None:
        return default
    if not _is_int(raw) or raw < 0:
        raise ConfigError(f"{name} must be a nonnegative integer")
    return raw


def _optional_nonnegative_int(raw: Any, name: str) -> int | None:
    """Normalise an OPTIONAL nonnegative integer limit.

    ``None`` and a blank/whitespace-only string mean the limit is unset (here:
    no deterministic daily-count ceiling; it is never a hidden default and never
    a target). Explicit integers -- including ``0`` -- are preserved with their
    existing semantics. Booleans, negatives, fractions and other non-numeric
    values are rejected rather than guessed or coerced.

    Plain digit strings are accepted because the native form field has no
    nullable number type: the owner types an optional number as text and leaves
    it blank to clear it. Existing saved integers therefore keep working.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ConfigError(
            f"{name} must be a nonnegative integer or blank (not true/false)")
    if _is_int(raw):
        if raw < 0:
            raise ConfigError(
                f"{name} must be a nonnegative integer or blank, got {raw}")
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ConfigError(
                f"{name} must be a nonnegative integer or blank, got {text!r}")
        try:
            return int(text)
        except ValueError:
            # Some Unicode characters (e.g. superscript digits) satisfy
            # str.isdigit() but are not accepted by int(); fail closed.
            raise ConfigError(
                f"{name} must be a nonnegative integer or blank, got {text!r}"
            ) from None
    raise ConfigError(
        f"{name} must be a nonnegative integer or blank, "
        f"got {type(raw).__name__}")


def _bounded_bool(raw: Any, name: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{name} must be true or false")
    return raw


def validate_config(raw: Any) -> dict[str, Any]:
    """Validate and normalise a user configuration, failing closed.

    Unknown top-level keys are reported as warnings on the returned config's
    ``_warnings`` list rather than silently accepted, but the version/schema and
    every known field are enforced strictly.
    """
    if raw is None:
        return default_config()
    if not isinstance(raw, dict):
        raise ConfigError("user configuration must be a JSON object",
                          remedy="rewrite user_config.json as a JSON object")

    found = _scan_credentials(raw)
    if found:
        raise ConfigError(
            f"credential-shaped key {found!r} is not allowed in configuration",
            remedy="remove it and use the native Hermes credential/setup flow",
        )

    schema = raw.get("schema", CONFIG_SCHEMA)
    if not _is_int(schema) or schema != CONFIG_SCHEMA:
        raise ConfigError(
            f"unsupported config schema {schema!r} (expected {CONFIG_SCHEMA})",
            remedy="run `hermes secretary configure migrate` or fix the schema field",
        )
    version = raw.get("version", CONFIG_VERSION)
    if not _is_int(version) or version < 0:
        raise ConfigError(
            f"corrupt config version {version!r}",
            remedy="run `hermes secretary configure migrate --from-policy <policy.json>`",
        )
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"config version {version} is newer than this plugin supports ({CONFIG_VERSION})",
            remedy="update the plugin; refusing to guess a future schema",
        )

    config = default_config()
    warnings: list[str] = []
    for key in raw:
        if key not in _SECTIONS and not str(key).startswith("_"):
            warnings.append(f"ignoring unknown config key {key!r}")

    timezone = raw.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str) or not _SAFE_TIMEZONE.fullmatch(timezone.strip()):
            raise ConfigError(
                f"timezone {timezone!r} is not a valid IANA timezone name",
                remedy="use e.g. \"UTC\" or another known IANA name, or omit it for the neutral default",
            )
        try:
            ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"timezone {timezone!r} is not a known IANA timezone",
                remedy="use e.g. \"UTC\" or another known IANA name",
            ) from exc
        config["timezone"] = timezone.strip()
    config["context_refresh"] = _bounded_bool(
        raw.get("context_refresh"), "context_refresh", False)

    notif = _require_mapping(raw.get("notification") or {}, "notification")
    base_notif = config["notification"]
    if "persona" in notif:
        persona = notif["persona"]
        if not isinstance(persona, str) or not persona.strip():
            raise ConfigError("notification.persona must be a non-empty string")
        base_notif["persona"] = persona.strip()[:64]
    if "notify_address" in notif:
        address = notif["notify_address"]
        if not isinstance(address, str) or not address.strip():
            raise ConfigError("notification.notify_address must be a non-empty string")
        base_notif["notify_address"] = address.strip()[:64]
    if "destination" in notif:
        dest = notif["destination"]
        if dest is None:
            dest = ""
        if not isinstance(dest, str) or (dest and not _SAFE_DELIVER.fullmatch(dest.strip())):
            raise ConfigError(
                f"notification.destination {dest!r} is not a safe deliver target",
                remedy="use \"local\", a chat target, or leave it empty",
            )
        base_notif["destination"] = dest.strip()
    base_notif["quiet_start_hour"] = _bounded_int(
        notif.get("quiet_start_hour"), "notification.quiet_start_hour", 0, 23,
        base_notif["quiet_start_hour"])
    base_notif["quiet_end_hour"] = _bounded_int(
        notif.get("quiet_end_hour"), "notification.quiet_end_hour", 0, 23,
        base_notif["quiet_end_hour"])
    base_notif["daily_normal_cap"] = _optional_nonnegative_int(
        notif.get("daily_normal_cap"), "notification.daily_normal_cap")
    # Presence (including an explicit null/blank) is different from omission: a
    # partial edit that omits the field must preserve the owner's saved value,
    # while an explicit blank clears it.
    config["_explicit_daily_cap"] = _field_present(
        raw, "notification.daily_normal_cap")
    base_notif["cooldown_minutes"] = _bounded_int(
        notif.get("cooldown_minutes"), "notification.cooldown_minutes", 0, 1440,
        base_notif["cooldown_minutes"])

    sources = _require_mapping(raw.get("sources") or {}, "sources")
    for key in ("public_feeds", "obligations", "mail", "calendar", "tasks"):
        config["sources"][key] = _bounded_bool(sources.get(key), f"sources.{key}",
                                               config["sources"][key])

    mail = _require_mapping(raw.get("mail") or {}, "mail")
    config["mail"]["accounts"] = _validate_str_list(
        mail.get("accounts"), "mail.accounts", _SAFE_ACCOUNT)
    aliases = _require_mapping(mail.get("aliases") or {}, "mail.aliases")
    clean_aliases: dict[str, str] = {}
    for key, value in aliases.items():
        if not isinstance(key, str) or not _SAFE_ACCOUNT.fullmatch(key.strip()):
            raise ConfigError(f"mail.aliases key {key!r} is not a safe account name")
        if not isinstance(value, str) or not _SAFE_ACCOUNT.fullmatch(value.strip()):
            raise ConfigError(f"mail.aliases[{key!r}] is not a safe account name")
        clean_aliases[key.strip()] = value.strip()
    config["mail"]["aliases"] = clean_aliases

    calendar = _require_mapping(raw.get("calendar") or {}, "calendar")
    config["calendar"]["accounts"] = _validate_str_list(
        calendar.get("accounts"), "calendar.accounts", _SAFE_ACCOUNT)
    config["calendar"]["calendars"] = _validate_str_list(
        calendar.get("calendars"), "calendar.calendars", _SAFE_CALENDAR)

    tasks = _require_mapping(raw.get("tasks") or {}, "tasks")
    config["tasks"]["accounts"] = _validate_str_list(
        tasks.get("accounts", calendar.get("accounts")), "tasks.accounts", _SAFE_ACCOUNT)
    if "accounts" not in tasks and calendar.get("accounts"):
        warnings.append("legacy tasks.accounts omitted: copied explicit calendar accounts; "
                        "set tasks.accounts explicitly to separate the scopes")
    config["tasks"]["lists"] = _validate_str_list(
        tasks.get("lists"), "tasks.lists", _SAFE_LABEL)
    adapter = tasks.get("adapter", "")
    if adapter is None:
        adapter = ""
    if not isinstance(adapter, str) or (adapter and not _SAFE_ACCOUNT.fullmatch(adapter.strip())):
        raise ConfigError("tasks.adapter must be empty or a safe adapter name")
    config["tasks"]["adapter"] = adapter.strip()
    if config["tasks"]["adapter"] not in {"", "tasks", "productivity_secretary"}:
        raise ConfigError("unsupported tasks.adapter; use empty, tasks or productivity_secretary")

    config["public_interests"] = _validate_str_list(
        raw.get("public_interests"), "public_interests", _SAFE_LABEL)
    config["public_sources"] = _validate_public_sources(raw.get("public_sources"))
    # An explicit empty list is authoritative and must clear inherited seed
    # feeds. An omitted list is also neutral for the typed configuration layer
    # unless a reported legacy passthrough explicitly retained seeds.
    legacy = (raw.get("advanced") or {}).get("legacy_policy") if isinstance(raw.get("advanced"), dict) else None
    config["_explicit_public_sources"] = bool(
        "public_sources" in raw
        or not (isinstance(legacy, dict) and "feed_seed_sources" in legacy))

    adaptive = _require_mapping(raw.get("adaptive") or {}, "adaptive")
    mode = adaptive.get("mode", "disabled")
    if mode not in {"disabled", "shadow", "active"}:
        raise ConfigError("adaptive.mode must be disabled, shadow or active")
    config["adaptive"]["mode"] = mode
    config["adaptive"]["privacy_review"] = _bounded_bool(
        adaptive.get("privacy_review"), "adaptive.privacy_review", False)
    config["adaptive"]["inferred_goals"] = _bounded_bool(
        adaptive.get("inferred_goals"), "adaptive.inferred_goals", False)
    if config["adaptive"]["inferred_goals"] and not config["adaptive"]["privacy_review"]:
        warnings.append(
            "adaptive.inferred_goals is enabled without adaptive.privacy_review; "
            "the structural privacy boundary still runs but the optional semantic "
            "review stays off")

    health = _require_mapping(raw.get("health") or {}, "health")
    config["health"]["critical_sources"] = _validate_str_list(
        health.get("critical_sources"), "health.critical_sources", _SAFE_SOURCE_NAME)

    bounds = _require_mapping(raw.get("bounds") or {}, "bounds")
    config["bounds"]["collector_budget_seconds"] = _bounded_int(
        bounds.get("collector_budget_seconds"), "bounds.collector_budget_seconds",
        5, 600, config["bounds"]["collector_budget_seconds"])
    config["bounds"]["max_candidates_per_kind"] = _bounded_int(
        bounds.get("max_candidates_per_kind"), "bounds.max_candidates_per_kind",
        1, 500, config["bounds"]["max_candidates_per_kind"])
    config["bounds"]["feed_max_sources_per_run"] = _bounded_int(
        bounds.get("feed_max_sources_per_run"), "bounds.feed_max_sources_per_run",
        0, 20, config["bounds"]["feed_max_sources_per_run"])

    adapters = _require_mapping(raw.get("adapters") or {}, "adapters")
    config["adapters"]["productivity_secretary"] = _bounded_bool(
        adapters.get("productivity_secretary"), "adapters.productivity_secretary",
        config["adapters"]["productivity_secretary"])
    config["adapters"]["gws_calendar"] = _bounded_bool(
        adapters.get("gws_calendar"), "adapters.gws_calendar",
        config["adapters"]["gws_calendar"])

    # Optional routed retrieval (native MCP Firecrawl). Off/neutral by default;
    # the server/tool are names only, never an endpoint or a credential value.
    retrieval = _require_mapping(raw.get("retrieval") or {}, "retrieval")
    for key in ("firecrawl_enabled", "firecrawl_consent"):
        config["retrieval"][key] = _bounded_bool(
            retrieval.get(key), f"retrieval.{key}", config["retrieval"][key])
    for key in ("server", "tool"):
        value = retrieval.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str) or (value and not _SAFE_MCP_NAME.fullmatch(value.strip())):
            raise ConfigError(
                f"retrieval.{key} must be empty or a safe MCP name",
                remedy="name an already-configured MCP server/tool, never a URL or a key",
            )
        config["retrieval"][key] = value.strip()
    for key, low, high in (
            ("max_attempts_per_cycle", 0, 100),
            ("max_attempts_per_day", 0, 1000),
            ("concurrency", 1, 8),
            ("timeout_seconds", 1, 600),
            ("cooldown_minutes", 0, 10080),
            ("cache_ttl_minutes", 0, 10080)):
        config["retrieval"][key] = _bounded_int(
            retrieval.get(key), f"retrieval.{key}", low, high, config["retrieval"][key])
    if config["retrieval"]["firecrawl_enabled"] and not config["retrieval"]["firecrawl_consent"]:
        warnings.append(
            "retrieval.firecrawl_enabled is on without retrieval.firecrawl_consent; "
            "the route stays unavailable until explicit consent is set")

    preparation = _require_mapping(raw.get("preparation") or {}, "preparation")
    config["preparation"]["enabled"] = _bounded_bool(
        preparation.get("enabled"), "preparation.enabled", False)
    scopes = preparation.get("scopes", [])
    if scopes is None:
        scopes = []
    if not isinstance(scopes, list) or any(
            not isinstance(item, str) or item.strip().lower() not in PREPARATION_SCOPES
            for item in scopes):
        raise ConfigError(
            "preparation.scopes must list supported scope kinds",
            remedy="use conversation/public/mail/calendar/tasks/documents/memory")
    config["preparation"]["source_consent"] = _bounded_bool(
        preparation.get("source_consent"), "preparation.source_consent", False)
    mode = preparation.get("mode", "")
    if mode is None:
        mode = ""
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "", "review_only", "prepare_private", "prepare_and_approve"):
        raise ConfigError(
            "preparation.mode must be empty, review_only, prepare_private or "
            "prepare_and_approve",
            remedy="choose an autonomy mode in setup; an invalid mode never grants preparation")
    config["preparation"]["mode"] = mode.strip().lower()
    config["preparation"]["scopes"] = list(dict.fromkeys(
        item.strip().lower() for item in scopes))

    # Explicit, reported compatibility passthrough of the remaining effective
    # legacy policy fields (migration parity). This is the ONLY place arbitrary
    # legacy policy scalars are retained; unknown keys are never silently dropped
    # because the caller can inspect this mapping and the migration report.
    advanced = _require_mapping(raw.get("advanced") or {}, "advanced")
    legacy_policy = advanced.get("legacy_policy")
    if legacy_policy is None:
        legacy_policy = {}
    legacy_policy = _require_mapping(legacy_policy, "advanced.legacy_policy")
    config["advanced"] = {"legacy_policy": copy.deepcopy(dict(legacy_policy))}
    provenance_marker = advanced.get("onboarding_provenance")
    if isinstance(provenance_marker, dict):
        marker: dict[str, Any] = {"schema": 1}
        marker["neutral_init"] = bool(provenance_marker.get("neutral_init"))
        marker["known_fields"] = [
            str(item) for item in (provenance_marker.get("known_fields") or [])
            if isinstance(item, str) and item in SEEDED_PATHS
        ]
        baseline = provenance_marker.get("baseline")
        if isinstance(baseline, dict):
            marker["baseline"] = {
                path: copy.deepcopy(baseline[path]) for path in SEEDED_PATHS
                if path in baseline
            }
        config["advanced"]["onboarding_provenance"] = marker
    discovery = advanced.get("legacy_account_discovery", [])
    if not isinstance(discovery, list) or any(
            kind not in {"calendar", "tasks"} for kind in discovery):
        raise ConfigError("advanced.legacy_account_discovery must list calendar/tasks only")
    if discovery:
        config["advanced"]["legacy_account_discovery"] = list(dict.fromkeys(discovery))

    config["version"] = version
    config["_warnings"] = warnings
    config["_explicit_version"] = version
    return config


# Canonical dotted paths that onboarding may treat as user-decided. Presence is
# evaluated on the RAW payload before validation fills neutral factory values.
SEEDED_PATHS = (
    "timezone",
    "notification.destination",
    "notification.daily_normal_cap",
    "notification.quiet_start_hour",
    "notification.quiet_end_hour",
    "sources.public_feeds",
    "sources.obligations",
    "sources.mail",
    "sources.calendar",
    "sources.tasks",
    "mail.accounts",
    "calendar.accounts",
    "calendar.calendars",
    "tasks.lists",
    "public_interests",
    "preparation.enabled",
    "preparation.scopes",
    "preparation.source_consent",
    "preparation.mode",
)

# Values used by the shipped neutral example. A full payload equal to these is
# generated/neutral, not evidence that a user chose UTC/5/0/8.
_NEUTRAL_EXAMPLE_VALUES = {
    "timezone": "UTC",
    "notification.destination": "local",
    "notification.daily_normal_cap": None,
    "notification.quiet_start_hour": 0,
    "notification.quiet_end_hour": 8,
    "sources.public_feeds": False,
    "sources.obligations": False,
    "sources.mail": False,
    "sources.calendar": False,
    "sources.tasks": False,
    "mail.accounts": [],
    "calendar.accounts": [],
    "calendar.calendars": [],
    "tasks.lists": [],
    "public_interests": [],
    "preparation.enabled": False,
    "preparation.scopes": [],
    "preparation.source_consent": False,
    "preparation.mode": "",
}

_FULL_NEUTRAL_KEYS = frozenset({
    "schema", "version", "timezone", "notification", "sources", "mail",
    "calendar", "tasks", "public_interests", "public_sources", "adaptive",
    "bounds", "adapters",
})


def raw_value(raw: Any, path: str) -> Any:
    node = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _field_present(raw: Any, path: str) -> bool:
    node = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def onboarding_provenance(raw: Any) -> dict[str, Any]:
    """Describe which raw settings were explicitly present/known.

    The marker written by neutral ``setup``/``configure init`` records a
    baseline, so factory defaults are never mistaken for user decisions. User
    edits made after that baseline (different value) are recognised, and an
    onboarding write records only its actually-chosen fields in
    ``known_fields``.
    """
    if not isinstance(raw, dict):
        return {"mode": "defaults", "fields": [], "present": []}
    present = [path for path in SEEDED_PATHS if _field_present(raw, path)]
    advanced = raw.get("advanced")
    marker = advanced.get("onboarding_provenance") if isinstance(advanced, dict) else None
    marker = marker if isinstance(marker, dict) else {}
    known = {str(item) for item in (marker.get("known_fields") or [])
             if isinstance(item, str) and item in SEEDED_PATHS}
    neutral_init = bool(marker.get("neutral_init"))
    baseline = marker.get("baseline") if isinstance(marker.get("baseline"), dict) else {}
    neutral_defaults = default_config()
    fits_factory = (
        set(_FULL_NEUTRAL_KEYS) <= set(raw)
        and all(raw_value(raw, path) in (
            value, raw_value(neutral_defaults, path))
            for path, value in _NEUTRAL_EXAMPLE_VALUES.items()))
    if neutral_init:
        fields = set(known)
        for path in present:
            reference = baseline.get(path, _NEUTRAL_EXAMPLE_VALUES.get(path))
            if path in known or raw_value(raw, path) != reference:
                fields.add(path)
        mode = "neutral_init"
    elif fits_factory and not known:
        fields = set()
        mode = "factory_neutral"
    else:
        fields = set(present) | known
        mode = "explicit"
    return {"mode": mode, "fields": sorted(fields), "present": present}


def neutral_init_marker(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the persisted provenance marker for a generated neutral config."""
    return {
        "schema": 1,
        "neutral_init": True,
        "known_fields": [],
        "baseline": {path: copy.deepcopy(raw_value(config, path))
                     for path in SEEDED_PATHS},
    }


def _validate_public_sources(raw: Any) -> list[dict[str, Any]]:
    """Public source specs are structural only; credentials are rejected above."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("public_sources must be a list")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("public_sources entries must be objects")
        entry: dict[str, Any] = {}
        for key in ("kind", "url", "provenance", "interest"):
            if key in item:
                if not isinstance(item[key], str):
                    raise ConfigError(f"public_sources[].{key} must be a string")
                entry[key] = item[key][:500]
        if "cadence_minutes" in item:
            entry["cadence_minutes"] = _bounded_int(
                item["cadence_minutes"], "public_sources[].cadence_minutes", 1, 10080, 60)
        if not entry.get("kind") or not entry.get("url"):
            raise ConfigError("public_sources entries require 'kind' and 'url'")
        out.append(entry)
    return out


def policy_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Map a validated user config onto explicit policy keys.

    Only the keys the configuration layer owns are returned; nothing about the
    evaluator, cron, credentials or native provider/search config is duplicated.
    """
    notif = config["notification"]
    # Start from the explicit, reported compatibility passthrough so EVERY
    # retained effective legacy policy field survives; the typed section mapping
    # below then takes precedence over it for the keys the config layer owns.
    overrides: dict[str, Any] = {}
    advanced = config.get("advanced") or {}
    if isinstance(advanced, dict) and isinstance(advanced.get("legacy_policy"), dict):
        for key, value in advanced["legacy_policy"].items():
            overrides[key] = copy.deepcopy(value)
    typed: dict[str, Any] = {
        "timezone": config["timezone"],
        "context_refresh": config["context_refresh"],
        "persona": notif["persona"],
        "notify_address": notif["notify_address"],
        "quiet_start_hour": notif["quiet_start_hour"],
        "quiet_end_hour": notif["quiet_end_hour"],
        "daily_normal_cap": notif["daily_normal_cap"],
        "cooldown_minutes": notif["cooldown_minutes"],
        "feeds_enabled": config["sources"]["public_feeds"],
        "obligations_enabled": config["sources"]["obligations"],
        "mail_enabled": config["sources"]["mail"],
        "calendar_enabled": config["sources"]["calendar"],
        "tasks_enabled": config["sources"]["tasks"],
        "mail_accounts": list(config["mail"]["accounts"]),
        "mail_account_aliases": dict(config["mail"]["aliases"]),
        "calendar_accounts": list(config["calendar"]["accounts"]),
        "calendar_ids": list(config["calendar"]["calendars"]),
        "task_accounts": list(config["tasks"]["accounts"]),
        "task_lists": list(config["tasks"]["lists"]),
        "task_adapter": config["tasks"]["adapter"],
        "public_interests": list(config["public_interests"]),
        "adaptive_mode": config["adaptive"]["mode"],
        "adaptive_privacy_review_enabled": config["adaptive"]["privacy_review"],
        "adaptive_inferred_goals_enabled": config["adaptive"]["inferred_goals"],
        "health_critical_sources": list(config["health"]["critical_sources"]),
        "preparation_enabled": config["preparation"]["enabled"],
        "preparation_scopes": list(config["preparation"]["scopes"]),
        "preparation_source_consent": config["preparation"]["source_consent"],
        "preparation_mode": config["preparation"]["mode"],
        "collector_budget_seconds": config["bounds"]["collector_budget_seconds"],
        "max_candidates_per_kind": config["bounds"]["max_candidates_per_kind"],
        "feed_max_sources_per_run": config["bounds"]["feed_max_sources_per_run"],
        "productivity_secretary_enabled": config["adapters"]["productivity_secretary"],
        "gws_calendar_enabled": config["adapters"]["gws_calendar"],
        "firecrawl_enabled": config["retrieval"]["firecrawl_enabled"],
        "firecrawl_consent": config["retrieval"]["firecrawl_consent"],
        "firecrawl_mcp_server": config["retrieval"]["server"],
        "firecrawl_mcp_tool": config["retrieval"]["tool"],
        "firecrawl_max_attempts_per_cycle": config["retrieval"]["max_attempts_per_cycle"],
        "firecrawl_max_attempts_per_day": config["retrieval"]["max_attempts_per_day"],
        "firecrawl_concurrency": config["retrieval"]["concurrency"],
        "firecrawl_timeout_seconds": config["retrieval"]["timeout_seconds"],
        "firecrawl_cooldown_minutes": config["retrieval"]["cooldown_minutes"],
        "firecrawl_cache_ttl_minutes": config["retrieval"]["cache_ttl_minutes"],
        "_user_config_version": config.get("version", CONFIG_VERSION),
    }
    overrides.update(typed)
    # Preserve an explicitly saved legacy daily ceiling when the typed config
    # never mentions that field. An explicit typed value (including null/blank,
    # which clears the cap) always wins; omission must not silently erase it.
    if not config.get("_explicit_daily_cap"):
        legacy_cap = advanced.get("legacy_policy") if isinstance(advanced, dict) else None
        if isinstance(legacy_cap, dict) and "daily_normal_cap" in legacy_cap:
            overrides["daily_normal_cap"] = copy.deepcopy(
                legacy_cap["daily_normal_cap"])
    # Explicit legacy migration may retain library alias discovery where the
    # original policy omitted account scope. Neutral defaults never set this.
    for kind in advanced.get("legacy_account_discovery", []):
        if not config[kind]["accounts"]:
            overrides.pop(f"{kind if kind == 'calendar' else 'task'}_accounts", None)
    if notif["destination"]:
        overrides["deliver_target"] = notif["destination"]
    if config.get("_explicit_public_sources", True):
        # Empty means empty: an explicit (or neutral) typed configuration must
        # never inherit regional/personal seed feeds from the legacy policy.
        overrides["feed_seed_sources"] = copy.deepcopy(config["public_sources"])
    return overrides


def apply_user_config(policy: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return ``policy`` with the explicit user-configuration overrides applied."""
    merged = dict(policy)
    merged.update(policy_overrides(config))
    return merged


# --------------------------------------------------------------------------- #
# Native settings preference
# --------------------------------------------------------------------------- #

_UNSET = object()


def _classify_native(value: Any) -> tuple[str, Any]:
    """Classify a native ``user_config`` value as unset / ok / invalid.

    A present-but-malformed value is INVALID, never silently treated as absent:
    an object (or a JSON object string) is acceptable, an empty/absent value is
    unset, and anything else fails closed so a broken native setting can never be
    bypassed by a local file or built-in default.
    """
    if value is None or value is _UNSET:
        return "unset", None
    if isinstance(value, Mapping):
        return "ok", dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "unset", None
        try:
            parsed = json.loads(text)
        except ValueError:
            return "invalid", "native user_config setting is not valid JSON"
        if isinstance(parsed, dict):
            return "ok", parsed
        return "invalid", ("native user_config setting must be a JSON object, "
                           f"got {type(parsed).__name__}")
    return "invalid", (f"native user_config setting has unsupported type "
                       f"{type(value).__name__}")


def _read_native_setting(native: Any) -> tuple[str, Any]:
    """Read the native plugin settings ``user_config`` value, fail-closed.

    Accepts a real ``PluginContext``-like object exposing ``get_config`` (the
    supported accessor) or a plain mapping. Returns ``("unset"|"ok"|"invalid",
    value)``. A read FAILURE is ``invalid`` (not unset): a context that cannot be
    consulted must not silently degrade to a local file or built-in defaults.
    """
    if native is None:
        return "unset", None
    if isinstance(native, Mapping):
        if NATIVE_SETTINGS_KEY not in native:
            return "unset", None
        return _classify_native(native.get(NATIVE_SETTINGS_KEY))
    getter = getattr(native, "get_config", None)
    if not callable(getter):
        getter = native if callable(native) else None
    if getter is None:
        return "unset", None
    try:
        value = getter(NATIVE_SETTINGS_KEY, _UNSET)
    except Exception as exc:  # noqa: BLE001 - a failed read is fail-closed
        return "invalid", f"native user_config read failed ({type(exc).__name__})"
    return _classify_native(value)


# Ordinary native settings controls. These are the SAME validated fields the
# CLI/file owner uses; the native form is not a second preference store.
NATIVE_FLAT_FIELDS = {
    "timezone": ("timezone",),
    "context_refresh": ("context_refresh",),
    "destination": ("notification", "destination"),
    "daily_normal_cap": ("notification", "daily_normal_cap"),
    "quiet_start_hour": ("notification", "quiet_start_hour"),
    "quiet_end_hour": ("notification", "quiet_end_hour"),
    "cooldown_minutes": ("notification", "cooldown_minutes"),
    "public_feeds": ("sources", "public_feeds"),
    "obligations": ("sources", "obligations"),
    "mail_enabled": ("sources", "mail"),
    "calendar_enabled": ("sources", "calendar"),
    "tasks_enabled": ("sources", "tasks"),
    "public_interests": ("public_interests",),
    "public_sources": ("public_sources",),
    "discovery_mode": ("adaptive", "mode"),
    "discovery_privacy_review": ("adaptive", "privacy_review"),
    "discovery_inferred_goals": ("adaptive", "inferred_goals"),
    "collector_budget_seconds": ("bounds", "collector_budget_seconds"),
    "max_candidates_per_kind": ("bounds", "max_candidates_per_kind"),
    "feed_max_sources_per_run": ("bounds", "feed_max_sources_per_run"),
    "preparation_enabled": ("preparation", "enabled"),
    "preparation_scopes": ("preparation", "scopes"),
    "preparation_source_consent": ("preparation", "source_consent"),
    "preparation_mode": ("preparation", "mode"),
}


def _native_getter(native: Any):
    if isinstance(native, Mapping):
        def _get(key, default=None):
            return native.get(key, default)
        return _get
    getter = getattr(native, "get_config", None)
    if callable(getter):
        return getter
    return native if callable(native) else None


def _read_native_flat_setting(native: Any) -> tuple[str, Any]:
    """Build one typed config payload from ordinary flat native controls.

    Only keys the plugin manifest actually declares are read. Delegating their
    validation to :func:`validate_config` keeps the native form, CLI, file and
    runner on one owner; no duplicate range/cross-field logic exists here.
    """
    getter = _native_getter(native)
    if getter is None:
        return "unset", None
    raw: dict[str, Any] = {"schema": CONFIG_SCHEMA, "version": CONFIG_VERSION}
    seen = False
    try:
        for key, (section, *rest) in NATIVE_FLAT_FIELDS.items():
            value = getter(key, _UNSET)
            if value is _UNSET:
                continue
            if key in {"public_interests", "public_sources"} and isinstance(value, str):
                try:
                    value = json.loads(value) if value.strip() else []
                except ValueError:
                    return "invalid", f"native {key} setting is not valid JSON"
            seen = True
            if not rest:
                raw[section] = value
                continue
            target = raw.setdefault(section, {})
            if not isinstance(target, dict):
                target = raw[section] = {}
            target[rest[0]] = value
    except Exception as exc:  # noqa: BLE001 - failed read is fail-closed
        return "invalid", f"native settings read failed ({type(exc).__name__})"
    return ("ok", raw) if seen else ("unset", None)


def _overlay_mapping(base: Any, overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-overlay a partial form payload onto a base config mapping.

    Lists and scalars replace; mappings merge one level at a time. This preserves
    every unrelated scope/preference/bound the base already authorised while an
    ordinary form edit changes only its own field.
    """
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _overlay_mapping(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _merge_flat_payload(base_raw: Any, flat_raw: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay explicitly-present flat controls without changing base version."""
    if base_raw is None:
        merged = {"schema": CONFIG_SCHEMA, "version": CONFIG_VERSION}
    elif isinstance(base_raw, dict):
        merged = copy.deepcopy(base_raw)
    else:
        # A malformed base (e.g. a JSON array file) must stay malformed so the
        # one validator fails closed; flat controls never mask it.
        return copy.deepcopy(base_raw)
    # The flat payload always carries the current schema/version for a
    # standalone form-only config. It must never silently upgrade a stale file
    # or opaque native payload: migration stays explicit.
    for key, value in flat_raw.items():
        if key in {"schema", "version"}:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay_mapping(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def native_flat_values(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ordinary native-form values for a validated config.

    This is a projection of the same validated owner, not a second store: the
    manifest keys map onto the already-normalised typed fields. Validation runs
    first so a caller can never write an unvalidated form value.
    """
    validated = validate_config(dict(config))
    notif = validated["notification"]
    adaptive = validated["adaptive"]
    bounds = validated["bounds"]
    sources = validated["sources"]
    return {
        "timezone": validated["timezone"],
        "context_refresh": validated["context_refresh"],
        "destination": notif["destination"],
        # The native form has no nullable number type, so this one optional
        # numeric control uses blank text for unset while an explicit saved
        # integer is projected unchanged. validate_config accepts both.
        "daily_normal_cap": (
            "" if notif["daily_normal_cap"] is None
            else notif["daily_normal_cap"]),
        "quiet_start_hour": notif["quiet_start_hour"],
        "quiet_end_hour": notif["quiet_end_hour"],
        "cooldown_minutes": notif["cooldown_minutes"],
        "public_feeds": sources["public_feeds"],
        "obligations": sources["obligations"],
        "mail_enabled": sources["mail"],
        "calendar_enabled": sources["calendar"],
        "tasks_enabled": sources["tasks"],
        "public_interests": copy.deepcopy(validated["public_interests"]),
        "public_sources": copy.deepcopy(validated["public_sources"]),
        "discovery_mode": adaptive["mode"],
        "discovery_privacy_review": adaptive["privacy_review"],
        "discovery_inferred_goals": adaptive["inferred_goals"],
        "collector_budget_seconds": bounds["collector_budget_seconds"],
        "max_candidates_per_kind": bounds["max_candidates_per_kind"],
        "feed_max_sources_per_run": bounds["feed_max_sources_per_run"],
        "preparation_enabled": validated["preparation"]["enabled"],
        "preparation_scopes": copy.deepcopy(validated["preparation"]["scopes"]),
        "preparation_source_consent": validated["preparation"]["source_consent"],
        "preparation_mode": validated["preparation"]["mode"],
    }


def _same_form_value(left: Any, right: Any) -> bool:
    """Type-sensitive equality (``False`` must not equal ``0`` for a switch)."""
    return type(left) is type(right) and left == right


def native_flat_changes(native: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the ordinary form values that would actually change.

    The caller uses this before taking a backup: an empty result is a true
    no-op, so no backup and no write are needed. Values are validated once here
    and again by :func:`project_native_settings` before any write.
    """
    values = native_flat_values(config)
    getter = _native_getter(native)
    changes: dict[str, Any] = {}
    for key, value in values.items():
        if getter is not None:
            current = getter(key, _UNSET)
            if current is not _UNSET and _same_form_value(current, value):
                continue
        changes[key] = value
    return changes


def project_native_settings(native: Any, config: Mapping[str, Any]) -> list[str]:
    """Mirror validated ordinary controls into the native settings form.

    This is the migration/projection seam used after ``configure init`` or
    ``configure migrate``. It writes through the host's documented
    ``ctx.set_config`` writer (or a mutable settings mapping in tests), skipping
    values that already match, so the Desktop form shows the effective values
    without creating a divergent second preference store. Complex account scope
    remains in the versioned config file and is not widened here.
    """
    values = native_flat_values(config)
    writer = getattr(native, "set_config", None)
    if not callable(writer):
        from collections.abc import MutableMapping
        if isinstance(native, MutableMapping):
            def writer(key: str, value: Any) -> None:  # noqa: ANN001 - local shim
                native[key] = value
            getter = _native_getter(native)
        else:
            raise ConfigError(
                "native settings writer unavailable",
                remedy="run configure through the native plugin context",
            )
    else:
        getter = _native_getter(native)
    written: list[str] = []
    for key, value in values.items():
        if getter is not None:
            current = getter(key, _UNSET)
            if current is not _UNSET and _same_form_value(current, value):
                continue
        writer(key, copy.deepcopy(value))
        written.append(key)
    return written


@dataclass
class LoadedConfig:
    """Result of loading + validating a user configuration."""

    config: dict[str, Any]
    present: bool
    source: str = "defaults"
    warnings: list[str] = field(default_factory=list)
    migrated: bool = False
    migration_report: dict[str, Any] = field(default_factory=dict)
    # ``None`` means a legacy caller constructed LoadedConfig directly; a real
    # load always supplies the raw-presence provenance described above.
    provenance: dict[str, Any] | None = None

    @property
    def version(self) -> int:
        return int(self.config.get("version", CONFIG_VERSION))

    def summary(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "source": self.source,
            "version": self.version,
            "timezone": self.config["timezone"],
            # None is the honest "no deterministic daily ceiling" value; it is
            # never serialised as 0 or restored to a hidden five.
            "daily_normal_cap": self.config["notification"]["daily_normal_cap"],
            "sources": dict(self.config["sources"]),
            "mail_accounts": list(self.config["mail"]["accounts"]),
            "calendars": list(self.config["calendar"]["calendars"]),
            "adaptive_mode": self.config["adaptive"]["mode"],
            "critical_sources": list(self.config.get("health", {}).get("critical_sources", [])),
            "adapters": dict(self.config["adapters"]),
            "retrieval": dict(self.config.get("retrieval", {})),
            "warnings": list(self.warnings),
        }


def load_user_config(
    path: str | Path | None = None,
    *,
    native: Any = None,
    migrate: bool = False,
    strict: bool = True,
) -> LoadedConfig:
    """Load, validate and (when asked) migrate a user configuration.

    Preference order: a native plugin ``user_config`` setting (or ordinary flat
    native form controls layered over it), then the explicit file at ``path``.
    Flat controls are authoritative per field but preserve every unrelated
    scope/bound they do not mention. A present-but-stale version is only
    accepted when ``migrate`` is true; an unknown/corrupt version fails closed.
    """
    native_state, native_raw = _read_native_setting(native)
    if native_state == "invalid":
        raise ConfigError(
            f"invalid native user_config setting: {native_raw}",
            remedy=("fix plugins.entries."
                    f"{NATIVE_PLUGIN_ID}.settings.{NATIVE_SETTINGS_KEY} to a JSON "
                    "object or remove it (a malformed setting never falls back)"),
        )
    # Ordinary flat controls (the real Desktop/TUI form) are read even when the
    # opaque user_config key exists: a form edit must never be silently ignored
    # behind an older native blob.
    flat_state, flat_raw = _read_native_flat_setting(native)
    if flat_state == "invalid":
        raise ConfigError(
            f"invalid native settings: {flat_raw}",
            remedy="correct or remove the offending native plugin setting",
        )

    source = "defaults"
    raw: Any = None
    if native_state == "ok":
        # An opaque native configuration is authoritative over a local file.
        raw, source = native_raw, "native"
    elif path is not None and Path(path).exists():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"cannot read user configuration {path}: {exc}",
                remedy="fix or remove the file, or re-run `hermes secretary configure init`",
            ) from exc
        source = "file"

    if flat_state == "ok":
        # Flat form fields are per-field authoritative and preserve everything
        # else in the file/opaque payload they are layered over.
        if raw is None and native_state == "unset":
            raw = {"schema": CONFIG_SCHEMA, "version": CONFIG_VERSION}
        raw = _merge_flat_payload(raw, flat_raw)
        source = "native"

    if raw is None:
        config = default_config()
        return LoadedConfig(config=config, present=False, source="defaults",
                            provenance={"mode": "defaults", "fields": [],
                                        "present": []})

    provenance = onboarding_provenance(raw)
    config = validate_config(raw)
    explicit_version = config.get("_explicit_version", CONFIG_VERSION)
    migrated = False
    report: dict[str, Any] = {}
    if explicit_version < CONFIG_VERSION:
        if not migrate:
            raise ConfigError(
                f"config version {explicit_version} requires migration to {CONFIG_VERSION}",
                remedy="run `hermes secretary configure migrate` to migrate the "
                       "existing config file in place, or add `--from-policy "
                       "<policy.json>` for a legacy policy",
            )
        config, report = migrate_config(config)
        migrated = True
    if not strict:
        config.pop("_warnings", None)
    warnings = list(config.get("_warnings", []))
    return LoadedConfig(config=config, present=True, source=source,
                        warnings=warnings, migrated=migrated, migration_report=report,
                        provenance=provenance)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def migrate_legacy(
    legacy_policy: dict[str, Any] | None,
    *,
    legacy_preferences: dict[str, Any] | None = None,
    destination: str = "",
    mail_accounts: list[str] | None = None,
    calendar_accounts: list[str] | None = None,
    calendar_ids: list[str] | None = None,
    task_accounts: list[str] | None = None,
    task_lists: list[str] | None = None,
    task_adapter: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map a legacy policy/preferences tree onto an explicit v1 configuration.

    The mapping preserves the legacy behaviour rather than widening it. Explicit
    migration inputs (``mail_accounts``/``calendar_accounts``/``calendar_ids``/
    ``task_accounts``/``task_lists``/``task_adapter``) win over retained policy
    fields, including explicit empty scopes. Formerly omitted calendar/task
    accounts retain library discovery through an explicit migration marker;
    mail retains the original formal/informal fallback only when omitted.
    EVERY remaining effective legacy policy field is retained as an explicit,
    reported compatibility passthrough so no setting is silently dropped. Global
    pause in the legacy preferences is deliberately NOT copied (it stays in
    preferences, so migration can never silently unpause or pause).
    """
    policy = legacy_policy if isinstance(legacy_policy, dict) else {}
    config = default_config()
    report: dict[str, Any] = {"from": "legacy-policy", "mapped": [], "notes": [],
                              "retained_effective_fields": []}

    timezone = policy.get("timezone")
    if isinstance(timezone, str) and _SAFE_TIMEZONE.fullmatch(timezone.strip()):
        config["timezone"] = timezone.strip()
        report["mapped"].append("timezone")
    else:
        report["notes"].append(f"legacy timezone {timezone!r} not carried; neutral UTC used")

    config["context_refresh"] = _bounded_bool(
        policy.get("context_refresh"), "context_refresh", False)

    notif = config["notification"]
    notif["persona"] = str(policy.get("persona") or notif["persona"])[:64]
    notif["notify_address"] = str(policy.get("notify_address") or notif["notify_address"])[:64]
    notif["destination"] = destination.strip() if isinstance(destination, str) else ""
    for src_key, dst_key in (("quiet_start_hour", "quiet_start_hour"),
                             ("quiet_end_hour", "quiet_end_hour"),
                             ("daily_normal_cap", "daily_normal_cap"),
                             ("cooldown_minutes", "cooldown_minutes")):
        if _is_int(policy.get(src_key)):
            notif[dst_key] = (_nonnegative_int(policy[src_key], src_key, notif[dst_key])
                              if src_key == "daily_normal_cap" else policy[src_key])
    report["mapped"].append("notification")

    config["sources"]["public_feeds"] = bool(policy.get("feeds_enabled", True))
    config["sources"]["obligations"] = bool(policy.get("obligations_enabled", True))
    # The legacy private connectors were always attempted; make that explicit.
    for kind in ("mail", "calendar", "tasks"):
        config["sources"][kind] = _bounded_bool(
            policy.get(f"{kind}_enabled"), f"{kind}_enabled", True)
    if mail_accounts is not None:
        config["mail"]["accounts"] = _validate_str_list(mail_accounts, "mail.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("mail.accounts (explicit migration input)")
    else:
        config["mail"]["accounts"] = _validate_str_list(
            policy.get("mail_accounts", ["formal", "informal"]), "mail.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("mail.accounts (prior policy or legacy formal/informal fallback)")
    config["mail"]["aliases"] = copy.deepcopy(policy.get("mail_account_aliases", {}))
    discovery: list[str] = []
    if calendar_accounts is None and "calendar_accounts" in policy:
        calendar_accounts = policy["calendar_accounts"]
    if calendar_accounts is not None:
        config["calendar"]["accounts"] = _validate_str_list(
            calendar_accounts, "calendar.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("calendar.accounts (explicit migration input)")
    elif "calendar_accounts" not in policy:
        discovery.append("calendar")
    if calendar_ids is None and "calendar_ids" in policy:
        calendar_ids = policy["calendar_ids"]
    if calendar_ids is not None:
        config["calendar"]["calendars"] = _validate_str_list(
            calendar_ids, "calendar.calendars", _SAFE_CALENDAR)
        report["mapped"].append("calendar.calendars (explicit migration input)")
    if task_accounts is None:
        if "task_accounts" in policy:
            task_accounts = policy["task_accounts"]
        elif calendar_accounts is not None:
            task_accounts = calendar_accounts
            report["notes"].append("legacy shared calendar aliases copied to tasks.accounts")
        else:
            discovery.append("tasks")
    if task_accounts is not None:
        config["tasks"]["accounts"] = _validate_str_list(
            task_accounts, "tasks.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("tasks.accounts (explicit/retained legacy scope)")
    if task_lists is None and "task_lists" in policy:
        task_lists = policy["task_lists"]
    if task_lists is not None:
        config["tasks"]["lists"] = _validate_str_list(task_lists, "tasks.lists", _SAFE_LABEL)
        report["mapped"].append("tasks.lists (explicit migration input)")
    if task_adapter is None and "task_adapter" in policy:
        task_adapter = policy["task_adapter"]
    if task_adapter is not None:
        adapter_text = str(task_adapter).strip()
        if adapter_text and not _SAFE_ACCOUNT.fullmatch(adapter_text):
            raise ConfigError("task_adapter is not a safe adapter name")
        config["tasks"]["adapter"] = adapter_text
        report["mapped"].append("tasks.adapter (explicit migration input)")
    config["public_interests"] = _validate_str_list(
        policy.get("public_interests"), "public_interests", _SAFE_LABEL)

    seeds = policy.get("feed_seed_sources")
    if isinstance(seeds, list):
        try:
            config["public_sources"] = _validate_public_sources(seeds)
            report["mapped"].append("public_sources")
        except ConfigError as exc:
            config["public_sources"] = []
            report["notes"].append(f"legacy feed_seed_sources not carried: {exc}")

    mode = policy.get("adaptive_mode")
    if mode in {"disabled", "shadow", "active"}:
        config["adaptive"]["mode"] = mode
    config["adaptive"]["privacy_review"] = bool(policy.get("adaptive_privacy_review_enabled", False))
    config["adaptive"]["inferred_goals"] = bool(policy.get("adaptive_inferred_goals_enabled", False))
    report["mapped"].append("adaptive")

    # Explicit per-source health critical list; neutral when absent.
    critical = policy.get("health_critical_sources")
    if isinstance(critical, list):
        config["health"]["critical_sources"] = _validate_str_list(
            critical, "health.critical_sources", _SAFE_SOURCE_NAME)

    config["bounds"]["collector_budget_seconds"] = _bounded_int(
        policy.get("collector_budget_seconds"), "collector_budget_seconds", 5, 600, 60)
    config["bounds"]["max_candidates_per_kind"] = _bounded_int(
        policy.get("max_candidates_per_kind"), "max_candidates_per_kind", 1, 500, 60)
    config["bounds"]["feed_max_sources_per_run"] = _bounded_int(
        policy.get("feed_max_sources_per_run"), "feed_max_sources_per_run", 0, 20, 2)
    report["mapped"].append("bounds")

    # Routed-retrieval parity: enable/consent, named server/tool and local caps.
    config["retrieval"]["firecrawl_enabled"] = _bounded_bool(
        policy.get("firecrawl_enabled"), "firecrawl_enabled", False)
    config["retrieval"]["firecrawl_consent"] = _bounded_bool(
        policy.get("firecrawl_consent"), "firecrawl_consent", False)
    config["retrieval"]["server"] = str(policy.get("firecrawl_mcp_server") or "")
    config["retrieval"]["tool"] = str(policy.get("firecrawl_mcp_tool") or "")
    for src, dst in (
            ("firecrawl_max_attempts_per_cycle", "max_attempts_per_cycle"),
            ("firecrawl_max_attempts_per_day", "max_attempts_per_day"),
            ("firecrawl_concurrency", "concurrency"),
            ("firecrawl_timeout_seconds", "timeout_seconds"),
            ("firecrawl_cooldown_minutes", "cooldown_minutes"),
            ("firecrawl_cache_ttl_minutes", "cache_ttl_minutes")):
        if policy.get(src) is not None:
            config["retrieval"][dst] = policy[src]
    report["mapped"].append("retrieval")

    # Preserve the compatibility adapter and the shipped secretary library route.
    config["adapters"]["productivity_secretary"] = _bounded_bool(
        policy.get("productivity_secretary_enabled"), "productivity_secretary_enabled", True)
    config["adapters"]["gws_calendar"] = _bounded_bool(
        policy.get("gws_calendar_enabled"), "gws_calendar_enabled", False)
    report["mapped"].append("adapters.productivity_secretary")

    # Complete effective parity: retain every remaining legacy policy key as an
    # explicit, reported compatibility field. The typed mapping above takes
    # precedence over these when the policy is rebuilt, so nothing is widened and
    # nothing is silently dropped.
    _mapped_keys = {
        "timezone", "context_refresh", "persona", "notify_address",
        "quiet_start_hour", "quiet_end_hour",
        "daily_normal_cap", "cooldown_minutes", "feeds_enabled", "obligations_enabled",
        "feed_seed_sources", "adaptive_mode", "adaptive_privacy_review_enabled",
        "adaptive_inferred_goals_enabled", "collector_budget_seconds",
        "max_candidates_per_kind", "feed_max_sources_per_run",
        "health_critical_sources", "preparation_enabled", "preparation_scopes",
        "preparation_source_consent",
        "firecrawl_enabled", "firecrawl_consent", "firecrawl_mcp_server",
        "firecrawl_mcp_tool", "firecrawl_max_attempts_per_cycle",
        "firecrawl_max_attempts_per_day", "firecrawl_concurrency",
        "firecrawl_timeout_seconds", "firecrawl_cooldown_minutes",
        "firecrawl_cache_ttl_minutes",
    }
    retained = {k: copy.deepcopy(policy[k]) for k in policy if k not in _mapped_keys}
    config["advanced"] = {"legacy_policy": retained}
    if discovery:
        config["advanced"]["legacy_account_discovery"] = discovery
    report["source_mappings"] = {
        "mail": copy.deepcopy(config["mail"]),
        "calendar": copy.deepcopy(config["calendar"]),
        "tasks": copy.deepcopy(config["tasks"]),
        "adapters": copy.deepcopy(config["adapters"]),
        "legacy_account_discovery": discovery,
    }
    report["notes"].extend([
        "empty calendar IDs retain selected/primary/holiday calendar reads within configured accounts",
        "empty task lists retain all-visible-list reads within configured task accounts",
        "omitted legacy account scopes retain library discovery only through explicit migration",
    ])
    report["retained_effective_fields"] = sorted(retained)
    if retained:
        report["mapped"].append("advanced.legacy_policy (complete effective parity)")

    if legacy_preferences is not None:
        report["notes"].append(
            "preferences (explicit/inferred/snoozes/pause) were not moved: they stay "
            "in preferences.json so migration cannot silently pause or unpause")

    config["version"] = CONFIG_VERSION
    return validate_config(config), report


def migrate_config(stale: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the only in-place migration path: a stale version is revalidated.

    This never invents fields; it normalises the existing mapping to the current
    schema after explicit opt-in. Unknown future versions are rejected earlier.
    """
    normalized = copy.deepcopy(stale)
    normalized["version"] = CONFIG_VERSION
    normalized["schema"] = CONFIG_SCHEMA
    migrated = validate_config(normalized)
    return migrated, {"from": f"version {stale.get('version')}", "to": CONFIG_VERSION,
                      "mapped": ["normalised to current schema"]}


def schema_summary() -> dict[str, Any]:
    """Machine-readable description of the configuration surface for docs/CLI."""
    return {
        "schema": CONFIG_SCHEMA,
        "version": CONFIG_VERSION,
        "timezone_default": DEFAULT_TIMEZONE,
        "sections": list(_SECTIONS),
        "credential_values_allowed": False,
        "native_settings": f"plugins.entries.{NATIVE_PLUGIN_ID}.settings.{NATIVE_SETTINGS_KEY}",
    }


__all__ = [
    "CONFIG_SCHEMA", "CONFIG_VERSION", "DEFAULT_TIMEZONE", "NATIVE_SETTINGS_KEY",
    "ConfigError", "LoadedConfig", "default_config", "validate_config",
    "load_user_config", "migrate_legacy", "migrate_config", "policy_overrides",
    "apply_user_config", "native_flat_values", "native_flat_changes",
    "project_native_settings", "schema_summary",
]
