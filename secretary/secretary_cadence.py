"""Portal cadence: plugin-owned elapsed interval plus the native wake gate.

Why this exists
---------------
The host's cron editor clears ``pending_slot`` and the quota hold whenever a
schedule is rewritten, and it exposes no conditional (compare-and-set) edit, so
a plugin cannot reschedule the Portal job without risking scheduled work. The
gate keeps the *job* cheap and stable and moves the cadence into plugin state:

* the job ticks on a fixed 15-minute expression and runs this plugin's gate as
  its pre-run script;
* the gate prints ``{"wakeAgent": false}`` until the owner's interval has
  elapsed since the last real wake, so no agent (and no tokens) start early;
* changing the cadence is an atomic write to a small owner-only state file, so
  job id, history, continuity, retries and quota holds are never touched.

Trade-off, stated plainly: spacing is accurate to one tick, not to the second.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # package import
    from .. import paths
except ImportError:  # direct/standalone import
    import paths  # type: ignore

SCHEMA = 1
TICK_EXPR = "*/15 * * * *"
GATE_RUNNER = "proactive_secretary_portal_gate.py"
MIN_MINUTES = 30
MAX_MINUTES = 1440
DEFAULT_MINUTES = 180
STATE_DIR = "cadence"
STATE_NAME = "portal.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def plan_path() -> Path:
    return Path(paths.data_root()) / STATE_DIR / STATE_NAME


def parse_interval(text: Any) -> int:
    """Accept ``90``/``90m``/``6h``; bare numbers are minutes."""
    if isinstance(text, bool):
        raise ValueError("cadence must be a number of minutes or a duration like 6h")
    if isinstance(text, int):
        minutes = text
    else:
        raw = str(text or "").strip().lower()
        if not raw:
            raise ValueError("cadence must not be empty")
        unit = raw[-1]
        try:
            if unit == "h":
                minutes = int(round(float(raw[:-1]) * 60))
            elif unit == "m":
                minutes = int(round(float(raw[:-1])))
            else:
                minutes = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "cadence must be a whole number of minutes or a duration like 6h") from exc
    if minutes < MIN_MINUTES or minutes > MAX_MINUTES:
        raise ValueError(
            f"cadence must be between {MIN_MINUTES} and {MAX_MINUTES} minutes")
    return minutes


def _default_plan() -> dict[str, Any]:
    return {"ok": True, "present": False, "schema": SCHEMA,
            "interval_minutes": DEFAULT_MINUTES, "anchor": "", "last_wake": "",
            "revision": 0}


def read_plan() -> dict[str, Any]:
    """Read the plan. A corrupt or foreign file is reported, never overwritten."""
    path = plan_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default_plan()
    except OSError as exc:
        return {"ok": False, "error": f"cadence store unreadable: {type(exc).__name__}"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "cadence store is not valid JSON"}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {"ok": False, "error": "cadence store has an unknown schema"}
    try:
        interval = parse_interval(data.get("interval_minutes"))
    except ValueError as exc:
        return {"ok": False, "error": f"cadence store is invalid: {exc}"}
    plan = _default_plan()
    plan.update({
        "present": True,
        "interval_minutes": interval,
        "anchor": str(data.get("anchor") or ""),
        "last_wake": str(data.get("last_wake") or ""),
        "revision": int(data.get("revision") or 0),
    })
    return plan


def _write(plan: dict[str, Any]) -> None:
    path = plan_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {key: plan[key] for key in
               ("schema", "interval_minutes", "anchor", "last_wake", "revision")}
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".portal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_plan(minutes: Any) -> dict[str, Any]:
    """Set the interval. Idempotent when the value is unchanged."""
    interval = parse_interval(minutes)
    current = read_plan()
    if not current.get("ok"):
        raise ValueError(current.get("error") or "cadence store is unreadable")
    changed = current.get("interval_minutes") != interval or not current.get("present")
    if not changed:
        return {**current, "changed": False}
    now = _now()
    plan = {
        "ok": True, "present": True, "schema": SCHEMA, "interval_minutes": interval,
        "anchor": current.get("anchor") or _iso(now),
        "last_wake": current.get("last_wake") or "",
        "revision": int(current.get("revision") or 0) + 1,
    }
    _write(plan)
    return {**plan, "changed": True}


def is_due(plan: dict[str, Any], now: datetime | None = None) -> tuple[bool, int]:
    """Return ``(due, remaining_minutes)`` for the stored interval."""
    if not plan.get("ok", True):
        return True, 0
    interval = int(plan.get("interval_minutes") or DEFAULT_MINUTES)
    last = _parse_iso(plan.get("last_wake"))
    moment = now or _now()
    if last is None:
        return True, 0
    elapsed = (moment - last).total_seconds() / 60.0
    if elapsed >= interval:
        return True, 0
    return False, max(1, int(round(interval - elapsed)))


def stamp_wake(plan: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Record a real wake. Concurrent callers cannot double-stamp one interval."""
    fresh = read_plan()
    if not fresh.get("ok"):
        return fresh
    moment = now or _now()
    due, _remaining = is_due(fresh, moment)
    if not due:
        return {**fresh, "stamped": False}
    stamped = {
        "ok": True, "present": True, "schema": SCHEMA,
        "interval_minutes": int(fresh.get("interval_minutes") or DEFAULT_MINUTES),
        "anchor": fresh.get("anchor") or _iso(moment),
        "last_wake": _iso(moment),
        "revision": int(fresh.get("revision") or 0) + 1,
    }
    _write(stamped)
    return {**stamped, "stamped": True}


