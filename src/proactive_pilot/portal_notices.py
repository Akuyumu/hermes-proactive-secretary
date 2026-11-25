"""Existing-state-store transport for narrow plugin notice proposals.

This module is deliberately a transport, not another runtime:

- ``submit_notice`` is called by the native plugin proposal tool AFTER the ROOT
  wrapper has obtained and verified host identity.  It locks the injected
  ``StateStore``, structurally validates a bounded proposal, and appends one
  pending item to the private ``portal_notices.json`` artifact.
- ``dispatch_notice`` is called by the existing monitoring ``run_once`` path
  while it already holds the same store lock and has loaded ``notify_state``
  strictly.  It reuses :func:`gates.decide` and
  :func:`gates.record_delivery`; it never sends, prints, calls a model, or
  invents its own policy.

Nothing here reads the ambient environment or a native identity store.  The
identity argument is trusted host data whose shape is checked only for
transport safety; the root wrapper owns obtaining and verifying it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import ownerpaths as _ownerpaths
from . import guidance
from .evaluator import EvaluatorResult
from .gates import decide as gates_decide
from .gates import record_delivery as gates_record_delivery
from .state import CorruptStateError

PORTAL_NOTICES_ARTIFACT = "portal_notices.json"
NOTIFY_STATE_ARTIFACT = "notify_state.json"
QUEUE_SCHEMA = 1

# Must stay identical to cron.PORTAL_JOB_NAME.  Kept as a local literal so this
# transport never imports a top-level ``cron`` module that could be the native
# scheduler package or this plugin's wrapper depending on launch context.
PORTAL_JOB_NAME = "hermes-proactive-secretary-portal"

MAX_PENDING = 32
MAX_SEEN = 512
MAX_EXECUTIONS = 512
SEEN_RETENTION = timedelta(days=7)
MAX_EXPIRY_AHEAD = timedelta(hours=24)

MAX_EVENT_KEY = 160
MAX_TOPIC = 80
MAX_MESSAGE = 350
MAX_EVIDENCE = 8
MAX_EVIDENCE_LEN = 240
MAX_EXPIRY_TEXT = 64

_IDENTITY_KEYS = ("job_id", "job_name", "execution_id", "profile")
_IDENTITY_LIMITS = {
    "job_id": 128,
    "job_name": 160,
    "execution_id": 160,
    "profile": 64,
}
_PROPOSAL_KEYS = {"event_key", "topic", "message", "evidence_refs", "expiry"}
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Defensive shape checks for untrusted text.  These are NOT used to execute or
# render anything; matching text is simply refused before it can become a
# delivery draft.
_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?previous|disregard\s+(?:all\s+)?previous"
    r"|system\s+prompt|you\s+are\s+now|assistant\s*:|system\s*:"
    r"|<\s*/?\s*[a-zA-Z][^>]{0,200}>|javascript\s*:"
    r"|on(?:error|load|click)\s*="
    r"|\b(?:curl|wget)\s+|(?:^|[;&|]\s*)(?:bash|sh|powershell|cmd)\s+"
    r"|rm\s+-rf|\$\(|`|\.\./|/etc/",
    re.IGNORECASE,
)

_TERMINAL_REASONS = {
    "duplicate",
    "expired",
    "malformed expiry",
    "naive expiry",
    "expiry beyond source time",
    "invalid",
    "error",
}


# ---------------------------------------------------------------------------
# small structural helpers
# ---------------------------------------------------------------------------
def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 or ch in "\u2028\u2029" for ch in text)


def _bounded_text(value: Any, limit: int, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str) or isinstance(value, bool):
        return False
    if len(value) > limit:
        return False
    if not allow_empty and not value.strip():
        return False
    return not _has_control(value)


def _injection_flag(text: str) -> str | None:
    match = _INJECTION_RE.search(text)
    return match.group(0) if match else None


def _empty_queue() -> dict[str, Any]:
    return {"schema": QUEUE_SCHEMA, "pending": [], "seen": {}, "executions": {}}


def _valid_queue_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if not _bounded_text(item.get("id"), 64):
        return False
    if not _bounded_text(item.get("event_key"), MAX_EVENT_KEY):
        return False
    if not _bounded_text(item.get("topic"), MAX_TOPIC):
        return False
    if not _bounded_text(item.get("message"), MAX_MESSAGE):
        return False
    if not _bounded_text(item.get("expiry"), MAX_EXPIRY_TEXT):
        return False
    evidence = item.get("evidence_refs")
    if not isinstance(evidence, list) or not (1 <= len(evidence) <= MAX_EVIDENCE):
        return False
    for ref in evidence:
        if not _bounded_text(ref, MAX_EVIDENCE_LEN):
            return False
    return True


def _validate_queue(queue: Any) -> bool:
    if not isinstance(queue, dict) or queue.get("schema") != QUEUE_SCHEMA:
        return False
    pending = queue.get("pending")
    seen = queue.get("seen")
    executions = queue.get("executions")
    if not isinstance(pending, list) or len(pending) > MAX_PENDING:
        return False
    if not isinstance(seen, dict) or not isinstance(executions, dict):
        return False
    for item in pending:
        if not _valid_queue_item(item):
            return False
    for key, value in seen.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            return False
    for key, value in executions.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            return False
    return True


def _load_queue(store: Any) -> dict[str, Any]:
    """Strictly load the artifact.  Missing is empty; malformed is refusal."""
    root = getattr(store, "root", None)
    if root is not None:
        root_path = Path(root)
        if root_path.is_symlink() or (
            root_path / PORTAL_NOTICES_ARTIFACT
        ).is_symlink():
            raise CorruptStateError(
                f"{PORTAL_NOTICES_ARTIFACT}: symlinked queue path refused"
            )
    queue = store.load(PORTAL_NOTICES_ARTIFACT, _empty_queue(), strict=True)
    if not _validate_queue(queue):
        raise CorruptStateError(
            f"{PORTAL_NOTICES_ARTIFACT}: invalid queue shape"
        )
    return queue


def _parse_expiry(value: Any, now: datetime) -> tuple[datetime | None, str]:
    if not isinstance(value, str) or isinstance(value, bool):
        return None, "invalid_expiry"
    if not value or len(value) > MAX_EXPIRY_TEXT or _has_control(value):
        return None, "invalid_expiry"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, "invalid_expiry"
    if parsed.tzinfo is None:
        return None, "naive_expiry"
    if parsed <= now:
        return None, "expired"
    if parsed > now + MAX_EXPIRY_AHEAD:
        return None, "expiry_too_far"
    return parsed, ""


def _validate_identity(identity: Any) -> tuple[dict[str, str] | None, str]:
    if not isinstance(identity, dict):
        return None, "identity must be an object"
    clean: dict[str, str] = {}
    for key in _IDENTITY_KEYS:
        if key not in identity:
            return None, f"missing identity field: {key}"
        value = identity[key]
        if not _bounded_text(value, _IDENTITY_LIMITS[key]):
            return None, f"invalid identity field: {key}"
        clean[key] = value
    profile = clean["profile"]
    if not _PROFILE_RE.fullmatch(profile) or ".." in profile:
        return None, "invalid identity profile"
    return clean, ""


def _validate_proposal(proposal: Any, now: datetime) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(proposal, dict):
        return None, "proposal must be an object"
    if set(proposal.keys()) != _PROPOSAL_KEYS:
        return None, "proposal must contain exactly event_key/topic/message/evidence_refs/expiry"

    event_key = proposal.get("event_key")
    topic = proposal.get("topic")
    message = proposal.get("message")
    evidence = proposal.get("evidence_refs")
    expiry = proposal.get("expiry")

    if not _bounded_text(event_key, MAX_EVENT_KEY):
        return None, "invalid event_key"
    if not _bounded_text(topic, MAX_TOPIC):
        return None, "invalid topic"
    if not _bounded_text(message, MAX_MESSAGE):
        return None, "invalid message"
    if not isinstance(evidence, list) or isinstance(evidence, tuple):
        return None, "invalid evidence_refs"
    if not (1 <= len(evidence) <= MAX_EVIDENCE):
        return None, "evidence_refs must contain 1..8 items"
    for ref in evidence:
        if not _bounded_text(ref, MAX_EVIDENCE_LEN):
            return None, "invalid evidence_refs item"

    parsed_expiry, expiry_error = _parse_expiry(expiry, now)
    if parsed_expiry is None:
        return None, expiry_error

    for text in (event_key, topic, message, *evidence):
        marker = _injection_flag(text)
        if marker:
            return None, f"injection-like text refused: {marker[:40]}"

    return {
        "event_key": event_key,
        "topic": topic,
        "message": message,
        "evidence_refs": list(evidence),
        "expiry": expiry,
        "_expiry": parsed_expiry,
    }, ""


def _receipt(
    *,
    ok: bool,
    accepted: bool,
    status: str,
    reason: str = "",
    event_key: str = "",
    notice_id: str = "",
    expiry: str = "",
    pending: int | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": 1,
        "ok": ok,
        "accepted": accepted,
        "status": status,
        "reason": reason[:160],
    }
    if event_key:
        receipt["event_key"] = event_key[:MAX_EVENT_KEY]
    if notice_id:
        receipt["id"] = notice_id
    if expiry:
        receipt["expiry"] = expiry[:MAX_EXPIRY_TEXT]
    if pending is not None:
        receipt["pending"] = pending
    return receipt


def _notice_id(execution_id: str, event_key: str) -> str:
    raw = f"{execution_id}\x1f{event_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _remove_pending(queue: dict[str, Any], event_key: str) -> None:
    queue["pending"] = [
        item for item in queue["pending"] if item.get("event_key") != event_key
    ]


def _terminal(
    queue: dict[str, Any], item: dict[str, Any], status: str, now: datetime
) -> None:
    key = item.get("event_key")
    _remove_pending(queue, key if isinstance(key, str) else "")
    if isinstance(key, str):
        queue["seen"][key] = {
            "status": status,
            "at": now.isoformat(),
            "expiry": str(item.get("expiry", ""))[:MAX_EXPIRY_TEXT],
        }


def _prune_expired_pending(queue: dict[str, Any], now: datetime) -> bool:
    kept: list[dict[str, Any]] = []
    changed = False
    for item in queue["pending"]:
        _parsed, error = _parse_expiry(item.get("expiry"), now)
        if error in {"expired", "invalid_expiry", "naive_expiry"}:
            key = item.get("event_key")
            if isinstance(key, str):
                queue["seen"][key] = {
                    "status": "expired" if error == "expired" else "invalid",
                    "at": now.isoformat(),
                    "expiry": str(item.get("expiry", ""))[:MAX_EXPIRY_TEXT],
                }
            changed = True
            continue
        kept.append(item)
    queue["pending"] = kept
    return changed


def _prune_seen(queue: dict[str, Any], now: datetime) -> None:
    seen = queue["seen"]
    pending_keys = {str(item.get("event_key")) for item in queue["pending"]}
    cutoff = now - SEEN_RETENTION
    for key, record in list(seen.items()):
        if key in pending_keys or not isinstance(record, dict):
            continue
        stamp = record.get("at")
        if not isinstance(stamp, str):
            continue
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if at.tzinfo is not None and at < cutoff:
            del seen[key]

    if len(seen) > MAX_SEEN:
        removable = sorted(
            (
                (key, record)
                for key, record in seen.items()
                if key not in pending_keys and isinstance(record, dict)
            ),
            key=lambda pair: str(pair[1].get("at", "")),
        )
        while len(seen) > MAX_SEEN and removable:
            key, _record = removable.pop(0)
            seen.pop(key, None)


_HISTORY_EVIDENCE = re.compile(
    r"(^|/)(\.planning|plans|progress\.md|findings\.md)(/|$)|\bplanning-with-files\b",
    re.IGNORECASE,
)


def _evidence_is_only_history(refs: list[str]) -> bool:
    """True when every evidence reference is a planning/history artifact.

    History is legitimate context, but it is not evidence that something changed
    now: a repeat notice must point at a source or a genuinely due trigger.
    """
    if not refs:
        return False
    return all(bool(_HISTORY_EVIDENCE.search(str(ref))) for ref in refs)


def _guidance_root(store: Any) -> Any:
    """Canonical owner data root for guidance, independent of the state store.

    The notice state store is rooted at ``<data_root>/var``; guidance lives at
    ``<data_root>/guidance`` so the writer (tool/CLI, which use ``data_root``)
    and the reader cannot drift apart.
    """
    try:
        from paths import data_root  # plugin root module (runner/installed)
        return Path(data_root())
    except Exception:  # noqa: BLE001 - package import or odd sys.path
        root = getattr(store, "root", None)
        if root is None:
            return None
        path = Path(root)
        return path.parent if path.name == "var" else path


def _prune_executions(queue: dict[str, Any]) -> None:
    executions = queue["executions"]
    if len(executions) <= MAX_EXECUTIONS:
        return
    ordered = sorted(
        executions.items(),
        key=lambda pair: str(pair[1].get("at", "")) if isinstance(pair[1], dict) else "",
    )
    for key, _value in ordered[: len(executions) - MAX_EXECUTIONS]:
        executions.pop(key, None)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------
def submit_notice(
    store: Any, clock: Any, *, identity: Any, proposal: Any
) -> dict[str, Any]:
    """Validate and queue one bounded notice proposal.

    The caller (the native plugin tool) must already have obtained trusted host
    identity.  This function validates its shape only; it does not prove the
    identity against a native execution store.

    Returns a JSON-serializable receipt.  Exact repetition within one native
    execution returns the original receipt; a different event in that same
    execution is refused.
    """
    clean_identity, identity_error = _validate_identity(identity)
    if clean_identity is None:
        return _receipt(
            ok=False, accepted=False, status="invalid_identity", reason=identity_error
        )

    now = clock.now()
    clean_proposal, proposal_error = _validate_proposal(proposal, now)
    if clean_proposal is None:
        return _receipt(
            ok=False, accepted=False, status="invalid_proposal", reason=proposal_error
        )

    event_key = clean_proposal["event_key"]
    expiry = clean_proposal["expiry"]
    notice_id = _notice_id(clean_identity["execution_id"], event_key)

    # Deterministic owner guidance: a closed topic is refused before anything is
    # queued, so a changed countdown or a fresh event key cannot slip past it.
    if _evidence_is_only_history(clean_proposal.get("evidence_refs", [])):
        return _receipt(
            ok=False, accepted=False, status="evidence_is_history",
            reason=("this proposal rests only on planning history; a notice needs a "
                    "current source change or a due authorised trigger"),
            event_key=event_key, notice_id=notice_id, expiry=expiry,
        )
    blocked = guidance.suppressed(
        _guidance_root(store),
        labels=[clean_proposal.get("topic")],
        keys=[event_key, *clean_proposal.get("evidence_refs", [])],
        now=now,
    )
    if blocked:
        return _receipt(
            ok=False, accepted=False,
            status="suppressed" if blocked != "unavailable" else "guidance_unavailable",
            reason=("the owner closed this topic" if blocked != "unavailable" else
                    "owner guidance is unreadable; discretionary notices are held"),
            event_key=event_key,
            notice_id=notice_id, expiry=expiry,
        )

    with store.lock() as acquired:
        if not acquired:
            return _receipt(
                ok=False,
                accepted=False,
                status="locked",
                reason="state store lock unavailable",
                event_key=event_key,
            )

        try:
            queue = _load_queue(store)
        except CorruptStateError as exc:
            return _receipt(
                ok=False,
                accepted=False,
                status="corrupt_queue",
                reason=str(exc),
                event_key=event_key,
            )

        _prune_expired_pending(queue, now)
        _prune_seen(queue, now)

        executions = queue["executions"]
        prior = executions.get(clean_identity["execution_id"])
        if isinstance(prior, dict):
            prior_key = prior.get("event_key")
            if prior_key == event_key:
                saved_receipt = prior.get("receipt")
                if isinstance(saved_receipt, dict):
                    return dict(saved_receipt)
                return _receipt(
                    ok=True,
                    accepted=True,
                    status="queued",
                    reason="already queued",
                    event_key=event_key,
                    notice_id=str(prior.get("id", notice_id)),
                    expiry=str(prior.get("expiry", expiry)),
                )
            return _receipt(
                ok=False,
                accepted=False,
                status="execution_used",
                reason="this native execution already submitted a different event",
                event_key=event_key,
            )

        if event_key in queue["seen"]:
            return _receipt(
                ok=False,
                accepted=False,
                status="duplicate",
                reason="event already queued or emitted",
                event_key=event_key,
                notice_id=notice_id,
                expiry=expiry,
            )

        if len(queue["pending"]) >= MAX_PENDING:
            return _receipt(
                ok=False,
                accepted=False,
                status="queue_full",
                reason="pending portal queue is full",
                event_key=event_key,
            )

        item = {
            "id": notice_id,
            "event_key": event_key,
            "topic": clean_proposal["topic"],
            "message": clean_proposal["message"],
            "evidence_refs": clean_proposal["evidence_refs"],
            "expiry": expiry,
            "received_at": now.isoformat(),
            "origin": {
                "job_id": clean_identity["job_id"],
                "job_name": clean_identity["job_name"],
                "profile": clean_identity["profile"],
            },
        }
        queue["pending"].append(item)
        queue["seen"][event_key] = {
            "status": "pending",
            "at": now.isoformat(),
            "expiry": expiry,
        }

        receipt = _receipt(
            ok=True,
            accepted=True,
            status="queued",
            reason="",
            event_key=event_key,
            notice_id=notice_id,
            expiry=expiry,
            pending=len(queue["pending"]),
        )
        executions[clean_identity["execution_id"]] = {
            "event_key": event_key,
            "id": notice_id,
            "expiry": expiry,
            "at": now.isoformat(),
            "receipt": receipt,
        }
        _prune_executions(queue)

        try:
            store.save(PORTAL_NOTICES_ARTIFACT, queue)
        except Exception:
            return _receipt(
                ok=False,
                accepted=False,
                status="save_failed",
                reason="could not atomically save portal queue",
                event_key=event_key,
            )

        return dict(receipt)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def _relabel_emitted(state: dict[str, Any], signature: Any) -> None:
    if not isinstance(signature, str) or not signature:
        return
    items = state.get("items")
    if isinstance(items, dict):
        record = items.get(signature)
        if isinstance(record, dict):
            record["status"] = "emitted-unconfirmed"
    delivered = state.get("delivered")
    if isinstance(delivered, list):
        for row in reversed(delivered):
            if isinstance(row, dict) and row.get("signature") == signature:
                row["status"] = "emitted-unconfirmed"
                break


def _path_key(value: Any) -> str:
    try:
        return os.path.normcase(
            str(Path(value).expanduser().resolve(strict=False))
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        try:
            return os.path.normcase(
                os.path.abspath(os.path.expanduser(str(value)))
            )
        except (TypeError, ValueError):
            return ""


def _same_path(left: Any, right: Any) -> bool:
    left_key = _path_key(left)
    right_key = _path_key(right)
    return bool(left_key) and left_key == right_key


def _origin_refusal(status: str, reason: str) -> dict[str, Any]:
    return {"message": "", "status": status, "reason": reason[:160]}


def _default_job_reader(job_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Read one live native job through the public read-only cron API."""
    try:
        from cron.jobs import get_job, is_job_runnable
    except Exception:  # noqa: BLE001 - absent/unusable API fails closed
        return None, False
    if not callable(get_job) or not callable(is_job_runnable):
        return None, False
    try:
        job = get_job(str(job_id))
    except Exception:  # noqa: BLE001 - read failure is not authorization
        return None, False
    if not isinstance(job, dict):
        return None, False
    try:
        runnable = bool(is_job_runnable(job))
    except Exception:  # noqa: BLE001 - cannot prove runnable => deny
        return None, False
    return job, runnable


