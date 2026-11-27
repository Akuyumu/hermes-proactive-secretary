"""Native tool wrapper for the narrow portal-notice proposal queue.

This is the only place that obtains cron identity for a notice proposal.  The
transport in ``proactive_pilot.portal_notices`` deliberately trusts the identity
it is given; this root wrapper verifies that a real native scheduled execution,
the plugin's exact ownership ledger record, and the active profile all agree
*before* constructing a state store or writing anything.

Nothing here sends, prints, starts a scheduler, creates a job, or reads private
sources at import/registration time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

try:  # package import
    from .. import paths
    from ..cron import PORTAL_JOB_NAME, load_job_ownership
except ImportError:  # direct/standalone import
    import paths  # type: ignore
    from cron import PORTAL_JOB_NAME, load_job_ownership  # type: ignore

PROPOSAL_KEYS = ("event_key", "topic", "message", "evidence_refs", "expiry")
ACTIONS = ("submit", "budget", "observations", "observations_ack")
MAX_MESSAGE = 350
MAX_EVENT_KEY = 160
MAX_TOPIC = 80
MAX_EVIDENCE = 8
MAX_EVIDENCE_LEN = 240
MAX_EXPIRY_TEXT = 64
MAX_OBSERVATION_ID = 64
OBSERVATION_BATCH_LIMIT = 5
# The producer fixes the journal scope in ``proactive_pilot.main`` via the
# private ``_OBSERVATION_SCOPE``.  Importing that runner module here would pull
# the whole run loop into the tool surface, so this consumer keeps one local
# constant with the producer's exact value instead of a second hand-typed
# literal at each call site.
OBSERVATION_SCOPE = "owner-local"
_OBSERVATION_FIELDS = (
    "kind",
    "source",
    "ref",
    "phase",
    "observed_at",
    "observation_id",
    "version_id",
)
_OBSERVATION_ACK_KEYS = ("observation_id", "version_id")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NON_PRIVATE_DESTINATIONS = frozenset({"local", "origin"})
_EMISSION_STATUSES = frozenset({"emitted-unconfirmed"})

TOOL_DESCRIPTION = (
    "Queue one bounded user-facing notice through the existing "
    "monitoring gates. This only proposes a notice; it does not send, "
    "complete work, or bypass quiet hours, caps, or delivery controls. "
    "Write at most two sentences of context plus one concrete action, and "
    "use first person for work you did. Never include hashes, opaque ids, "
    "file paths, timestamps, field labels, or \"Two items.\"-style "
    "enumerations; put detail behind a \"show\" reply."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": (
                "submit queues one bounded proposal; budget returns the "
                "current read-only delivery budget; observations lists a "
                "bounded batch of pending owner-local journal entries; "
                "observations_ack acknowledges one exact observation revision."
            ),
        },
        "event_key": {
            "type": "string",
            "maxLength": MAX_EVENT_KEY,
            "description": "Stable identifier for the semantic event.",
        },
        "topic": {
            "type": "string",
            "maxLength": MAX_TOPIC,
            "description": "Short user-facing topic.",
        },
        "message": {
            "type": "string",
            "maxLength": MAX_MESSAGE,
            "description": (
                "One bounded notice message: at most two sentences of context "
                "plus one concrete action, first person for work you did; no "
                "hashes, opaque ids, file paths, timestamps, field labels or "
                "item-count enumerations; detail stays behind a \"show\" reply."
            ),
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_EVIDENCE_LEN},
            "minItems": 1,
            "maxItems": MAX_EVIDENCE,
            "description": "References supporting the notice.",
        },
        "expiry": {
            "type": "string",
            "format": "date-time",
            "maxLength": MAX_EXPIRY_TEXT,
            "description": "Timezone-aware ISO 8601 expiry within 24 hours.",
        },
        "observation_id": {
            "type": "string",
            "maxLength": MAX_OBSERVATION_ID,
            "description": (
                "Exact observation id returned by the observations action."
            ),
        },
        "version_id": {
            "type": "string",
            "maxLength": MAX_OBSERVATION_ID,
            "description": (
                "Exact observation revision returned by the observations action."
            ),
        },
    },
    "required": [],
    "anyOf": [
        {
            "required": ["action"],
            "properties": {"action": {"const": "budget"}},
        },
        {
            "required": ["action"],
            "properties": {"action": {"const": "observations"}},
        },
        {
            "required": ["action", "observation_id", "version_id"],
            "properties": {"action": {"const": "observations_ack"}},
        },
        {"required": list(PROPOSAL_KEYS)},
    ],
    "additionalProperties": False,
}

# The native registry builds provider-facing definitions from the function-level
# envelope; the bare parameter object above is not injected into its
# ``function.parameters``.  Keep ``TOOL_SCHEMA`` as the public parameter constant
# for compatibility and wrap it here for registration.
TOOL_FUNCTION_SCHEMA: dict[str, Any] = {
    "name": "secretary_propose_notice",
    "description": TOOL_DESCRIPTION,
    "parameters": TOOL_SCHEMA,
}


def _bounded_text(value: Any, limit: int) -> bool:
    if not isinstance(value, str) or isinstance(value, bool):
        return False
    return bool(value.strip()) and len(value) <= limit


def _refusal(status: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "accepted": False,
        "status": status,
        "reason": str(reason)[:300],
    }


def _host_identity(ctx: Any) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Return (identity, refusal).  Never touches disk state."""
    getter = getattr(ctx, "current_cron_execution", None)
    if not callable(getter):
        return None, _refusal(
            "unsupported_host", "native cron execution context is unavailable"
        )
    try:
        execution = getter()
    except Exception as exc:  # noqa: BLE001 - bounded denial, no traceback
        return None, _refusal(
            "unsupported_host",
            f"cannot read native cron execution: {type(exc).__name__}",
        )
    if execution is None:
        return None, _refusal(
            "not_cron_execution", "proposal tool requires a native scheduled run"
        )

    job_name = getattr(execution, "job_name", None)
    job_id = getattr(execution, "job_id", None)
    execution_id = getattr(execution, "execution_id", None)
    execution_profile = getattr(execution, "profile", None)

    if job_name != PORTAL_JOB_NAME or not _bounded_text(job_name, 160):
        return None, _refusal(
            "foreign_job", "execution is not the portal proposal job"
        )
    if not _bounded_text(job_id, 128):
        return None, _refusal("missing_job_id", "execution has no exact job id")
    if not _bounded_text(execution_id, 160):
        return None, _refusal(
            "missing_execution_id", "execution has no exact execution id"
        )
    if not isinstance(execution_profile, str) or not _PROFILE_RE.fullmatch(
        execution_profile
    ):
        return None, _refusal("invalid_profile", "execution profile is malformed")

    context_profile = getattr(ctx, "profile_name", None)
    if callable(context_profile):
        # The host contract is a string property; never call model/provider code.
        return None, _refusal(
            "unsupported_host", "profile_name is not a string property"
        )
    if not isinstance(context_profile, str) or not context_profile:
        return None, _refusal(
            "missing_profile", "active host profile is unavailable"
        )
    if context_profile != execution_profile:
        return None, _refusal(
            "profile_mismatch", "execution belongs to a different profile"
        )

    try:
        ownership_path = paths.job_ownership_path()
        if Path(ownership_path).is_symlink():
            return None, _refusal(
                "ownership_unavailable", "job ownership path is a symlink"
            )
        ownership = load_job_ownership(ownership_path)
    except Exception as exc:  # noqa: BLE001 - fail closed without writes
        return None, _refusal(
            "ownership_unavailable",
            f"cannot read job ownership: {type(exc).__name__}",
        )
    jobs = ownership.get("jobs") if isinstance(ownership, dict) else None
    record = jobs.get(PORTAL_JOB_NAME) if isinstance(jobs, dict) else None
    if not isinstance(record, dict):
        return None, _refusal(
            "unowned_job", "portal job has no plugin ownership record"
        )
    if str(record.get("name") or "") != PORTAL_JOB_NAME:
        return None, _refusal("unowned_job", "ownership name does not match")
    if str(record.get("id") or "") != job_id:
        return None, _refusal(
            "ownership_mismatch", "execution id does not match the ownership record"
        )

    return {
        "job_id": job_id,
        "job_name": PORTAL_JOB_NAME,
        "execution_id": execution_id,
        "profile": execution_profile,
    }, None