def next_due_iso(plan: dict[str, Any]) -> str:
    last = _parse_iso(plan.get("last_wake"))
    if last is None:
        return ""
    interval = int(plan.get("interval_minutes") or DEFAULT_MINUTES)
    return _iso(last + timedelta(minutes=interval))


def gated_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Job-spec fields that turn the Portal job into a gated tick."""
    return {"schedule": TICK_EXPR, "script": GATE_RUNNER}


_BUSY_KEYS = ("pending_slot", "quota_hold_until", "retry_at", "next_attempt")


def _row_state(row: dict[str, Any]) -> str:
    for key in ("state", "status"):
        value = str(row.get(key) or "").strip().lower()
        if value in {"paused", "disabled", "stopped"}:
            return "paused"
        if value in {"active", "enabled", "running", "scheduled"}:
            return "active"
    paused = row.get("paused")
    if isinstance(paused, bool):
        return "paused" if paused else "active"
    enabled = row.get("enabled")
    if isinstance(enabled, bool):
        return "active" if enabled else "paused"
    return "unknown"


def blocked_reason(rows: list[dict[str, Any]]) -> str:
    """Empty string when a conversion is safe; otherwise a plain refusal."""
    if not rows:
        return "no owned Portal job is present"
    if len(rows) > 1:
        return "more than one job uses the Portal name"
    row = rows[0]
    state = _row_state(row)
    if state != "active":
        return f"the Portal job is {state}; resume it before changing cadence"
    for key in _BUSY_KEYS:
        if row.get(key):
            return (f"the Portal job holds {key}; wait for it to clear so no "
                    "scheduled work or retry is discarded")
    return ""


def _expr(schedule: Any) -> str:
    if isinstance(schedule, dict):
        return str(schedule.get("expr") or schedule.get("display") or "")
    return str(schedule or "")


def convert(backend: Any, minutes: Any, *, name: str | None = None,
            apply_state: bool = True) -> dict[str, Any]:
    """Make the Portal job a gated 15-minute tick with the requested interval.

    Refuses (no mutation at all) when the job is missing, duplicated, paused or
    holding scheduled/retry state. The job id and every unrelated field are
    preserved; the only writes are ``schedule`` and ``script``.
    """
    if name is None:
        try:
            from .. import cron as cron_mod
            name = cron_mod.PORTAL_JOB_NAME
        except Exception:  # noqa: BLE001
            name = "hermes-proactive-secretary-portal"
    interval = parse_interval(minutes)
    rows = [row for row in backend.list_jobs() if str(row.get("name") or "") == name]
    reason = blocked_reason(rows)
    if reason:
        return {"ok": False, "status": "refused", "error": reason, "job": name}
    row = rows[0]
    job_id = str(row.get("id") or "")
    if not job_id:
        return {"ok": False, "status": "refused", "error": "the Portal job has no id",
                "job": name}
    before_context = row.get("context_from")

    plan = read_plan()
    if not plan.get("ok"):
        return {"ok": False, "status": "refused", "error": plan.get("error"),
                "job": name}

    if apply_state:
        try:
            from .. import runner_install
        except ImportError:  # direct/standalone import
            import runner_install  # type: ignore
        try:
            runner_install.install_runners(
                plugin_root=paths.plugin_root(),
                template_root=paths.plugin_root() / "runners",
                scripts_dir=paths.scripts_dir(),
                ownership_path=paths.job_ownership_path(),
                include_gate=True,
            )
        except Exception as exc:  # noqa: BLE001 - a missing gate would fail every tick
            return {"ok": False, "status": "refused",
                    "error": f"the wake gate could not be installed ({type(exc).__name__})",
                    "job": name}
        plan = write_plan(interval)

    try:
        from cron import jobs as native_jobs  # host module, same profile
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "unavailable",
                "error": f"native cron API is unavailable ({type(exc).__name__})",
                "job": name}
    try:
        native_jobs.update_job(job_id, gated_fields(plan))
    except Exception as exc:  # noqa: BLE001 - report, never retry blindly
        return {"ok": False, "status": "error",
                "error": f"schedule update failed ({type(exc).__name__}: {exc})",
                "job": name, "job_id": job_id}

    after = [row for row in backend.list_jobs() if str(row.get("id") or "") == job_id]
    if len(after) != 1:
        return {"ok": False, "status": "unverified",
                "error": "the job could not be re-read after the change",
                "job": name, "job_id": job_id}
    view = after[0]
    if _expr(view.get("schedule")) != TICK_EXPR or _row_script(view) != GATE_RUNNER:
        return {"ok": False, "status": "unverified",
                "error": "readback does not show the gated schedule and script",
                "job": name, "job_id": job_id}
    if view.get("context_from") != before_context:
        return {"ok": False, "status": "unverified",
                "error": "continuity setting changed during the update",
                "job": name, "job_id": job_id}
    return {"ok": True, "status": "gated", "job": name, "job_id": job_id,
            "interval_minutes": interval, "next_due": next_due_iso(plan),
            "revision": int(plan.get("revision") or 0)}


def _row_script(row: dict[str, Any]) -> str:
    value = row.get("script")
    if not value and isinstance(row.get("mode"), dict):
        value = row["mode"].get("script")
    return Path(str(value or "")).name


def effective(backend: Any, job_name: str | None = None, *, name: str | None = None) -> dict[str, Any]:
    """Read-only effective view: stored interval vs what the live job does."""
    name = name or job_name
    if name is None:
        try:
            from .. import cron as cron_mod  # noqa: WPS433 - light alias only
            name = cron_mod.PORTAL_JOB_NAME
        except Exception:  # noqa: BLE001 - caller may pass the name
            name = "hermes-proactive-secretary-portal"
    plan = read_plan()
    rows = [row for row in backend.list_jobs() if str(row.get("name") or "") == name]
    row = rows[0] if len(rows) == 1 else {}
    schedule = row.get("schedule")
    if isinstance(schedule, dict):
        schedule = schedule.get("expr") or schedule.get("display") or ""
    gated = (len(rows) == 1
             and str(schedule or "") == TICK_EXPR
             and _row_script(row) == GATE_RUNNER)
    return {
        "ok": plan.get("ok", True),
        "error": plan.get("error", ""),
        "job": name,
        "rows": len(rows),
        "interval_minutes": int(plan.get("interval_minutes") or DEFAULT_MINUTES),
        "anchor": plan.get("anchor", ""),
        "last_wake": plan.get("last_wake", ""),
        "next_due": next_due_iso(plan),
        "revision": int(plan.get("revision") or 0),
        "job_schedule": str(schedule or ""),
        "job_script": _row_script(row),
        "gated": bool(gated),
    }
