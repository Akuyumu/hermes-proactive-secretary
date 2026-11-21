#!/usr/bin/env python3
"""Native scheduled preparation preflight (deterministic; no agent runtime).

This file is intended to be installed as a native cron *script* job. The host
scheduler passes its bounded stdout as the wake-gate/prompt context:

* no eligible work / blocked gate -> final line ``{"wakeAgent": false}``: the
  host starts no agent and spends zero model tokens;
* one due accepted responsibility -> ``{"wakeAgent": true, "context": ...}``
  with bounded instructions/data after an atomic claim.

It never calls a model, provider, source connector or delivery path, and it does
not create/update cron jobs. A disabled plugin, missing user configuration,
pause, missing scope, malformed state or missing source consent fails closed
with an actionable stderr diagnostic and the silent final JSON.
"""
from __future__ import annotations

import argparse
import json
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


def _diag(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _silent() -> None:
    print(json.dumps({"wakeAgent": False}))


def _defaults() -> argparse.Namespace:
    import paths

    return argparse.Namespace(
        state_dir=str(paths.state_dir()),
        policy=str(paths.policy_path()),
        preferences=str(paths.preferences_path()),
        user_config=str(paths.user_config_path()),
        seeded_clock=None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proactive-secretary-preparation",
        description="Deterministic private-preparation preflight (no model calls).")
    parser.add_argument("--state-dir")
    parser.add_argument("--policy")
    parser.add_argument("--preferences")
    parser.add_argument("--user-config")
    parser.add_argument("--seeded-clock", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except Exception as exc:  # noqa: BLE001 - deterministic fail-closed boundary
        # Never print raw state, private facts, paths or the claim token.
        _diag({"diagnostic": "preparation-runner-failed", "reason": type(exc).__name__})
        _silent()
        return 1


def _run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    defaults = _defaults()
    for name in ("state_dir", "policy", "preferences", "user_config"):
        if getattr(args, name) is None:
            setattr(args, name, getattr(defaults, name))

    if not _native_gate():
        _silent()
        return 0

    try:
        from proactive_pilot.clock import Clock, SeededClock
        from proactive_pilot.feedback import FeedbackError, load_preferences_file
        from proactive_pilot.policy import load_policy
        from proactive_pilot.preparation import preflight, public_context
        from proactive_pilot.state import CorruptStateError, StateStore
        from proactive_pilot.userconfig import (
            ConfigError,
            apply_user_config,
            load_user_config,
        )
    except Exception as exc:  # noqa: BLE001 - missing/broken runtime fails closed
        _diag({"diagnostic": "preparation-unavailable", "reason": type(exc).__name__})
        _silent()
        return 1

    clock = SeededClock(args.seeded_clock) if args.seeded_clock else Clock()
    now = clock.now()
    try:
        prefs = load_preferences_file(Path(args.preferences), strict=True)
    except FeedbackError as exc:
        _diag({"diagnostic": "preferences-invalid", "reason": type(exc).__name__})
        _silent()
        return 1

    try:
        loaded = load_user_config(Path(args.user_config), strict=True)
    except ConfigError as exc:
        _diag({"diagnostic": "configuration-invalid", "reason": type(exc).__name__})
        _silent()
        return 1
    policy = load_policy(Path(args.policy))
    if loaded.present:
        policy = apply_user_config(policy, loaded.config)

    store = StateStore(Path(args.state_dir), clock)
    try:
        result = preflight(
            store,
            policy=policy,
            prefs=prefs,
            config_present=loaded.present,
            now=now,
        )
    except (CorruptStateError, ValueError, OSError) as exc:
        _diag({"diagnostic": "preparation-state-invalid", "reason": type(exc).__name__})
        _silent()
        return 1

    if not result.get("wake"):
        _diag({"diagnostic": "preparation-skip", "reason": result.get("reason", "")})
        _silent()
        return 0

    # stdout is not a secure bearer channel: redact the claim token. Any
    # model/draft step must run in the same bounded process as the claim.
    print(json.dumps({
        "wakeAgent": True,
        "context": public_context(result["context"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