def _proposal_from_args(args: Mapping[str, Any]) -> dict[str, Any]:
    return {key: args.get(key) for key in PROPOSAL_KEYS}


def _budget_policy(ctx: Any) -> dict[str, Any]:
    """Load the effective policy without creating or mutating any state."""
    from proactive_pilot.policy import load_policy
    from proactive_pilot.userconfig import apply_user_config, load_user_config

    base = load_policy(paths.policy_path())
    loaded = load_user_config(paths.user_config_path(), native=ctx)
    if loaded.present:
        return apply_user_config(base, loaded.config)
    return base


def _budget_receipt(ctx: Any) -> dict[str, Any]:
    """Derive the current delivery budget from existing state; never writes."""
    from proactive_pilot import gates
    from proactive_pilot.clock import Clock
    from proactive_pilot.portal_notices import _load_queue
    from proactive_pilot.state import CorruptStateError, StateStore

    clock = Clock()
    now = clock.now()
    try:
        policy = _budget_policy(ctx)
        cap = gates.daily_normal_cap(policy)
    except Exception as exc:  # noqa: BLE001 - invalid policy/config fails closed
        return _refusal(
            "invalid_config", f"cannot read delivery policy: {type(exc).__name__}"
        )

    try:
        store = StateStore(paths.state_dir(), clock)
        state = store.load_notify_state(strict=True)
        queue = _load_queue(store)
    except CorruptStateError as exc:
        return _refusal("corrupt_state", str(exc))
    except Exception as exc:  # noqa: BLE001 - read failure, never a mutation
        return _refusal(
            "store_unavailable", f"cannot read state store: {type(exc).__name__}"
        )

    delivered = gates.delivered_today(state, policy, now)
    remaining = max(0, cap - delivered) if cap is not None else None
    today = gates.local_day(now, policy)

    return {
        "ok": True,
        "status": "ok",
        "day": today,
        "cap": cap,
        "delivered_today": delivered,
        "remaining": remaining,
        "queue_pending": len(queue["pending"]),
        "quiet_hours": gates.in_quiet_hours(policy, now),
        "cooldown_active": gates.cooldown_active(
            state, now, int(policy.get("cooldown_minutes", 30))
        ),
        "recent_emissions_available": False,
        "recent_emissions_note": (
            "emission history is unavailable: this path has no trusted proof "
            "that the configured destination is the owner-private DM, so the "
            "delivery ledger is not read and no history is exposed."
        ),
    }


