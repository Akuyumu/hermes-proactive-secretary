"""Lifecycle service for the single local normal-agent portal cron job.

Only the native normal-agent lane is managed here. Local delivery remains
enforced; notice proposals are queued for the existing monitoring lane rather
than sent directly. This module never edits cron.py, the CLI, or any other
worker's files.
"""
from __future__ import annotations

import inspect
import json
from typing import Any

try:  # package import
    from .. import cron, paths
    from . import secretary_portal_controls as portal_controls
except ImportError:  # direct/standalone import
    import cron  # type: ignore
    import paths  # type: ignore
    from secretary import secretary_portal_controls as portal_controls  # type: ignore

LIMITATIONS = (
    "notifications depend on an already-enabled monitoring lane; a proposal is not "
    "completion or delivery; only the local cron final output is managed here; "
    "quiet/cap remain enforced by that lane; native MCP has full trust outside "
    "cron_mode; memory/adaptation behaviour is not runtime verified"
)

PROMPT = (
    "Act as the owning profile's private portal, not a notifier. You run with "
    "the same owning-profile Hermes identity, tools, connectors, skills, memory, "
    "and standing permission and approval rules as an ordinary session; there is "
    "no separate portal tool allowlist. Proactively seek useful information in "
    "already authorised sources and native capabilities available to this "
    "profile, not only fixed collector candidates or accepted tasks. Inspect the "
    "accepted responsibilities, standing instructions and authorised sources "
    "available in this owning profile, and compare them with prior progress in native "
    "memory and existing work artifacts. Eagerly investigate, prepare private useful "
    "artifacts, suggest actionable next steps, verify and persist progress, and "
    "notice material changes. Use changed evidence as the main signal and avoid "
    "repeating work on unchanged inputs; however, a new preparation opportunity "
    "may still be useful without any source revision, and not every useful action "
    "needs a new explicit responsibility within standing authority. When an "
    "approved source, capability or routine expires or fails in a way a simple "
    "replacement cannot fix, do not only report the failure: investigate a "
    "successor or workaround, verify it against the original intent with real "
    "evidence, and surface the prepared change together with the exact action "
    "needed. Restoring an already-approved source within its original purpose is "
    "yours to do autonomously and report afterwards; adding new sources or "
    "categories, changing credentials, spending, or any external send stays "
    "pending for the user and must never be treated as implied by this "
    "instruction. Execute work "
    "with native tools, then read back the actual result before claiming success. "
    "Persist progress, evidence and any next due follow-up in native memory or "
    "existing work artifacts; never create a parallel memory, preference, "
    "scheduling or permission engine. Consult any additional active follow-ups "
    "through the typed secretary_followup tool; when there are none or it is "
    "unavailable, use your original initiative independently. Follow-ups never "
    "grant new authority and must never add a schedule or mutate this per-watch "
    "prompt. Apply explicit corrections, preserve "
    "standing obligations and temporary scope, and never treat thanks, silence, "
    "or non-response as a request to drop an obligation or as learned dislike. "
    "Leave every approval-required action unexecuted and surface a concrete "
    "pending blocker with the exact approval needed. For a user notice, use "
    "secretary_propose_notice so the existing monitoring gates decide delivery; "
    "never send directly or attempt to evade plugin controls. Keep the native "
    "final local. Prefer meaningful silence when no changed evidence warrants "
    "action; never spam or fill quotas. Learned comfort or preference never grants "
    "new authority, sources, or spending. This prompt is not enforcement or a "
    "sandbox and provides no safety guarantee; native MCP tools retain full trust "
    "outside cron_mode. Native skills and workdir pins remain optional."
)

ACTIONS = {"preview", "enable", "status", "pause", "resume", "remove"}
PINS = ("schedule", "model", "provider", "reasoning_effort", "skill", "deliver", "workdir")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_LIVE_KEYS = {
    "schedule": ("schedule",),
    "model": ("model",),
    "provider": ("provider",),
    "reasoning_effort": ("reasoning_effort", "reasoning"),
    "skill": ("skills", "skill"),
    "deliver": ("deliver",),
    "workdir": ("workdir",),
    "context_from": ("context_from",),
    "prompt": ("prompt",),
}


