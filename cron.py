"""Thin, testable wrapper over the Hermes native cron surface.

The plugin owns two default jobs (a 15-minute monitoring job and a 1-minute
requested-reminder dispatcher) plus an explicit opt-in preparation script job
that is never armed merely by enabling preparation. This module speaks to the
installed ``hermes cron`` CLI through a small :class:`CronBackend` protocol so
the CLI logic can be unit tested without a live scheduler.

Real surface (parent-verified on the target host; overridable per verb via env
for a differently spelled release)::

    hermes cron create <schedule> [prompt] [--name NAME] [--script SCRIPT]
                       [--no-agent] [--deliver TARGET] [--workdir DIR]
    hermes cron list [--all]        # TEXT OUTPUT ONLY -- no --json flag
    hermes cron remove <job_id>     # POSITIONAL job id, not a name

The schedule is positional. Script jobs use ``--script`` (a filename under the
profile's scripts directory) together with ``--no-agent``; there is no
``--command`` flag and no JSON output.

A normal-agent job is the same native create verb *without* ``--script`` or
``--no-agent``: the prompt is the positional argument after the schedule, and
optional native pins are passed as separate argv tokens using the supported
spellings ``--model``, ``--provider``, ``--reasoning-effort``, ``--skill``
(repeatable), ``--workdir`` and ``--deliver``. Omitting a pin inherits the host's
normal native agent configuration; this module never invents a global fallback
for it.

Hard rules:
- The backend is only touched from explicit user commands (``setup`` /
  ``disable`` / ``uninstall``), never at import or ``register()`` time.
- Every mutation is verified by listing jobs back; a claimed removal that is
  still present is reported as a failure, never silently swallowed.
- Nothing here installs, enables, or publishes anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

PILOT_SCRIPT = "proactive_portal_runner.py"
MONITORING_SCRIPT = PILOT_SCRIPT
REMINDER_SCRIPT = "proactive_portal_reminder_runner.py"
PREPARATION_SCRIPT = "proactive_portal_prepare_runner.py"

# Namespaced to this plugin so a pre-existing unrelated job of a generic name can
# never be matched (and therefore never removed) by accident.
MONITORING_JOB_NAME = "hermes-proactive-portal-monitoring"
LEGACY_PILOT_JOB_NAME = "hermes-proactive-secretary-pilot"
# Backwards-compatible source symbol only: no new owner-facing job uses it.
PILOT_JOB_NAME = MONITORING_JOB_NAME
REMINDER_JOB_NAME = "hermes-proactive-portal-reminders"
PREPARATION_JOB_NAME = "hermes-proactive-portal-preparation"
PORTAL_JOB_NAME = "hermes-proactive-portal-portal"
# Retired owner-facing names from before the portal identity. Only an exact
# ownership-ledger id/name match can be removed; a foreign same-named row is
# always left untouched. The legacy ``-pilot`` name keeps its dedicated
# in-place rename path above.
RETIRED_PORTAL_JOB_NAME = "hermes-proactive-secretary-portal"
RETIRED_JOB_NAMES: tuple[str, ...] = (
    "hermes-proactive-secretary-monitoring",
    "hermes-proactive-secretary-reminders",
    "hermes-proactive-secretary-preparation",
    RETIRED_PORTAL_JOB_NAME,
)
OWNED_JOB_NAMES: tuple[str, ...] = (MONITORING_JOB_NAME, REMINDER_JOB_NAME)
LEGACY_OWNED_JOB_NAMES: tuple[str, ...] = (LEGACY_PILOT_JOB_NAME,)
OPTIONAL_JOB_NAMES: tuple[str, ...] = (PREPARATION_JOB_NAME,)
KNOWN_JOB_NAMES: tuple[str, ...] = (
    MONITORING_JOB_NAME, REMINDER_JOB_NAME, PREPARATION_JOB_NAME,
    PORTAL_JOB_NAME, LEGACY_PILOT_JOB_NAME,
)
OWNED_PLUGIN = "hermes-proactive-portal"
JOB_OWNERSHIP_SCHEMA = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_profile_path(left: Path, right: Path) -> bool:
    """Compare two home paths without requiring either to exist."""
    def key(value: Path) -> str:
        try:
            return os.path.normcase(
                str(Path(value).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError):
            return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))

    return key(left) == key(right)


class CronError(RuntimeError):
    """The Hermes cron surface could not be reached or used safely."""


def _load_served_profile_child_env():
    """Return the native served-profile child-env builder (lazy import)."""
    from tools.environments.local import served_profile_child_env
    return served_profile_child_env


@dataclass
class CronJobSpec:
    """Declarative description of one native cron job this plugin owns.

    Script jobs set ``script`` and leave ``prompt`` empty. Normal-agent jobs set
    ``prompt`` and leave ``script`` empty. The optional native pins below are
    only emitted when explicitly supplied; an empty value means "inherit the
    host's normal native configuration", not "resolve to some plugin default".
    """

    name: str
    schedule: str
    script: str = ""
    deliver: str = "local"
    workdir: str = ""
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    model: str = ""
    provider: str = ""
    reasoning: str = ""
    skills: list[str] = field(default_factory=list)
    context_from: list[str] = field(default_factory=list)


@dataclass
class NormalAgentJobSpec(CronJobSpec):
    """Backward-compatible explicit lane for a native normal-agent job.

    ``script`` must stay empty: this spec can never silently turn into the
    legacy ``--no-agent`` script lane. Delivery defaults to empty here so an
    omitted value inherits the host's native delivery instead of this plugin
    inventing ``local``.
    """

    deliver: str = ""


# A descriptive alias for callers that spell the lane out in full.
NormalAgentCronJobSpec = NormalAgentJobSpec


@dataclass
class CronJobResult:
    name: str
    action: str  # created | already-present | updated | removed | missing
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "action": self.action, "detail": self.detail}


class CronBackend(Protocol):
    def is_available(self) -> bool: ...
    def list_jobs(self) -> list[dict[str, Any]]: ...
    def add_job(self, spec: CronJobSpec) -> dict[str, Any]: ...
    def remove_job(self, job_id: str) -> dict[str, Any]: ...
    def rename_job(self, job_id: str, name: str) -> dict[str, Any]: ...
    def pause_job(self, job_id: str) -> dict[str, Any]: ...
    def resume_job(self, job_id: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Text parsing (``hermes cron list`` prints a table of job blocks)
# --------------------------------------------------------------------------- #

_JOB_HEADER = re.compile(r"^\s*(\S+)\s+\[(\w+)\]\s*$")
_FIELD = re.compile(r"^\s+([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")
_FIELD_KEYS = {
    "name": "name",
    "schedule": "schedule",
    "next run": "next_run",
    "deliver": "deliver",
    "script": "script",
    "mode": "mode",
    "workdir": "workdir",
    "last run": "last_run",
    # Normal-agent detail is only compared when the native text output actually
    # exposes it; absent fields are reported as bounded verification gaps.
    "prompt": "prompt",
    "model": "model",
    "provider": "provider",
    "reasoning": "reasoning",
    "reasoning effort": "reasoning",
    "skills": "skills",
    "context from": "context_from",
}


def parse_cron_list(text: str) -> list[dict[str, Any]]:
    """Parse the text table printed by ``hermes cron list`` into job dicts.

    Each block is headed by ``<job_id> [active|paused]`` followed by indented
    ``Field: value`` lines. Unrecognised text is ignored rather than guessed at.
    """
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        header = _JOB_HEADER.match(line)
        if header:
            if current is not None:
                jobs.append(current)
            current = {"id": header.group(1), "state": header.group(2)}
            continue
        if current is None:
            continue
        match = _FIELD.match(line)
        if not match:
            continue
        mapped = _FIELD_KEYS.get(match.group(1).strip().lower())
        if mapped:
            current[mapped] = match.group(2).strip()
    if current is not None:
        jobs.append(current)
    return jobs


def _job_id(job: dict[str, Any]) -> str:
    return str(job.get("id") or job.get("job_id") or "")


def _find_job(jobs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((j for j in jobs if str(j.get("name") or "") == name), None)


def _is_agent_spec(spec: CronJobSpec) -> bool:
    """True only for the normal-agent lane (a real prompt, no script)."""
    return bool(str(getattr(spec, "prompt", "") or "").strip())


def _mode_is_agent(mode: str) -> bool:
    text = str(mode or "").strip().lower()
    return bool(text) and "no-agent" not in text and "agent" in text


def _normalize_skills(value: Any) -> list[str]:
    """Normalize a skills pin to an ordered list without inventing values."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