def _read_job(
    job_id: str, job_reader: Any
) -> tuple[dict[str, Any] | None, bool]:
    """Read a job, optionally through the internal test seam.

    The seam may return ``(job, runnable)`` or a job dict with an explicit
    ``_runnable`` boolean; a plain dict also accepts the same enabled/pause
    fields the native predicate checks.  Production always uses the native
    ``get_job``/``is_job_runnable`` pair.
    """
    if job_reader is None:
        return _default_job_reader(job_id)
    if not callable(job_reader):
        return None, False
    try:
        result = job_reader(str(job_id))
    except Exception:  # noqa: BLE001 - seam failure is a closed decision
        return None, False
    if isinstance(result, tuple) and len(result) == 2:
        job, runnable = result
        return (job if isinstance(job, dict) else None), bool(runnable)
    if isinstance(result, dict):
        explicit = result.get("_runnable")
        if isinstance(explicit, bool):
            return result, explicit
        state = str(result.get("state") or "").strip().lower()
        paused = result.get("paused")
        enabled = result.get("enabled", True)
        runnable = (
            enabled is not False
            and paused is not True
            and state not in {"paused", "disabled", "stopped"}
        )
        return result, runnable
    return None, False


def _load_origin_ledger() -> tuple[dict[str, Any] | None, str]:
    """Read the existing ownership ledger; never create or update it here."""
    try:
        root = _ownerpaths.data_root()
    except Exception:  # noqa: BLE001 - profile cannot be proven
        return None, "active profile data root is unavailable"
    path = Path(root) / "cron_job_ownership.json"
    if path.is_symlink():
        return None, "cron job ownership ledger is unavailable"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "cron job ownership ledger is unavailable"
    jobs = loaded.get("jobs") if isinstance(loaded, dict) else None
    if not isinstance(jobs, dict):
        return None, "cron job ownership ledger has no jobs map"
    return jobs, ""