def _arg(args: Any, name: str, default: Any = None) -> Any:
    if isinstance(args, dict):
        return args.get(name, default)
    return getattr(args, name, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _control(args: Any, controls: dict[str, Any] | None, key: str) -> Any:
    """Read one already-frozen control, else fall back to caller CLI args."""
    if controls is not None and key in controls:
        return controls[key]
    if key == "reasoning_effort":
        return _arg(args, "reasoning_effort", _arg(args, "reasoning"))
    return _arg(args, key)


def _check_reasoning(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("reasoning_effort must be a string")
    text = value.strip()
    if not text:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("reasoning_effort contains control characters")
    canonical = text.lower()
    if canonical not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be one of: "
                         + ", ".join(REASONING_EFFORTS))
    return canonical


def _read_config(getter: Any, key: str) -> Any:
    try:
        return getter(key, "")
    except Exception as exc:  # noqa: BLE001 - surface a clear pre-backend failure
        raise ValueError(f"{key} could not be read: {exc}") from exc


def _resolve_controls(args: Any, native_ctx: Any) -> dict[str, Any]:
    """Freeze preview/enable model controls from one config read per key.

    Explicit CLI values keep their existing precedence. The configured primary
    pair is consulted only when neither CLI field was supplied; a partial or
    wrongly typed pair fails before any backend work. A blank reasoning setting
    means native inheritance, never a hardcoded model.
    """
    getter = getattr(native_ctx, "get_config", None)
    if not callable(getter):
        getter = None

    raw_provider = _arg(args, "provider")
    raw_model = _arg(args, "model")
    provider = raw_provider
    model = raw_model
    if not _text(raw_provider) and not _text(raw_model) and getter is not None:
        configured_provider = _read_config(getter, "primary_provider")
        configured_model = _read_config(getter, "primary_model")
        for key, value in (("primary_provider", configured_provider),
                           ("primary_model", configured_model)):
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
        configured_provider = configured_provider.strip()
        configured_model = configured_model.strip()
        if bool(configured_provider) != bool(configured_model):
            raise ValueError(
                "primary_provider and primary_model must be set together or both left empty")
        if configured_provider:
            provider = configured_provider
            model = configured_model

    raw_effort = _arg(args, "reasoning_effort", _arg(args, "reasoning"))
    if not _text(raw_effort) and getter is not None:
        raw_effort = _read_config(getter, "portal_reasoning_effort")
    effort = _check_reasoning(raw_effort)
    return {
        "model": model,
        "provider": provider,
        "reasoning_effort": effort or None,
    }


def _skills_arg(value: Any) -> list[str]:
    """Preserve an exact, case-sensitive, ordered skill list from argparse."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


def _norm(value: Any) -> Any:
    # Identifiers (model/provider/workdir/skills) are case-sensitive; only
    # presentation state/status names are lowercased elsewhere.
    if isinstance(value, (list, tuple, set)):
        return tuple(_text(v) for v in value if _text(v))
    return _text(value)


def _job_id(job: dict[str, Any]) -> str:
    for key in ("id", "job_id", "jobId"):
        value = _text(job.get(key))
        if value:
            return value
    return ""


def _find_name(jobs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((j for j in jobs if _text(j.get("name")) == name), None)


def _live(job: dict[str, Any], key: str) -> Any:
    for field in _LIVE_KEYS.get(key, (key,)):
        value = job.get(field)
        if value is not None and _text(value):
            return value
    return None


def _recorded(record: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(record, dict):
        return None
    fields = _LIVE_KEYS.get(key, (key,))
    for field in fields:
        value = record.get(field)
        if value is not None and _text(value):
            return value
    return None


def _state(job: dict[str, Any]) -> str:
    for key in ("state", "status"):
        value = _text(job.get(key)).lower()
        if value in {"paused", "disabled", "stopped"}:
            return "paused"
        if value in {"active", "enabled", "running", "scheduled"}:
            return "active"
    paused = job.get("paused")
    if isinstance(paused, bool):
        return "paused" if paused else "active"
    return "unknown"


def _locate(cron_mod: Any, jobs: list[dict[str, Any]], ownership: dict[str, Any],
            name: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, str]:
    record = cron_mod.owned_job_record(ownership, name)
    same_name = _find_name(jobs, name)
    if not isinstance(record, dict):
        if same_name is not None:
            return "foreign", same_name, None, "same-named job is not in this plugin's ledger"
        return "missing", None, None, "no owned job recorded and none present"

    recorded_id = _text(record.get("id"))
    owned = next(
        (j for j in jobs
         if _job_id(j) == recorded_id and _text(j.get("name")) == name),
        None,
    ) if recorded_id else None
    if owned is not None:
        return "owned", owned, record, ""
    if same_name is not None:
        return "foreign", same_name, record, "ledger id/name does not match the live job"
    return "missing", None, record, "recorded job no longer present"


def _deliver_ok(args: Any) -> tuple[str, str]:
    raw = _text(_arg(args, "deliver", "local") or "local").lower().replace("_", "-")
    if raw in {"", "local", "local-only"}:
        return "local", ""
    return "", f"deliver={raw!r} rejected: only local delivery is supported"


def _build_spec(cron_mod: Any, args: Any, controls: dict[str, Any] | None = None) -> Any:
    schedule = _text(_arg(args, "schedule"))
    skills = _skills_arg(_arg(args, "skill"))
    values: dict[str, Any] = {
        "name": cron_mod.PORTAL_JOB_NAME,
        "schedule": schedule,
        "prompt": PROMPT,
        "deliver": "local",
        # Native self continuity: each run receives the previous meaningful
        # archived answer as context. It is evidence only, never authority.
        "context_from": ["self"],
    }
    optional = {
        "model": _control(args, controls, "model"),
        "provider": _control(args, controls, "provider"),
        "reasoning_effort": _control(args, controls, "reasoning_effort"),
        "skills": skills,
        "workdir": _arg(args, "workdir"),
    }
    for key, value in optional.items():
        if isinstance(value, (list, tuple, set)):
            if not value:
                continue
        elif value is None or not _text(value):
            continue
        values[key] = value

    cls = cron_mod.NormalAgentJobSpec
    try:
        allowed = set(inspect.signature(cls).parameters)
    except (TypeError, ValueError):
        allowed = set()
    if not allowed:
        return cls(**values)

    aliases = {"reasoning_effort": ("reasoning_effort", "reasoning"),
               "skills": ("skills", "skill")}
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        target = next((a for a in aliases.get(key, (key,)) if a in allowed), None)
        if target:
            kwargs[target] = value
    missing = {"name", "schedule", "prompt"} - set(kwargs)
    if missing:
        raise ValueError("spec does not accept required fields: " + ", ".join(sorted(missing)))
    return cls(**kwargs)


def _desired(args: Any, controls: dict[str, Any] | None = None) -> dict[str, Any]:
    skills = _skills_arg(_arg(args, "skill"))
    raw = {
        "schedule": _arg(args, "schedule"),
        "model": _control(args, controls, "model"),
        "provider": _control(args, controls, "provider"),
        "reasoning_effort": _control(args, controls, "reasoning_effort"),
        "skill": skills,
        "deliver": "local",
        "workdir": _arg(args, "workdir"),
        # The work contract is reconciled like any other shape field, but it is
        # not a small settings pin and is never echoed in requested/effective pins.
        "prompt": PROMPT,
        # Reconcile native self continuity during preview/enable. This is also a
        # shape field, not an ordinary settings pin.
        "context_from": ["self"],
    }
    return {k: v for k, v in raw.items()
            if (v if isinstance(v, (list, tuple, set)) else _text(v))}


def _assess(job: dict[str, Any], record: dict[str, Any] | None,
            desired: dict[str, Any]) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    unknown: list[str] = []
    for key, wanted in desired.items():
        live = _live(job, key)
        if live is not None:
            if key == "context_from": live = cron._normalize_context_from(live); wanted = cron._normalize_context_from(wanted)
            if _norm(live) != _norm(wanted):
                changed.append(key)
            continue
        if key == "prompt" or key == "context_from":
            # A ledger value is this plugin's own claim, not live readback. When
            # the native listing hides the prompt or context pin, a stale/missing
            # record must never be presented as verified-current, and self
            # context must never be silently fabricated.
            unknown.append(key)
            continue
        recorded = _recorded(record, key)
        if recorded is not None and _norm(recorded) == _norm(wanted):
            continue
        unknown.append(key)
    return changed, unknown


def _effective(job: dict[str, Any] | None, record: dict[str, Any] | None,
               desired: dict[str, Any]) -> dict[str, Any]:
    pins: dict[str, Any] = {}
    for key in PINS:
        live = _live(job, key) if job is not None else None
        if live is not None:
            pins[key] = live
        elif _recorded(record, key) is not None:
            pins[key] = "inherited/unverified"
        elif key in desired:
            pins[key] = "unverified"
        else:
            pins[key] = "inherited/unverified"
    return pins


def _result_status(result: Any) -> tuple[str, str]:
    status = _text(getattr(result, "status", "")) or _text(getattr(result, "action", ""))
    detail = _text(getattr(result, "detail", ""))
    if "partial" in detail.lower():
        status = "partial"
    return status, detail


def _emit(payload: dict[str, Any], as_json: bool) -> int:
    payload.setdefault("limitations", LIMITATIONS)
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        bits = [f"{payload.get('action', 'portal')}: {payload.get('status', 'failed')}"]
        if payload.get("job_id"):
            bits.append("id=" + str(payload["job_id"]))
        if payload.get("detail"):
            bits.append(str(payload["detail"]))
        bits.append("limitations=" + LIMITATIONS)
        print("; ".join(bits))
    return 0 if payload.get("ok") else 1


def _failure(action: str, status: str, detail: str, as_json: bool,
             **extra: Any) -> int:
    payload: dict[str, Any] = {
        "action": action, "status": status, "ok": False, "detail": detail,
    }
    payload.update(extra)
    return _emit(payload, as_json)


def portal_command(args: Any, *, backend: Any = None, native_ctx: Any = None) -> int:
    """Run one portal lifecycle action and print a JSON or short human receipt.

    Preview and enable may read the plugin settings once through
    ``native_ctx.get_config`` to freeze that operation's model controls. Other
    lifecycle actions never read or change native configuration.
    """
    as_json = bool(_arg(args, "json", False) or _arg(args, "as_json", False))
    action = _text(_arg(args, "portal_action")).lower()
    if action not in ACTIONS:
        return _failure(action or "unknown", "error",
                        "portal_action must be one of: " + ", ".join(sorted(ACTIONS)),
                        as_json)
    deliver, problem = _deliver_ok(args)
    if not deliver:
        return _failure(action, "rejected", problem, as_json)
    if action in {"preview", "enable"} and not _text(_arg(args, "schedule")):
        return _failure(action, "error", "a schedule is required for preview/enable",
                        as_json)

    spec = None
    controls = None
    if action in {"preview", "enable"}:
        try:
            controls = _resolve_controls(args, native_ctx)
        except Exception as exc:
            return _failure(action, "error", f"invalid portal controls: {exc}", as_json)
        try:
            spec = _build_spec(cron, args, controls)
            # Same immediate validation for preview and enable, before any
            # backend construction/listing or job/ledger mutation.
            cron.validate_job_spec(spec)
        except Exception as exc:
            return _failure(action, "error", f"invalid job specification: {exc}", as_json)
    try:
        if backend is None:
            backend = cron.HermesCronBackend()
        ownership_path = paths.job_ownership_path()
        ownership = cron.load_job_ownership(ownership_path)
    except Exception as exc:  # backend/config construction is outside our control
        return _failure(action, "error", f"could not prepare portal backend: {exc}", as_json)

    name = cron.PORTAL_JOB_NAME
    retired_removed: list[dict[str, Any]] = []
    if action == "enable":
        # Cut over only the retired portal row recorded as ours; the new name is
        # then reconciled normally below. A failed removal aborts before any
        # mutation rather than leaving two portal jobs armed.
        try:
            retired_removed = cron.reconcile_retired_jobs(
                backend, {name}, ownership=ownership,
                ownership_path=ownership_path,
                retired_names=(cron.RETIRED_PORTAL_JOB_NAME,))
        except Exception as exc:
            return _failure(action, "error",
                            f"could not retire the old portal job: {exc}", as_json,
                            name=name)

    if action in {"enable", "resume"}:
        # An explicit enable/resume must never silently bypass an outstanding
        # global pause. Read-only actions (preview/status/pause/remove) remain
        # usable; the refusal happens before any backend listing or mutation.
        try:
            paused, why = portal_controls.global_pause_outstanding(
                ownership_path, paths.preferences_path())
        except Exception as exc:  # noqa: BLE001 - unknown global state fails closed
            return _failure(action, "error",
                            f"could not verify global pause state: {exc}",
                            as_json, name=name)
        if paused:
            return _failure(
                action, "globally-paused",
                (why or "global pause is active")
                + "; run `hermes secretary resume` before this action",
                as_json, name=name)

    try:
        jobs = list(backend.list_jobs())
    except Exception as exc:
        return _failure(action, "error", f"could not list cron jobs: {exc}", as_json)
    kind, job, record, why = _locate(cron, jobs, ownership, name)
    job_id = _job_id(job) if job is not None else _text((record or {}).get("id"))
    desired = _desired(args, controls) if spec is not None else {}

    if action in {"preview", "status"}:
        payload: dict[str, Any] = {
            "action": action, "name": name, "job_id": job_id,
            "effective_pins": _effective(job, record, desired),
        }
        if action == "preview":
            payload["requested_pins"] = {
                key: value for key, value in desired.items() if key in PINS
            }
        if kind == "owned":
            status, unknown = _assess(job, record, desired) if action == "preview" else ([], [])
            state = _state(job or {})
            payload.update(ok=True, status=(
                "inconclusive" if unknown else
                ("would-update" if status else "unchanged")
            ) if action == "preview" else state if state != "unknown" else "present")
            payload["state"] = state
            if unknown:
                payload["ok"] = False
                payload["detail"] = ("inconclusive: live readback does not expose "
                                     + ", ".join(unknown) + "; refusing to infer change")
                return _emit(payload, as_json)
            return _emit(payload, as_json)
        if kind == "foreign":
            return _failure(action, "foreign", why, as_json, name=name, job_id=job_id,
                            effective_pins=payload["effective_pins"])
        if kind == "mismatch":
            return _failure(action, "mismatch", why, as_json, name=name, job_id=job_id)
        payload.update(ok=True, status="would-create" if action == "preview" else "missing",
                       detail=why)
        return _emit(payload, as_json)

    if action == "enable":
        if kind == "foreign":
            return _failure(action, "foreign", why, as_json, name=name, job_id=job_id)
        if kind == "owned":
            changed, unknown = _assess(job or {}, record, desired)
            if unknown:
                return _failure(
                    action, "inconclusive",
                    "live readback does not expose " + ", ".join(unknown)
                    + "; refusing to recreate blindly", as_json,
                    name=name, job_id=job_id,
                    effective_pins=_effective(job, record, desired),
                )
        try:
            result = cron.ensure_normal_agent_job(
                backend, spec, ownership=ownership, ownership_path=ownership_path)
            status, detail = _result_status(result)
            jobs_after = list(backend.list_jobs())
            kind2, job2, record2, why2 = _locate(cron, jobs_after, ownership, name)
            payload = {
                "action": action, "name": name, "ok": kind2 == "owned",
                "status": "partial" if "partial" in detail.lower() else status or "enabled",
                "detail": detail or why2,
                "job_id": _job_id(job2) if job2 is not None else job_id,
                "effective_pins": _effective(job2, record2, desired),
                "state": _state(job2 or {}) if job2 is not None else "unknown",
            }
            if retired_removed:
                payload["retired"] = retired_removed
            if payload["status"] == "already-present" and payload["state"] == "paused":
                payload["detail"] = (payload["detail"] + "; " if payload["detail"] else "") + \
                    "existing owned paused state left paused"
            return _emit(payload, as_json)
        except Exception as exc:
            return _failure(action, "error", f"enable failed: {exc}", as_json, name=name)

    if action in {"pause", "resume"}:
        if kind != "owned":
            return _failure(action, kind, why or "owned exact id/name required",
                            as_json, name=name, job_id=job_id)
        exact_id = _job_id(job or {})
        if not exact_id or exact_id != _text((record or {}).get("id")):
            return _failure(action, "mismatch",
                            "live id/name does not match the ledger; refusing mutation",
                            as_json, name=name, job_id=job_id)
        method_name = "pause_job" if action == "pause" else "resume_job"
        method = getattr(backend, method_name, None)
        if not callable(method):
            return _failure(action, "unsupported",
                            f"backend has no {method_name}", as_json, name=name, job_id=exact_id)
        try:
            method(exact_id)
            after = list(backend.list_jobs())
            row = next((j for j in after if _job_id(j) == exact_id), None)
            if row is None:
                return _failure(action, "unverified",
                                "job disappeared from readback; no deletion performed",
                                as_json, name=name, job_id=exact_id)
            state = _state(row)
            expected = "paused" if action == "pause" else "active"
            if state != expected:
                return _failure(action, "unverified",
                                f"readback state={state!r}, expected {expected!r}",
                                as_json, name=name, job_id=exact_id)
            return _emit({"action": action, "name": name, "job_id": exact_id,
                          "ok": True, "status": expected, "state": state}, as_json)
        except Exception as exc:
            return _failure(action, "error", f"{method_name} failed: {exc}", as_json,
                            name=name, job_id=exact_id)

    # remove
    if kind != "owned":
        return _failure(action, kind, why or "foreign/mismatched jobs are never removed",
                        as_json, name=name, job_id=job_id)
    if not job_id or job_id != _text((record or {}).get("id")):
        return _failure(action, "mismatch",
                        "exact recorded id/name not verified; refusing removal",
                        as_json, name=name, job_id=job_id)
    try:
        result = cron.remove_job(backend, name, ownership=ownership,
                                 ownership_path=ownership_path)
        status, detail = _result_status(result)
        return _emit({"action": action, "name": name, "job_id": job_id,
                      "ok": status == "removed", "status": status or "unknown",
                      "detail": detail}, as_json)
    except Exception as exc:
        return _failure(action, "error", f"remove failed: {exc}", as_json,
                        name=name, job_id=job_id)
