"""Policy + preference artifacts and deterministic (non-importance) gates.

Safeguards here NEVER decide whether something is personally important; that is
the evaluator model's job. They only enforce the user's requested envelope:
quiet hours, daily cap, cooldown, dedupe, expiry, stale-source handling, the
lock, and explicit preference suppression the user asked for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import DEFAULT_TIMEZONE, SG
from .state import parse_iso

try:  # preferences schema lives with the feedback module
    from .feedback import FeedbackError, load_preferences_file
except Exception:  # pragma: no cover - defensive; only used with strict load
    FeedbackError = ValueError  # type: ignore
    load_preferences_file = None  # type: ignore

DEFAULT_POLICY: dict[str, Any] = {
    "timezone": "UTC",
    "context_refresh": False,
    "quiet_start_hour": 0,
    "quiet_end_hour": 8,
    # Optional ceiling, never a target. ``None`` means no deterministic daily
    # message-count ceiling (not a hidden five). An explicit 0 suppresses
    # ordinary discretionary interruptions only; requested reminders and urgent
    # evidence remain. This is an ordinary-message cap, never a total cap.
    "daily_normal_cap": None,
    "cooldown_minutes": 30,
    "urgent_max_minutes": 60,
    "calendar_horizon_hours": 48,
    "mail_window_hours": 48,
    "task_horizon_days": 2,
    "task_baseline_days": 14,
    "max_mail_items": 15,
    "mail_preview_items": 1,
    "mail_preview_timeout_seconds": 2,
    "max_calendar_items": 20,
    "max_task_items": 20,
    # Bounded envelope metadata page; a large mailbox must never hydrate
    # hundreds of rows just to drop them locally.
    "source_page_size": 25,
    # Hard ceiling per source kind (mail/calendar/tasks) before the packet cap.
    # A single huge inventory may not expand the packet unboundedly, and the
    # fair sampler guarantees every attended kind keeps representation.
    "max_candidates_per_kind": 60,
    "state_retention_days": 30,
    "preference_retention_days": 30,
    "stale_after_minutes": 45,
    "deliver_uncertain_note": "[delivery unconfirmed]",
    "evaluator_failure_notice_threshold": 3,
    # Per-source coverage notice cooldown: stops a fail/recover/fail flap from
    # re-notifying the same source, while still allowing a genuine later loss
    # to surface once the window lapses. Retained for the legacy helper; the
    # ordinary scheduled path now uses the per-source health gate below.
    "coverage_notice_cooldown_minutes": 60,
    # -- per-source health gate (durable, bounded recovery) -------------------
    # A retryable timeout/503 is silent at first. A chronic loss only alerts when
    # a source is explicitly critical AND it has failed at least this many
    # attempted reads AND been failing for at least the persistent window AND
    # safe recovery is exhausted. Mail/calendar are NOT critical by default.
    "source_health_failure_threshold": 3,
    "source_health_persistent_minutes": 120,
    "source_health_cooldown_minutes": 60,
    "source_health_max_recovery_attempts": 2,
    # Neutral default: no source is critical until the user's validated
    # configuration names it. This is the ONLY way a source becomes critical.
    "health_critical_sources": [],
    # -- automatic reversible public-source maintenance -----------------------
    # Standing public-maintenance authority: a persistently defective public
    # source may be replaced by a freshly verified equivalent from the SAME
    # interest. Transient/rate-limited/deferred observations never trigger this.
    # One bounded probe per cycle and a cooldown prevent loops. The old record is
    # parked with lineage, not deleted.
    "source_replacement_failure_threshold": 3,
    "source_replacement_max_attempts": 3,
    "source_replacement_max_probes_per_cycle": 1,
    "source_replacement_cooldown_minutes": 360,
    "source_replacement_max_sources_per_cycle": 2,
    "source_replacement_decision_timeout_seconds": 20,
    "source_replacement_max_tokens": 500,
    # Explicit standing consent for automatic public maintenance on ordinary
    # sources. adaptive_mode="active" is also standing consent; feeds_enabled
    # alone is not.
    "public_source_maintenance_enabled": False,
    # Typed configuration proposals are evidence-backed, deduplicated, expiring
    # and only ever shown through the ordinary attention envelope.
    "config_proposal_ttl_hours": 168,
    # -- public awareness expansion -------------------------------------------
    "feeds_enabled": True,
    "obligations_enabled": True,
    "collector_budget_seconds": 60,
    # Whole-run wall-clock ceiling. The per-call/per-category budgets bound each
    # source, but a pure-Python spin (or a hung child the budgets miss) could
    # still hold the shared run lock indefinitely; this outer deadline aborts
    # and releases it. 0 disables the bound.
    "run_wallclock_seconds": 300,
    # Fair category allocations over the global collector budget. Obligations
    # reserve metadata time EVERY run so core mail can never starve them.
    "core_budget_seconds": 35,
    "public_budget_seconds": 10,
    "obligation_budget_seconds": 15,
    "max_public_registry": 12,
    "feed_cadence_minutes": 60,
    "reddit_min_interval_minutes": 60,
    "discovery_cadence_minutes": 360,
    "discovery_topics_cap": 4,
    "feed_timeout_seconds": 15,
    "feed_max_bytes": 2097152,
    "feed_max_items": 15,
    "feed_max_sources_per_run": 2,
    "generic_websites_opt_in": False,
    "public_findings_retention_days": 30,
    # -- V0.4.0 routed retrieval fallbacks (STAGE-ONLY; both OFF by default) ----
    # The direct DNS-pinned HTTPS transport remains the only default route. These
    # two opt-in fallbacks exist ONLY for a 200 JavaScript shell on a PUBLIC page;
    # a challenge/interstitial/paywall/login page is terminal and is never
    # rendered or bypassed. Both require an explicit owner consent flag and a
    # verified host interface (an injected approved renderer / a native secret),
    # otherwise the capability is reported unavailable rather than faked.
    "native_render_fallback_enabled": False,
    "native_render_consent": False,
    "firecrawl_enabled": False,
    "firecrawl_consent": False,
    "retrieval_fallback_max_bytes": 2097152,
    # Native MCP Firecrawl adapter (server/tool are NAMED by configuration, never
    # guessed; no endpoint/key/OAuth code is embedded here). Attempts are locally
    # capped per cycle and per UTC day, concurrency-bounded, and a provider 429
    # triggers a cooldown. These are local-attempt proposals, NOT paid credits.
    "firecrawl_mcp_server": "",
    "firecrawl_mcp_tool": "",
    "firecrawl_mcp_url_arg": "url",
    "firecrawl_mcp_formats_arg": "formats",
    # Verified-schema freshness: null omits the field; 0 requests a live fetch.
    "firecrawl_mcp_max_age": None,
    "firecrawl_schema_verified": False,
    "firecrawl_max_attempts_per_cycle": 1,
    "firecrawl_max_attempts_per_day": 10,
    "firecrawl_concurrency": 1,
    "firecrawl_timeout_seconds": 30,
    "firecrawl_cooldown_minutes": 60,
    "firecrawl_cache_ttl_minutes": 60,
    # -- adaptive watch acquisition (STAGE-ONLY; DISABLED by default) ---------
    # Feature mode is the single honest switch:
    #   disabled -> no adaptive planning/acquisition anywhere (default);
    #   shadow   -> isolated public-only shadow CLI only, no packet candidates;
    #   active   -> adaptive evidence may pass through the ordinary packet gates.
    # ``adaptive_shadow_enabled``/``adaptive_evidence_enabled`` are retained only
    # for backwards compatibility with staged callers and can never widen mode.
    "adaptive_mode": "disabled",
    "adaptive_shadow_enabled": False,
    "adaptive_evidence_enabled": False,
    "adaptive_plan_cadence_minutes": 360,
    "adaptive_max_queries_per_watch": 2,
    "adaptive_max_turns": 4,
    "adaptive_plan_timeout_seconds": 60,
    "adaptive_query_max_chars": 120,
    "adaptive_registry_limit": 12,
    "adaptive_cadence_minutes": 360,
    "adaptive_fetch_max_sources_per_run": 2,
    # -- broad public discovery via the native search bridge (STAGE-ONLY, off) --
    # When enabled, a supported adaptive plan also searches the broad public web
    # (existing configured provider only) and registers strictly vetted public
    # feed/page sources. Disabled by default so the GitHub/Reddit baseline is
    # unchanged. No new keys/accounts/deps and no live canary in this stage.
    "adaptive_broad_discovery_enabled": False,
    # -- autonomous inferred goals (STAGE-ONLY; DISABLED by default) ----------
    # When enabled AND no explicit watch is active, a native zero-tool model may
    # propose ONE bounded, public-safe, revisable investigatory goal from the
    # EXISTING permitted ephemeral task/calendar title context. Inferred goals
    # live in a SEPARATE owner-only registry (``adaptive_inferred.json``), are
    # never explicit watches, never appear as a user request, and are only ever
    # acquired through the same bounded public routes. Off by default so the
    # ordinary baseline stays byte-for-byte unchanged.
    "adaptive_inferred_goals_enabled": False,
    # Bounded durable fairness between explicit watches and autonomous inferred
    # discovery. Explicit watches keep priority, but every period-th planning
    # slot is offered to inferred discovery, so no number of saved explicit
    # watches can permanently disable adaptation. The cursor is persisted in the
    # isolated adaptive registry so it survives restart. Values below 2 are
    # clamped to 2 (never a permanent explicit-only path).
    "adaptive_inferred_fairness_period": 3,
    # Opt-in separate zero-tool SEMANTIC privacy review. Default OFF: the
    # always-on STRUCTURAL privacy boundary still runs (no model call), so
    # enabling inferred goals adds zero extra model calls unless this is set.
    "adaptive_privacy_review_enabled": False,
    "inferred_goals_max": 3,
    "inferred_goals_daily_cap": 3,
    "inferred_goals_ttl_days": 14,
    "inferred_goals_cooldown_minutes": 360,
    "inferred_goals_failure_backoff_minutes": 360,
    "inferred_goals_failure_threshold": 2,
    # Bounded proposal-attempt backoff: repeated failed/invalid proposals consume
    # the daily attempt cap and, past this threshold, stop further model proposal
    # calls for the window instead of retrying without limit.
    "inferred_proposal_failure_threshold": 3,
    "inferred_proposal_backoff_minutes": 360,
    "native_search_module": "tools.web_tools",
    "native_search_function": "web_search_tool",
    "native_search_max_queries_per_cycle": 2,
    "native_search_cache_ttl_minutes": 60,
    "native_search_rate_limit_backoff_minutes": 60,
    "native_search_max_discovered_sources": 3,
    # -- trusted conversational intake (explicit requests only) ----------------
    "intake_max_turns": 2,
    "intake_timeout_seconds": 60,
    # -- bounded private preparation (opt-in; neutral default OFF) ------------
    # Disabled until the user explicitly enables it and approves the small
    # per-responsibility scope vocabulary. Enabling never creates a cron job.
    "preparation_enabled": False,
    "preparation_scopes": [],
    "preparation_source_consent": False,
    "preparation_mode": "",
    # -- local-only phone context (STAGE-ONLY, opt-in) ------------------------
    # Disabled by default. When enabled, the phone is consulted ONLY around an
    # upcoming meaningful in-person calendar event, on the fixed local origin,
    # with coordinates kept local/ephemeral. This is per-pilot policy, never
    # Hermes core config. See proactive_pilot.location.
    "location_context_enabled": False,
    "location_event_window_minutes": 90,
    "location_force_cadence_minutes": 15,
    "feed_seed_sources": [],  # populated from publicfeeds.default_seed_sources at load
    "obligation_bootstrap_days": 90,
    "obligation_incremental_hours": 72,
    "obligation_scan_limit": 300,
    "obligation_max_pages_per_run": 1,
    # Small historical metadata page so one page can be scanned and its cursor
    # persisted inside the reserved obligation window. The obligation flow now
    # uses the supported native Gmail route, so the id page defaults to a small
    # 10 and at most a few metadata gets run per account per run.
    "obligation_page_size": 10,
    "obligation_metadata_items_per_run": 3,
    "obligation_preview_items": 1,
    "obligation_preview_chars": 200,
    "obligation_preview_timeout_seconds": 3,
    "obligation_retention_days": 30,
    "evaluator": {
        "enabled": True,
        "toolset": "context_engine",
        "max_turns": 12,
        "run_budget_seconds": 90,
        "timeout_seconds": 100,
        "continue_name": "pilot scout",
        "continue_enabled": True,
    },
    "mail_noise_patterns": [
        "linkedin", "glassdoor", "indeed", "job alert", "jobs@",
        "newsletter", "noreply@", "no-reply@", "updates-noreply",
        "digest@", "notifications@",
    ],
}

DEFAULT_PREFERENCES: dict[str, Any] = {
    "schema": 1,
    "version": 0,
    "explicit": [],
    "inferred": [],
    "snoozes": [],
    "paused": {"active": False},
}


def load_policy(path: Path | None) -> dict[str, Any]:
    import json

    policy = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    if not policy.get("feed_seed_sources"):
        try:
            from .publicfeeds import default_seed_sources

            policy["feed_seed_sources"] = default_seed_sources()
        except Exception:  # noqa: BLE001 - seeds are optional configuration
            policy["feed_seed_sources"] = []
    if path and Path(path).exists():
        try:
            override = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(override, dict):
                for key, value in override.items():
                    if isinstance(value, dict) and isinstance(policy.get(key), dict):
                        policy[key].update(value)
                    else:
                        policy[key] = value
        except (OSError, ValueError):
            pass
    return policy


def load_preferences(path: Path | None, *, strict: bool = False) -> dict[str, Any]:
    """Load preferences.

    Non-strict keeps legacy fallback-to-default behavior for tests. Strict mode
    is used by the runtime so a corrupt preferences file fails closed instead of
    silently dropping explicit suppressions and re-sending.
    """
    import json

    if path and Path(path).exists():
        if strict and load_preferences_file is not None:
            prefs = load_preferences_file(Path(path), strict=True)
        else:
            prefs = json.loads(json.dumps(DEFAULT_PREFERENCES))
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key in ("explicit", "inferred", "snoozes"):
                        if isinstance(data.get(key), list):
                            prefs[key] = data[key]
                    if isinstance(data.get("paused"), dict):
                        prefs["paused"] = data["paused"]
            except (OSError, ValueError):
                return prefs
    else:
        prefs = json.loads(json.dumps(DEFAULT_PREFERENCES))
    prefs.setdefault("version", 0)
    return prefs


def policy_zone(policy: dict[str, Any]) -> ZoneInfo:
    """Return the configured zone; invalid values use neutral UTC.

    The generic plugin never assumes a regional location. A malformed legacy
    value therefore falls back to UTC (never a regional default) while the validated
    user configuration path still rejects invalid IANA names explicitly.
    """
    name = policy.get("timezone") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def localize(now: datetime, policy: dict[str, Any]) -> datetime:
    """Convert an aware instant to the configured local timezone.

    A naive instant is treated as already-local (fixtures and legacy state).
    This is the single conversion used by quiet hours, local-day caps and the
    runtime clock contract.
    """
    if now.tzinfo is None:
        return now.replace(tzinfo=policy_zone(policy))
    return now.astimezone(policy_zone(policy))


def local_day(now: datetime, policy: dict[str, Any]) -> str:
    return localize(now, policy).date().isoformat()


def daily_normal_cap(policy: dict[str, Any]) -> int | None:
    """Return the explicit ordinary-message ceiling, or ``None`` when unset.

    Unset (``None``/blank) means no deterministic daily-count ceiling; it is
    never coerced to ``0`` or restored to a hidden five. Explicit nonnegative
    integers (including ``0``) keep their existing meaning. Invalid values
    (booleans, negatives, fractions, non-numeric text) raise ``ValueError``
    instead of silently disabling the ceiling. This bounds ordinary
    discretionary messages only -- urgent evidence and user-requested reminders
    keep their existing exceptions.
    """
    raw = policy.get("daily_normal_cap")
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(
            "daily_normal_cap must be a nonnegative integer or blank, "
            f"not boolean {raw!r}")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError(
                "daily_normal_cap must be a nonnegative integer or blank, "
                f"not {raw!r}")
        return int(text)
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError(
                "daily_normal_cap must be a nonnegative integer or blank, "
                f"not {raw!r}")
        return raw
    raise ValueError(
        "daily_normal_cap must be a nonnegative integer or blank, "
        f"not {raw!r}")


def stored_local_day(value: Any, policy: dict[str, Any], now: datetime) -> str:
    """Local day of a stored ISO timestamp, normalising naive legacy values."""
    try:
        parsed = parse_iso(str(value), now)
    except (ValueError, TypeError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo or timezone.utc)
    return local_day(parsed, policy)


def in_quiet_hours(policy: dict[str, Any], now: datetime) -> bool:
    start = int(policy.get("quiet_start_hour", 0))
    end = int(policy.get("quiet_end_hour", 8))
    hour = localize(now, policy).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def active_snooze(prefs: dict[str, Any], now: datetime,
                  target_id: Any = None) -> bool:
    """True for a global pause or an unscoped active snooze.

    Scoped snoozes (kind/match/target_id) are enforced per-item in
    ``preference_blocks``; a global pause is intentionally total and reversible
    via ``resume``. An id-only snooze is scoped even when its matcher is empty,
    and only matches when the caller supplies the same target id.
    """
    paused = prefs.get("paused") or {}
    if isinstance(paused, dict) and paused.get("active"):
        until = paused.get("until")
        if not until:
            return True
        try:
            if parse_iso(str(until), now) > now:
                return True
        except ValueError:
            return True  # malformed pause: stay paused (fail closed)
    wanted = str(target_id or "").strip().lower()
    for row in prefs.get("snoozes", []):
        row_target = str(row.get("target_id") or "").strip().lower()
        if row_target:
            if not wanted or row_target != wanted:
                continue
        elif row.get("kind") or row.get("match"):
            continue  # scoped: handled per item
        try:
            until = parse_iso(row["until"], _TZ(now))
        except (KeyError, ValueError):
            continue
        if until > now:
            return True
    return False


def _TZ(now: datetime):
    return now.tzinfo


def is_noise(item: dict[str, Any], policy: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(field, "")) for field in ("sender", "subject", "source")
    ).lower()
    return any(pat in haystack for pat in policy.get("mail_noise_patterns", []))


def suppression_rule(kind: str) -> str | None:
    """Map an explicit preference scope to a candidate kind."""
    mapping = {
        "mail_suppress": "mail",
        "calendar_suppress": "calendar",
        "task_suppress": "task",
        "feed_suppress": "feed",
        "obligation_suppress": "obligation",
    }
    return mapping.get(kind)


def preference_blocks(item: dict[str, Any], prefs: dict[str, Any], now: datetime) -> bool:
    """True when an explicit, unexpired user preference suppresses this candidate.

    Only explicit rules block. Conditional rules (e.g. the career-noise
    exception) never blanket-suppress; they are passed to the evaluator so it
    can decide whether an item is exceptionally fitting. Inferred rules are
    advisory and never silently suppress. Active scoped snoozes do block.
    """
    kind = item.get("kind")
    text = f"{item.get('subject','')} {item.get('message','')}".lower()
    item_target = str(item.get("target_id") or "").strip().lower()
    for row in prefs.get("snoozes", []):
        if not (row.get("kind") or row.get("match") or row.get("target_id")):
            continue
        row_target = str(row.get("target_id") or "").strip().lower()
        if row_target:
            # A target-scoped snooze only matches the same target id. Without
            # an id to compare it must fail open to delivery, never suppress a
            # sibling by text. The id is the matcher; kind/label are not.
            if not item_target or row_target != item_target:
                continue
        else:
            row_kind = str(row.get("kind") or "")
            if row_kind and row_kind not in {kind, "any"}:
                continue
            match = str(row.get("match") or "").lower().strip()
            if match and match not in text:
                continue
        try:
            if parse_iso(row["until"], now) > now:
                return True
        except (KeyError, ValueError):
            continue
    for rule in prefs.get("explicit", []):
        if rule.get("kind") not in {kind, "any"}:
            continue
        if not rule.get("suppress", False):
            continue
        if rule.get("conditional"):
            continue  # never blanket-suppress a conditional rule
        expiry = rule.get("expires_at")
        if expiry:
            try:
                if parse_iso(expiry, now) <= now:
                    continue
            except ValueError:
                pass
        match = str(rule.get("match", "")).lower().strip()
        if match and match in text:
            return True
    return False


def cooldown_active(state: dict[str, Any], now: datetime, cooldown_minutes: int) -> bool:
    last = state.get("last_delivery_at")
    if not last:
        return False
    try:
        if parse_iso(last, now) > now - timedelta(minutes=cooldown_minutes):
            return True
    except ValueError:
        return False
    return False