def _authorize_pending(
    store: Any,
    pending: list[dict[str, Any]],
    *,
    native_ctx: Any,
    job_reader: Any,
) -> dict[str, Any] | None:
    """Prove every queued origin still belongs to this exact live portal.

    Validation happens before any gate evaluation, delivery record, or queue
    disposition, so a removed/paused/foreign origin cannot mutate state.
    """
    if native_ctx is None:
        return _origin_refusal(
            "origin_denied", "no native profile context for queued notices"
        )
    context_profile = getattr(native_ctx, "profile_name", None)
    if callable(context_profile) or not isinstance(context_profile, str) or not context_profile:
        return _origin_refusal(
            "origin_denied", "native profile context is unavailable"
        )

    try:
        expected_root = Path(_ownerpaths.data_root()) / "var"
    except Exception:  # noqa: BLE001 - never retarget by launch HOME
        return _origin_refusal(
            "origin_unavailable", "active profile data root is unavailable"
        )
    if not _same_path(getattr(store, "root", None), expected_root):
        return _origin_refusal(
            "origin_denied", "store root does not match the active profile"
        )

    ledger, ledger_error = _load_origin_ledger()
    if ledger is None:
        return _origin_refusal("origin_unavailable", ledger_error)
    record = ledger.get(PORTAL_JOB_NAME)
    if not isinstance(record, dict):
        return _origin_refusal(
            "origin_denied", "portal job has no ownership record"
        )
    if str(record.get("name") or "") != PORTAL_JOB_NAME:
        return _origin_refusal(
            "origin_denied", "portal ownership name does not match"
        )
    recorded_id = str(record.get("id") or "")
    if not recorded_id:
        return _origin_refusal(
            "origin_unavailable", "portal ownership record has no job id"
        )

    for item in pending:
        origin = item.get("origin")
        if not isinstance(origin, dict):
            return _origin_refusal(
                "origin_denied", "queued notice has no origin"
            )
        job_id = origin.get("job_id")
        job_name = origin.get("job_name")
        profile = origin.get("profile")
        if not all(isinstance(value, str) and value for value in (
                job_id, job_name, profile)):
            return _origin_refusal(
                "origin_denied", "queued notice origin is malformed"
            )
        if job_name != PORTAL_JOB_NAME:
            return _origin_refusal(
                "origin_denied", "queued notice came from a foreign job name"
            )
        if profile != context_profile:
            return _origin_refusal(
                "origin_denied", "queued notice belongs to another profile"
            )
        if recorded_id != job_id:
            return _origin_refusal(
                "origin_denied", "queued job id does not match ownership"
            )
        job, runnable = _read_job(job_id, job_reader)
        if not isinstance(job, dict):
            return _origin_refusal(
                "origin_denied", "portal job is no longer present"
            )
        live_id = job.get("id")
        if not isinstance(live_id, str) or not live_id or live_id != job_id:
            return _origin_refusal(
                "origin_denied", "live portal job id does not match"
            )
        live_name = str(job.get("name") or job.get("job_name") or "")
        if live_name != job_name:
            return _origin_refusal(
                "origin_denied", "live portal job name does not match"
            )
        if not runnable:
            return _origin_refusal(
                "origin_denied", "portal job is paused or disabled"
            )
    return None


