#!/usr/bin/env python3
"""Native-cron runner: the proactive-secretary monitoring pipeline.

Installed by ``hermes secretary setup`` into the profile's scripts directory and
run by the native scheduler with ``--no-agent``: its stdout is the single
permitted alert line, or empty for silence.

Before doing ANY work the runner checks the shared native enable/disable gate
(``runtime_gate``): a plugin disabled in ``plugins.enabled``/``plugins.disabled``
— or a removed plugin root or unreadable config — exits silently with empty
stdout and never imports the pipeline, reads runtime state, collects, evaluates,
or produces a delivery payload. With no arguments (the scheduler case) it uses
the plugin's profile-safe state/policy/preferences paths. Explicit arguments are
forwarded unchanged so the same file can be exercised offline by tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

_EMBEDDED_ROOT = r"{{PLUGIN_ROOT}}"
if _EMBEDDED_ROOT.startswith("{{"):
    PLUGIN_ROOT = Path(__file__).resolve().parents[1]
else:
    PLUGIN_ROOT = Path(_EMBEDDED_ROOT)
for _entry in (str(PLUGIN_ROOT / "src"), str(PLUGIN_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def _native_gate() -> bool:
    """Fail-closed native toggle check, run before any pipeline work.

    Prints only a diagnostic to stderr; disabled/removed/unreadable always
    returns False so the caller exits 0 with empty stdout.
    """
    try:
        from runtime_gate import plugin_enabled
        allowed = plugin_enabled(PLUGIN_ROOT)
    except Exception as exc:  # noqa: BLE001 - a broken gate must fail closed
        print(f"proactive-secretary: skipping run (gate unavailable: {exc})",
              file=sys.stderr)
        return False
    if not allowed:
        print("proactive-secretary: plugin is not enabled "
              "(plugins.enabled); skipping run", file=sys.stderr)
        return False
    return True


def _default_argv() -> list[str]:
    import paths  # noqa: E402 - resolved from the installed plugin root
    return [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
    ]


def _native_cli_bridge() -> int | None:
    """Scheduled route: run the ordinary pipeline through the registered native CLI.

    The scheduler invokes this script with NO arguments. Rather than importing the
    pipeline directly (which would leave ``ctx.llm``/native settings unavailable),
    we invoke the registered ``hermes secretary pipeline`` handler with fixed argv
    so native plugin discovery builds and passes the host context.

    Fail-closed contract:
    * the direct-import compatibility fallback is used ONLY when the native CLI
      is verifiably unavailable BEFORE any work starts (returning ``None``), and
      that is reported on stderr;
    * any failure or timeout AFTER the native pipeline was spawned (OSError,
      SubprocessError/TimeoutExpired) returns a non-zero code so the caller NEVER
      retries a second collector/model run and never bypasses a failed native
      context. Stderr is forwarded separately; only the alert line ever reaches
      stdout.
    """
    import os
    import shutil
    import subprocess

    hermes = os.environ.get("HERMES_BIN") or shutil.which("hermes")
    if not hermes:
        print("proactive-secretary: native hermes CLI unavailable; using the "
              "direct-import compatibility fallback", file=sys.stderr)
        return None
    import paths  # noqa: E402 - resolved from the installed plugin root

    argv = [hermes, "secretary", "pipeline", "--data-dir", str(paths.data_root())]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=300, check=False)
    except subprocess.TimeoutExpired:
        print("proactive-secretary: native pipeline timed out AFTER start; "
              "failing closed (no retry/fallback)", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"proactive-secretary: native pipeline failed AFTER start "
              f"({type(exc).__name__}); failing closed (no retry/fallback)",
              file=sys.stderr)
        return 1
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode
    out = (proc.stdout or "").strip()
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    if not _native_gate():
        raise SystemExit(0)
    if not sys.argv[1:]:
        _bridge_rc = _native_cli_bridge()
        if _bridge_rc is not None:
            raise SystemExit(_bridge_rc)
    from proactive_pilot.main import main as _pilot_main  # noqa: E402
    _argv = sys.argv[1:] or _default_argv()
    raise SystemExit(_pilot_main(_argv))
