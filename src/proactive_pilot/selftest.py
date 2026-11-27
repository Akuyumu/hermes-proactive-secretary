"""Self-tests. Exercise the real validator, gates, lock and dedupe functions.

Every test runs against a temp state dir with a seeded clock. Synthetic data
only; no production source is read and no message is sent.
"""
from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path

from .candidates import build_packet
from .clock import SeededClock
from .evaluator import EvaluatorResult, fixture_packet, validate_object
from .fixtures import fixture_evaluator, fixture_results
from .gates import decide, record_delivery
from .policy import DEFAULT_POLICY, in_quiet_hours
from .state import StateStore


class _Result:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail


def _packet(name: str, clock, policy):
    results = fixture_results(name, clock)
    return results, build_packet(results, clock=clock, policy=policy, baseline={})


def _adversarial_corpus_path() -> Path:
    """Locate the shipped adversarial-query corpus from the plugin root."""
    return Path(__file__).resolve().parents[2] / "fixtures" / "self_test_corpus.json"


def _load_adversarial_corpus() -> list[str] | None:
    """Load the fixture data; ``None`` means the corpus is missing/malformed."""
    try:
        payload = json.loads(_adversarial_corpus_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    queries = payload.get("dangerous_queries") if isinstance(payload, dict) else None
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        return None
    return queries


def run_self_test() -> int:
    outcomes: list[_Result] = []
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    # Deterministic self-test fixtures use +08 wall-clock strings. Pin their
    # interpretation explicitly so the neutral UTC runtime default never shifts
    # these offline assertions.
    policy["timezone"] = "Asia/Singapore"
    policy["_preferences"] = {"explicit": [], "inferred": []}

    # 1. healthy empty -> evaluator silence -> no send
    clock = SeededClock("2026-10-02T20:00:00")
    _, packet = _packet("healthy_empty", clock, policy)
    ev = fixture_evaluator("healthy_empty")
    prop = ev.evaluate(packet, policy)
    dec = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state={})
    outcomes.append(_Result("healthy_empty_silent", not dec["send"], dec["reason"]))

    # 2. failure -> coverage notice generated once (deduped), and not a fabricated alert
    results, packet = _packet("failure", clock, policy)
    from .gates import coverage_notice
    state = {}
    n1 = coverage_notice(results, state, policy)
    n2 = coverage_notice(results, state, policy)
    outcomes.append(_Result("failure_notice_once", n1 is not None and n2 is None, f"{n1!r}/{n2!r}"))

    # 3. urgent calendar with evidence -> send, bypasses quiet hours
    night = SeededClock("2026-10-02T03:00:00")
    _, packet = _packet("urgent_calendar", night, policy)
    prop = fixture_evaluator("urgent_calendar").evaluate(packet, policy)
    dec = decide(prop, packet, clock=night, policy=policy, prefs=policy["_preferences"], state={})
    outcomes.append(_Result("urgent_sends_at_night", dec["send"] and dec["urgent"], dec["reason"]))

    # 4. quiet hours normal -> suppressed
    day = SeededClock("2026-10-02T12:00:00")
    _, packet = _packet("changed_event", day, policy)
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    qclock = SeededClock("2026-10-02T03:00:00")
    dec_q = decide(prop, packet, clock=qclock, policy=policy, prefs=policy["_preferences"], state={})
    outcomes.append(_Result("normal_quiet_suppressed", not dec_q["send"] and dec_q["reason"] == "quiet hours", dec_q["reason"]))

    # 5. known urgent/quiet invalid exception: urgent claim without evidence rejected
    _, packet = _packet("quiet_invalid_exception", night, policy)
    prop = fixture_evaluator("quiet_invalid_exception").evaluate(packet, policy)
    outcomes.append(_Result("urgent_without_evidence_invalid", prop.action == "invalid", prop.reason))

    # 6. dedupe: same candidate twice -> second suppressed
    _, packet = _packet("changed_event", day, policy)
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    state: dict = {}
    d1 = decide(prop, packet, clock=day, policy=policy, prefs=policy["_preferences"], state=state)
    d1.update({"kind": prop.kind, "key": prop.key, "expiry": prop.expiry})
    state = record_delivery(state, d1, packet, day, policy)
    d2 = decide(prop, packet, clock=day, policy=policy, prefs=policy["_preferences"], state=state)
    outcomes.append(_Result("dedupe_second_suppressed", d1["send"] and not d2["send"] and d2["reason"] == "duplicate", d2["reason"]))

    # 7. expiry: expired ledger entry no longer blocks? we test _expired indirectly via dedupe skip
    from .gates import _expired
    outcomes.append(_Result("expiry_helper", _expired({"expiry": "2026-10-01T00:00:00+08:00"}, day) is True, ""))

    # 8. hard cap: five (explicit configured ceiling) normal deliveries then sixth suppressed
    state = {"delivered": [{"at": f"2026-10-02T{9 + i:02d}:00:00+08:00", "signature": s, "urgent": False}
                           for i, s in enumerate(("a", "b", "c", "d", "e"))]}
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    # Avoid cooldown interference. Use a scenario-local copy so this explicit
    # ceiling never mutates the shared policy for later scenarios.
    state.pop("last_delivery_at", None)
    cap_policy = json.loads(json.dumps(policy))
    cap_policy["daily_normal_cap"] = 5
    dec_cap = decide(prop, packet, clock=day, policy=cap_policy,
                     prefs=cap_policy["_preferences"], state=state)
    outcomes.append(_Result("hard_cap_suppresses_6th", not dec_cap["send"] and dec_cap["reason"] == "daily cap", dec_cap["reason"]))

    # 9. changed event passes validation and would send
    _, packet = _packet("changed_event", day, policy)
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    outcomes.append(_Result("changed_event_valid", prop.action == "notify", prop.action))

    # 10. concurrent lock: second acquire fails
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), day)
        with store.lock() as first:
            if first:
                store2 = StateStore(Path(tmp), day)
                with store2.lock() as second:
                    outcomes.append(_Result("lock_prevents_overlap", not second, f"first={first} second={second}"))

    # 11. malformed JSON -> invalid, silence
    prop = validate_object({"nope": True}, fixture_packet("evt:x"))
    outcomes.append(_Result("malformed_json_invalid", prop.action == "invalid", prop.reason))
    from .evaluator import _extract_json
    outcomes.append(_Result("unparseable_extract_none", _extract_json("not json at all") is None, ""))

    # 12. noise suppressed by evaluator (silence)
    _, packet = _packet("noise", day, policy)
    prop = fixture_evaluator("noise").evaluate(packet, policy)
    dec = decide(prop, packet, clock=day, policy=policy, prefs=policy["_preferences"], state={})
    outcomes.append(_Result("generic_noise_silent", not dec["send"], dec["reason"]))

    # 13. quiet hours boundary function
    outcomes.append(_Result("quiet_boundary", in_quiet_hours(policy, SeededClock("2026-10-02T07:59:00").now())
                            and not in_quiet_hours(policy, SeededClock("2026-10-02T08:00:00").now()), ""))

    # 14. task baseline short-circuit is now content-aware
    from .candidates import is_unchanged_task_list
    tpacket = {"candidates": [{"kind": "tasks", "ref": "task:p:t1", "version": "v1",
                               "status": "needsAction", "due_date": "2026-10-20",
                               "phase": "far"}], "generated_at": "2026-10-02T20:00:00+08:00"}
    base_state = {"baseline_tasks": {"task:p:t1": {"version": "v1", "status": "needsAction",
                                                   "due_date": "2026-10-20", "phase": "far"}}}
    outcomes.append(_Result("unchanged_tasks_shortcircuit", is_unchanged_task_list(tpacket, base_state) is True, ""))
    changed = json.loads(json.dumps(tpacket))
    changed["candidates"][0]["version"] = "v2"
    outcomes.append(_Result("changed_task_version_wakes", is_unchanged_task_list(changed, base_state) is False, ""))
    crit = json.loads(json.dumps(tpacket))
    crit["candidates"][0]["phase"] = "overdue"
    outcomes.append(_Result("changed_task_phase_wakes", is_unchanged_task_list(crit, base_state) is False, ""))

    # 15. transactional coverage notice: suppressed consumption is retryable
    results, _packet_fail = _packet("failure", clock, policy)
    from .gates import coverage_notice
    state_tx: dict = {}
    assert coverage_notice(results, state_tx, policy, commit=False) is not None
    outcomes.append(_Result("coverage_notice_retryable_when_uncommitted",
                            "failed_sources" not in state_tx))
    coverage_notice(results, state_tx, policy)
    outcomes.append(_Result("coverage_notice_deduped_once_committed",
                            coverage_notice(results, state_tx, policy) is None))

    # 16. evaluator failure counter: bounded notice after repeated failures
    from .gates import EVALUATOR_FAILURE_NOTICE, note_evaluator_outcome
    prefs = policy["_preferences"]
    fail_state: dict = {}
    o1 = note_evaluator_outcome(EvaluatorResult("error"), fail_state, policy, prefs, day.now())
    o2 = note_evaluator_outcome(EvaluatorResult("error"), fail_state, policy, prefs, day.now())
    o3 = note_evaluator_outcome(EvaluatorResult("error"), fail_state, policy, prefs, day.now())
    outcomes.append(_Result("evaluator_failure_notice_after_threshold",
                            o1 is None and o2 is None and o3 == EVALUATOR_FAILURE_NOTICE
                            and fail_state["evaluator_failures"]["count"] == 3))
    o4 = note_evaluator_outcome(EvaluatorResult("error"), fail_state, policy, prefs, day.now())
    outcomes.append(_Result("evaluator_failure_notice_not_repeated", o4 is None))
    note_evaluator_outcome(EvaluatorResult("silence"), fail_state, policy, prefs, day.now())
    outcomes.append(_Result("evaluator_failure_success_resets",
                            fail_state["evaluator_failures"]["count"] == 0))

    # 17. fair packet cap: tasks cannot starve mail; omission marks partial
    from .candidates import _cap_candidates
    fake = ([{"kind": "tasks", "ref": f"task:{i}"} for i in range(10)]
            + [{"kind": "mail", "ref": "mail:1"}])
    picked, dropped = _cap_candidates(fake, 3)
    picked_kinds = {c["kind"] for c in picked}
    outcomes.append(_Result("fair_cap_prevents_task_starvation",
                            "mail" in picked_kinds and picked_kinds == {"tasks", "mail"}))
    outcomes.append(_Result("fair_cap_marks_omissions", len(dropped) == 8))

    # 18. invalid mail date marks partial, RFC2822 understood
    from .collectors import _parse_time
    outcomes.append(_Result("rfc2822_mail_date_parsed",
                            _parse_time("Fri, 03 Oct 2026 09:00:00 +0800") is not None))
    outcomes.append(_Result("invalid_mail_date_none", _parse_time("not a date") is None))

    # 19. preview budget excludes sensitive messages
    import subprocess as _sp
    from .collectors import collect_himalaya

    class _Runner:
        def __init__(self):
            self.reads = []

        def run(self, argv, timeout):
            if "envelope" in argv:
                return 0, json.dumps({"envelopes": [
                    {"id": "otp1", "subject": "Your OTP code", "from": [{"email": "x@bank"}],
                     "date": "2026-10-02T19:00:00Z", "has-attachment": False},
                    {"id": "n1", "subject": "Project notes", "from": [{"email": "p@x"}],
                     "date": "2026-10-02T19:30:00Z", "has-attachment": False},
                ]}), ""
            if "read" in argv:
                self.reads.append(argv[argv.index("read") + 1])
                return 0, "plain safe body", ""
            return 1, "", "unexpected"

    r = _Runner()
    res = collect_himalaya("formal", 15, clock=clock, runner=r, preview_limit=1)
    outcomes.append(_Result("preview_budget_skips_sensitive",
                            r.reads == ["n1"] and res.items[0]["preview"] == "[redacted: sensitive message]"))

    # 20. invalid kind rejected
    obj = {"schema": 1, "action": "notify", "kind": "email", "key": "evt:x", "message": "hi", "urgency": "normal"}
    outcomes.append(_Result("invalid_kind_rejected", validate_object(obj, fixture_packet("evt:x")).action == "invalid", ""))

    # 21. per-kind cap terminates on an over-limit single kind (CPU-spin guard)
    from .candidates import _kind_capped
    big = [{"kind": "mail", "ref": f"mail:{i}"} for i in range(61)]
    capped = _kind_capped(big, 60)
    outcomes.append(_Result("kind_cap_terminates_and_caps", len(capped) == 60, f"len={len(capped)}"))

    # 22. whole-run deadline aborts a hung run and is not swallowable
    from .watchdog import RunDeadlineExceeded, run_deadline
    aborted = False
    # ALL timed-probe work (import, marker computation, busy loop and the
    # deadline context itself) is inside the try so a signal delivered right at
    # the boundary can never escape as an unhandled RunDeadlineExceeded. The real
    # watchdog is unchanged; this only removes a test-side race.
    try:
        import time as _time
        _end = _time.monotonic() + 3.0
        with run_deadline(0.2):
            while _time.monotonic() < _end:
                pass
    except RunDeadlineExceeded:
        aborted = True
    outcomes.append(_Result("run_deadline_aborts_hung_run",
                            aborted and not issubclass(RunDeadlineExceeded, Exception), ""))

    # 23. reminder busy status: explicit mutations are distinct from success;
    # scheduled dispatch keeps a silent exit-0 skip so overlap never becomes
    # native failure spam (and a busy skip is not proof the registry was read).
    from . import reminders as _reminders
    outcomes.append(_Result("reminder_busy_status_distinct",
                            _reminders.EXIT_BUSY != _reminders.EXIT_OK
                            and _reminders.EXIT_BUSY == 75
                            and _reminders.DISPATCH_BUSY_EXIT == _reminders.EXIT_OK,
                            str(_reminders.EXIT_BUSY)))

    # -- adaptive watch acquisition (stage-only; all fake, no network) --------
    from . import planner as _planner
    from . import watchstore as _watchstore
    from .adaptive import collect_adaptive as _collect_adaptive
    from .planner import FakePlanner as _FakePlanner
    from .planner import validate_plan as _validate_plan
    from .planner import validate_query as _validate_query
    from .watchstore import WatchError as _WatchError

    with tempfile.TemporaryDirectory() as tmp:
        wstore = StateStore(Path(tmp), day)
        w = _watchstore.apply_action(wstore, {
            "action": "add", "public_topic": "quantum networking", "scope": "reddit",
            "provenance": "explicit user request"}, day.now())
    outcomes.append(_Result("watch_add_versioned_and_public_safe",
                            w["version"] == 1 and w["status"] == "active"))
    rejected = False
    try:
        _watchstore.validate_public_topic("alex@example.com")
    except _WatchError:
        rejected = True
    outcomes.append(_Result("watch_validation_rejects_pii", rejected))

    corpus = _load_adversarial_corpus()
    if corpus is None:
        outcomes.append(_Result(
            "adversarial_corpus_present", False,
            f"missing or invalid {_adversarial_corpus_path()}"))
        bad_plans = [{"queries": ["alex@example.com"]}, {"queries": ["x"] * 3}]
    else:
        outcomes.append(_Result("adversarial_corpus_present", True,
                                f"{len(corpus)} queries"))
        bad_plans = ([{"queries": ["alex@example.com"]}, {"queries": ["x"] * 3}]
                     + [{"queries": [query]} for query in corpus])
    for bad in bad_plans:
        try:
            _validate_plan(bad, policy)
        except _planner.PlannerError:
            continue
        outcomes.append(_Result(f"plan_rejects_{len(bad['queries'])}", False, str(bad)))
        break
    else:
        outcomes.append(_Result("malformed_pii_plan_fails_closed", True))
    outcomes.append(_Result("novel_nonsensitive_query_allowed",
                            _validate_query("mycorrhizal networks") == "mycorrhizal networks"))

    with tempfile.TemporaryDirectory() as tmp:
        wstore = StateStore(Path(tmp), day)
        _watchstore.apply_action(wstore, {
            "action": "add", "public_topic": "quantum networking", "scope": "reddit",
            "provenance": "explicit user request"}, day.now())
        recorder = _FakePlanner({"queries": ["quantum networking"], "scope": "reddit",
                                 "semantic": True, "provenance": "model"})

        class _F:
            calls = []

            def __call__(self, url, **kw):
                self.calls.append(url)
                raise AssertionError("no fetch when paused")

        f = _F()
        summary = _collect_adaptive(policy, day, store=wstore, planner=recorder,
                                    fetcher=f, paused=True)
        outcomes.append(_Result("adaptive_pause_zero_reads_and_model",
                                summary["status"] == "skipped" and recorder.calls == []
                                and f.calls == []))

    with tempfile.TemporaryDirectory() as tmp:
        wstore = StateStore(Path(tmp), day)
        w = _watchstore.apply_action(wstore, {
            "action": "add", "public_topic": "quantum networking", "scope": "reddit",
            "provenance": "explicit user request"}, day.now())
        changed = _watchstore.apply_action(wstore, {
            "action": "change", "id": w["id"], "public_topic": "renewable energy",
            "provenance": "explicit user request"}, day.now())
        outcomes.append(_Result("watch_change_supersedes_plan",
                                changed["version"] == 2 and changed["plan"] is None))

    # -- native search bridge + broad discovery + trusted intake (fake) --------
    from . import discovery as _disc
    from .intake import IntakeError as _IntakeError
    from .intake import intake_to_action as _intake_to_action
    from .searchbridge import STATUS_EMPTY as _S_EMPTY
    from .searchbridge import STATUS_INVALID as _S_INVALID
    from .searchbridge import STATUS_OK as _S_OK
    from .searchbridge import STATUS_RATE_LIMITED as _S_RL
    from .searchbridge import FakeSearchBridge as _FakeSearch
    from .searchbridge import _default_url_validator as _bridge_validator
    from .searchbridge import outcome_from_payload as _bridge_outcome

    ok = _bridge_outcome("q", {"success": True, "data": {"web": [
        {"title": "T", "url": "https://example.org/x"}]}}, 0, url_validator=_bridge_validator)
    outcomes.append(_Result("native_bridge_ok_hit", ok.status == _S_OK and len(ok.hits) == 1))
    empty = _bridge_outcome("q", {"success": True, "data": {"web": []}}, 0)
    outcomes.append(_Result("native_bridge_healthy_empty_distinct",
                            empty.status == _S_EMPTY and empty.healthy_empty))
    malformed = _bridge_outcome("q", "not a dict", 0)
    outcomes.append(_Result("native_bridge_malformed_invalid", malformed.status == _S_INVALID))
    limited = _bridge_outcome("q", {"success": False, "error": "429 rate limit"}, 0)
    outcomes.append(_Result("native_bridge_rate_limited", limited.status == _S_RL))

    bplan = {"watch_id": "w_selftest", "watch_version": 1, "scope": "public",
             "queries": ["topic one"]}
    bpolicy = json.loads(json.dumps(policy))
    bpolicy["adaptive_broad_discovery_enabled"] = True
    bpayload = {"success": True, "data": {"web": [
        {"title": "News", "url": "https://news.example/x"}]}}
    found, bsummary = _disc.discover_public_sources(
        bplan, bpolicy, _FakeSearch({"topic one": bpayload}),
        resolver=lambda host: ["93.184.216.34"])
    outcomes.append(_Result("broad_discovery_vetted_source",
                            len(found) == 1 and bsummary["discovered"] == 1))
    found_private, _ = _disc.discover_public_sources(
        bplan, bpolicy, _FakeSearch({"topic one": bpayload}),
        resolver=lambda host: ["10.0.0.1"])
    outcomes.append(_Result("broad_discovery_private_dns_rejected", found_private == []))

    ack_rejected = False
    try:
        _intake_to_action("thanks", policy=policy, now=day.now())
    except _IntakeError:
        ack_rejected = True
    outcomes.append(_Result("trusted_intake_ack_rejected", ack_rejected))
    action = _intake_to_action("watch for mycorrhizal networks", policy=policy,
                               model=None, now=day.now())
    outcomes.append(_Result("trusted_intake_explicit_fallback",
                            action.get("action") == "add"
                            and "mycorrhizal" in action.get("public_topic", "")))

    # -- per-source health gate (durable incidents, bounded recovery) ---------
    from . import source_health as _sh
    from .collectors import SourceResult as _SR
    hp = json.loads(json.dumps(policy))
    hp["health_critical_sources"] = ["mail:formal"]
    hstate: dict = {}
    t0 = day.now()
    _sh.record_observations(hstate, [_SR("mail:formal", "error", error="HTTP 503")], t0, hp)
    outcomes.append(_Result("health_retryable_silent_initially",
                            _sh.health_alert(hstate, t0, hp, commit=False) is None))
    for _ in range(3):
        _sh.record_observations(hstate, [_SR("mail:formal", "error", error="401 unauthorized")], t0, hp)
    hstate["source_health"]["sources"]["mail:formal"]["first_failure_at"] = (
        t0 - timedelta(hours=3)).isoformat()
    later = t0 + timedelta(minutes=61)
    _sh.dispatch_gate(hstate, "mail:formal", later, hp)
    _sh.record_observations(hstate, [_SR("mail:formal", "error", error="401 unauthorized")], later, hp)
    later2 = later + timedelta(minutes=61)
    _sh.dispatch_gate(hstate, "mail:formal", later2, hp)
    _sh.record_observations(hstate, [_SR("mail:formal", "error", error="401 unauthorized")], later2, hp)
    first = _sh.health_alert(hstate, later2, hp, commit=True)
    outcomes.append(_Result("health_critical_persistent_alerts_once",
                            first is not None
                            and _sh.health_alert(hstate, later2, hp, commit=False) is None))
    dstate: dict = {}
    for _ in range(3):
        _sh.record_observations(dstate, [_SR("mail:formal", "error", error="429 Retry-After: 3600")], t0, hp)
    blocked = _sh.dispatch_gate(dstate, "mail:formal", t0 + timedelta(minutes=5), hp)
    outcomes.append(_Result("health_429_blocks_until_due", not blocked["dispatch"]))
    outcomes.append(_Result("health_cached_ok_not_recovery",
                            _sh.classify_observation(
                                _SR("mail:formal", "ok", cached=True)) == _sh.OBSERVATION_NON_EVIDENTIAL))
    outcomes.append(_Result("health_routine_message_guarded",
                            _sh.routine_health_reason("Coverage note: mail could not be read") is not None
                            and _sh.routine_health_reason("Your payment is due tomorrow") is None))

    # report
    passed = sum(1 for o in outcomes if o.passed)
    print(json.dumps({
        "self_test": "proactive-pilot",
        "total": len(outcomes),
        "passed": passed,
        "failed": len(outcomes) - passed,
        "results": [{"name": o.name, "passed": o.passed, "detail": o.detail} for o in outcomes],
    }, indent=2))
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(run_self_test())