def _observations_receipt() -> dict[str, Any]:
    """Read a bounded batch of pending journal entries; never mutates state."""
    from proactive_pilot.clock import Clock
    from proactive_pilot.planner_observations import (
        JournalLockError,
        JournalOverflowError,
        PlannerObservationJournal,
    )
    from proactive_pilot.state import CorruptStateError, StateStore

    try:
        store = StateStore(paths.state_dir(), Clock())
    except Exception as exc:  # noqa: BLE001 - bounded denial, no writes
        return _refusal(
            "store_unavailable", f"cannot open state store: {type(exc).__name__}"
        )

    try:
        journal = PlannerObservationJournal(store, OBSERVATION_SCOPE)
        pending = journal.pending()
    except JournalLockError as exc:
        return _refusal("journal_locked", str(exc))
    except JournalOverflowError as exc:
        return _refusal("journal_overflow", str(exc))
    except CorruptStateError as exc:
        return _refusal("corrupt_state", str(exc))
    except Exception as exc:  # noqa: BLE001 - read failure, never a mutation
        return _refusal(
            "store_unavailable",
            f"cannot read observation journal: {type(exc).__name__}",
        )

    observations = []
    for index, record in enumerate(pending[:OBSERVATION_BATCH_LIMIT]):
        item = {"index": index}
        for field in _OBSERVATION_FIELDS:
            item[field] = record.get(field)
        observations.append(item)
    return {
        "ok": True,
        "status": "ok",
        "observations": observations,
    }


def _observation_ack_receipt(
    observation_id: str, version_id: str
) -> dict[str, Any]:
    """Acknowledge one exact observation revision; never by index alone."""
    from proactive_pilot.clock import Clock
    from proactive_pilot.planner_observations import (
        JournalLockError,
        JournalOverflowError,
        ObservationNotFoundError,
        PlannerObservationJournal,
        StaleObservationError,
    )
    from proactive_pilot.state import CorruptStateError, StateStore

    try:
        store = StateStore(paths.state_dir(), Clock())
    except Exception as exc:  # noqa: BLE001 - bounded denial, no writes
        return _refusal(
            "store_unavailable", f"cannot open state store: {type(exc).__name__}"
        )

    try:
        journal = PlannerObservationJournal(store, OBSERVATION_SCOPE)
        record = journal.acknowledge(observation_id, version_id)
    except JournalLockError as exc:
        return _refusal("journal_locked", str(exc))
    except JournalOverflowError as exc:
        return _refusal("journal_overflow", str(exc))
    except ObservationNotFoundError as exc:
        return _refusal("observation_not_found", str(exc))
    except StaleObservationError as exc:
        return _refusal("stale_observation", str(exc))
    except CorruptStateError as exc:
        return _refusal("corrupt_state", str(exc))
    except Exception as exc:  # noqa: BLE001 - bounded refusal, no sends
        return _refusal(
            "ack_failed",
            f"observation was not acknowledged: {type(exc).__name__}",
        )

    return {
        "ok": True,
        "status": "ok",
        "observation_id": record.get("observation_id"),
        "version_id": record.get("version_id"),
        "acknowledged": record.get("acknowledged"),
        "acknowledged_at": record.get("acknowledged_at"),
    }