def dispatch_notice(
    store: Any,
    clock: Any,
    *,
    policy: dict[str, Any],
    prefs: dict[str, Any],
    state: dict[str, Any],
    native_ctx: Any = None,
    job_reader: Any = None,
) -> dict[str, Any]:
    """Attempt to emit at most one pending portal notice.

    The caller ALREADY holds the store lock and has loaded ``notify_state``
    strictly.  This function never sends or prints; it returns ``message`` only
    after both notify-state and queue dispositions have been saved.

    Empty or missing queues are a true no-op with zero writes.
    """
    if not isinstance(state, dict) or not isinstance(policy, dict) or not isinstance(prefs, dict):
        return {
            "message": "",
            "status": "invalid_input",
            "reason": "state, policy and prefs must be objects",
        }

    now = clock.now()

    try:
        queue = _load_queue(store)
    except CorruptStateError as exc:
        return {
            "message": "",
            "status": "corrupt_queue",
            "reason": str(exc)[:160],
        }

    if not queue["pending"]:
        # Empty/missing queue is a true no-op: no ledger read and no writes.
        return {"message": "", "status": "empty", "reason": "no pending notices"}

    # Revalidate origin before any notification-state or queue mutation.
    origin_error = _authorize_pending(
        store, queue["pending"], native_ctx=native_ctx, job_reader=job_reader
    )
    if origin_error is not None:
        return origin_error

    changed = False
    blocked_reason = ""

    for item in list(queue["pending"]):
        if not _valid_queue_item(item):
            _terminal(queue, item, "invalid", now)
            changed = True
            continue

        event_key = item["event_key"]
        # Re-check at emission: a topic closed after it was queued must not go out.
        blocked = guidance.suppressed(
            _guidance_root(store),
            labels=[item.get("topic")],
            keys=[event_key, *(item.get("evidence_refs") or [])],
            now=now,
        )
        if blocked:
            _terminal(queue, item,
                      "suppressed" if blocked != "unavailable" else "guidance_unavailable",
                      now)
            changed = True
            continue
        _parsed, expiry_error = _parse_expiry(item.get("expiry"), now)
        if expiry_error in {"expired", "invalid_expiry", "naive_expiry"}:
            _terminal(
                queue,
                item,
                "expired" if expiry_error == "expired" else "invalid",
                now,
            )
            changed = True
            continue

        key = "portal:" + event_key
        packet = {"candidates": [{"ref": key, "kind": "feed", "version": ""}]}
        proposal = EvaluatorResult(
            action="notify",
            kind="feed",
            key=key,
            message=item["message"],
            urgency="normal",
            expiry=item["expiry"],
            evidence=list(item["evidence_refs"]),
        )

        try:
            decision = gates_decide(
                proposal,
                packet,
                clock=clock,
                policy=policy,
                prefs=prefs,
                state=state,
            )
        except Exception:
            return {
                "message": "",
                "status": "state_error",
                "reason": "gates could not evaluate notify state",
            }

        if decision.get("send"):
            decision["kind"] = "feed"
            decision["key"] = key
            decision["expiry"] = item["expiry"]
            try:
                gates_record_delivery(state, decision, packet, clock, policy)
            except Exception:
                return {
                    "message": "",
                    "status": "state_error",
                    "reason": "could not record emission",
                }

            _relabel_emitted(state, decision.get("signature"))
            _remove_pending(queue, event_key)
            queue["seen"][event_key] = {
                "status": "emitted-unconfirmed",
                "at": now.isoformat(),
                "expiry": item["expiry"],
            }

            # Persist the emission ledger first, then the queue disposition.
            # A partial save must still never retry the consumed semantic event.
            try:
                store.save(NOTIFY_STATE_ARTIFACT, state)
            except Exception:
                return {
                    "message": "",
                    "status": "save_failed",
                    "reason": "notify state save failed",
                }
            try:
                store.save(PORTAL_NOTICES_ARTIFACT, queue)
            except Exception:
                return {
                    "message": "",
                    "status": "save_failed",
                    "reason": "portal queue save failed",
                }

            # Record what this run actually handed to the delivery boundary.
            # The delivered message itself is unchanged; the owner-only ledger
            # only makes an earlier notice resolvable on a later turn. A ledger
            # failure is diagnostic and must never block this emission.
            origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
            try:
                from . import deliveries as _deliveries
                _deliveries.record_emission(
                    store,
                    message=item["message"],
                    kind="portal-notice",
                    topic=item.get("topic") or "",
                    event_key=event_key,
                    notice_id=str(item["id"]),
                    evidence_refs=item.get("evidence_refs") or (),
                    job_id=str(origin.get("job_id") or ""),
                    job_name=str(origin.get("job_name") or ""),
                    destination=str(
                        policy.get("deliver_target")
                        or policy.get("destination") or ""),
                    now=now,
                    status="emitted-unconfirmed",
                )
            except Exception:  # noqa: BLE001 - ledger is diagnostic only
                pass

            return {
                "schema": 1,
                "message": item["message"],
                "status": "emitted-unconfirmed",
                "reason": "ok",
                "event_key": event_key,
                "id": item["id"],
                "expiry": item["expiry"],
            }

        reason = str(decision.get("reason", "blocked"))[:80]
        if reason in _TERMINAL_REASONS:
            _terminal(queue, item, reason, now)
            changed = True
            continue

        # Quiet/cap/cooldown/snooze/preference leaves this item pending.  Keep
        # scanning: one old or explicitly-preferenced event must not block all
        # later events forever.
        if not blocked_reason:
            blocked_reason = reason

    if changed:
        try:
            store.save(PORTAL_NOTICES_ARTIFACT, queue)
        except Exception:
            return {
                "message": "",
                "status": "save_failed",
                "reason": "portal queue save failed",
            }

    if blocked_reason:
        return {
            "message": "",
            "status": "blocked",
            "reason": blocked_reason,
        }
    return {"message": "", "status": "empty", "reason": "no eligible notice"}
