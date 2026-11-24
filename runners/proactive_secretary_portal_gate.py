#!/usr/bin/env python3
"""Portal cadence wake gate — the Portal job's native pre-run script.

Contract (host side): the last non-empty stdout line decides the tick.

* ``{"wakeAgent": false}`` — not due yet: the host starts no agent, so the tick
  costs one short Python process and zero model tokens.
* ``{"wakeAgent": true, "context": ...}`` — due: the interval was stamped before
  the decision is printed, so a crash cannot cause a second wake in the same
  window.

Failure policy: a broken gate must never mute the secretary. An unreadable or
corrupt store, a missing plugin flag module or any unexpected error prints a
one-line stderr diagnostic and ``{"wakeAgent": true}`` — one wasted wake is
cheaper than silence.

This file never calls a model, connector or delivery path, and never creates,
updates or pauses a cron job.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_EMBEDDED_ROOT = r"{{PLUGIN_ROOT}}"
if _EMBEDDED_ROOT.startswith("{{"):
    PLUGIN_ROOT = Path(__file__).resolve().parents[1]
else:
    PLUGIN_ROOT = Path(_EMBEDDED_ROOT)


def _load_cadence():
    """Load the cadence module by path so the package's heavier init stays out."""
    import importlib.util

    module_path = PLUGIN_ROOT / "secretary" / "secretary_cadence.py"
    spec = importlib.util.spec_from_file_location(
        "proactive_secretary_cadence_gate", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cadence module not found")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _plugin_enabled() -> bool:
    try:
        for entry in (str(PLUGIN_ROOT / "src"), str(PLUGIN_ROOT)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        from runtime_gate import plugin_enabled  # type: ignore
        return bool(plugin_enabled(PLUGIN_ROOT))
    except Exception as exc:  # noqa: BLE001 - gate failure must not mute the portal
        print(f"proactive-portal-gate: gate unavailable ({type(exc).__name__}); "
              "allowing the wake", file=sys.stderr)
        return True


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    status_only = any(arg in {"--status", "status"} for arg in (argv or sys.argv[1:]))
    if not _plugin_enabled():
        _emit({"wakeAgent": False, "reason": "plugin disabled"})
        return 0
    try:
        cadence = _load_cadence()
        plan = cadence.read_plan()
        if not plan.get("ok"):
            print(f"proactive-portal-gate: {plan.get('error')}; allowing the wake",
                  file=sys.stderr)
            _emit({"wakeAgent": True, "reason": "cadence store unreadable"})
            return 0
        due, remaining = cadence.is_due(plan)
        interval = int(plan.get("interval_minutes") or 0)
        if status_only:
            _emit({"wakeAgent": bool(due), "due": bool(due),
                   "interval_minutes": interval, "remaining_minutes": remaining,
                   "last_wake": plan.get("last_wake", ""),
                   "next_due": cadence.next_due_iso(plan)})
            return 0
        if not due:
            _emit({"wakeAgent": False})
            return 0
        cadence.stamp_wake(plan)
        _emit({"wakeAgent": True,
               "context": ("portal wake due; interval="
                           f"{interval}m; last={plan.get('last_wake') or 'never'}")})
        return 0
    except Exception as exc:  # noqa: BLE001 - fail open, never silent
        print(f"proactive-portal-gate: unexpected error ({type(exc).__name__}: {exc}); "
              "allowing the wake", file=sys.stderr)
        _emit({"wakeAgent": True, "reason": "gate error"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
