"""Isolated adaptive shadow runner.

The shadow path exists so the parent can exercise adaptive watch acquisition
against PUBLIC sources only, without touching the ordinary run at all:

- It reads no mail, calendar, task, reminder or private source. It imports no
  private collector. It runs ONLY the adaptive acquisition on approved public
  routes.
- It never writes to stdout (no candidate may leak into the scheduler's
  deliverable stream) and never mutates notification state or budgets outside
  the named adaptive shadow artifacts.
- It is disabled by default: the ordinary ``--run`` path does not invoke it. The
  explicit ``--shadow`` CLI flag plus ``adaptive_shadow_enabled`` policy (or
  ``--force-shadow``) is required.

Tests inject a fake planner/fetcher (or a workspace fixture file); no default
points at the network.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import publicfeeds as pf
from .adaptive import collect_adaptive
from .clock import Clock, SeededClock
from .collectors import Budget
from .feedback import FeedbackError, load_preferences_file
from .planner import FakePlanner, NativePlanner
from .policy import active_snooze, load_policy
from .state import StateStore
from .watchdog import run_deadline


def _diag(payload: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _emit(text: str) -> None:
    # The shadow path must never emit a candidate. Kept for symmetry with the
    # main bridge; the only call site passes "".
    if text:
        sys.stdout.write(text.strip() + "\n")


class FixtureFetcher:
    """Deterministic public fetcher for tests / offline recall.

    ``responses`` maps a URL (or URL prefix) to a dict with optional ``status``,
    ``headers`` and ``body``. Unknown URLs raise ``FetchFailed`` so a test can
    prove an unapproved host was never contacted.
    """

    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs: Any) -> pf.FetchResponse:
        self.calls.append(url)
        spec = self.responses.get(url)
        if spec is None:
            for key, value in self.responses.items():
                if url.startswith(key):
                    spec = value
                    break
        if spec is None:
            raise pf.FetchFailed("no fake response for url")
        if isinstance(spec, Exception):
            raise spec
        status = int(spec.get("status", 200))
        headers = dict(spec.get("headers") or {})
        body = spec.get("body", "")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return pf.FetchResponse(status, headers, body, url)


def load_fixture(path: str | Path) -> tuple[Any, FixtureFetcher]:
    """Load a workspace fixture file: ``{"plan": {...}, "responses": {...}}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("shadow fixture must be a JSON object")
    plan = data.get("plan")
    planner = FakePlanner(plan) if isinstance(plan, dict) else None
    return planner, FixtureFetcher(data.get("responses") or {})


def shadow_command(args: argparse.Namespace) -> int:
    clock = SeededClock(args.seeded_clock) if getattr(args, "seeded_clock", None) else Clock()
    policy = load_policy(getattr(args, "policy", None))
    try:
        prefs = load_preferences_file(Path(args.preferences), strict=True)
    except FeedbackError as exc:
        print(f"proactive-portal shadow: preferences corrupt, failing closed: {exc}", file=sys.stderr)
        _emit("")
        return 1
    policy["_preferences"] = prefs

    mode = str(policy.get("adaptive_mode") or "disabled")
    enabled = (
        mode in {"shadow", "active"}
        or bool(policy.get("adaptive_shadow_enabled", False))
        or bool(getattr(args, "force_shadow", False))
    )
    if not enabled:
        _diag({"command": "shadow", "diagnostic": "shadow-disabled",
               "reason": "adaptive_shadow_enabled is false and --force-shadow absent"})
        _emit("")
        return 0

    # Global pause is absolute: ZERO planning and ZERO network.
    if active_snooze(prefs, clock.now()):
        _diag({"command": "shadow", "diagnostic": "skip-paused",
               "reason": "global pause active; no planning or network"})
        _emit("")
        return 0

    store = StateStore(Path(args.state_dir), clock)
    planner: Any = None
    fetcher: Any = None
    fixture = getattr(args, "shadow_fixture", None)
    if fixture:
        planner, fetcher = load_fixture(fixture)
    elif getattr(args, "no_model", False):
        planner = None
    else:
        planner = NativePlanner()

    # H1: the shadow path must route the REAL native adapter too, not a fake.
    # Built only when broad discovery is enabled and no fixture fake is used.
    search_bridge: Any = None
    if (fixture is None
            and bool(policy.get("adaptive_broad_discovery_enabled", False))):
        from .searchbridge import build_native_bridge
        search_bridge = build_native_bridge(policy, store=store)

    with store.lock("adaptive_shadow.lock") as acquired:
        if not acquired:
            _diag({"command": "shadow", "diagnostic": "skip-lock",
                   "reason": "another shadow run holds the lock"})
            _emit("")
            return 0
        # The HTTP budget must NOT be charged for the zero-tool planning phase:
        # it is restarted after planning so the first fetch sees the full public
        # allocation. The whole shadow run is additionally bounded by the
        # existing whole-run wall-clock (<= 300s) so a hung model cannot stall it.
        budget = Budget(float(policy.get("public_budget_seconds", 10)), label="adaptive-shadow")
        run_seconds = float(policy.get("run_wallclock_seconds", 300) or 0)
        try:
            with run_deadline(run_seconds):
                summary = collect_adaptive(
                    policy, clock, store=store, planner=planner, fetcher=fetcher,
                    budget=budget, restart_budget=True,
                    force_plan=bool(getattr(args, "force_shadow", False)),
                    search_bridge=search_bridge,
                    resolver=getattr(args, "dns_resolver", None),
                )
        except Exception as exc:  # noqa: BLE001 - shadow never breaks the scheduler
            print(f"proactive-portal shadow internal error: {exc}", file=sys.stderr)
            _emit("")
            return 1
    _diag({"command": "shadow", "summary": _redact_summary(summary)})
    _emit("")
    return 0


def _redact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Bound what the shadow diagnostic prints: no raw feed text, ever."""
    planned = []
    for plan in summary.get("planned", []):
        planned.append({
            "watch_id": plan.get("watch_id"),
            "plan_version": plan.get("plan_version"),
            "scope": plan.get("scope"),
            "semantic": plan.get("semantic"),
            "provenance": plan.get("provenance"),
            "query_count": len(plan.get("queries") or []),
        })
    return {
        "status": summary.get("status"),
        "reason": summary.get("reason"),
        "planned_watches": summary.get("planned_watches"),
        "used_model": summary.get("used_model"),
        "counts": summary.get("counts", {}),
        "planned": planned,
        "sources": [
            {"kind": s.get("kind"), "host": s.get("host"),
             "status": s.get("status"), "count": s.get("count")}
            for s in summary.get("sources", [])
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proactive-portal-shadow",
        description="Isolated adaptive watch shadow runner (public sources only).",
    )
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--preferences", required=True)
    parser.add_argument("--seeded-clock", default=None)
    parser.add_argument("--force-shadow", action="store_true")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--shadow-fixture", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return shadow_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