def _normalize_context_from(value: Any) -> list[str]:
    """Normalize a native ``context_from`` pin to an ordered list of ids.

    A single string is one reference (never comma-split, because a job id may
    contain punctuation); list/tuple entries are stripped and blank entries
    dropped. Missing values mean native inheritance (no context pin).
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


def _same_agent_field(job: dict[str, Any], field_name: str, expected: Any,
                      *, skills: bool = False) -> bool:
    """Compare one field only when the native text row exposes it."""
    if field_name not in job:
        return True
    actual = job.get(field_name)
    if skills:
        return _normalize_skills(actual) == _normalize_skills(expected)
    return str(actual or "") == str(expected or "")


def _same_job(job: dict[str, Any], spec: CronJobSpec) -> bool:
    if str(job.get("schedule") or "") != spec.schedule:
        return False
    if _is_agent_spec(spec):
        # A normal-agent row must never be mistaken for, or silently treated as,
        # the script/no-agent lane.
        if str(job.get("script") or ""):
            return False
        mode = str(job.get("mode") or "")
        if mode and not _mode_is_agent(mode):
            return False
        if not _same_agent_field(job, "prompt", spec.prompt):
            return False
        if not _same_agent_field(job, "model", spec.model):
            return False
        if not _same_agent_field(job, "provider", spec.provider):
            return False
        if not _same_agent_field(job, "reasoning", spec.reasoning):
            return False
        if not _same_agent_field(job, "skills", spec.skills, skills=True):
            return False
        if "context_from" in job and (
                _normalize_context_from(job.get("context_from"))
                != _normalize_context_from(getattr(spec, "context_from", []))):
            return False
        if spec.deliver and str(job.get("deliver") or "") != spec.deliver:
            return False
        if spec.workdir and str(job.get("workdir") or "") != spec.workdir:
            return False
        return True
    if str(job.get("script") or "") != spec.script:
        return False
    if str(job.get("deliver") or "") != spec.deliver:
        return False
    if spec.workdir and str(job.get("workdir") or "") != spec.workdir:
        return False
    return True


_UNSAFE_ARGV = re.compile(r"[\x00-\x1f\x7f]")


def _validate_text(label: str, value: Any, *, required: bool) -> str:
    if not isinstance(value, str):
        raise CronError(f"{label} must be a string")
    text = value
    if required and not text.strip():
        raise CronError(f"{label} must not be empty")
    if _UNSAFE_ARGV.search(text):
        raise CronError(f"{label} contains control characters/newlines; refusing argv injection")
    if text.startswith("-"):
        raise CronError(f"{label} must not start with '-' (would be parsed as a flag)")
    return text


def validate_job_spec(spec: CronJobSpec) -> None:
    """Reject empty, malformed, injected, or ambiguous script/agent specs.

    Validation happens before any backend call, so a rejected spec can never
    create or remove a native job.
    """
    _validate_text("job name", getattr(spec, "name", ""), required=True)
    _validate_text("schedule", getattr(spec, "schedule", ""), required=True)
    script = getattr(spec, "script", "") or ""
    prompt = getattr(spec, "prompt", "") or ""
    model = getattr(spec, "model", "") or ""
    provider = getattr(spec, "provider", "") or ""
    reasoning = getattr(spec, "reasoning", "") or ""
    raw_skills = getattr(spec, "skills", [])
    if not isinstance(raw_skills, (str, list, tuple)):
        raise CronError("skills must be a string or list of strings")
    if isinstance(raw_skills, (list, tuple)) and any(
            not isinstance(item, str) for item in raw_skills):
        raise CronError("skills must be a list of strings")
    skills = _normalize_skills(raw_skills)
    raw_context = getattr(spec, "context_from", [])
    if not isinstance(raw_context, (str, list, tuple)):
        raise CronError("context_from must be a string or list of strings")
    if isinstance(raw_context, (list, tuple)) and any(
            not isinstance(item, str) for item in raw_context):
        raise CronError("context_from must be a list of strings")
    context_from = _normalize_context_from(raw_context)
    unsupported_context = [ref for ref in context_from if ref != "self"]
    if unsupported_context:
        raise CronError(
            "this backend can only express native self context via --continuity; "
            "unsupported context_from reference(s): "
            + ", ".join(unsupported_context)
        )
    agent_pins = bool(model or provider or reasoning or skills)

    if script:
        _validate_text("script", script, required=True)
        if prompt or agent_pins or context_from:
            raise CronError(
                "ambiguous cron job spec: script/--no-agent and normal-agent "
                "prompt/native pins cannot be combined"
            )
    else:
        if prompt:
            _validate_text("prompt", prompt, required=True)
        elif agent_pins or context_from:
            raise CronError("normal-agent native pins require a non-empty prompt")
        else:
            raise CronError("cron job spec needs either a script or a normal-agent prompt")
        for label, value in (
            ("model", model), ("provider", provider), ("reasoning", reasoning),
        ):
            if value:
                _validate_text(label, value, required=False)
        for skill in skills:
            _validate_text("skills entry", skill, required=True)
        for reference in context_from:
            _validate_text("context_from entry", reference, required=True)

    deliver = getattr(spec, "deliver", "") or ""
    if deliver:
        _validate_text("deliver", deliver, required=False)
    workdir = getattr(spec, "workdir", "") or ""
    if workdir:
        _validate_text("workdir", workdir, required=False)


# --------------------------------------------------------------------------- #
# Job-ownership ledger (names are NOT proof of ownership)
# --------------------------------------------------------------------------- #

def load_job_ownership(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load the plugin's own record of the jobs it created.

    The record maps the plugin's job name -> ``{id, name, created_at, ...}``. It
    is the only authority for removal; a same-named job absent from the ledger is
    treated as foreign and is never adopted, overwritten, or removed.
    """
    data: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    return {"schema": JOB_OWNERSHIP_SCHEMA, "plugin": OWNED_PLUGIN, "jobs": jobs}


