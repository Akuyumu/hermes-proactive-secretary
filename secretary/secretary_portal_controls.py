"""Small internal helper for global-pause / owned-portal lifecycle coordination.

This module deliberately owns no new persistence. The only durable marker is a
small ``global_pause`` object stored inside the EXISTING cron job-ownership
ledger record for the single owned portal job, bound to that record's exact job
id. A manual portal pause never creates the marker, so a later global resume can
never resume a job it did not pause. A foreign same-named row, a stale/mismatched
id, a malformed marker, or an unreadable/symlinked/corrupt ledger fails without
touching any live job.

The caller supplies the native cron backend (or a zero-argument factory), so this
module never constructs one itself and never reads native configuration.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import
    from .. import cron
except ImportError:  # direct/standalone import
    import cron  # type: ignore

GLOBAL_PAUSE_KEY = "global_pause"
PORTAL_JOB_NAME = cron.PORTAL_JOB_NAME
_PAUSED = {"paused", "disabled", "stopped"}
_ACTIVE = {"active", "enabled", "running", "scheduled"}


class PortalControlError(RuntimeError):
    """A global-pause/secretary operation could not be proven safe."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _job_id(job: dict[str, Any]) -> str:
    for key in ("id", "job_id", "jobId"):
        value = _text(job.get(key))
        if value:
            return value
    return ""


def _job_name(job: dict[str, Any]) -> str:
    return _text(job.get("name"))


def _state(job: dict[str, Any]) -> str:
    for key in ("state", "status"):
        value = _text(job.get(key)).lower()
        if value in _PAUSED:
            return "paused"
        if value in _ACTIVE:
            return "active"
    paused = job.get("paused")
    if isinstance(paused, bool):
        return "paused" if paused else "active"
    return "unknown"


def _empty_ownership() -> dict[str, Any]:
    return {"schema": cron.JOB_OWNERSHIP_SCHEMA, "plugin": cron.OWNED_PLUGIN,
            "jobs": {}}


