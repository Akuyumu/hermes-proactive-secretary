#!/usr/bin/env python3
"""Native cron script bridge: one bounded private-preparation unit.

This is the *only* supported preparation schedule shape. It is installed as a
native ``--no-agent`` script job: this process performs no agent run, no tool
loop and no direct messaging. With no arguments (the scheduler case) it calls
the registered ``hermes secretary prepare`` handler with fixed argv so native
plugin discovery builds the host context and the plugin can use the existing
configured-only auxiliary lane.

Output contract: empty stdout on every path (no work, blocked gate, unsupported
model lane or a written artifact). Diagnostics go to stderr only. The artifact
is queued for the ordinary discretionary attention/delivery path; this script
never sends and never prints a claim token or private fact.

Fail closed:

* a disabled/unreadable plugin or unavailable native CLI writes no artifact;
* a timeout/error after the native handler starts is never retried here;
* no direct-import fallback exists, because that would bypass the native model
  and settings context.
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
    try:
        from runtime_gate import plugin_enabled
        return bool(plugin_enabled(PLUGIN_ROOT))
    except Exception as exc:  # noqa: BLE001 - a broken gate fails closed
        print("proactive-preparation: skipping run (gate unavailable: "
              f"{type(exc).__name__})", file=sys.stderr)
        return False


def _bridge() -> int:
    import os
    import shutil
    import subprocess

    hermes = os.environ.get("HERMES_BIN") or shutil.which("hermes")
    if not hermes:
        print("proactive-preparation: native hermes CLI unavailable; failing "
              "closed without a model call", file=sys.stderr)
        return 1
    try:
        import paths  # noqa: E402 - resolved from the installed plugin root
    except Exception as exc:  # noqa: BLE001
        print("proactive-preparation: plugin paths unavailable "
              f"({type(exc).__name__}); failing closed", file=sys.stderr)
        return 1

    argv = [hermes, "secretary", "prepare", "--data-dir", str(paths.data_root())]
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=300, check=False)
    except subprocess.TimeoutExpired:
        print("proactive-preparation: native prepare timed out AFTER start; "
              "failing closed (no retry)", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print("proactive-preparation: native prepare failed AFTER start "
              f"({type(exc).__name__}); failing closed (no retry)", file=sys.stderr)
        return 1
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    # Deliberately ignore stdout: a preparation script job is silent. Only the
    # later attention gate may emit an artifact-ready notice.
    return proc.returncode


if __name__ == "__main__":
    if not _native_gate():
        raise SystemExit(0)
    raise SystemExit(_bridge())