def save_job_ownership(path: str | os.PathLike[str] | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)


def owned_job_record(ownership: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(ownership, dict):
        return None
    record = (ownership.get("jobs") or {}).get(name)
    return record if isinstance(record, dict) else None


def record_owned_job(ownership: dict[str, Any], spec: CronJobSpec, job: dict[str, Any],
                     created_at: str | None = None) -> dict[str, Any]:
    record = {
        "id": _job_id(job),
        "name": spec.name,
        "created_at": created_at or _now_iso(),
        "schedule": spec.schedule,
        "script": spec.script,
        "prompt": getattr(spec, "prompt", "") or "",
        "deliver": spec.deliver,
        "workdir": spec.workdir,
        "mode": str(job.get("mode") or ("agent" if _is_agent_spec(spec) else "no-agent")),
    }
    if _is_agent_spec(spec):
        record.update({
            "model": getattr(spec, "model", "") or "",
            "provider": getattr(spec, "provider", "") or "",
            "reasoning": getattr(spec, "reasoning", "") or "",
            "skills": _normalize_skills(getattr(spec, "skills", [])),
            "context_from": _normalize_context_from(getattr(spec, "context_from", [])),
        })
    ownership.setdefault("jobs", {})[spec.name] = record
    return record



# --------------------------------------------------------------------------- #
# Default backend: shell out to ``hermes cron``
# --------------------------------------------------------------------------- #

class HermesCronBackend:
    """Default backend: shell out to ``hermes cron`` using the real verb shape.

    The three verbs default to ``create`` / ``list`` / ``remove`` and may be
    overridden (for a differently spelled release) with
    ``HERMES_CRON_CREATE_VERB`` / ``HERMES_CRON_LIST_VERB`` /
    ``HERMES_CRON_REMOVE_VERB``. ``HERMES_BIN`` overrides the executable.
    """

    def __init__(
        self,
        hermes_bin: str | None = None,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        timeout: int = 60,
        create_verb: str | None = None,
        list_verb: str | None = None,
        remove_verb: str | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin or os.environ.get("HERMES_BIN") or "hermes"
        self._runner = runner
        self.timeout = timeout
        self.create_verb = create_verb or os.environ.get("HERMES_CRON_CREATE_VERB") or "create"
        self.list_verb = list_verb or os.environ.get("HERMES_CRON_LIST_VERB") or "list"
        self.remove_verb = remove_verb or os.environ.get("HERMES_CRON_REMOVE_VERB") or "remove"

    def _subprocess_env(self) -> dict[str, str]:
        """Request-owned child env bound to the active profile home.

        The native SDK builds the env for a process acting for the active
        (possibly served) profile. For a routed request it drops the launch
        profile's ``.env`` residue and bridged ``TERMINAL_*`` values and scrubs
        every provider/tool credential, then pins the target home and applies
        the SDK's own ``HOME``/``TMPDIR`` semantics. Cron metadata needs no
        provider credentials, so credentials are explicitly not inherited.

        The plugin's data override belongs only to a child serving the same
        home as this process; it is never leaked to a routed foreign profile.
        A missing or failing native helper aborts before any spawn.
        """
        try:
            from proactive_pilot import ownerpaths
        except Exception as exc:  # noqa: BLE001 - cannot prove the profile binding
            raise CronError(
                f"cannot load the active profile resolver: {exc}") from exc
        try:
            active = Path(ownerpaths.hermes_home())
            launch = Path(ownerpaths.launch_home())
        except Exception as exc:  # noqa: BLE001 - fail closed, never read launch A
            raise CronError(
                f"cannot resolve the active Hermes profile for cron: {exc}") from exc
        try:
            build_env = _load_served_profile_child_env()
            env = build_env(target_home=str(active), inherit_credentials=False)
        except Exception as exc:  # noqa: BLE001 - never fall back to launch env
            raise CronError(
                f"cannot build a sanitized cron child environment: {exc}") from exc
        if not isinstance(env, dict):
            raise CronError(
                "native served_profile_child_env returned an unexpected shape")
        if _same_profile_path(active, launch):
            data_root = os.environ.get(ownerpaths.DATA_ENV)
            if data_root is not None:
                env[ownerpaths.DATA_ENV] = data_root
        else:
            env.pop(ownerpaths.DATA_ENV, None)
        return env

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(argv)
        env = self._subprocess_env()
        return subprocess.run(
            argv, shell=False, capture_output=True, text=True,
            timeout=self.timeout, check=False, env=env,
        )

    def _cron_argv(self, *args: str) -> list[str]:
        return [self.hermes_bin, "cron", *args]

    def is_available(self) -> bool:
        try:
            proc = self._run(self._cron_argv("--help"))
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def list_jobs(self) -> list[dict[str, Any]]:
        try:
            proc = self._run(self._cron_argv(self.list_verb, "--all"))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot list cron jobs: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(f"cron list failed (exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}")
        return parse_cron_list(proc.stdout)

    def list_jobs_for_status(self) -> list[dict[str, Any]]:
        """Read jobs in-process through the native read-only cron API.

        Status is a projection, not a scheduler command. Spawning another
        managed runtime here can start a dependency installer under a
        synthetic/empty profile, so status prefers this in-process read. The
        native ``cron.jobs.list_jobs`` already normalizes ``load_jobs`` and is
        read-only; it is never used by ``ensure_job``/``remove_job``. When the
        native API is missing or cannot be proven to belong to the active
        profile, raise :class:`CronError` so callers keep the honest
        recorded-but-unverified state instead of falling back to the CLI.
        """
        try:
            from cron import jobs as native_jobs
        except Exception as exc:  # noqa: BLE001 - absent/shadowed SDK degrades
            raise CronError(f"native cron listing is unavailable: {exc}") from exc
        reader = getattr(native_jobs, "list_jobs", None)
        if not callable(reader):
            raise CronError("native cron.jobs.list_jobs is unavailable")
        try:
            from hermes_constants import get_hermes_home
        except Exception as exc:  # noqa: BLE001 - cannot prove the profile binding
            raise CronError(f"native profile binding is unavailable: {exc}") from exc
        expected = os.environ.get("HERMES_HOME")
        if expected:
            try:
                bound = Path(get_hermes_home()).resolve()
            except Exception as exc:  # noqa: BLE001 - binding cannot be verified
                raise CronError(f"native profile binding is unavailable: {exc}") from exc
            if bound != Path(expected).resolve():
                raise CronError("native cron API is not bound to the active profile")
        try:
            try:
                rows = reader(include_disabled=True)
            except TypeError:
                rows = reader()
        except Exception as exc:  # noqa: BLE001 - recorded/unverified is honest
            raise CronError(f"native cron listing failed: {exc}") from exc
        if not isinstance(rows, (list, tuple)):
            raise CronError("native cron listing returned an unexpected shape")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["id"] = str(row.get("id") or row.get("job_id") or "")
            item["name"] = str(row.get("name") or "")
            if not item.get("state"):
                item["state"] = "active" if row.get("enabled", True) else "paused"
            if not item.get("next_run"):
                item["next_run"] = str(
                    row.get("next") or row.get("next_run_at") or "")
            normalized.append(item)
        return normalized

    def rename_job(self, job_id: str, name: str) -> dict[str, Any]:
        """Rename one job in place through the native update API.

        ``hermes cron`` has no rename verb. The supported host seam is the
        in-process ``cron.jobs.update_job(id, {"name": ...})`` call, which
        preserves the job id and every other stored field. If that seam is not
        present, fail with an explicit blocker rather than recreating the job.
        """
        try:
            from cron import jobs as native_jobs
        except Exception as exc:  # noqa: BLE001 - absent/shadowed SDK is a blocker
            raise CronError(
                f"native cron update API is unavailable; no safe in-place rename: {exc}"
            ) from exc
        updater = getattr(native_jobs, "update_job", None)
        if not callable(updater):
            raise CronError(
                "native cron.jobs.update_job is unavailable; no safe in-place rename"
            )
        # The native jobs API is already bound to the active profile by the
        # host. Do not compare it to the ambient HERMES_HOME: a host serving
        # profile B from a process launched with profile A is legitimate.
        getter = getattr(native_jobs, "get_job", None)
        before = None
        if callable(getter):
            try:
                before = getter(str(job_id))
            except Exception as exc:  # noqa: BLE001 - cannot prove the row
                raise CronError(f"cannot read cron job {job_id!r} before rename: {exc}") from exc
            if not isinstance(before, dict):
                raise CronError(f"cron job {job_id!r} is not present")
        try:
            updated = updater(str(job_id), {"name": str(name)})
        except Exception as exc:  # noqa: BLE001 - host rejected the update
            raise CronError(f"native cron rename of {job_id!r} failed: {exc}") from exc
        if not isinstance(updated, dict) or str(updated.get("name") or "") != str(name):
            raise CronError(f"native cron rename of {job_id!r} was not confirmed")
        if not callable(getter):
            return dict(updated)
        after = getter(str(job_id))
        if not isinstance(after, dict) or str(after.get("name") or "") != str(name):
            raise CronError(f"native cron rename of {job_id!r} was not read back")
        expected = {**before, "name": str(name)}
        if after != expected:
            changed = sorted(
                key for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            )
            raise CronError(
                "native rename changed fields beyond name ("
                + ", ".join(changed) + "); refusing to claim preservation"
            )
        return dict(after)

    def add_job(self, spec: CronJobSpec) -> dict[str, Any]:
        validate_job_spec(spec)
        if _is_agent_spec(spec):
            # Normal native agent lane: real prompt positional, and deliberately
            # no --script/--no-agent. Optional pins are separate argv tokens;
            # omitted pins inherit the host's normal native configuration.
            argv = self._cron_argv(
                self.create_verb, spec.schedule, spec.prompt,
                "--name", spec.name,
            )
            if spec.deliver:
                argv += ["--deliver", spec.deliver]
            if spec.workdir:
                argv += ["--workdir", spec.workdir]
            if getattr(spec, "model", ""):
                argv += ["--model", spec.model]
            if getattr(spec, "provider", ""):
                argv += ["--provider", spec.provider]
            if getattr(spec, "reasoning", ""):
                argv += ["--reasoning-effort", spec.reasoning]
            for skill in _normalize_skills(getattr(spec, "skills", [])):
                argv += ["--skill", skill]
            # The native CLI expresses context_from=['self'] as a single boolean
            # --continuity flag. There is no --context-from flag, and validation
            # has already rejected any unsupported non-self reference.
            if _normalize_context_from(getattr(spec, "context_from", [])):
                argv += ["--continuity"]
        else:
            argv = self._cron_argv(
                self.create_verb, spec.schedule,
                "--name", spec.name,
                "--script", spec.script,
                "--no-agent",
                "--deliver", spec.deliver,
            )
            if spec.workdir:
                argv += ["--workdir", spec.workdir]
        try:
            proc = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot add cron job {spec.name!r}: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(
                f"cron create {spec.name!r} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
        return {"name": spec.name, "created": True, "argv": argv}

    def _set_job_state(self, job_id: str, verb: str, expected_state: str) -> dict[str, Any]:
        try:
            proc = self._run(self._cron_argv(verb, str(job_id)))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot {verb} cron job {job_id!r}: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(
                f"cron {verb} {job_id!r} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
        match = next(
            (j for j in self.list_jobs() if _job_id(j) == str(job_id)),
            None,
        )
        if match is None:
            raise CronError(f"cron job {job_id!r} is not present after {verb}")
        state = str(match.get("state") or "")
        if state != expected_state:
            raise CronError(
                f"cron job {job_id!r} state is {state or 'unknown'!r} after {verb}; "
                f"expected {expected_state!r}"
            )
        return {"id": str(job_id)}

    def pause_job(self, job_id: str) -> dict[str, Any]:
        result = self._set_job_state(job_id, "pause", "paused")
        result["paused"] = True
        return result

    def resume_job(self, job_id: str) -> dict[str, Any]:
        result = self._set_job_state(job_id, "resume", "active")
        result["resumed"] = True
        return result

    def remove_job(self, job_id: str) -> dict[str, Any]:
        try:
            proc = self._run(self._cron_argv(self.remove_verb, str(job_id)))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot remove cron job {job_id!r}: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(
                f"cron remove {job_id!r} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
        return {"id": job_id, "removed": True}


# --------------------------------------------------------------------------- #
# Idempotent, verified helpers
# --------------------------------------------------------------------------- #

def _select_created_row(jobs: list[dict[str, Any]], spec: CronJobSpec,
                        before_ids: set[str]) -> dict[str, Any] | None:
    """Select exactly one new row with the target name and a genuinely new id."""
    candidates = [
        row for row in jobs
        if str(row.get("name") or "") == spec.name
        and _job_id(row)
        and _job_id(row) not in before_ids
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise CronError(
            f"cron job {spec.name!r} creation is ambiguous: multiple new rows share "
            "the name; refusing to guess or adopt one"
        )
    return None


def _verify_normal_agent_row(row: dict[str, Any], spec: CronJobSpec) -> list[str]:
    """Verify id+mode+any exposed native fields; return honest verification gaps.

    A missing id or mode is inconclusive and fails. Prompt/model/... detail that
    the native text listing does not expose is reported as a bounded gap instead
    of being fabricated; an exposed value that differs is always a hard failure.
    """
    job_id = _job_id(row)
    if not job_id:
        raise CronError(
            f"cron job {spec.name!r} was created but the readback has no exact job id"
        )
    gaps: list[str] = []
    mode = str(row.get("mode") or "")
    if not mode:
        raise CronError(
            f"cron job {spec.name!r} was created but the readback does not expose "
            "its mode; cannot prove normal-agent mode"
        )
    if not _mode_is_agent(mode):
        raise CronError(
            f"cron job {spec.name!r} readback mode {mode!r} is not normal-agent"
        )
    if str(row.get("script") or ""):
        raise CronError(
            f"cron job {spec.name!r} readback unexpectedly has script "
            f"{row.get('script')!r}; refusing to claim a normal-agent job"
        )
    checks = (
        ("prompt", spec.prompt, False),
        ("model", getattr(spec, "model", ""), False),
        ("provider", getattr(spec, "provider", ""), False),
        ("reasoning", getattr(spec, "reasoning", ""), False),
        ("skills", getattr(spec, "skills", []), True),
        ("deliver", spec.deliver, False),
        ("workdir", spec.workdir, False),
    )
    for field_name, expected, is_skills in checks:
        if is_skills and not _normalize_skills(expected):
            continue
        if not is_skills and not str(expected or ""):
            continue
        if field_name not in row:
            gaps.append(field_name)
            continue
        actual = row.get(field_name)
        if is_skills:
            matches = _normalize_skills(actual) == _normalize_skills(expected)
        else:
            matches = str(actual or "") == str(expected or "")
        if not matches:
            raise CronError(
                f"cron job {spec.name!r} readback {field_name}={actual!r} does not "
                f"match requested {expected!r}"
            )
    expected_context = _normalize_context_from(
        getattr(spec, "context_from", []))
    if expected_context:
        if "context_from" not in row:
            gaps.append("context_from")
        elif _normalize_context_from(row.get("context_from")) != expected_context:
            raise CronError(
                f"cron job {spec.name!r} readback context_from="
                f"{row.get('context_from')!r} does not match requested "
                f"{expected_context!r}"
            )
    return gaps


def ensure_normal_agent_job(backend: CronBackend, spec: CronJobSpec, *,
                            ownership: dict[str, Any] | None = None,
                            ownership_path: str | os.PathLike[str] | None = None,
                            now: str | None = None) -> CronJobResult:
    """Explicit normal-agent creation path (never the script/--no-agent lane)."""
    if not _is_agent_spec(spec):
        raise CronError("ensure_normal_agent_job requires a non-empty prompt")
    if str(getattr(spec, "script", "") or ""):
        raise CronError("normal-agent jobs must not set a script")
    return ensure_job(backend, spec, ownership=ownership,
                      ownership_path=ownership_path, now=now)


def ensure_job(backend: CronBackend, spec: CronJobSpec, *,
               ownership: dict[str, Any] | None = None,
               ownership_path: str | os.PathLike[str] | None = None,
               now: str | None = None) -> CronJobResult:
    """Idempotently create ``spec``; re-create it if its shape changed.

    When ``ownership`` is supplied, ownership is enforced: a same-named job that
    was NOT recorded by this plugin is refused (never adopted or overwritten),
    and every created/updated job's id is recorded. Without ``ownership`` the
    legacy name-based behaviour is preserved for script jobs; the normal-agent
    lane is stricter and refuses to adopt an existing same-named row it cannot
    prove it owns.
    """
    validate_job_spec(spec)
    agent = _is_agent_spec(spec)
    jobs = backend.list_jobs()
    existing = _find_job(jobs, spec.name)
    record = owned_job_record(ownership, spec.name) if ownership is not None else None

    if existing is not None:
        job_id = _job_id(existing)
        if ownership is not None and (record is None or str(record.get("id") or "") != job_id):
            raise CronError(
                f"refusing to adopt foreign cron job {spec.name!r} (id={job_id or '?'}): "
                "this plugin did not create it. Remove or rename the conflicting job, "
                "then re-run setup."
            )
        if agent and ownership is None:
            raise CronError(
                f"refusing to adopt existing cron job {spec.name!r} "
                f"(id={job_id or '?'}) without an exact ownership record"
            )
        if _same_job(existing, spec):
            detail = "unchanged"
            if agent:
                gaps = _verify_normal_agent_row(existing, spec)
                detail = ("unchanged; " + (
                    "verification=partial (readback does not expose: "
                    + ", ".join(gaps) + ")" if gaps else "verification=full"))
            return CronJobResult(spec.name, "already-present", detail)
        if not job_id:
            raise CronError(f"cron job {spec.name!r} has no id to remove")
        backend.remove_job(job_id)
        if (next((j for j in backend.list_jobs() if _job_id(j) == job_id), None) is not None
                or _find_job(backend.list_jobs(), spec.name) is not None):
            raise CronError(f"cron job {spec.name!r} did not disappear before update")
        before_ids = {_job_id(j) for j in jobs if _job_id(j)}
        backend.add_job(spec)
        after = backend.list_jobs()
        updated = _select_created_row(after, spec, before_ids)
        if updated is None:
            raise CronError(
                f"cron job {spec.name!r} did not reappear with a new exact id after update"
            )
        detail = "shape changed; recreated"
        if agent:
            gaps = _verify_normal_agent_row(updated, spec)
            if gaps:
                detail += ("; verification=partial (readback does not expose: "
                           + ", ".join(gaps) + ")")
        if ownership is not None:
            record_owned_job(ownership, spec, updated, created_at=(record or {}).get("created_at") or now)
            save_job_ownership(ownership_path, ownership)
        return CronJobResult(spec.name, "updated", detail)

    before_ids = {_job_id(j) for j in jobs if _job_id(j)}
    backend.add_job(spec)
    after = backend.list_jobs()
    created = _select_created_row(after, spec, before_ids)
    if created is None:
        raise CronError(
            f"cron job {spec.name!r} was not confirmed with a new exact id after creation"
        )
    detail = ""
    if agent:
        gaps = _verify_normal_agent_row(created, spec)
        detail = ("id={}; verification={}".format(
            _job_id(created), "full" if not gaps else
            "partial (readback does not expose: " + ", ".join(gaps) + ")"))
    if ownership is not None:
        record_owned_job(ownership, spec, created, created_at=now)
        save_job_ownership(ownership_path, ownership)
    return CronJobResult(spec.name, "created", detail)


def remove_job(backend: CronBackend, name: str, *,
               ownership: dict[str, Any] | None = None,
               ownership_path: str | os.PathLike[str] | None = None) -> CronJobResult:
    """Remove only a job this plugin recorded (id AND name must match).

    With ``ownership``: a same-named but unrecorded job is foreign and is never
    removed; a recorded id that no longer exists is a harmless no-op. Without
    ``ownership`` the legacy name-based removal is preserved.
    """
    jobs = backend.list_jobs()
    match = _find_job(jobs, name)

    if ownership is None:
        if match is None:
            return CronJobResult(name, "missing", "not present")
        job_id = _job_id(match)
        if not job_id:
            raise CronError(f"cron job {name!r} has no id to remove")
        backend.remove_job(job_id)
        if _find_job(backend.list_jobs(), name) is not None:
            raise CronError(f"cron job {name!r} is still present after removal")
        return CronJobResult(name, "removed", f"id={job_id}")

    record = owned_job_record(ownership, name)
    if record is None:
        if match is not None:
            return CronJobResult(
                name, "foreign",
                f"left untouched: foreign job id={_job_id(match) or '?'} shares the name; "
                "this plugin did not create it",
            )
        return CronJobResult(name, "missing", "no owned job recorded and none present")

    recorded_id = str(record.get("id") or "")
    owned_match = next(
        (j for j in jobs if _job_id(j) == recorded_id and str(j.get("name") or "") == name),
        None,
    )
    if owned_match is None:
        ownership["jobs"].pop(name, None)
        save_job_ownership(ownership_path, ownership)
        if match is not None:
            return CronJobResult(
                name, "foreign",
                "left untouched: the recorded job is gone but a foreign job now uses the name",
            )
        return CronJobResult(name, "missing", "recorded job no longer present")
    if not recorded_id:
        raise CronError(f"cron job {name!r} has no recorded id to remove")
    backend.remove_job(recorded_id)
    if next((j for j in backend.list_jobs() if _job_id(j) == recorded_id), None) is not None:
        raise CronError(f"cron job {name!r} (id={recorded_id}) is still present after removal")
    ownership["jobs"].pop(name, None)
    save_job_ownership(ownership_path, ownership)
    return CronJobResult(name, "removed", f"id={recorded_id}")


def reconcile_retired_jobs(
    backend: CronBackend,
    desired_names: set[str] | frozenset[str] | tuple[str, ...],
    *,
    ownership: dict[str, Any] | None = None,
    ownership_path: str | os.PathLike[str] | None = None,
    retired_names: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Remove only retired owner-facing jobs recorded as ours.

    A name is retired when it no longer appears in the desired specs. Removal
    still requires the ledger's exact id AND name, so a foreign job that merely
    shares a retired name is never touched. Names absent from the ledger are
    skipped without adopting them. The legacy ``-pilot`` name is handled by
    ``migrate_legacy_monitoring_job`` and is not removed here.
    """
    if ownership is None:
        ownership = load_job_ownership(ownership_path)
    wanted = {str(name) for name in desired_names}
    removed: list[dict[str, Any]] = []
    for name in (retired_names if retired_names is not None else RETIRED_JOB_NAMES):
        if name in wanted or owned_job_record(ownership, name) is None:
            continue
        result = remove_job(
            backend, name, ownership=ownership, ownership_path=ownership_path)
        removed.append(result.to_dict())
    return removed


def classify_jobs(jobs: list[dict[str, Any]], ownership: dict[str, Any] | None) -> dict[str, Any]:
    """Split the plugin's known job names into owned / foreign / missing.

    Ownership is proven by the exact recorded id AND name; a same-named row is
    foreign and is never adopted. The optional preparation job is included so a
    recorded-and-present preparation job is counted instead of hidden. A legacy
    ``-pilot`` row is recognised only through its own exact ownership record.

    "No jobs of ours" (``state == "none"``) is distinct from "a foreign job
    happens to share a name" (``state`` in ``{"foreign", "mixed"}``).
    """
    owned: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in KNOWN_JOB_NAMES:
        if name in seen:
            continue
        seen.add(name)
        record = owned_job_record(ownership, name)
        by_id: dict[str, Any] | None = None
        if record:
            recorded_id = str(record.get("id") or "")
            by_id = next(
                (j for j in jobs if _job_id(j) == recorded_id and str(j.get("name") or "") == name),
                None,
            )
        by_name = _find_job(jobs, name)
        if by_id is not None:
            owned.append({"id": _job_id(by_id), "name": name, "state": str(by_id.get("state") or "")})
        elif by_name is not None:
            foreign.append({"id": _job_id(by_name), "name": name,
                            "state": str(by_name.get("state") or "")})
        elif record:
            missing.append(name)
    optional = [row for row in owned if row["name"] in OPTIONAL_JOB_NAMES]
    legacy = [row for row in owned if row["name"] in LEGACY_OWNED_JOB_NAMES]
    if owned and foreign:
        state = "mixed"
    elif foreign:
        state = "foreign"
    elif owned:
        state = "owned"
    elif missing:
        state = "missing"
    else:
        state = "none"
    return {"state": state, "owned": owned, "foreign": foreign, "missing": missing,
            "optional": optional, "legacy": legacy, "jobs": owned}


def _owned_row(jobs: list[dict[str, Any]], record: dict[str, Any] | None,
               name: str) -> dict[str, Any] | None:
    """Return the live row matching a ledger record by exact id and name."""
    if not isinstance(record, dict):
        return None
    recorded_id = str(record.get("id") or "")
    if not recorded_id:
        return None
    return next(
        (j for j in jobs
         if _job_id(j) == recorded_id and str(j.get("name") or "") == name),
        None,
    )


def legacy_monitoring_job(jobs: list[dict[str, Any]],
                          ownership: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the exact recorded legacy monitoring row, never a name-only match."""
    record = owned_job_record(ownership, LEGACY_PILOT_JOB_NAME)
    return _owned_row(jobs, record, LEGACY_PILOT_JOB_NAME)


def migrate_legacy_monitoring_job(
    backend: CronBackend,
    *,
    ownership: dict[str, Any] | None = None,
    ownership_path: str | os.PathLike[str] | None = None,
    apply: bool = True,
) -> CronJobResult:
    """Idempotently rename only the recorded legacy ``-pilot`` job in place.

    The host update API changes only ``name``: job id, schedule, delivery,
    enabled/paused state, next run, run history, unrelated fields and the
    plugin's ownership metadata are preserved. A foreign or mismatched row is
    never adopted, and an occupied monitoring name is never overwritten.
    ``apply=False`` is a read-only plan. A host without the safe update seam is
    reported as a blocker and nothing is recreated or removed.
    """
    if ownership is None:
        ownership = load_job_ownership(ownership_path)
    jobs = backend.list_jobs()
    legacy_record = owned_job_record(ownership, LEGACY_PILOT_JOB_NAME)
    monitoring_record = owned_job_record(ownership, MONITORING_JOB_NAME)
    legacy_rows = [j for j in jobs if str(j.get("name") or "") == LEGACY_PILOT_JOB_NAME]
    monitoring_rows = [j for j in jobs if str(j.get("name") or "") == MONITORING_JOB_NAME]
    legacy_owned = _owned_row(jobs, legacy_record, LEGACY_PILOT_JOB_NAME)
    monitoring_owned = _owned_row(jobs, monitoring_record, MONITORING_JOB_NAME)

    if monitoring_owned is not None:
        if legacy_owned is not None:
            raise CronError(
                "both the legacy and monitoring jobs are recorded and present; "
                "refusing to guess which one should survive"
            )
        legacy_id = str((legacy_record or {}).get("id") or "")
        monitoring_id = _job_id(monitoring_owned)
        if legacy_record and legacy_id == monitoring_id:
            # A rename completed but the old ledger key survived. Drop only the
            # duplicate alias; the canonical record and live row are untouched.
            if apply:
                ownership["jobs"].pop(LEGACY_PILOT_JOB_NAME, None)
                save_job_ownership(ownership_path, ownership)
                return CronJobResult(
                    MONITORING_JOB_NAME, "ledger-reconciled",
                    f"id={monitoring_id}; stale legacy ownership alias removed",
                )
            return CronJobResult(
                MONITORING_JOB_NAME, "would-reconcile",
                f"id={monitoring_id}; stale legacy ownership alias would be removed",
            )
        if legacy_record:
            raise CronError(
                "legacy and monitoring ownership records disagree while the "
                "monitoring job is present; refusing to adopt or overwrite"
            )
        return CronJobResult(MONITORING_JOB_NAME, "already-migrated",
                             f"id={monitoring_id}; monitoring job already owned")

    if monitoring_rows:
        if legacy_record is not None and legacy_owned is None and not legacy_rows:
            legacy_id = str(legacy_record.get("id") or "")
            if not legacy_id:
                raise CronError("the legacy ownership record has no job id; refusing to rename")
            if len(monitoring_rows) != 1:
                raise CronError(
                    "multiple monitoring-named rows are present; refusing to guess "
                    "which one the recorded legacy id refers to"
                )
            row = monitoring_rows[0]
            if _job_id(row) != legacy_id:
                raise CronError(
                    "ownership mismatch: the monitoring-named row id "
                    f"{_job_id(row) or '?'} is not the recorded legacy id "
                    f"{legacy_id!r}; refusing to reconcile"
                )
            if monitoring_record and str(monitoring_record.get("id") or "") != legacy_id:
                raise CronError(
                    "monitoring ownership record has a different id; refusing to overwrite"
                )
            # The host rename landed before the ledger save. Reconcile only the
            # canonical ownership key; never issue a second host mutation.
            new_record = dict(legacy_record)
            new_record["name"] = MONITORING_JOB_NAME
            if apply:
                ownership["jobs"].pop(LEGACY_PILOT_JOB_NAME, None)
                ownership["jobs"][MONITORING_JOB_NAME] = new_record
                save_job_ownership(ownership_path, ownership)
                return CronJobResult(
                    MONITORING_JOB_NAME, "ledger-reconciled",
                    f"id={legacy_id}; interrupted host rename reconciled without mutation",
                )
            return CronJobResult(
                MONITORING_JOB_NAME, "would-reconcile",
                f"id={legacy_id}; interrupted host rename would be reconciled",
            )
        raise CronError(
            f"refusing to adopt foreign monitoring job {MONITORING_JOB_NAME!r} "
            f"(id={_job_id(monitoring_rows[0]) or '?'}): this plugin did not create it"
        )

    if legacy_record is None:
        if legacy_rows:
            raise CronError(
                f"refusing to adopt foreign legacy job {LEGACY_PILOT_JOB_NAME!r} "
                f"(id={_job_id(legacy_rows[0]) or '?'}): no exact ownership record"
            )
        return CronJobResult(MONITORING_JOB_NAME, "not-needed",
                             "no recorded legacy monitoring job")

    legacy_id = str(legacy_record.get("id") or "")
    if not legacy_id:
        raise CronError("the legacy ownership record has no job id; refusing to rename")

    if legacy_owned is None:
        if legacy_rows:
            raise CronError(
                f"ownership mismatch for {LEGACY_PILOT_JOB_NAME!r}: recorded id "
                f"{legacy_id!r} does not match live id "
                f"{_job_id(legacy_rows[0])!r}; refusing to adopt"
            )
        same_id = next((j for j in jobs if _job_id(j) == legacy_id), None)
        if same_id is not None:
            if str(same_id.get("name") or "") == MONITORING_JOB_NAME:
                # The host rename landed but the ledger did not. Reconcile the
                # canonical key without touching the live job.
                new_record = dict(legacy_record)
                new_record["name"] = MONITORING_JOB_NAME
                if monitoring_record and str(monitoring_record.get("id") or "") != legacy_id:
                    raise CronError(
                        "monitoring ownership record has a different id; refusing to overwrite"
                    )
                if apply:
                    ownership["jobs"].pop(LEGACY_PILOT_JOB_NAME, None)
                    ownership["jobs"][MONITORING_JOB_NAME] = new_record
                    save_job_ownership(ownership_path, ownership)
                    return CronJobResult(
                        MONITORING_JOB_NAME, "ledger-reconciled",
                        f"id={legacy_id}; live rename already present",
                    )
                return CronJobResult(
                    MONITORING_JOB_NAME, "would-reconcile",
                    f"id={legacy_id}; live rename already present",
                )
            raise CronError(
                f"recorded legacy job id {legacy_id!r} now has unexpected name "
                f"{same_id.get('name')!r}; refusing to rename"
            )
        return CronJobResult(
            LEGACY_PILOT_JOB_NAME, "missing",
            f"recorded legacy job id={legacy_id} is no longer present",
        )

    if monitoring_record and str(monitoring_record.get("id") or "") != legacy_id:
        raise CronError(
            "monitoring ownership record has a different id; refusing to overwrite it"
        )
    before_ids = [_job_id(j) for j in jobs]
    if not apply:
        return CronJobResult(
            MONITORING_JOB_NAME, "would-migrate",
            f"id={legacy_id}; {LEGACY_PILOT_JOB_NAME} -> {MONITORING_JOB_NAME}",
        )
    rename = getattr(backend, "rename_job", None)
    if not callable(rename):
        raise CronError(
            "this host cron backend has no safe in-place rename (native "
            "cron.jobs.update_job); refusing to recreate or remove the job"
        )
    renamed = rename(legacy_id, MONITORING_JOB_NAME)
    if isinstance(renamed, dict) and (
        _job_id(renamed) != legacy_id
        or str(renamed.get("name") or "") != MONITORING_JOB_NAME
    ):
        raise CronError(
            "the host rename did not preserve the recorded job id and target name; "
            "ownership ledger was not updated"
        )
    after_jobs = backend.list_jobs()
    after = _owned_row(after_jobs, {"id": legacy_id}, MONITORING_JOB_NAME)
    if after is None:
        raise CronError(
            f"rename of {LEGACY_PILOT_JOB_NAME!r} to {MONITORING_JOB_NAME!r} "
            "was not confirmed; nothing was recreated"
        )
    if any(_job_id(j) == legacy_id and str(j.get("name") or "") == LEGACY_PILOT_JOB_NAME
           for j in after_jobs):
        raise CronError("legacy job name is still present after rename")
    if sorted(_job_id(j) for j in after_jobs if _job_id(j)) != sorted(before_ids):
        raise CronError("rename changed the set of job ids; refusing to continue")
    # Full-field equality is enforced by HermesCronBackend.rename_job against the
    # native getter (before/after). This method intentionally does not infer
    # preservation from the CLI's lossy listing.
    new_record = dict(legacy_record)
    new_record["name"] = MONITORING_JOB_NAME
    ownership["jobs"].pop(LEGACY_PILOT_JOB_NAME, None)
    ownership["jobs"][MONITORING_JOB_NAME] = new_record
    save_job_ownership(ownership_path, ownership)
    return CronJobResult(
        MONITORING_JOB_NAME, "migrated",
        f"id={legacy_id}; renamed {LEGACY_PILOT_JOB_NAME} -> {MONITORING_JOB_NAME} "
        "in place via the host update API; no job was recreated or removed",
    )



class InMemoryCronBackend:
    """Deterministic backend for tests (and a safe ``--no-cron`` dry inspection)."""

    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs: list[dict[str, Any]] = [dict(j) for j in (jobs or [])]
        self._counter = 0
        self.fail_remove = False

    def is_available(self) -> bool:
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [dict(j) for j in self.jobs]

    def add_job(self, spec: CronJobSpec) -> dict[str, Any]:
        validate_job_spec(spec)
        self._counter += 1
        agent = _is_agent_spec(spec)
        job = {
            "id": f"job-{self._counter}",
            "name": spec.name,
            "schedule": spec.schedule,
            "script": spec.script,
            "deliver": spec.deliver,
            "workdir": spec.workdir,
            "mode": "agent" if agent else "no-agent",
            "state": "active",
            "last_run": "",
        }
        if agent:
            job.update({
                "prompt": spec.prompt,
                "model": getattr(spec, "model", ""),
                "provider": getattr(spec, "provider", ""),
                "reasoning": getattr(spec, "reasoning", ""),
                "skills": _normalize_skills(getattr(spec, "skills", [])),
                "context_from": _normalize_context_from(
                    getattr(spec, "context_from", [])),
            })
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> dict[str, Any]:
        if self.fail_remove:
            return {"id": job_id, "removed": False}
        self.jobs = [j for j in self.jobs if _job_id(j) != str(job_id)]
        return {"id": job_id, "removed": True}

    def rename_job(self, job_id: str, name: str) -> dict[str, Any]:
        for job in self.jobs:
            if _job_id(job) == str(job_id):
                job["name"] = str(name)
                return dict(job)
        raise CronError(f"cron job {job_id!r} is not present to rename")

    def _set_job_state(self, job_id: str, state: str, flag: str) -> dict[str, Any]:
        for job in self.jobs:
            if _job_id(job) == str(job_id):
                job["state"] = state
                result = {"id": str(job_id)}
                result[flag] = True
                return result
        raise CronError(f"cron job {job_id!r} is not present to {flag}")

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self._set_job_state(job_id, "paused", "paused")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self._set_job_state(job_id, "active", "resumed")


def build_job_specs(*, deliver: str = "local", workdir: str = "",
                    include_preparation: bool = False) -> list[CronJobSpec]:
    """Build the canonical jobs this plugin owns.

    Preparation is a separate explicit opt-in: ``preparation_enabled`` in the
    user configuration never arms anything by itself. Only
    ``hermes portal setup --with-preparation`` passes ``True`` here.
    """
    specs = [
        CronJobSpec(
            name=MONITORING_JOB_NAME,
            schedule="*/15 * * * *",
            script=MONITORING_SCRIPT,
            deliver=deliver,
            workdir=workdir,
            description="Proactive secretary collector + evaluator (every 15 minutes)",
        ),
        CronJobSpec(
            name=REMINDER_JOB_NAME,
            schedule="* * * * *",
            script=REMINDER_SCRIPT,
            deliver=deliver,
            workdir=workdir,
            description="Requested-reminder dispatcher (every 1 minute)",
        ),
    ]
    if include_preparation:
        specs.append(CronJobSpec(
            name=PREPARATION_JOB_NAME,
            schedule="*/10 * * * *",
            script=PREPARATION_SCRIPT,
            deliver="local",
            workdir=workdir,
            description=("One bounded private-preparation unit (no-agent script; "
                         "silent unless an artifact is queued)"),
        ))
    return specs