def _dispatch(args: Mapping[str, Any], ctx: Any) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        return _refusal("invalid_args", "tool arguments must be an object")
    action = str(args.get("action") or "submit").strip().lower()
    if action not in ACTIONS:
        return _refusal("invalid_args", f"unknown proposal action {action!r}")

    if action == "budget":
        extra = set(args.keys()) - {"action"}
        if extra:
            return _refusal("invalid_args", "budget takes no proposal fields")
        _identity, refusal = _host_identity(ctx)
        if refusal is not None:
            return refusal
        return _budget_receipt(ctx)

    if action == "observations":
        extra = set(args.keys()) - {"action"}
        if extra:
            return _refusal("invalid_args", "observations takes no extra fields")
        _identity, refusal = _host_identity(ctx)
        if refusal is not None:
            return refusal
        return _observations_receipt()

    if action == "observations_ack":
        extra = set(args.keys()) - {"action"} - set(_OBSERVATION_ACK_KEYS)
        if extra:
            return _refusal(
                "invalid_args", "unexpected observation acknowledgement fields"
            )
        observation_id = args.get("observation_id")
        version_id = args.get("version_id")
        if not _bounded_text(observation_id, MAX_OBSERVATION_ID):
            return _refusal(
                "invalid_args",
                "observation_id must be a bounded non-empty string",
            )
        if not _bounded_text(version_id, MAX_OBSERVATION_ID):
            return _refusal(
                "invalid_args", "version_id must be a bounded non-empty string"
            )
        _identity, refusal = _host_identity(ctx)
        if refusal is not None:
            return refusal
        return _observation_ack_receipt(observation_id, version_id)

    extra = set(args.keys()) - set(PROPOSAL_KEYS) - {"action"}
    if extra:
        return _refusal("invalid_args", "unexpected proposal fields")

    identity, refusal = _host_identity(ctx)
    if refusal is not None:
        return refusal

    proposal = _proposal_from_args(args)

    # Imported lazily so registration/import cannot touch profile state.
    from proactive_pilot.clock import Clock
    from proactive_pilot.portal_notices import submit_notice
    from proactive_pilot.state import StateStore

    try:
        store = StateStore(paths.state_dir(), Clock())
    except Exception as exc:  # noqa: BLE001 - bounded denial, no writes
        return _refusal(
            "store_unavailable", f"cannot open state store: {type(exc).__name__}"
        )

    try:
        receipt = submit_notice(
            store, Clock(), identity=identity, proposal=proposal
        )
    except Exception as exc:  # noqa: BLE001 - bounded refusal, no sends
        return _refusal(
            "submit_failed", f"proposal was not queued: {type(exc).__name__}"
        )
    if not isinstance(receipt, dict):
        return _refusal("submit_failed", "proposal returned an unexpected result")
    return receipt


def tool_handler(ctx: Any):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        try:
            return json.dumps(
                _dispatch(args, ctx), ensure_ascii=False, sort_keys=True
            )
        except Exception as exc:  # noqa: BLE001 - bounded tool error
            return json.dumps(
                _refusal("error", f"{type(exc).__name__}: {str(exc)[:200]}"),
                ensure_ascii=False,
                sort_keys=True,
            )

    return handler


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    """Register the proposal tool only on a host that can prove cron context."""
    getter = getattr(ctx, "current_cron_execution", None)
    if not callable(getter):
        return {
            "portal_notice_tool": False,
            "portal_notice_tool_error": "native cron execution context unavailable",
        }
    register = (
        register_tool
        if register_tool is not None
        else getattr(ctx, "register_tool", None)
    )
    if not callable(register):
        return {
            "portal_notice_tool": False,
            "portal_notice_tool_error": "register_tool unavailable",
        }
    try:
        register(
            "secretary_propose_notice",
            "secretary",
            TOOL_FUNCTION_SCHEMA,
            tool_handler(ctx),
            description=TOOL_DESCRIPTION,
            emoji="",
        )
        return {"portal_notice_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration must stay optional
        return {
            "portal_notice_tool": False,
            "portal_notice_tool_error": f"{type(exc).__name__}: {exc}",
        }


__all__ = ["TOOL_SCHEMA", "register_tool", "tool_handler"]