def load_ownership_strict(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load the ownership ledger, refusing symlinks/corruption instead of resetting.

    ``cron.load_job_ownership`` intentionally degrades to an empty ledger for
    general callers. A pause/resume operation must not: an unreadable ledger
    could hide the fact that a portal opted in, so it fails closed here.
    """
    if path is None:
        return _empty_ownership()
    target = Path(path)
    try:
        if target.is_symlink():
            raise PortalControlError(
                f"cron ownership ledger {target} is a symlink; refusing to use it")
    except OSError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} could not be inspected: {exc}") from exc
    try:
        if not target.exists():
            return _empty_ownership()
    except OSError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} could not be inspected: {exc}") from exc
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} could not be read: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} is corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise PortalControlError(
            f"cron ownership ledger {target} is not a JSON object")
    jobs = data.get("jobs")
    if jobs is None:
        jobs = {}
    if not isinstance(jobs, dict):
        raise PortalControlError(
            f"cron ownership ledger {target} has a non-object jobs map")
    return {
        "schema": data.get("schema", cron.JOB_OWNERSHIP_SCHEMA),
        "plugin": data.get("plugin", cron.OWNED_PLUGIN),
        "jobs": jobs,
    }


def _save_ownership(path: str | os.PathLike[str] | None,
                    ownership: dict[str, Any]) -> None:
    if path is None:
        raise PortalControlError("cron ownership ledger path is unavailable")
    target = Path(path)
    try:
        if target.is_symlink():
            raise PortalControlError(
                f"cron ownership ledger {target} is a symlink; refusing to write it")
    except OSError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} could not be inspected: {exc}") from exc
    try:
        cron.save_job_ownership(target, ownership)
    except OSError as exc:
        raise PortalControlError(
            f"cron ownership ledger {target} could not be written: {exc}") from exc


def portal_record(ownership: dict[str, Any]) -> dict[str, Any] | None:
    record = (ownership.get("jobs") or {}).get(PORTAL_JOB_NAME)
    return record if isinstance(record, dict) else None


def portal_marker(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    marker = record.get(GLOBAL_PAUSE_KEY)
    return marker if isinstance(marker, dict) else None


def _exact_row(jobs: list[dict[str, Any]], job_id: str,
               name: str = PORTAL_JOB_NAME) -> dict[str, Any] | None:
    return next(
        (j for j in jobs if _job_id(j) == job_id and _job_name(j) == name),
        None,
    )


def _same_name_row(jobs: list[dict[str, Any]],
                   name: str = PORTAL_JOB_NAME) -> dict[str, Any] | None:
    return next((j for j in jobs if _job_name(j) == name), None)


def _resolve_backend(backend: Any) -> Any:
    if callable(backend) and not hasattr(backend, "list_jobs"):
        return backend()
    return backend


def _failure(portal: str, detail: str, *, scheduler: bool = True,
             job_id: str = "") -> dict[str, Any]:
    return {"ok": False, "portal": portal, "scheduler": scheduler,
            "job_id": job_id, "detail": detail}


def _read_record(path: str | os.PathLike[str] | None) -> tuple[dict[str, Any],
                                                              dict[str, Any] | None]:
    ownership = load_ownership_strict(path)
    return ownership, portal_record(ownership)


def _valid_record_id(record: dict[str, Any]) -> str:
    job_id = _text(record.get("id"))
    if not job_id:
        raise PortalControlError("owned portal record has no exact job id")
    return job_id


def _validate_marker(record: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    raw = record.get(GLOBAL_PAUSE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PortalControlError("global pause marker is malformed")
    marker_id = _text(raw.get("job_id"))
    if not marker_id or marker_id != job_id:
        raise PortalControlError(
            "global pause marker is not bound to the owned portal job id")
    return raw


def global_pause_outstanding(
    ownership_path: str | os.PathLike[str] | None,
    preferences_path: str | os.PathLike[str] | None,
) -> tuple[bool, str]:
    """Return whether an explicit global pause is outstanding.

    The ownership marker is authoritative for a portal job paused BY the global
    pause. The existing preferences ``paused`` flag is also an outstanding global
    pause (it may predate a portal opt-in). Missing files mean "neutral"; a
    corrupt owned ledger or preferences file raises instead of pretending work is
    stopped/unstopped.
    """
    ownership, record = _read_record(ownership_path)
    if record is not None:
        job_id = _valid_record_id(record)
        marker = _validate_marker(record, job_id)
        if marker is not None:
            return True, "owned portal job is marked paused by global pause"
    if not preferences_path:
        return False, ""
    target = Path(preferences_path)
    try:
        if target.is_symlink():
            raise PortalControlError(
                f"preferences {target} is a symlink; refusing to infer pause state")
    except OSError as exc:
        raise PortalControlError(
            f"preferences {target} could not be inspected: {exc}") from exc
    try:
        if not target.exists():
            return False, ""
    except OSError as exc:
        raise PortalControlError(
            f"preferences {target} could not be inspected: {exc}") from exc
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PortalControlError(
            f"preferences {target} could not be read: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PortalControlError(
            f"preferences {target} are corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise PortalControlError(
            f"preferences {target} are not a JSON object")
    paused = data.get("paused")
    if isinstance(paused, dict) and paused.get("active"):
        return True, "global pause is active in preferences"
    return False, ""


def pause_portal(backend: Any, ownership_path: str | os.PathLike[str] | None, *,
                 now: str | None = None) -> dict[str, Any]:
    """Pause the exact owned portal job and record the id-bound global marker.

    No portal record means the legacy global pause behaviour is preserved: no
    backend call, no job, no ledger write. A job already paused before this call
    is left paused WITHOUT a marker, so a later global resume cannot resume a
    manual pause.
    """
    try:
        ownership, record = _read_record(ownership_path)
        if record is None:
            return {"ok": True, "portal": "not-opted-in", "scheduler": False,
                    "job_id": "", "detail": "no owned portal job is opted in"}
        job_id = _valid_record_id(record)
        marker = _validate_marker(record, job_id)
    except PortalControlError as exc:
        return _failure("invalid-ledger", str(exc), scheduler=False)

    resolved = _resolve_backend(backend)
    if resolved is None or not callable(getattr(resolved, "list_jobs", None)):
        return _failure("unavailable", "cron backend is unavailable", job_id=job_id)
    try:
        jobs = list(resolved.list_jobs())
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return _failure("error", f"could not list cron jobs: {exc}", job_id=job_id)

    row = _exact_row(jobs, job_id)
    if row is None:
        if _same_name_row(jobs) is not None:
            return _failure(
                "foreign",
                "a foreign job uses the portal name; refusing to pause it",
                job_id=job_id)
        return _failure(
            "missing", "the recorded portal job is not present; nothing was paused",
            job_id=job_id)

    state = _state(row)
    if state == "paused":
        if marker is not None:
            return {"ok": True, "portal": "already-paused", "scheduler": True,
                    "job_id": job_id,
                    "detail": "owned portal job is already paused by global pause"}
        return {"ok": True, "portal": "already-paused-manual", "scheduler": True,
                "job_id": job_id,
                "detail": ("owned portal job was already paused before global pause; "
                           "left paused and not marked")}
    if state != "active":
        return _failure(
            "unverified",
            f"owned portal job state is {state!r}; refusing to claim it was paused",
            job_id=job_id)

    try:
        resolved.pause_job(job_id)
    except Exception as exc:  # noqa: BLE001 - honest failure
        return _failure("error", f"portal pause failed: {exc}", job_id=job_id)
    try:
        after = list(resolved.list_jobs())
    except Exception as exc:  # noqa: BLE001 - readback is mandatory
        return _failure(
            "unverified",
            f"portal pause was issued but readback failed: {exc}",
            job_id=job_id)
    readback = _exact_row(after, job_id)
    if readback is None:
        return _failure("unverified",
                        "portal job disappeared from readback after pause",
                        job_id=job_id)
    if _state(readback) != "paused":
        return _failure(
            "unverified",
            f"portal readback state is {_state(readback)!r}; expected 'paused'",
            job_id=job_id)

    if marker is None:
        record[GLOBAL_PAUSE_KEY] = {"job_id": job_id,
                                    "paused_at": now or _now_iso()}
    try:
        _save_ownership(ownership_path, ownership)
    except PortalControlError as exc:
        return _failure(
            "partial",
            "portal job is paused but the global marker could not be saved: "
            f"{exc}", job_id=job_id)
    return {"ok": True, "portal": "paused", "scheduler": True,
            "job_id": job_id,
            "detail": "owned portal job paused and marked by global pause"}


def resume_portal(backend: Any, ownership_path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Resume only a portal job that the global pause itself paused.

    The id-bound marker is the sole authority. A missing marker (manual pause or
    no global pause) leaves the job alone. Marker removal happens only after a
    verified active readback; if that write fails, the job is re-paused so the
    ledger cannot claim a stopped portal while it is actually running.
    """
    try:
        ownership, record = _read_record(ownership_path)
        if record is None:
            return {"ok": True, "portal": "not-opted-in", "scheduler": False,
                    "job_id": "", "detail": "no owned portal job is opted in"}
        job_id = _valid_record_id(record)
        marker = _validate_marker(record, job_id)
    except PortalControlError as exc:
        return _failure("invalid-ledger", str(exc), scheduler=False)

    if marker is None:
        return {"ok": True, "portal": "not-global-paused", "scheduler": False,
                "job_id": job_id,
                "detail": "no global-pause marker; the portal job is left unchanged"}

    resolved = _resolve_backend(backend)
    if resolved is None or not callable(getattr(resolved, "list_jobs", None)):
        return _failure("unavailable", "cron backend is unavailable", job_id=job_id)
    try:
        jobs = list(resolved.list_jobs())
    except Exception as exc:  # noqa: BLE001 - keep marker and paused state
        return _failure("error", f"could not list cron jobs: {exc}", job_id=job_id)

    row = _exact_row(jobs, job_id)
    if row is None:
        if _same_name_row(jobs) is not None:
            return _failure(
                "foreign",
                "a foreign job uses the portal name; refusing to resume it",
                job_id=job_id)
        return _failure(
            "missing", "the recorded portal job is not present; marker retained",
            job_id=job_id)

    state = _state(row)
    if state == "active":
        record.pop(GLOBAL_PAUSE_KEY, None)
        try:
            _save_ownership(ownership_path, ownership)
        except PortalControlError as exc:
            return _failure(
                "partial",
                "portal job is active but the stale global marker could not be "
                f"cleared: {exc}", job_id=job_id)
        return {"ok": True, "portal": "already-active", "scheduler": True,
                "job_id": job_id,
                "detail": "portal job was already active; stale marker cleared"}
    if state != "paused":
        return _failure(
            "unverified",
            f"owned portal job state is {state!r}; refusing to resume it",
            job_id=job_id)

    try:
        resolved.resume_job(job_id)
    except Exception as exc:  # noqa: BLE001 - marker and paused state retained
        return _failure("error", f"portal resume failed: {exc}", job_id=job_id)
    try:
        after = list(resolved.list_jobs())
    except Exception as exc:  # noqa: BLE001 - readback is mandatory
        return _failure(
            "unverified",
            f"portal resume was issued but readback failed: {exc}",
            job_id=job_id)
    readback = _exact_row(after, job_id)
    if readback is None:
        return _failure("unverified",
                        "portal job disappeared from readback after resume",
                        job_id=job_id)
    if _state(readback) != "active":
        return _failure(
            "unverified",
            f"portal readback state is {_state(readback)!r}; expected 'active'",
            job_id=job_id)

    record.pop(GLOBAL_PAUSE_KEY, None)
    try:
        _save_ownership(ownership_path, ownership)
    except PortalControlError as exc:
        # Never leave an active portal while the ledger still claims it is
        # stopped by the global pause. Best-effort restore the paused state.
        try:
            resolved.pause_job(job_id)
        except Exception:  # noqa: BLE001 - reported as partial below
            pass
        return _failure(
            "partial",
            "portal was resumed but the global marker could not be cleared; "
            f"attempted to restore the paused state: {exc}", job_id=job_id)
    return {"ok": True, "portal": "resumed", "scheduler": True,
            "job_id": job_id,
            "detail": "owned portal job resumed and global marker removed"}


__all__ = [
    "GLOBAL_PAUSE_KEY", "PortalControlError", "global_pause_outstanding",
    "load_ownership_strict", "pause_portal", "portal_marker", "portal_record",
    "resume_portal",
]
