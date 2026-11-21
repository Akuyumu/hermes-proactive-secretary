"""``hermes secretary`` CLI for the hermes-proactive-secretary plugin.

This module is imported lazily (only when the ``secretary`` command tree is
built or run) so unrelated ``hermes`` startup is unaffected. It never touches a
source, the network, or the scheduler at import time.

Commands
    setup      one-command, idempotent, verified setup
    status     enabled/disabled, job state, last run, coverage, counts
    job        manage the proactive secretary job (a nested action)
    pause      global pause (stops collection AND delivery)
    resume     clear the global pause
    disable    pause + remove this plugin's own cron jobs
    uninstall  alias of disable (data is preserved, never deleted)
    migrate-jobs  rename only the recorded legacy monitoring job in place
    self-test  offline deterministic self-test
    run-once   dry, no-model run that never sends
    watch      add | list | remove a public watch
    feedback   passthrough to the validated feedback command
    doctor     environment and wiring diagnostics

Every command supports ``--json`` and returns a non-zero exit on failure.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

try:  # package import
    from .. import cron as cron_mod
    from .. import paths
    from .. import runner_install
    from .. import state_import
except ImportError:  # direct/standalone import
    import cron as cron_mod  # type: ignore
    import paths  # type: ignore
    import runner_install  # type: ignore
    import state_import  # type: ignore
from . import secretary_portal_controls as portal_controls
from . import secretary_status

# Host-built native context captured from the registered CLI handler closure.
# It is only ever the context Hermes constructs and passes to ``register(ctx)``;
# this module never builds one. Model calls through it stay host-owned and
# gated. ``None`` until the host wires the plugin.
_NATIVE_CONTEXT: Any = None


def set_native_context(ctx: Any) -> None:
    """Record the host-provided context from the plugin registration closure."""
    global _NATIVE_CONTEXT
    _NATIVE_CONTEXT = ctx


def get_native_context() -> Any:
    """Return the host-provided context (or ``None`` in an offline/test process)."""
    return _NATIVE_CONTEXT


def _ensure_src() -> None:
    src = str(paths.plugin_root() / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")


def _common_parent() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", default=None,
        help="override the owner-only runtime data directory (advanced/testing)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    return common


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes secretary`` argparse tree."""
    common = _common_parent()
    subs = subparser.add_subparsers(dest="secretary_command")
    p = {name: subs.add_parser(name, parents=[common], help=help_)
         for name, help_ in (
             ("setup", "One-command setup: policy, state, native cron jobs, verification"),
              ("status", "Show enabled state, jobs, last run, coverage and counts"),
              ("job", "Manage the proactive secretary job "
                      "(preview | enable | status | pause | resume | remove)"),
              ("pause", "Global pause: stop collection and delivery"),
             ("resume", "Clear the global pause"),
             ("disable", "Pause and remove this plugin's own cron jobs"),
             ("uninstall", "Alias of disable (runtime data is preserved)"),
             ("migrate-jobs", "In-place rename of only the recorded legacy monitoring job"),
             ("self-test", "Run the offline deterministic self-test"),
             ("run-once", "Dry, no-model run (never sends)"),
             ("watch", "Manage public watches (add | list | remove)"),
             ("feedback", "Record or inspect notification preferences"),
             ("configure", "Validated per-user configuration (show | validate | preview | project | init | migrate)"),
             ("pipeline", "Internal: scheduled ordinary pipeline via the native context bridge"),
             ("prepare", "Internal: one bounded private-preparation draft (opt-in; no agent job)"),
             ("responsibilities", "Capture/list/inspect/update/cancel accepted responsibilities and inspect handoff"),
             ("context", "Foreground bounded context refresh: list | show | update | revoke"),
             ("reminders", "Requested reminders (dispatch | check | list | status | add | cancel)"),
             ("deliveries", "Read recent delivered proactive notices (--since | --limit)"),
             ("doctor", "Check the environment and wiring"),
          )}
    job = p["job"]
    job.add_argument(
        "portal_action",
        choices=("preview", "enable", "status", "pause", "resume", "remove"),
        help="portal lifecycle action",
    )
    job.add_argument("--schedule", default="",
                     help="native schedule for enable (default: service validation)")
    job.add_argument("--model", default="")
    job.add_argument("--provider", default="")
    job.add_argument("--reasoning-effort", default="", dest="reasoning_effort")
    job.add_argument("--skill", action="append", default=[],
                     help="native skill pin (repeatable)")
    job.add_argument("--deliver", default="local")
    job.add_argument("--workdir", default="")
    p["setup"].add_argument("--force", action="store_true",
                            help="overwrite an existing policy file (default: keep it)")
    p["setup"].add_argument("--no-cron", action="store_true",
                            help="skip native cron job creation (offline verification only)")
    p["setup"].add_argument("--import-from", default=None, metavar="LEGACY_TREE",
                            help="copy durable legacy state (var/, policy.json, preferences.json) "
                                 "before arming anything; refuses conflicting targets")
    p["setup"].add_argument("--import-dry-run", action="store_true",
                            help="preview --import-from only: inventory and hashes, no changes")
    p["setup"].add_argument("--fixture", default="healthy_empty",
                            help="offline fixture used for the no-model dry-run")
    p["setup"].add_argument("--deliver", default=None,
                            help="delivery target for alerts (e.g. local, origin, platform:chat_id). "
                                 "Defaults to 'local' with a warning; never guessed.")
    p["setup"].add_argument("--with-preparation", action="store_true", dest="with_preparation",
                            help="explicit opt-in: install the owned preparation runner and one "
                                 "native --no-agent script job. Setting preparation_enabled "
                                 "alone never arms anything.")
    p["run-once"].add_argument("--fixture", default=None,
                               help="run against a synthetic fixture instead of live sources")
    p["run-once"].add_argument("--check", action="store_true",
                               help="collect-only coverage check (implies dry, never sends)")
    p["run-once"].add_argument("--model", action="store_true",
                               help="use the host ctx.llm lane (default: deterministic no-model)")
    p["pipeline"].add_argument("--fixture", default=None,
                               help="run against a synthetic fixture instead of live sources")
    p["pipeline"].add_argument("--check", action="store_true",
                               help="collect-only coverage check (never sends)")
    p["prepare"].add_argument("--now", default=None,
                              help="internal deterministic clock for tests only")
    rp = p["responsibilities"]
    rp.add_argument("action", choices=("list", "inspect", "capture", "accept", "update",
                                       "cancel", "handoff"),
                    help="foreground responsibility lifecycle action")
    rp.add_argument("--id", default="", dest="responsibility_id")
    rp.add_argument("--kind", choices=("commitment", "opportunity"), default="commitment")
    rp.add_argument("--desired-outcome", default="")
    rp.add_argument("--acceptance-criterion", default="")
    rp.add_argument("--due-at", default="")
    rp.add_argument("--next-check-at", default="")
    rp.add_argument("--source-version", default="")
    rp.add_argument("--source-ref", default="")
    rp.add_argument("--conversation-ref", default="")
    rp.add_argument("--evidence-quote", default="")
    rp.add_argument("--reason", default="")
    cp = p["context"]
    cp.add_argument("action", choices=("list", "show", "update", "revoke"))
    cp.add_argument("--id", default="")
    cp.add_argument("--note", default="")
    cp.add_argument("--provenance", default="")
    cp.add_argument("--kind", default="")
    cp.add_argument("--expires-at", default="")
    cp.add_argument("--owner-declared", action="store_true")
    p["reminders"].add_argument("reminder_args", nargs=argparse.REMAINDER,
                                help="arguments forwarded to the validated reminder CLI "
                                     "(default: dispatch)")
    p["deliveries"].add_argument("--since", default=None,
                                 help="only rows at/after this ISO timestamp")
    p["deliveries"].add_argument("--limit", type=int, default=10,
                                 help="maximum rows to print (default 10, hard max 50)")
    for _name in ("disable", "uninstall"):
        p[_name].add_argument("--purge", action="store_true",
                              help="also delete the owner-only runtime data directory")
    p["migrate-jobs"].add_argument("--apply", action="store_true",
                                   help="apply the verified in-place rename (default: dry-run)")
    p["watch"].add_argument("action", choices=("add", "list", "remove"))
    p["watch"].add_argument("value", nargs="?", default="",
                            help="topic for add, watch id for remove")
    p["watch"].add_argument("--scope", default="public")
    p["watch"].add_argument("--cadence-minutes", type=int, default=None)
    p["feedback"].add_argument("feedback_args", nargs=argparse.REMAINDER,
                               help="arguments forwarded to the validated feedback CLI")
    p["configure"].add_argument("action",
                                choices=("show", "validate", "preview", "project", "init",
                                         "migrate"),
                                help="show/validate/preview the config, project the "
                                     "validated effective config into the native form, "
                                     "init a neutral example, or migrate a legacy policy")
    p["configure"].add_argument("--file", default=None,
                                help="user configuration path (default: <data>/user_config.json)")
    p["configure"].add_argument("--from-policy", default=None, metavar="POLICY",
                                help="legacy policy.json to migrate from")
    p["configure"].add_argument("--mail-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured mail account (repeatable)")
    p["configure"].add_argument("--calendar-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured calendar account (repeatable)")
    p["configure"].add_argument("--calendar-id", action="append", default=None,
                                metavar="ID",
                                help="migrate: copy an existing configured calendar id (repeatable)")
    p["configure"].add_argument("--task-list", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured task list (repeatable)")
    p["configure"].add_argument("--task-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured task account (repeatable)")
    p["configure"].add_argument("--adapter", default=None,
                                help="migrate: preserve the named compatibility adapter selection")
    p["configure"].add_argument("--force", action="store_true",
                                help="overwrite an existing nonempty configuration")
    p["configure"].add_argument("--dry-run", action="store_true",
                                help="preview only; write nothing")
    subparser.set_defaults(func=secretary_command)


def _data_override(args: argparse.Namespace) -> None:
    override = getattr(args, "data_dir", None)
    if override:
        os.environ[paths.DATA_ENV] = str(override)


def _emit(obj: Any, *, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(human)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _prefs() -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.feedback import load_preferences_file
    return load_preferences_file(paths.preferences_path(), strict=False)


def _is_paused() -> bool:
    paused = _prefs().get("paused") or {}
    return bool(isinstance(paused, dict) and paused.get("active"))


def _backend() -> cron_mod.HermesCronBackend:
    return cron_mod.HermesCronBackend()


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

OWNED_JOB_NAMES = cron_mod.OWNED_JOB_NAMES


def _write_policy(force: bool) -> dict[str, Any]:
    default = paths.default_policy_path()
    if not default.is_file():
        raise FileNotFoundError(f"shipped default policy missing: {default}")
    target = paths.policy_path()
    if target.exists() and not force:
        return {"policy": str(target), "action": "kept"}
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(default.read_text(encoding="utf-8"))
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return {"policy": str(target), "action": "written"}


def _backup_before_change(reason: str) -> dict[str, Any]:
    """Snapshot this plugin's own settings records before a settings write.

    Raises on failure so a caller can stop before changing anything. Only the
    plugin's declared records are included (never credentials, .env, host
    memories or unrelated configuration).
    """
    _ensure_src()
    from proactive_pilot import config_backup
    ctx = get_native_context()
    return config_backup.create_backup(
        paths.data_root(), native=ctx, state=getattr(ctx, "state", None), reason=reason)


def _project_native_settings(config: dict[str, Any], *, reason: str = "configure project",
                             backup: bool = True) -> dict[str, Any]:
    """Project validated ordinary controls onto the native settings form.

    Uses only the host-provided context writer. Offline processes (and the
    scheduled subprocess lane) have no writer, so they report that fact and
    write nothing; they never construct a host context themselves. A true no-op
    creates no backup and performs no write; a real change is backed up first
    and a backup failure prevents every projection write.
    """
    _ensure_src()
    from collections.abc import MutableMapping
    from proactive_pilot import userconfig
    ctx = get_native_context()
    if ctx is None or (not callable(getattr(ctx, "set_config", None))
                       and not isinstance(ctx, MutableMapping)):
        return {"projected": False, "reason": "no native settings writer"}
    try:
        changes = userconfig.native_flat_changes(ctx, config)
        if not changes:
            return {"projected": True, "written": [], "noop": True, "backup": None}
        snapshot = _backup_before_change(reason) if backup else None
        written = userconfig.project_native_settings(ctx, config)
    except Exception as exc:  # noqa: BLE001 - report, never half-claim success
        return {"projected": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"projected": True, "written": written, "noop": False,
            "changed": sorted(changes), "backup": snapshot}


def _same_config_file(path: Path, payload: dict[str, Any]) -> bool:
    """True when the target already contains exactly this logical config."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    clean = {key: value for key, value in payload.items()
             if not str(key).startswith("_")}
    return existing == clean


def _native_projection_changes(config: dict[str, Any]) -> dict[str, Any]:
    """Preview ordinary native-form changes without writing anything."""
    from collections.abc import MutableMapping
    _ensure_src()
    from proactive_pilot import userconfig
    ctx = get_native_context()
    if ctx is None or (not callable(getattr(ctx, "set_config", None))
                       and not isinstance(ctx, MutableMapping)):
        return {}
    return userconfig.native_flat_changes(ctx, config)


def _ensure_user_config() -> dict[str, Any]:
    """Prepare the neutral per-user configuration for a NEW generic install.

    Idempotent and never overwriting: if a configuration already exists it is
    kept. Only the shipped neutral example is written, so a fresh install starts
    with generic defaults (no accounts, no seeds, adaptive off) instead of any
    implicit private-account assumption.
    """
    target = paths.user_config_path()
    if target.exists() and target.read_text(encoding="utf-8").strip():
        return {"user_config": str(target), "action": "kept"}
    example = paths.user_config_example_path()
    if not example.is_file():
        return {"user_config": str(target), "action": "missing-example"}
    _ensure_src()
    from proactive_pilot import userconfig
    payload = userconfig.validate_config(json.loads(example.read_text(encoding="utf-8")))
    payload.setdefault("advanced", {})["onboarding_provenance"] = (
        userconfig.neutral_init_marker(payload))
    status = _write_user_config(target, payload, force=False)
    return {"user_config": str(target), "action": status}


def _ensure_preferences() -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.clock import Clock
    from proactive_pilot.feedback import empty_preferences, load_preferences_file, save_preferences_file
    target = paths.preferences_path()
    if target.exists():
        return {"preferences": str(target), "action": "kept"}
    prefs = empty_preferences()
    save_preferences_file(target, prefs, Clock().now(), lock=False)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return {"preferences": str(target), "action": "written"}


def _effective_config_and_policy() -> tuple[Any, dict[str, Any]]:
    """Resolve the SAME effective policy setup/status/doctor/production use.

    Native settings are authoritative; absent configuration applies neutral
    generic defaults for production entrypoints. A present-invalid setting
    raises and is reported rather than silently falling back.
    """
    _ensure_src()
    from proactive_pilot.policy import load_policy
    from proactive_pilot.userconfig import apply_user_config, default_config
    loaded = _authoritative_config()
    base = load_policy(paths.policy_path())
    effective = (apply_user_config(base, loaded.config) if loaded.present
                 else apply_user_config(base, default_config()))
    return loaded, effective


def _freshness(last_success: str, now: Any, stale_minutes: int) -> tuple[bool, str]:
    if not last_success:
        return False, "never-verified"
    try:
        from datetime import datetime, timedelta
        parsed = datetime.fromisoformat(str(last_success))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        age = now - parsed
        if age <= timedelta(minutes=max(1, stale_minutes)):
            return True, "fresh"
        return False, "stale"
    except (TypeError, ValueError):
        return False, "unknown"


def _capability_truth(config: dict[str, Any], state: dict[str, Any],
                      public_registry: dict[str, Any] | None = None,
                      adaptive_coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Separate installed/available from enabled/authorized/verified/fresh.

    Public truth is evaluated per configured source when the runtime registry /
    adaptive coverage is supplied; without them the legacy health-only view is
    preserved for callers that only have configuration and notify state.
    """
    from datetime import datetime
    health = (state.get("source_health") or {}).get("sources") or {}
    now = datetime.now().astimezone()
    stale = int((state.get("source_health") or {}).get("stale_after_minutes") or 45)

    def status(name: str, available: bool, enabled: bool, scope: list[str],
               remedy: str, prefixes: tuple[str, ...] = ()) -> dict[str, Any]:
        configured = bool(enabled and scope)
        prefixes = (name + ":", *prefixes)
        records = [row for key, row in health.items()
                   if key == name or str(key).startswith(prefixes)]
        successes = sorted(str(row.get("last_success_at") or "") for row in records
                           if row.get("last_success_at"))
        last = successes[-1] if successes else ""
        fresh, freshness = _freshness(last, now, stale) if configured else (False, "not-configured")
        verified = bool(last and available)
        fresh = bool(fresh and available)
        if not available:
            if configured:
                freshness = "unavailable"
            label = "unavailable"
            remedy_text = remedy
        elif not enabled:
            label = "disabled"
            remedy_text = "Enable this source in the same native/CLI configuration when wanted."
        elif not scope:
            label = "enabled-unscoped"
            remedy_text = "Add an explicit account/source scope; installed binaries are not coverage."
        elif not verified:
            label = "configured-unverified"
            remedy_text = "No successful read recorded yet; run a check after authorising access."
        elif not fresh:
            label = "configured-stale"
            remedy_text = "Last successful read is stale; review source health or authorisation."
        else:
            label = "verified-fresh"
            remedy_text = ""
        return {
            "available": available, "configured": configured, "authorized": bool(scope),
            "verified": verified, "fresh": fresh, "freshness": freshness,
            "last_success_at": last, "status": label, "remedy": remedy_text,
        }

    sources = config.get("sources") or {}
    mail = (config.get("mail") or {}).get("accounts") or []
    cal = (config.get("calendar") or {}).get("accounts") or []
    calendars = (config.get("calendar") or {}).get("calendars") or []
    tasks = (config.get("tasks") or {}).get("accounts") or []
    lists = (config.get("tasks") or {}).get("lists") or []
    public_scope = (config.get("public_sources") or config.get("public_interests") or [])
    try:
        from proactive_pilot.source_truth import public_truth
        public_entry = public_truth(
            config, state, registry=public_registry, adaptive=adaptive_coverage, now=now)
    except Exception:  # noqa: BLE001 - preserve the legacy truth if the helper fails
        public_entry = status("public", True, bool(sources.get("public_feeds")),
                             list(public_scope),
                             "Add explicit public interests/sources; empty means none.",
                             prefixes=("feed:",))
    return {
        "mail": status("mail", shutil.which("himalaya") is not None,
                       bool(sources.get("mail")), mail,
                       "Install/authorise the read-only himalaya mail connector."),
        "calendar": status("calendar", _secretary_src_available(),
                           bool(sources.get("calendar")), cal or calendars,
                           f"Set {_secretary_env_hint()} to an authorised calendar library."),
        "tasks": status("tasks", _secretary_src_available(),
                        bool(sources.get("tasks")), tasks or lists,
                        f"Set {_secretary_env_hint()} and an explicit task adapter/list."),
        "public": public_entry,
        "obligations": status("obligation",
                              shutil.which("himalaya") is not None,
                              bool(sources.get("obligations")), mail,
                              "Authorise configured mail accounts for obligation checks."),
    }


def _secretary_src_dir() -> Path:
    try:
        return Path(_secretary_src())
    except Exception:  # noqa: BLE001
        return Path("")


def _secretary_src_available() -> bool:
    """True only when the resolved calendar/tasks library actually exists."""
    try:
        src = _secretary_src_dir()
        text = str(src)
        return text not in {"", "."} and src.is_dir()
    except Exception:  # noqa: BLE001 - an unknown library is unavailable
        return False


def detect_capabilities() -> dict[str, Any]:
    """Best-effort, read-only connector detection (never installs anything).

    ``available`` is only installation/transport capability. The separate
    ``truth`` matrix reports enabled/configured/authorized/verified/fresh
    coverage so an installed-but-disabled connector is never presented as
    monitored, and each gap carries a concrete remedy.
    """
    _ensure_src()
    himalaya = shutil.which("himalaya") is not None
    secretary_src = Path(_secretary_src())
    secretary = _secretary_src_available()
    monitoring = "full" if (secretary or himalaya) else "public-only"
    routed: dict[str, Any] = {}
    truth: dict[str, Any] = {}
    try:
        loaded, effective = _effective_config_and_policy()
        from proactive_pilot import routed_retrieval
        # The flat policy drives routed retrieval, but capability truth needs the
        # validated nested configuration (sources/mail/calendar/tasks) from the
        # same canonical owner. Passing the flat policy made every configured
        # private source look disabled and monitoring falsely public-only.
        routed = routed_retrieval.capabilities(effective, ctx=get_native_context())
        state = _read_json(paths.state_dir() / "notify_state.json")
        public_registry = _read_json(paths.state_dir() / "public_registry.json")
        adaptive_coverage = _read_json(paths.state_dir() / "adaptive_coverage.json")
        truth = _capability_truth(loaded.config, state,
                                  public_registry=public_registry or None,
                                  adaptive_coverage=adaptive_coverage or None)
        # Availability is not coverage: only an enabled AND explicitly scoped
        # private source counts as more than public-only capability.
        private_configured = any(
            isinstance(truth.get(name), dict) and truth[name].get("configured")
            for name in ("mail", "calendar", "tasks", "obligations"))
        monitoring = "full" if private_configured else "public-only"
    except Exception as exc:  # noqa: BLE001 - detection must never fail setup
        routed = {"error": f"unavailable ({type(exc).__name__})"}
        truth = {"error": f"unavailable ({type(exc).__name__})"}
    return {
        "himalaya_mail": himalaya,
        "productivity_secretary": secretary,
        "secretary_src": str(secretary_src),
        "secretary_src_candidates": _secretary_src_candidates(),
        "secretary_src_env": "SECRETARY_SRC",
        "calendar_tasks": secretary,
        "public_sources": True,
        "monitoring": monitoring,
        "routed_retrieval": routed,
        "truth": truth,
    }

def _secretary_src() -> str:
    try:
        from proactive_pilot.collectors import resolve_secretary_src
        return resolve_secretary_src()
    except Exception:  # noqa: BLE001 - detection must never fail setup
        return str(paths.hermes_home() / "productivity-secretary" / "src")


def _secretary_src_candidates() -> list[str]:
    try:
        from proactive_pilot.collectors import secretary_src_candidates
        return secretary_src_candidates()
    except Exception:  # noqa: BLE001 - detection must never fail setup
        return [str(paths.hermes_home() / "productivity-secretary" / "src"),
                str(Path.home() / "productivity-secretary" / "src")]


def _secretary_env_hint() -> str:
    return "SECRETARY_SRC (or PRODUCTIVITY_SECRETARY_SRC)"


def _authoritative_config() -> Any:
    """Load the authoritative validated configuration (native settings first).

    Raises ``userconfig.ConfigError`` when a present native/file configuration is
    invalid. A completely absent configuration is NOT an error (neutral defaults).
    """
    _ensure_src()
    from proactive_pilot import userconfig
    return userconfig.load_user_config(paths.user_config_path(), native=get_native_context())


def _run_offline_checks(fixture: str) -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.selftest import run_self_test
    from proactive_pilot.main import build_parser, run_once

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        test_rc = run_self_test()
    try:
        test_report = json.loads(buf.getvalue())
    except ValueError:
        test_report = {"raw": buf.getvalue()[-500:]}

    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
        "--fixture", fixture, "--dry-run", "--no-model",
    ]
    ns = build_parser().parse_args(argv)
    # Thread the host-built context so the authoritative (native) configuration
    # is validated during the offline check too; a no-model fixture run reads no
    # private connector and no location context.
    ns.native_ctx = get_native_context()
    # Same production contract as the scheduled pipeline: absent configuration
    # applies neutral generic defaults instead of arming legacy private sources.
    ns.require_config = True
    dry_rc = run_once(ns)
    return {
        "self_test_rc": test_rc,
        "self_test": {k: test_report.get(k) for k in ("total", "passed", "failed")},
        "dry_run_rc": dry_rc,
    }


def setup_command(args: argparse.Namespace, *, backend: cron_mod.CronBackend | None = None) -> int:
    """One-command idempotent setup. Returns 0 only on a fully verified state.

    Ordering is deliberate: data/policy/preferences are prepared (migration
    first if requested), the offline self-test + no-send fixture check run, and
    only THEN are the verified runners installed and the cron jobs armed. A
    failing check therefore creates zero cron jobs.
    """
    as_json = bool(getattr(args, "json", False))
    import_from = getattr(args, "import_from", None)
    if bool(getattr(args, "import_dry_run", False)):
        if not import_from:
            _emit({"ok": False, "error": "--import-dry-run requires --import-from <legacy_tree>"},
                  as_json=as_json,
                  human="setup --import-dry-run requires --import-from <legacy_tree>")
            return 2
        return _import_preview(import_from, as_json)

    result: dict[str, Any] = {
        "plugin": paths.PLUGIN_NAME,
        "data_root": str(paths.data_root()),
        "steps": [],
        "jobs": [],
        "runners": [],
        "warnings": [],
    }
    deliver_arg = getattr(args, "deliver", None)
    created: list[str] = []
    try:
        paths.ensure_data_dirs()
        result["steps"].append({"step": "data-dir", "status": "ok",
                                "detail": str(paths.data_root())})
        if import_from:
            imported = state_import.import_legacy(str(import_from))
            result["import"] = imported
            result["steps"].append({
                "step": "import", "status": "ok",
                "detail": f"{imported['counts']['copied']} copied, "
                          f"{imported['counts']['identical']} identical"})
        result["policy"] = _write_policy(bool(getattr(args, "force", False)))
        result["preferences"] = _ensure_preferences()
        # Prepare the generic configuration on a NEW install BEFORE any runner or
        # cron work. An existing deployment keeps its policy and must migrate its
        # own configuration explicitly; a neutral config is never forced over it.
        if result["policy"].get("action") == "written" and not import_from:
            result["user_config"] = _ensure_user_config()
        else:
            _uc = paths.user_config_path()
            result["user_config"] = {
                "user_config": str(_uc),
                "action": "kept" if (_uc.exists() and _uc.read_text(
                    encoding="utf-8").strip()) else
                    "deferred (existing policy; run `hermes secretary configure migrate`)",
            }
        # Validate the AUTHORITATIVE configuration with the host context BEFORE
        # any runner or cron work. A present-but-invalid native/file setting is a
        # hard failure: it can never be ignored by a fixture or a file default.
        loaded_cfg = _authoritative_config()
        result["user_config_effective"] = {
            "present": bool(loaded_cfg.present),
            "source": loaded_cfg.source,
            "destination": loaded_cfg.config["notification"]["destination"],
        }
        # An explicit setup (new or existing policy) backfills the same safe
        # projection. It never rewrites the config file and creates no grants
        # or jobs; a true no-op writes and backs up nothing.
        result["native_projection"] = _project_native_settings(
            loaded_cfg.config, reason="setup explicit projection")
        if result["native_projection"].get("error"):
            result["warnings"].append(
                "native settings projection did not complete: "
                + str(result["native_projection"]["error"]))
        # Effective notification destination: explicit --deliver wins, else a
        # real configured destination. 'local' is the safe fallback (record but
        # do not deliver), so it is not treated as a configured target; only when
        # NEITHER is set do we warn.
        configured_dest = (loaded_cfg.config["notification"]["destination"]
                           if loaded_cfg.present else "").strip()
        effective_dest = "" if configured_dest.lower() == "local" else configured_dest
        if deliver_arg:
            deliver = str(deliver_arg).strip()
        elif effective_dest:
            deliver = effective_dest
        else:
            deliver = "local"
        result["capabilities"] = detect_capabilities()

        # Verify BEFORE installing runners or arming cron.
        result["checks"] = _run_offline_checks(str(getattr(args, "fixture", "healthy_empty")))
        checks_ok = (result["checks"].get("self_test_rc") == 0
                     and result["checks"].get("dry_run_rc") == 0)
        result["steps"].append({
            "step": "verify", "status": "ok" if checks_ok else "failed",
            "detail": f"self-test rc={result['checks'].get('self_test_rc')}, "
                      f"dry-run rc={result['checks'].get('dry_run_rc')}"})
        if not checks_ok:
            raise RuntimeError(
                "offline verification failed "
                f"(self-test rc={result['checks'].get('self_test_rc')}, "
                f"dry-run rc={result['checks'].get('dry_run_rc')})")

        # Verified: install the owned runner templates before creating jobs.
        want_preparation = bool(getattr(args, "with_preparation", False))
        result["preparation"] = {
            "opt_in": want_preparation,
            "note": ("preparation runner/job requested explicitly"
                     if want_preparation else
                     "preparation runner/job not installed; enabling preparation does not arm it"),
        }
        result["runners"] = runner_install.install_runners(
            plugin_root=paths.plugin_root(),
            template_root=paths.runners_source_dir(),
            scripts_dir=paths.scripts_dir(),
            ownership_path=paths.runner_ownership_path(),
            include_preparation=want_preparation,
        )
        result["deliver"] = deliver
        if not deliver_arg and not effective_dest:
            result["warnings"].append(
                "no --deliver target given; defaulting to 'local'. Alerts will be recorded but "
                "not delivered until a target is set "
                "(e.g. --deliver origin or --deliver platform:chat_id)."
            )

        if not getattr(args, "no_cron", False):
            cron_backend = backend or _backend()
            if not cron_backend.is_available():
                raise cron_mod.CronError(
                    "the Hermes cron surface is unavailable; cannot create the two native jobs. "
                    "Re-run with a working `hermes cron` (or `--no-cron` for an offline check)."
                )
            specs = _job_specs(deliver, include_preparation=want_preparation)
            ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
            legacy_live = cron_mod.legacy_monitoring_job(
                cron_backend.list_jobs(), ownership)
            if legacy_live is not None:
                raise cron_mod.CronError(
                    "the recorded legacy monitoring job is still present "
                    f"(id={legacy_live.get('id') or '?'}); run `hermes secretary "
                    "migrate-jobs --apply` first. Setup refuses to create a duplicate "
                    "or recreate the existing schedule."
                )
            result["retired"] = cron_mod.reconcile_retired_jobs(
                cron_backend, {spec.name for spec in specs},
                ownership=ownership, ownership_path=paths.job_ownership_path())
            for spec in specs:
                res = cron_mod.ensure_job(
                    cron_backend, spec,
                    ownership=ownership, ownership_path=paths.job_ownership_path(),
                )
                result["jobs"].append({**res.to_dict(), "schedule": spec.schedule,
                                       "deliver": spec.deliver, "script": spec.script})
                if res.action == "created":
                    created.append(spec.name)
        else:
            result["warnings"].append("cron jobs were skipped (--no-cron)")

        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - fail loudly and roll back honestly
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        if created and not getattr(args, "no_cron", False):
            rolled, failures = _rollback_created_jobs(created, backend)
            if rolled:
                result["rolled_back"] = rolled
            if failures:
                result["rollback_failures"] = failures
        _emit(result, as_json=as_json, human=_setup_human(result))
        return 1

    _emit(result, as_json=as_json, human=_setup_human(result))
    return 0


def _import_preview(legacy_tree: str, as_json: bool) -> int:
    """Preview an explicit legacy import: inventory/hashes only, no changes."""
    result: dict[str, Any] = {
        "plugin": paths.PLUGIN_NAME,
        "dry_run": True,
        "legacy_root": str(legacy_tree),
        "steps": [],
        "warnings": [],
    }
    try:
        manifest = state_import.preview(str(legacy_tree))
        result["import"] = manifest
        result["ok"] = bool(manifest.get("ok"))
    except Exception as exc:  # noqa: BLE001 - preview must fail loudly, not crash
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    _emit(result, as_json=as_json, human=_setup_human(result))
    return 0 if result["ok"] else 1


def _rollback_created_jobs(created: list[str],
                           backend: cron_mod.CronBackend | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Remove only the jobs created in this run; report anything that stayed.

    A removal that raised or that did not actually remove the job is recorded as
    a failure rather than claimed as rolled back.
    """
    rolled: list[str] = []
    failures: list[dict[str, Any]] = []
    rollback_backend = backend or _backend()
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    for name in created:
        try:
            res = cron_mod.remove_job(
                rollback_backend, name,
                ownership=ownership, ownership_path=paths.job_ownership_path(),
            )
        except Exception as exc:  # noqa: BLE001 - honest failure, never suppressed
            failures.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if res.action in ("removed", "missing"):
            rolled.append(name)
        else:
            failures.append({"name": name, "action": res.action, "detail": res.detail})
    return rolled, failures


def _job_specs(deliver: str = "local", *,
               include_preparation: bool = False) -> list[cron_mod.CronJobSpec]:
    return cron_mod.build_job_specs(
        deliver=deliver, workdir=str(paths.plugin_root()),
        include_preparation=include_preparation)


def _setup_human(result: dict[str, Any]) -> str:
    if result.get("dry_run") and "import" in result:
        imp = result["import"]
        counts = imp.get("counts", {})
        lines = [
            "Proactive Secretary legacy import preview (no changes made).",
            f"  legacy        : {imp.get('legacy_root', '?')}",
            f"  files         : {counts.get('total', 0)} total, "
            f"{counts.get('copied', 0)} to copy, {counts.get('identical', 0)} identical, "
            f"{counts.get('excluded', 0)} excluded, {counts.get('conflicts', 0)} conflicts",
            f"  manifest sha  : {imp.get('manifest_sha256', '')}",
        ]
        if imp.get("excluded"):
            lines.append(f"  excluded names: {', '.join(imp['excluded'])}")
        for conflict in imp.get("conflicts", []):
            lines.append(f"  conflict      : {conflict['rel']} -> {conflict['target']} "
                         f"({conflict.get('reason', '')})")
        if not result.get("ok"):
            lines.append(f"  error         : {result.get('error', 'conflicting target state')}")
        return "\n".join(lines)
    if not result.get("ok"):
        lines = ["Proactive Secretary setup FAILED.", f"  reason: {result.get('error', 'unknown')}"]
        if result.get("rolled_back"):
            lines.append(f"  rolled back jobs: {', '.join(result['rolled_back'])}")
        if result.get("rollback_failures"):
            lines.append(f"  rollback FAILURES (still present): "
                         f"{json.dumps(result['rollback_failures'], sort_keys=True)}")
        return "\n".join(lines)
    caps = result.get("capabilities", {})
    jobs = {j["name"]: j for j in result.get("jobs", [])}
    if caps.get("calendar_tasks") or caps.get("himalaya_mail"):
        monitored = ["public sources"]
        if caps.get("calendar_tasks"):
            monitored.append("calendar + tasks (shipped secretary library)")
        if caps.get("himalaya_mail"):
            monitored.append("authorised mail (himalaya, read-only metadata)")
    else:
        monitored = ["public sources only (no mail/calendar connector detected)"]
    lines = [
        "Proactive Secretary configuration and owned jobs were installed.",
        f"  data           : {result.get('data_root')}",
        f"  policy         : {result.get('policy', {}).get('action', '?')} "
        f"({result.get('policy', {}).get('policy', '')})",
        f"  monitored      : {', '.join(monitored)}",
        f"  deliver target : {result.get('deliver', 'local')}",
        "  delivery       : unverified until an explicit native canary",
        "  NOT covered    : undated tasks; blocked/deferred/truncated sources; "
        "delivery confirmation is owned by the native scheduler",
        "  jobs           :",
    ]
    display_names = list(OWNED_JOB_NAMES)
    try:
        recorded = cron_mod.load_job_ownership(paths.job_ownership_path()).get("jobs") or {}
    except Exception:  # noqa: BLE001 - display must never hide the rest of setup
        recorded = {}
    for name in cron_mod.OPTIONAL_JOB_NAMES:
        if name in jobs or name in recorded or (result.get("preparation") or {}).get("opt_in"):
            display_names.append(name)
    for name in display_names:
        job = jobs.get(name)
        lines.append(f"    - {name}: {job['action'] if job else 'MISSING'} "
                     f"[{job['schedule'] if job else '?'}]")
    if result.get("import"):
        counts = result["import"].get("counts", {})
        lines.append(f"  imported       : {counts.get('copied', 0)} copied, "
                     f"{counts.get('identical', 0)} identical, "
                     f"manifest {result['import'].get('manifest_sha256', '')[:12]}")
    checks = result.get("checks", {})
    lines.append(f"  self-test      : {checks.get('self_test', {})}")
    lines.append("  pause anytime  : hermes secretary pause")
    lines.append("  remove jobs    : hermes secretary disable")
    for warning in result.get("warnings", []):
        lines.append(f"  warning        : {warning}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# status / doctor
# --------------------------------------------------------------------------- #

def _job_states() -> dict[str, Any]:
    empty = {"available": False, "state": "unavailable", "jobs": [],
             "owned": [], "foreign": [], "missing": [], "error": ""}
    try:
        backend = _backend()
        if not backend.is_available():
            return dict(empty)
        jobs = backend.list_jobs()
    except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
        result = dict(empty)
        result["error"] = str(exc)
        return result
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    classified = cron_mod.classify_jobs(jobs, ownership)
    classified["available"] = True
    return classified


def _jobs_human(jobs: dict[str, Any]) -> str:
    if not jobs.get("available"):
        return "unavailable"
    owned = jobs.get("owned", [])
    foreign = jobs.get("foreign", [])
    if not owned and not foreign:
        return "none (no jobs of ours)"
    text = f"{len(owned)} owned"
    if owned:
        text += f" [{', '.join(j.get('name', '?') for j in owned)}]"
    if foreign:
        text += (f"; WARNING {len(foreign)} foreign job(s) share our name and are "
                 f"left untouched [{', '.join(j.get('name', '?') for j in foreign)}]")
    missing = jobs.get("missing", [])
    if missing:
        text += f"; missing {', '.join(missing)}"
    return text


def status_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    state = _read_json(paths.state_dir() / "notify_state.json")
    reminders = _read_json(paths.state_dir() / "reminders.json")
    prefs = _prefs()
    caps = detect_capabilities()
    # Same authoritative native-first configuration/settings as setup/doctor and
    # the runtime pipeline (a present-but-invalid setting is reported, never
    # silently replaced by a file/default).
    config_error = ""
    config_summary: dict[str, Any] = {}
    destination = ""
    try:
        _loaded = _authoritative_config()
        config_summary = _loaded.summary()
        destination = _loaded.config["notification"]["destination"]
    except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
        config_error = str(exc)
    active_reminders = sum(
        1 for r in reminders.get("reminders", {}).values()
        if isinstance(r, dict) and r.get("status") in {"pending", "uncertain"}
    )
    ctx = get_native_context()
    # One bounded safe projection shared with the /secretary status handler.
    try:
        projection = secretary_status.collect(ctx, cron_backend=_backend(),
                                              probe_empty=True)
    except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
        projection = {"model_status": {"strict_active": False,
                                       "blockers": [type(exc).__name__]},
                      "jobs": {"available": False, "owned": [], "foreign": [],
                               "missing": [], "state": "unavailable"},
                      "health_incident": None,
                      "prepare_queue": {"ready": 0, "error": type(exc).__name__},
                      "owned_jobs": [], "next_schedule": "",
                      "onboarding": {"status": "not_started", "next_step": "basics"}}
    model_routes: dict[str, Any] = dict(projection.get("model_status") or {})
    model_capability = dict(projection.get("model_capability") or {})
    if not model_capability:
        model_capability = secretary_status.model_capability_health(
            model_routes, paused=_is_paused())
    onboarding = dict(projection.get("onboarding") or {})
    onboarding.pop("next_question", None)
    onboarding = onboarding or {"status": "not_started", "next_step": "basics"}
    health_incident = projection.get("health_incident")
    prepare_queue = dict(projection.get("prepare_queue") or {"ready": 0, "error": ""})
    job_states = dict(projection.get("jobs") or {})
    owned_names = [str(j.get("name") or "") for j in projection.get("owned_jobs") or []
                   if isinstance(j, dict)]
    next_run = str(projection.get("next_schedule") or "")
    next_action = ""
    if str(onboarding.get("status") or "") == "pending_verification":
        next_action = ("hermes secretary setup --deliver "
                       + (destination or "<target>")
                       + " (then an explicitly authorised native canary)")
    payload = {
        "plugin": paths.PLUGIN_NAME,        "enabled": not _is_paused(),
        "paused": _is_paused(),
        "policy_present": paths.policy_path().is_file(),
        "data_root": str(paths.data_root()),
        "jobs": job_states,
        "capabilities": caps,
        "monitoring": caps.get("monitoring", "public-only"),
        "config": config_summary,
        "config_error": config_error,
        "deliver": destination or "local (default)",
        "last_monitoring_run": (state.get("last_inference") or {}).get("at", ""),
        # Compatibility alias retained for existing machine-readable callers.
        "last_pilot_run": (state.get("last_inference") or {}).get("at", ""),
        "coverage": state.get("health", {}),
        "models": model_routes,
        "model_capability": model_capability,
        "strict_active": bool(model_routes.get("strict_active", False)),
        "model_blockers": list(model_routes.get("blockers") or []),
        "onboarding": onboarding or {"status": "not_started", "next_step": "basics"},
        "next_action": next_action,
        "owned_jobs": owned_names,
        "next_schedule": next_run,
        "delivery": {
            "configured": bool(destination),
            "configured_target": destination,
            "state": "unverified" if destination else "not-configured",
            "verified_at": "",
            "pending_verification": bool(destination),
        },
        "health_incident": health_incident.get("incident"),
        "prepare_queue": prepare_queue,
        "counts": {
            "active_reminders": active_reminders,
            "explicit_preferences": len(prefs.get("explicit", [])),
            "inferred_preferences": len(prefs.get("inferred", [])),
            "snoozes": len(prefs.get("snoozes", [])),
        },
    }
    if caps.get("monitoring") == "public-only":
        monitoring = (f"public-only (calendar/tasks unavailable; set {_secretary_env_hint()} "
                      "to the library 'src' directory)")
    else:
        monitoring = caps.get("monitoring", "full")
    layout_line = secretary_status.layout_diagnostic(paths.plugin_root())
    human = "\n".join([
        ("Proactive Secretary: installed; "
         + ("paused (collection/delivery stopped)" if payload['paused'] else "not paused")
         + f"; onboarding={payload['onboarding'].get('status')}"
         + f"; jobs={len(owned_names)} owned"
         + f"; delivery={payload['delivery']['state']}"),
        f"  data root     : {payload['data_root']}",
        f"  policy        : {'present' if payload['policy_present'] else 'MISSING'}",
        f"  config        : {'invalid: ' + config_error if config_error else ('present' if config_summary.get('present') else 'defaults (neutral)')}",
        f"  deliver       : {payload['deliver']}",
        f"  monitoring    : {monitoring}",
        f"  cron backend  : {'available' if payload['jobs'].get('available') else 'unavailable'}",
        f"  jobs          : {_jobs_human(payload['jobs'])}",
        f"  last monitoring run: {payload['last_monitoring_run'] or 'never recorded'}",
        f"  active reminders: {active_reminders}",
        f"  model route   : strict_active={payload['strict_active']}"
        + (f" blockers={'; '.join(payload['model_blockers'])}" if payload['model_blockers'] else ""),
        f"  model capability: {model_capability.get('status', 'unknown')}"
        + (f" - {model_capability.get('detail')}"
           if model_capability.get("status") == "blocked" else ""),
        f"  prepare queue : {payload['prepare_queue'].get('ready', 0)} ready"
        + (f" (error {payload['prepare_queue'].get('error')})" if payload['prepare_queue'].get('error') else ""),
        f"  next schedule : {payload['next_schedule'] or 'none recorded'}",
    ])
    if layout_line:
        human += f"\n  {layout_line}"
    if next_action:
        human += f"\n  next action   : {next_action}"
    _emit(payload, as_json=as_json, human=human)
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})

    root = paths.plugin_root()
    add("plugin_root", root.is_dir(), str(root))
    add("src_package", (root / "src" / "proactive_pilot" / "__init__.py").is_file(),
        str(root / "src" / "proactive_pilot"))
    add("default_policy", paths.default_policy_path().is_file(), str(paths.default_policy_path()))
    templates = paths.runners_source_dir()
    for runner in cron_mod.PILOT_SCRIPT, cron_mod.REMINDER_SCRIPT:
        add(f"runner_template:{runner}", (templates / runner).is_file(), str(templates / runner))
    add("scripts_dir_writable", _writable(paths.scripts_dir()), str(paths.scripts_dir()))
    add("data_dir_writable", _writable(paths.data_root()), str(paths.data_root()))
    jobs = _job_states()
    add("cron_backend", bool(jobs.get("available")), json.dumps(jobs.get("error", "available") or "available"))
    if jobs.get("available"):
        job_names = [str(j.get("name") or "") for j in jobs.get("jobs") or []
                     if isinstance(j, dict)]
        job_names += [str(name) for name in jobs.get("missing") or []]
        add("owned_jobs", bool(jobs.get("state") not in {"foreign", "mixed"}),
            f"state={jobs.get('state', 'none')}; names={', '.join(job_names) or 'none'}",
            critical=False)
    caps = detect_capabilities()
    add("himalaya_mail", caps["himalaya_mail"],
        "read-only mail connector" if caps["himalaya_mail"]
        else "not found; install/authorise himalaya to enable mail monitoring",
        critical=False)
    if caps["productivity_secretary"]:
        add("productivity_secretary", True, caps["secretary_src"], critical=False)
    else:
        add("productivity_secretary", False,
            "not found (searched: "
            + ", ".join(caps.get("secretary_src_candidates", []))
            + f"); calendar/tasks unavailable, set {_secretary_env_hint()} to the "
              "library 'src' directory to enable them", critical=False)
    public_truth = (caps.get("truth") or {}).get("public") or {}
    add("public_sources", True,
        "public transport available; coverage=" + str(public_truth.get("status", "unknown"))
        + (" (" + str(public_truth.get("remedy")) + ")" if public_truth.get("remedy") else ""),
        critical=False)
    add("monitoring_mode", True, caps.get("monitoring", "public-only"), critical=False)
    truth = caps.get("truth") or {}
    if truth:
        add("capability_truth", True,
            "; ".join(f"{name}={row.get('status', 'unknown')}"
                      for name, row in sorted(truth.items())
                      if isinstance(row, dict) and row.get("status")),
            critical=False)
    routed = caps.get("routed_retrieval") or {}
    fc = routed.get("firecrawl") or {}
    render_cap = routed.get("native_render_fallback") or {}
    add("routed_firecrawl", bool(fc.get("available")),
        str(fc.get("reason") or "unavailable"), critical=False)
    add("native_render_fallback", bool(render_cap.get("available")),
        str(render_cap.get("reason") or "unavailable"), critical=False)
    # Validated user configuration + optional adapters (never credential reads).
    # The AUTHORITATIVE native-first configuration is loaded with the host ctx;
    # a present-but-invalid setting is a CRITICAL failure and can never be
    # ignored via a fixture or a file default.
    gws_enabled = False
    try:
        _loaded = _authoritative_config()
        gws_enabled = bool(_loaded.config.get("adapters", {}).get("gws_calendar"))
        _destination = _loaded.config["notification"]["destination"]
        add("user_config_valid", True,
            ("present" if _loaded.present else "defaults (no user configuration)")
            + f"; deliver={_destination or 'local (default)'}",
            critical=False)
    except Exception as exc:  # noqa: BLE001 - invalid config is a hard doctor failure
        add("user_config_valid", False,
            f"invalid configuration: {exc} "
            "(run `hermes secretary configure validate` or `configure migrate`)",
            critical=True)
    try:
        from proactive_pilot.gws_calendar import readiness_report
        gws = readiness_report({"gws_calendar_enabled": gws_enabled})
        add("gws_calendar", bool(gws.get("ready")),
            f"{gws.get('reason')} (backend={gws.get('backend') or 'none'})",
            critical=False)
    except Exception as exc:  # noqa: BLE001
        add("gws_calendar", False, f"probe failed: {type(exc).__name__}", critical=False)
    add("native_context", get_native_context() is not None,
        "host ctx available" if get_native_context() is not None else "offline (subprocess route)",
        critical=False)
    try:
        from .secretary_models import effective_route_status
        from proactive_pilot.native_runtime import bound_model_lane_status
        _ctx = get_native_context()
        _bound = bound_model_lane_status(_ctx)
        _route = effective_route_status(
            _ctx, strict_active=bool((_bound or {}).get("strict_active")))
        if _bound:
            _route.update(_bound)
        _capability = secretary_status.model_capability_health(
            _route, paused=_is_paused())
        add("model_capability", bool(_capability.get("healthy", True)),
            str(_capability.get("detail") or ""),
            critical=not bool(_capability.get("healthy", True)))
        add("model_strict_capability", bool(_route.get("strict_active")),
            ("strict configured-only capability available"
             if _route.get("strict_active") else
             "strict configured-only capability unavailable on this host "
             "(this is a capability line, not a provider-availability claim)")
            + (("; blockers=" + "; ".join(_route.get("blockers") or []))
               if _route.get("blockers") else ""),
            critical=False)
    except Exception as exc:  # noqa: BLE001
        add("model_capability", False,
            f"model capability check unavailable ({type(exc).__name__})",
            critical=True)
        add("model_strict_capability", False,
            f"route capability check unavailable ({type(exc).__name__})", critical=False)
    failed = [c for c in checks if c["critical"] and not c["ok"]]
    payload = {"ok": not failed, "checks": checks, "failed": [c["check"] for c in failed],
               "capabilities": caps}
    human = "\n".join(
        f"[{'ok' if c['ok'] else ('FAIL' if c['critical'] else 'off')}] "
        f"{c['check']}: {c['detail']}" for c in checks
    )
    _emit(payload, as_json=as_json, human=human)
    return 0 if not failed else 1



def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# pause / resume / disable / uninstall
# --------------------------------------------------------------------------- #

def _apply_feedback(action: str, provenance: str) -> int:
    _ensure_src()
    from proactive_pilot.clock import Clock
    from proactive_pilot.feedback import (
        FeedbackError, apply_action, load_preferences_file, save_preferences_file,
    )
    now = Clock().now()
    path = paths.preferences_path()
    prefs = load_preferences_file(path, strict=True)
    updated = apply_action(prefs, {"action": action, "provenance": provenance}, now)
    save_preferences_file(path, updated, now)
    return 0


def pause_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        _apply_feedback("pause", "hermes secretary pause")
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": str(exc)}, as_json=as_json, human=f"pause failed: {exc}")
        return 1
    try:
        portal = portal_controls.pause_portal(
            _backend, paths.job_ownership_path())
    except Exception as exc:  # noqa: BLE001 - deterministic pause is already saved
        portal = {"ok": False, "portal": "error", "error": str(exc)}
    if not portal.get("ok"):
        detail = portal.get("detail") or portal.get("error") or "portal pause failed"
        _emit({"ok": False, "paused": True, "portal": portal,
               "error": "collection and delivery are paused, but the owned portal "
                        f"job was not confirmed stopped: {detail}"},
              as_json=as_json,
              human=("Proactive Secretary paused for collection and delivery, but the "
                     f"owned portal job was NOT confirmed stopped: {detail}"))
        return 1
    _emit({"ok": True, "paused": True, "portal": portal}, as_json=as_json,
          human=("Proactive Secretary paused: collection and delivery are stopped"
                 + ("" if portal.get("portal") == "not-opted-in"
                    else f"; portal job {portal.get('portal')}")))
    return 0


def resume_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        _apply_feedback("resume", "hermes secretary resume")
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": str(exc)}, as_json=as_json, human=f"resume failed: {exc}")
        return 1
    try:
        portal = portal_controls.resume_portal(
            _backend, paths.job_ownership_path())
    except Exception as exc:  # noqa: BLE001 - deterministic pause is already cleared
        portal = {"ok": False, "portal": "error", "error": str(exc)}
    if not portal.get("ok"):
        detail = portal.get("detail") or portal.get("error") or "portal resume failed"
        _emit({"ok": False, "paused": False, "portal": portal,
               "error": "collection and delivery are resumed, but the owned portal "
                        f"job was not confirmed resumed: {detail}"},
              as_json=as_json,
              human=("Global pause cleared for collection and delivery, but the owned "
                     f"portal job was NOT confirmed resumed: {detail}"))
        return 1
    jobs = _job_states()
    owned = [str(j.get("name") or "") for j in jobs.get("owned") or [] if isinstance(j, dict)]
    ready = ("no longer paused; jobs recorded: " + (", ".join(owned) if owned else "none")
             + "; delivery is still unverified until an explicit native canary")
    _emit({"ok": True, "paused": False, "portal": portal, "jobs": owned,
           "readiness": "not-paused" if owned else "no-owned-jobs",
           "delivery": "unverified"},
          as_json=as_json,
          human=f"Proactive Secretary is {ready}. Run `hermes secretary setup --deliver "
                "<verified-target>` to arm jobs, then perform the explicit canary.")
    return 0


def migrate_jobs_command(args: argparse.Namespace) -> int:
    """Dry-run or apply the explicit in-place legacy monitoring rename.

    This command never recreates a job. It only calls a host-supported update
    seam on the exact recorded id; without that seam it fails and changes
    nothing.
    """
    as_json = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    try:
        backend = _backend()
        if not backend.is_available():
            raise cron_mod.CronError("the Hermes cron surface is unavailable")
        ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
        result = cron_mod.migrate_legacy_monitoring_job(
            backend,
            ownership=ownership,
            ownership_path=paths.job_ownership_path(),
            apply=apply,
        )
    except Exception as exc:  # noqa: BLE001 - a blocker is reported, never guessed
        _emit({"ok": False, "applied": apply,
               "error": f"{type(exc).__name__}: {exc}"},
              as_json=as_json, human=f"migrate-jobs FAILED: {exc}")
        return 1
    payload = {
        "ok": True,
        "applied": apply,
        "action": result.action,
        "name": result.name,
        "detail": result.detail,
        "legacy_name": cron_mod.LEGACY_PILOT_JOB_NAME,
        "monitoring_name": cron_mod.MONITORING_JOB_NAME,
    }
    if not apply:
        payload["note"] = "dry-run; no job or ownership record was changed"
    _emit(payload, as_json=as_json,
          human=(f"migrate-jobs: {result.action} ({result.detail})"
                 + ("" if apply else " [dry-run; re-run with --apply]")))
    return 0


def _remove_own_jobs() -> list[dict[str, Any]]:
    backend = _backend()
    removed: list[dict[str, Any]] = []
    if not backend.is_available():
        raise cron_mod.CronError("the Hermes cron surface is unavailable; cannot remove jobs")
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    names = (OWNED_JOB_NAMES + cron_mod.OPTIONAL_JOB_NAMES
             + cron_mod.LEGACY_OWNED_JOB_NAMES + cron_mod.RETIRED_JOB_NAMES)
    if cron_mod.owned_job_record(ownership, cron_mod.PORTAL_JOB_NAME) is not None:
        names += (cron_mod.PORTAL_JOB_NAME,)
    for name in names:
        res = cron_mod.remove_job(
            backend, name,
            ownership=ownership, ownership_path=paths.job_ownership_path(),
        )
        removed.append(res.to_dict())
    return removed


def _remove_own_runners() -> list[dict[str, Any]]:
    return runner_install.uninstall_runners(
        ownership_path=paths.runner_ownership_path(),
        scripts_dir=paths.scripts_dir(),
    )


def disable_command(args: argparse.Namespace, *, uninstall: bool = False) -> int:
    as_json = bool(getattr(args, "json", False))
    result: dict[str, Any] = {"ok": False, "removed": [], "scripts": [], "paused": False}
    try:
        _apply_feedback("pause", "hermes secretary disable")
        result["paused"] = True
        result["removed"] = _remove_own_jobs()
        result["scripts"] = _remove_own_runners()
        if uninstall and getattr(args, "purge", False):
            result["data_purged"] = _purge_data()
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        _emit(result, as_json=as_json, human=f"{'uninstall' if uninstall else 'disable'} FAILED: {exc}")
        return 1
    removed_desc = ", ".join(f"{r['name']} ({r['action']})" for r in result["removed"])
    scripts_desc = ", ".join(
        f"{Path(r['file']).name} ({r['action']})" for r in result["scripts"]
    )
    human = (
        f"Proactive Secretary {'uninstalled' if uninstall else 'disabled'}.\n"
        f"  paused : yes (collection and delivery stopped)\n"
        f"  removed: {removed_desc or 'no owned jobs were present'}\n"
        f"  scripts: {scripts_desc or 'no owned runner scripts were present'}"
    )
    if uninstall:
        human += "\n  data   : runtime data preserved (pass --purge to delete it)"
    _emit(result, as_json=as_json, human=human)
    return 0


def _purge_data() -> bool:
    import shutil as _shutil
    root = paths.data_root()
    if root.exists():
        _shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


def uninstall_command(args: argparse.Namespace) -> int:
    return disable_command(args, uninstall=True)


# --------------------------------------------------------------------------- #
# self-test / run-once / watch / feedback
# --------------------------------------------------------------------------- #

def selftest_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.selftest import run_self_test
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_self_test()
    try:
        report = json.loads(buf.getvalue())
    except ValueError:
        report = {"raw": buf.getvalue()[-1000:]}
    human = (f"self-test: {report.get('passed')}/{report.get('total')} passed "
             f"({report.get('failed')} failed)")
    _emit(report, as_json=as_json, human=human)
    return rc


def run_once_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.main import build_parser, run_once
    use_model = bool(getattr(args, "model", False))
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
    ]
    if not use_model:
        argv.append("--no-model")
    if getattr(args, "check", False):
        argv.append("--check")
    else:
        argv.append("--dry-run")
    if getattr(args, "fixture", None):
        argv += ["--fixture", str(args.fixture)]
    ns = build_parser().parse_args(argv)
    # The host-built context is threaded through the registered handler closure
    # for EVERY operational command -- including no-model run-once and the
    # scheduled pipeline -- so native settings validation and every model lane
    # see the same context. This module never constructs one.
    ns.native_ctx = get_native_context()
    # Production entry: with no configuration present, apply neutral generic
    # defaults (no implicit private/legacy sources) rather than the old policy.
    ns.require_config = True
    rc = run_once(ns)
    payload = {"ok": rc == 0, "mode": "check" if getattr(args, "check", False) else "dry-run",
               "fixture": getattr(args, "fixture", None), "rc": rc}
    _emit(payload, as_json=as_json,
          human=f"run-once ({payload['mode']}) completed rc={rc}; nothing was sent.")
    return rc


def pipeline_command(args: argparse.Namespace) -> int:
    """Internal scheduled handler: run the ordinary pipeline in the host context.

    The scheduled pilot runner invokes ``hermes secretary pipeline`` (fixed argv,
    same profile/data paths) so the registered handler closure receives the
    host-built ``ctx`` from native plugin discovery. That is what makes
    ``ctx.llm`` and native ``get_config`` authoritative on the normal schedule;
    the runner never constructs a context itself. A real ``--run`` pipeline is
    used (NOT run-once's dry-run), and stdout remains the single alert line.
    """
    _ensure_src()
    from proactive_pilot.main import build_parser, run_once
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
    ]
    if getattr(args, "check", False):
        argv.append("--check")
    if getattr(args, "fixture", None):
        argv += ["--fixture", str(args.fixture)]
    ns = build_parser().parse_args(argv)
    ns.native_ctx = get_native_context()
    # Production entry: a missing configuration must not silently re-enable the
    # legacy private/formal+informal sources.
    ns.require_config = True
    return run_once(ns)


def _preparation_gate() -> tuple[bool, str]:
    """Authoritative gate re-read for every preparation read/write step."""
    _ensure_src()
    try:
        from proactive_pilot.clock import Clock
        from proactive_pilot.feedback import load_preferences_file
        from proactive_pilot.preparation import gate_status

        loaded, policy = _effective_config_and_policy()
        prefs = load_preferences_file(paths.preferences_path(), strict=True)
        status = gate_status(policy, prefs, config_present=loaded.present,
                             now=Clock().now())
        return bool(status.get("allowed")), str(status.get("reason") or "")
    except Exception as exc:  # noqa: BLE001 - a broken gate fails closed
        return False, f"gate unavailable: {type(exc).__name__}"


def prepare_command(args: argparse.Namespace) -> int:
    """One bounded private-preparation unit through the native auxiliary lane.

    Invoked by the no-agent script bridge; it never creates an agent, agent job,
    tool loop or new messenger. The shared engine claims at most one due accepted
    responsibility, asks the configured native lane for one bounded structured
    draft, and lets the deterministic writer verify/queue it. No lane means no
    claim and no model call.
    """
    _ensure_src()
    from proactive_pilot.clock import SeededClock

    def _diag(reason: str, **extra: Any) -> None:
        payload = {"diagnostic": "preparation-skip", "reason": reason}
        payload.update(extra)
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)

    clock = SeededClock(args.now) if getattr(args, "now", None) else None
    ctx = get_native_context()
    if ctx is None:
        _diag("no native host context")
        return 0
    try:
        from . import secretary_preparation
    except Exception as exc:  # noqa: BLE001 - fail closed without work
        _diag("preparation runtime unavailable", error=type(exc).__name__)
        return 0
    result = secretary_preparation.run_unit(ctx, clock=clock)
    if not result.get("ok"):
        _diag(str(result.get("reason") or "preparation skipped"),
              stage=str(result.get("stage") or ""))
        return 0
    print(json.dumps({
        "ok": True,
        "responsibility_id": result["responsibility_id"],
        "artifact_ref": result["artifact_ref"],
        "queued": True,
        "external_effect": "none",
    }, sort_keys=True), file=sys.stderr)
    return 0

def context_command(args: argparse.Namespace) -> int:
    """Foreground bounded context refresh through the existing snapshot module.

    This is the production owner for context updates/revocation: it writes only
    validated, bounded entries and honours the existing expiry/revocation fences.
    It never reads a private provider or invents a raw history collector.
    """
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    import types
    from proactive_pilot.contextsnapshot import context_command as _context

    action = str(getattr(args, "action", "list"))
    ns = types.SimpleNamespace(
        feedback=f"context-{action}",
        state_dir=str(paths.state_dir()),
        id=str(getattr(args, "id", "") or ""),
        note=str(getattr(args, "note", "") or ""),
        provenance=str(getattr(args, "provenance", "") or ""),
        kind=str(getattr(args, "kind", "") or ""),
        expires_at=str(getattr(args, "expires_at", "") or ""),
        owner_declared=bool(getattr(args, "owner_declared", False)),
        seeded_clock=None,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _context(ns)
    output = buf.getvalue().strip()
    try:
        parsed = json.loads(output) if output else {}
    except ValueError:
        parsed = {"raw": output[-500:]}
    _emit(parsed, as_json=as_json, human=output or "context command completed")
    return rc


def responsibilities_command(args: argparse.Namespace) -> int:
    """Foreground responsibility lifecycle + handoff inspection.

    The CLI is itself an explicit foreground human action. Create/update/cancel
    still require an evidence quote and a ``conversation:`` source ref; no
    assistant assertion or model output can create authority here.
    """
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    try:
        from .secretary_responsibilities import dispatch
    except ImportError:  # pragma: no cover - direct module execution
        from secretary.secretary_responsibilities import dispatch

    action = str(getattr(args, "action", "list"))
    payload: dict[str, Any] = {
        "action": action,
        "responsibility_id": getattr(args, "responsibility_id", ""),
        "kind": getattr(args, "kind", "commitment"),
    }
    for key, attr in (
            ("desired_outcome", "desired_outcome"),
            ("acceptance_criterion", "acceptance_criterion"),
            ("due_at", "due_at"), ("next_check_at", "next_check_at"),
            ("source_version", "source_version"), ("source_ref", "source_ref"),
            ("conversation_ref", "conversation_ref"),
            ("evidence_quote", "evidence_quote"), ("reason", "reason")):
        value = getattr(args, attr, "")
        if value not in (None, ""):
            payload[key] = value
    try:
        result = dispatch(payload)
    except Exception as exc:  # noqa: BLE001 - bounded foreground error
        _emit({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
              as_json=as_json,
              human=f"responsibility {action} failed: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    if as_json:
        _emit(result, as_json=True, human="")
        return 0
    if action == "list":
        rows = result.get("responsibilities") or []
        lines = [f"{len(rows)} responsibility record(s):"]
        for rec in rows:
            lines.append(f"  {rec.get('id')} [{rec.get('status')}] "
                         f"{str(rec.get('desired_outcome') or '')[:80]}")
        human = "\n".join(lines)
    elif action == "handoff":
        handoff = result.get("handoff") or {}
        human = (f"Handoff proposal (NOT executed)\n"
                 f"  responsibility: {handoff.get('responsibility_id')}\n"
                 f"  artifact      : {handoff.get('artifact_ref')}\n"
                 f"  hash          : {handoff.get('artifact_hash')}\n"
                 f"  generation    : {(handoff.get('current_version') or {}).get('generation')}\n"
                 f"  target        : {json.dumps(handoff.get('target') or {}, sort_keys=True)}\n"
                 "Use normal foreground Hermes tools with an exact approval; this command "
                 "never sends, books, purchases or mutates anything.")
    else:
        rec = result.get("responsibility") or {}
        human = (f"Responsibility {rec.get('id')} [{rec.get('status')}] "
                 f"generation={rec.get('generation')}\n"
                 f"  outcome: {str(rec.get('desired_outcome') or '')[:120]}\n"
                 "No external action was taken.")
    _emit(result, as_json=False, human=human)
    return 0


def reminders_command(args: argparse.Namespace) -> int:
    """Registered deterministic reminder handler (no model, no ``ctx.llm``).

    Forwards the host-built native context to ``reminders.main`` so the SAME
    validated per-user configuration precedence as monitoring is authoritative
    for aliases/calendars. The command never builds a context and never requests
    a model; the scheduled no-argv runner enters this handler.
    """
    _ensure_src()
    from proactive_pilot.reminders import main as reminders_main
    rest = [str(a) for a in (getattr(args, "reminder_args", []) or [])]
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
        *rest,
    ]
    if not rest or not any(
            a in ("dispatch", "check", "list", "status")
            or a.startswith("--add") or a == "--cancel" or a == "--scheduler-command"
            for a in rest):
        argv.append("dispatch")
    return reminders_main(argv, native_ctx=get_native_context(), require_config=True)


def watch_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.main import main as pilot_main
    action = args.action
    if action == "list":
        argv = ["--feedback", "watch-list"]
    elif action == "add":
        if not args.value.strip():
            _emit({"ok": False, "error": "watch add requires a topic"},
                  as_json=as_json, human="watch add requires a topic")
            return 2
        argv = ["--feedback", "watch-add", "--watch-topic", args.value, "--scope", args.scope]
        if args.cadence_minutes is not None:
            argv += ["--minutes", str(args.cadence_minutes)]
    else:  # remove
        if not args.value.strip():
            _emit({"ok": False, "error": "watch remove requires a watch id"},
                  as_json=as_json, human="watch remove requires a watch id")
            return 2
        argv = ["--feedback", "watch-forget", "--id", args.value]
    argv = ["--state-dir", str(paths.state_dir()), "--policy", str(paths.policy_path()),
            "--preferences", str(paths.preferences_path()), *argv]
    return pilot_main(argv)


def feedback_command(args: argparse.Namespace) -> int:
    _ensure_src()
    from proactive_pilot.main import main as pilot_main
    rest = list(getattr(args, "feedback_args", []) or [])
    if not rest:
        _emit({"ok": False, "error": "feedback requires an action (e.g. list, add-explicit)"},
              as_json=bool(getattr(args, "json", False)),
              human="usage: hermes secretary feedback <action> [options]")
        return 2
    if str(rest[0]).startswith("config-proposal") or str(rest[0]) in {
            "config-show", "config-dismiss", "config-approve"}:
        # Use the SAME effective native/local configuration and current public
        # registry that the scheduled pipeline uses. Never apply a change here.
        import argparse as _argparse
        from proactive_pilot import config_proposals
        _loaded, effective = _effective_config_and_policy()
        public_registry = _read_json(paths.state_dir() / "public_registry.json")
        id_value = ""
        for index, token in enumerate(rest):
            if token == "--id" and index + 1 < len(rest):
                id_value = rest[index + 1]
        ns = _argparse.Namespace(
            feedback=rest[0], id=id_value, state_dir=str(paths.state_dir()),
            policy=effective, public_registry=public_registry, seeded_clock=None)
        return config_proposals.command(ns)
    argv = ["--state-dir", str(paths.state_dir()), "--policy", str(paths.policy_path()),
            "--preferences", str(paths.preferences_path()), "--feedback", *rest]
    return pilot_main(argv)


# --------------------------------------------------------------------------- #
# configure (validated per-user configuration)
# --------------------------------------------------------------------------- #

def _config_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "file", None)
    return Path(explicit) if explicit else paths.user_config_path()


def _write_user_config(path: Path, payload: dict[str, Any], force: bool) -> str:
    import json as _json
    if path.exists() and path.read_text(encoding="utf-8").strip() and not force:
        return "kept"
    # Never persist internal/derived keys (``_warnings``, ``_explicit_version``):
    # they are caller-facing diagnostics, and writing them makes every later load
    # warn about an unknown config key.
    payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return "written"


def configure_command(args: argparse.Namespace) -> int:
    """Validated configuration surface: show | validate | preview | init | migrate."""
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot import userconfig
    from proactive_pilot.policy import load_policy

    action = args.action
    path = _config_path(args)

    try:
        if action in ("show", "validate"):
            loaded = userconfig.load_user_config(path, native=get_native_context())
            payload = {
                "ok": True,
                "action": action,
                "path": str(path),
                "schema": userconfig.schema_summary(),
                "config": loaded.summary(),
                "policy_overrides": userconfig.policy_overrides(loaded.config),
                "native": {
                    "context": get_native_context() is not None,
                    "settings_preferred": loaded.source == "native",
                },
            }
            human = (f"config: {'present' if loaded.present else 'defaults'} "
                     f"({loaded.source}, version {loaded.version}); "
                     f"timezone={loaded.config['timezone']}")
            _emit(payload, as_json=as_json, human=human)
            return 0

        if action == "preview":
            loaded = userconfig.load_user_config(path, native=get_native_context())
            base = load_policy(paths.policy_path())
            overrides = userconfig.policy_overrides(loaded.config)
            changes = {k: {"from": base.get(k), "to": v}
                       for k, v in overrides.items() if base.get(k) != v}
            effective = userconfig.apply_user_config(
                base, loaded.config if loaded.present else userconfig.default_config())
            payload = {"ok": True, "action": "preview", "path": str(path),
                       "present": loaded.present, "changes": changes,
                       "effective": {
                           "timezone": effective.get("timezone"),
                           "daily_normal_cap": effective.get("daily_normal_cap"),
                           "quiet_start_hour": effective.get("quiet_start_hour"),
                           "quiet_end_hour": effective.get("quiet_end_hour"),
                           "sources": {k: effective.get(k) for k in
                                       ("feeds_enabled", "obligations_enabled",
                                        "mail_enabled", "calendar_enabled", "tasks_enabled")},
                       }}
            human = (f"config preview: {len(changes)} policy key(s) would change"
                     if loaded.present else "config preview: no user configuration present")
            _emit(payload, as_json=as_json, human=human)
            return 0

        if action == "project":
            loaded = userconfig.load_user_config(path, native=get_native_context())
            ctx = get_native_context()
            if ctx is None or not callable(getattr(ctx, "set_config", None)):
                _emit({"ok": False, "action": "project", "status": "failed",
                       "path": str(path), "present": loaded.present,
                       "source": loaded.source,
                       "native_projection": {"projected": False,
                                             "reason": "no native settings writer"}},
                      as_json=as_json,
                      human="config project failed: no native settings writer available")
                return 1
            changes = _native_projection_changes(loaded.config)
            if getattr(args, "dry_run", False):
                planned = sorted(changes)
                _emit({"ok": True, "action": "project", "dry_run": True,
                       "status": "planned" if planned else "unchanged",
                       "path": str(path), "present": loaded.present,
                       "source": loaded.source, "changed": planned,
                       "would_write": planned,
                       "native_projection": {"projected": True, "written": [],
                                             "noop": not planned, "backup": None,
                                             "dry_run": True, "changed": planned}},
                      as_json=as_json,
                      human=(f"config project (dry-run): {len(planned)} native "
                             f"field(s) would change; nothing written")
                      if planned else
                      "config project (dry-run): native form already matches; nothing would change")
                return 0
            if not changes:
                _emit({"ok": True, "action": "project", "status": "unchanged",
                       "path": str(path), "present": loaded.present,
                       "source": loaded.source, "changed": [],
                       "native_projection": {"projected": True, "written": [],
                                             "noop": True, "backup": None}},
                      as_json=as_json,
                      human="config project: native form already matches; nothing changed")
                return 0
            projection = _project_native_settings(
                loaded.config, reason="configure project")
            ok = bool(projection.get("projected"))
            _emit({"ok": ok, "action": "project",
                   "status": "projected" if ok else "failed", "path": str(path),
                   "present": loaded.present, "source": loaded.source,
                   "changed": sorted(changes), "native_projection": projection},
                  as_json=as_json,
                  human=(f"config project: {len(projection.get('written') or [])} native "
                         f"field(s) updated") if ok else
                        f"config project FAILED: {projection.get('error') or projection.get('reason')}")
            return 0 if ok else 1

        if action == "init":
            example = paths.user_config_example_path()
            if not example.is_file():
                raise FileNotFoundError(f"shipped neutral example missing: {example}")
            payload = userconfig.validate_config(json.loads(example.read_text(encoding="utf-8")))
            payload.setdefault("advanced", {})["onboarding_provenance"] = \
                userconfig.neutral_init_marker(payload)
            if getattr(args, "dry_run", False):
                _emit({"ok": True, "action": "init", "dry_run": True, "path": str(path)},
                      as_json=as_json, human=f"config init (dry-run): nothing written to {path}")
                return 0
            force = bool(getattr(args, "force", False))
            nonempty = path.exists() and bool(path.read_text(encoding="utf-8").strip())
            if nonempty and not force:
                status = "kept"
                projection = {"projected": False, "reason": "existing config kept"}
                backup = None
            else:
                same_file = _same_config_file(path, payload)
                changes = _native_projection_changes(payload)
                if same_file and not changes:
                    status = "unchanged"
                    projection = {"projected": True, "written": [], "noop": True,
                                  "backup": None}
                    backup = None
                else:
                    backup = _backup_before_change("configure init")
                    status = "unchanged" if same_file else _write_user_config(
                        path, payload, force=True)
                    projection = _project_native_settings(
                        payload, reason="configure init", backup=False)
            _emit({"ok": True, "action": "init", "status": status, "path": str(path),
                   "backup": backup, "native_projection": projection},
                  as_json=as_json,
                  human=f"config init: {status} {path}")
            return 0

        if action == "migrate":
            from_policy = getattr(args, "from_policy", None)
            source_is_config = False
            existing = None
            if from_policy:
                src = Path(from_policy)
                if not src.is_file():
                    raise FileNotFoundError(f"legacy policy not found: {src}")
                legacy = json.loads(src.read_text(encoding="utf-8"))
            elif path.is_file() and path.read_text(encoding="utf-8").strip():
                existing = json.loads(path.read_text(encoding="utf-8"))
                version = (existing.get("version", userconfig.CONFIG_VERSION)
                           if isinstance(existing, dict) else None)
                if isinstance(version, int) and not isinstance(version, bool) \
                        and version > userconfig.CONFIG_VERSION:
                    raise userconfig.ConfigError(
                        f"config version {version} is newer than this plugin supports "
                        f"({userconfig.CONFIG_VERSION})",
                        remedy="update the plugin; refusing to guess a future schema",
                    )
                # The command itself is the opt-in for an explicit in-place
                # migration / form re-projection of the existing user config.
                # Plain loads still never rewrite a stale file silently.
                src = path
                source_is_config = True
            else:
                src = paths.policy_path()
                legacy = json.loads(src.read_text(encoding="utf-8"))

            if source_is_config:
                version = (existing.get("version", userconfig.CONFIG_VERSION)
                           if isinstance(existing, dict) else None)
                if isinstance(version, int) and not isinstance(version, bool) \
                        and version < userconfig.CONFIG_VERSION:
                    migrated, report = userconfig.migrate_config(existing)
                else:
                    migrated = userconfig.validate_config(existing)
                    report = {"from": f"version {version}", "to": userconfig.CONFIG_VERSION,
                              "mapped": ["validated existing configuration and re-projected form"]}
            else:
                if not src.is_file():
                    raise FileNotFoundError(f"legacy policy not found: {src}")
                migrated, report = userconfig.migrate_legacy(
                    legacy if isinstance(legacy, dict) else {},
                    mail_accounts=getattr(args, "mail_account", None),
                    calendar_accounts=getattr(args, "calendar_account", None),
                    calendar_ids=getattr(args, "calendar_id", None),
                    task_lists=getattr(args, "task_list", None),
                    task_accounts=getattr(args, "task_account", None),
                    task_adapter=getattr(args, "adapter", None),
                )
            migrated = userconfig.validate_config(migrated)
            source_label = {"from_config": str(src)} if source_is_config \
                else {"from_policy": str(src)}
            if getattr(args, "dry_run", False):
                _emit({"ok": True, "action": "migrate", "dry_run": True,
                       "path": str(path), **source_label,
                       "report": report,
                       "policy_overrides": userconfig.policy_overrides(migrated)},
                      as_json=as_json, human=f"config migrate (dry-run): reviewed {src}")
                return 0
            force_write = bool(getattr(args, "force", False)) or source_is_config
            nonempty = path.exists() and bool(path.read_text(encoding="utf-8").strip())
            if nonempty and not force_write:
                status = "kept"
                projection = {"projected": False, "reason": "existing config kept"}
                backup = None
            else:
                same_file = _same_config_file(path, migrated)
                changes = _native_projection_changes(migrated)
                if same_file and not changes:
                    status = "unchanged"
                    projection = {"projected": True, "written": [], "noop": True,
                                  "backup": None}
                    backup = None
                else:
                    backup = _backup_before_change("configure migrate")
                    status = "written" if same_file else _write_user_config(
                        path, migrated, force=True)
                    projection = _project_native_settings(
                        migrated, reason="configure migrate", backup=False)
            _emit({"ok": True, "action": "migrate", "status": status,
                   "path": str(path), **source_label, "report": report,
                   "backup": backup, "native_projection": projection},
                  as_json=as_json,
                  human=f"config migrate: {status} {path} (from {src})")
            return 0
    except userconfig.ConfigError as exc:
        _emit({"ok": False, "action": action, "error": str(exc),
               "remedy": getattr(exc, "remedy", "")},
              as_json=as_json, human=f"configure {action} FAILED: {exc}")
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"},
              as_json=as_json, human=f"configure {action} FAILED: {exc}")
        return 1
    _emit({"ok": False, "error": f"unknown configure action {action!r}"}, as_json=as_json,
          human=f"unknown configure action {action!r}")
    return 2


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def _deliveries_error(as_json: bool, message: str) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(f"deliveries: {message}", file=sys.stderr)
    return 1


def deliveries_command(args: argparse.Namespace) -> int:
    """On-demand read surface for the owner-only delivery ledger."""
    as_json = bool(getattr(args, "json", False))
    since = getattr(args, "since", None)
    try:
        limit = int(getattr(args, "limit", 10))
    except (TypeError, ValueError):
        return _deliveries_error(as_json, "invalid --limit: expected an integer")
    if limit < 1:
        return _deliveries_error(as_json, "invalid --limit: must be at least 1")
    limit = min(limit, 50)

    _ensure_src()
    try:
        from proactive_pilot import deliveries as ledger
        from proactive_pilot.clock import Clock
        from proactive_pilot.state import CorruptStateError, StateStore
    except Exception as exc:  # noqa: BLE001 - report honestly, no fake success
        return _deliveries_error(as_json, f"ledger unavailable ({type(exc).__name__})")

    try:
        store = StateStore(Path(paths.state_dir()), Clock())
        rows = ledger.recent(store, since=since, limit=limit, strict=True)
    except ValueError as exc:
        return _deliveries_error(as_json, f"malformed --since: {exc}")
    except (paths.ProfileResolutionError, ledger.DeliveryLedgerError) as exc:
        return _deliveries_error(as_json, str(exc) or type(exc).__name__)
    except CorruptStateError as exc:
        return _deliveries_error(as_json, f"delivery ledger is corrupt: {exc}")
    except Exception as exc:  # noqa: BLE001 - unavailable/corrupt is not empty
        return _deliveries_error(
            as_json, f"delivery ledger unavailable ({type(exc).__name__})")

    payload_rows = [
        {
            "at": row.get("at", ""),
            "kind": row.get("kind", ""),
            "destination": row.get("destination", ""),
            "message": row.get("message", ""),
        }
        for row in rows
    ]
    if as_json:
        print(json.dumps({
            "ok": True,
            "since": since or "",
            "limit": limit,
            "count": len(payload_rows),
            "deliveries": payload_rows,
        }, indent=2, sort_keys=True))
    elif not payload_rows:
        print("no deliveries")
    else:
        for row in payload_rows:
            print(f"{row['at']}  {row['kind']}  {row['destination'] or '-'}")
            print(f"  {row['message']}")
    return 0


def portal_command(args: argparse.Namespace, *, backend: Any = None) -> int:
    """Lazy glue: the portal service owns actions, backend and JSON output."""
    from . import secretary_portal as _portal

    return _portal.portal_command(
        args,
        backend=_backend() if backend is None else backend,
        native_ctx=get_native_context(),
    )


_DISPATCH = {
    "setup": setup_command,
    "status": status_command,
    "job": portal_command,
    "pause": pause_command,
    "resume": resume_command,
    "disable": disable_command,
    "uninstall": uninstall_command,
    "migrate-jobs": migrate_jobs_command,
    "self-test": selftest_command,
    "run-once": run_once_command,
    "pipeline": pipeline_command,
    "prepare": prepare_command,
    "responsibilities": responsibilities_command,
    "context": context_command,
    "reminders": reminders_command,
    "deliveries": deliveries_command,
    "watch": watch_command,
    "feedback": feedback_command,
    "configure": configure_command,
    "doctor": doctor_command,
}


def secretary_command(args: argparse.Namespace) -> int:
    _data_override(args)
    sub = getattr(args, "secretary_command", None)
    if not sub:
        print("usage: hermes secretary {setup,status,job,pause,resume,disable,uninstall,"
              "migrate-jobs,self-test,run-once,prepare,responsibilities,watch,feedback,"
              "configure,deliveries,doctor}")
        return 2
    handler = _DISPATCH.get(sub)
    if handler is None:
        print(f"unknown secretary subcommand: {sub}", file=sys.stderr)
        return 2
    return handler(args)
