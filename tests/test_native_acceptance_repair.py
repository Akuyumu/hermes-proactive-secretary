"""Acceptance-repair coverage: native ctx lanes, fail-closed config, migration parity.

Everything here is offline fakes: no network, no model, no live profile, no
credential. The module proves the production repair wiring rather than a facade.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import interestproposal as ip  # noqa: E402
from proactive_pilot import intake as intake_mod  # noqa: E402
from proactive_pilot import native_runtime as nr  # noqa: E402
from proactive_pilot import reminders as R  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.planner import NativePlanner  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402


def _load_plugin_package():
    name = "hermes_proactive_secretary_repair_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")


class _Structured:
    def __init__(self, parsed):
        self.parsed = parsed
        self.text = json.dumps(parsed)


class StrictFakeLlm:
    """Fake that only accepts the documented PluginLlm.complete_structured fields.

    An unknown/invalid kwarg (e.g. a provider/model override) is a TypeError, so
    the lane cannot silently widen the call shape.
    """

    _ALLOWED = {"instructions", "input", "json_schema", "json_mode", "schema_name",
                "system_prompt", "temperature", "max_tokens", "timeout", "purpose", "task"}

    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def complete_structured(self, **kwargs):
        unknown = set(kwargs) - self._ALLOWED
        if unknown:
            raise TypeError(f"unexpected kwargs: {sorted(unknown)}")
        self.calls.append(kwargs)
        return _Structured(self.parsed)


class FakeCtx:
    def __init__(self, llm=None, settings=None, raise_get=False):
        if llm is not None:
            self.llm = llm
        self._settings = settings or {}
        self._raise_get = raise_get
        if llm is not None:
            nr.bind_model_lane(self, allowed=True, reason="test host lane")

    def get_config(self, key, default=None):
        if self._raise_get:
            raise RuntimeError("native settings unavailable")
        return self._settings.get(key, default)


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": [],
                              "paused": {"active": False}}
    policy.update(over)
    return policy


# --------------------------------------------------------------------------- #
# 1. fail-closed native config semantics
# --------------------------------------------------------------------------- #


class NativeConfigFailClosed(unittest.TestCase):
    def test_malformed_native_setting_does_not_fall_back_to_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "user_config.json"
            path.write_text(json.dumps({"schema": 1, "version": 1, "timezone": "UTC"}),
                            encoding="utf-8")
            ctx = FakeCtx(settings={"user_config": "{not json"})
            with self.assertRaises(uc.ConfigError) as caught:
                uc.load_user_config(path, native=ctx)
            self.assertIn("native", str(caught.exception))

    def test_non_object_native_setting_fails_closed(self):
        for bad in ([1, 2], 7, True):
            with self.assertRaises(uc.ConfigError):
                uc.load_user_config(None, native=FakeCtx(settings={"user_config": bad}))

    def test_native_read_failure_fails_closed(self):
        with self.assertRaises(uc.ConfigError):
            uc.load_user_config(None, native=FakeCtx(raise_get=True))

    def test_absent_native_setting_is_unset_not_invalid(self):
        loaded = uc.load_user_config(None, native=FakeCtx(settings={}))
        self.assertFalse(loaded.present)
        self.assertEqual(loaded.source, "defaults")

    def test_native_object_preferred_over_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "user_config.json"
            path.write_text(json.dumps({"schema": 1, "version": 1, "timezone": "America/New_York"}),
                            encoding="utf-8")
            ctx = FakeCtx(settings={"user_config": {"schema": 1, "version": 1,
                                                    "timezone": "Europe/London"}})
            loaded = uc.load_user_config(path, native=ctx)
            self.assertEqual(loaded.source, "native")
            self.assertEqual(loaded.config["timezone"], "Europe/London")


# --------------------------------------------------------------------------- #
# 2. every model lane reuses the host ctx.llm facade
# --------------------------------------------------------------------------- #


class NativeModelLaneTests(unittest.TestCase):
    def test_lane_is_model_free_resistant(self):
        ctx = FakeCtx(StrictFakeLlm({"x": 1}))
        lane = nr.build_model_lane(ctx, _policy(), path="disabled",
                                   purpose="secretary.disabled")
        self.assertFalse(lane.available)

    def test_planner_uses_ctx_llm_and_reports_route(self):
        payload = {"schema": 1, "queries": ["mycorrhizal networks"], "scope": "reddit",
                   "semantic": True, "provenance": "model"}
        llm = StrictFakeLlm(payload)
        watch = {"id": "ig_1", "version": 1, "public_topic": "mycorrhizal networks",
                 "scope": "reddit", "include": [], "exclude": [], "origin": "inferred"}

        def _resolver(*a, **k):  # must never be called on a supported runtime
            raise AssertionError("resolver must not run when ctx.llm is available")

        planner = NativePlanner(ctx=FakeCtx(llm), resolver=_resolver)
        plan = planner.plan(watch, _policy())
        self.assertEqual(plan["scope"], "reddit")
        self.assertEqual(planner.route, "ctx.llm")
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("model", llm.calls[0])
        self.assertEqual(llm.calls[0]["purpose"], "secretary.plan")

    def test_interest_proposer_uses_ctx_llm(self):
        payload = {"schema": 1, "origin": "inferred", "interests": [
            {"topic": "mycorrhizal networks", "include": [], "exclude": [],
             "scope": "reddit", "confidence": "medium"}]}
        llm = StrictFakeLlm(payload)

        def _resolver(*a, **k):
            raise AssertionError("resolver must not run")

        proposer = ip.NativeInterestProposer(ctx=FakeCtx(llm), resolver=_resolver)
        got = proposer.propose(["garden project"], _policy())
        self.assertEqual(got["interests"][0]["topic"], "mycorrhizal networks")
        self.assertEqual(proposer.route, "ctx.llm")

    def test_privacy_verifier_uses_ctx_llm(self):
        payload = {"schema": 1, "decision": "allow", "reason": "", "safe_topic": ""}
        llm = StrictFakeLlm(payload)

        def _resolver(*a, **k):
            raise AssertionError("resolver must not run")

        verifier = ip.NativePrivacyVerifier(ctx=FakeCtx(llm), resolver=_resolver)
        got = verifier.review("mycorrhizal networks", [], [], ["garden"], _policy())
        self.assertEqual(got["decision"], "allow")
        self.assertEqual(verifier.route, "ctx.llm")

    def test_intake_model_uses_ctx_llm(self):
        payload = {"schema": 1, "is_watch_request": True, "public_topic": "quantum",
                   "include": [], "exclude": [], "scope": "public", "confidence": "low",
                   "expiry": ""}
        llm = StrictFakeLlm(payload)

        def _resolver(*a, **k):
            raise AssertionError("resolver must not run")

        model = intake_mod.NativeIntakeModel(ctx=FakeCtx(llm), resolver=_resolver)
        got = model.extract("watch quantum please", _policy())
        self.assertTrue(got["is_watch_request"])
        self.assertEqual(model.route, "ctx.llm")


# --------------------------------------------------------------------------- #
# 3. complete effective migration parity + explicit migration inputs
# --------------------------------------------------------------------------- #


class MigrationParityTests(unittest.TestCase):
    def _legacy(self):
        return {
            "timezone": "Asia/Singapore",
            "feeds_enabled": True,
            "obligations_enabled": True,
            "collector_budget_seconds": 60,
            "run_wallclock_seconds": 123,
            "core_budget_seconds": 11,
            "public_budget_seconds": 12,
            "obligation_budget_seconds": 13,
            "max_public_registry": 7,
            "stale_after_minutes": 41,
            "obligation_scan_limit": 222,
            "evaluator": {"enabled": True, "toolset": "context_engine", "max_turns": 9},
            "mail_noise_patterns": ["linkedin"],
        }

    def test_every_remaining_effective_field_is_retained_and_reported(self):
        config, report = uc.migrate_legacy(self._legacy())
        self.assertIn("run_wallclock_seconds", report["retained_effective_fields"])
        self.assertIn("evaluator", report["retained_effective_fields"])
        overrides = uc.policy_overrides(uc.validate_config(config))
        self.assertEqual(overrides["run_wallclock_seconds"], 123)
        self.assertEqual(overrides["core_budget_seconds"], 11)
        self.assertEqual(overrides["evaluator"]["max_turns"], 9)
        self.assertEqual(overrides["obligation_scan_limit"], 222)
        # Typed keys still win and are not duplicated into the passthrough.
        self.assertEqual(overrides["timezone"], "Asia/Singapore")
        self.assertNotIn("timezone", report["retained_effective_fields"])

    def test_explicit_migration_inputs_preserve_existing_scope(self):
        config, report = uc.migrate_legacy(
            self._legacy(), mail_accounts=["work"], calendar_accounts=["work"],
            calendar_ids=["team@group.calendar.google.com"],
            task_lists=["Home"], task_adapter="tasks")
        overrides = uc.policy_overrides(uc.validate_config(config))
        self.assertEqual(overrides["mail_accounts"], ["work"])
        self.assertEqual(overrides["calendar_accounts"], ["work"])
        self.assertEqual(overrides["calendar_ids"], ["team@group.calendar.google.com"])
        self.assertEqual(overrides["task_lists"], ["Home"])
        self.assertEqual(overrides["task_adapter"], "tasks")
        self.assertTrue(any("explicit migration input" in m for m in report["mapped"]))

    def test_apply_full_passthrough_reconstructs_effective_policy(self):
        legacy = self._legacy()
        config, _ = uc.migrate_legacy(legacy)
        merged = uc.apply_user_config(legacy, uc.validate_config(config))
        for key, value in legacy.items():
            self.assertEqual(merged.get(key), value, key)


# --------------------------------------------------------------------------- #
# 4. deterministic reminders share the same config precedence + scope
# --------------------------------------------------------------------------- #


class ReminderConfigParity(unittest.TestCase):
    def _write(self, root, policy, config=None):
        policy_path = Path(root) / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        if config is not None:
            (Path(root) / "user_config.json").write_text(json.dumps(config), encoding="utf-8")
        return policy_path

    def test_explicit_config_restricts_event_aliases(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            config = {"schema": 1, "version": 1, "timezone": "UTC",
                      "calendar": {"accounts": ["work"], "calendars": ["primary"]},
                      "mail": {"accounts": ["work"]}}
            policy_path = self._write(tmp, {"timezone": "Asia/Singapore"}, config)
            policy, aliases, error = R.load_effective_policy(policy_path)
            self.assertEqual(error, "")
            self.assertEqual(aliases, ["work"])
            self.assertEqual(policy["calendar_accounts"], ["work"])
            self.assertEqual(policy["timezone"], "UTC")

    def test_missing_config_keeps_legacy_aliases_none(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            policy_path = self._write(tmp, {"timezone": "Asia/Singapore"})
            _policy_loaded, aliases, error = R.load_effective_policy(policy_path)
            self.assertEqual(error, "")
            self.assertIsNone(aliases)

    def test_invalid_config_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            policy_path = self._write(tmp, {"timezone": "Asia/Singapore"},
                                      {"schema": 1, "version": 1, "token": "abc"})
            _policy_loaded, aliases, error = R.load_effective_policy(policy_path)
            self.assertTrue(error)
            self.assertIsNone(aliases)

    def test_gws_event_verification_is_explicitly_unsupported(self):
        getter = R.SecretaryEventGetter(
            aliases=["work"],
            policy={"gws_calendar_enabled": True, "productivity_secretary_enabled": False})
        obs = getter.get("work", "primary", "e1")
        self.assertFalse(obs["ok"])
        self.assertEqual(obs["status"], "unsupported")
        # Non-destructive: a dispatch leaves the record active/deferred, never
        # canceled/emitted, and never fetches a foreign/default account.
        now = R.parse_aware("2026-10-05T09:00:00+00:00")
        reg = R.empty_registry()
        reg["reminders"]["r1"] = {
            "id": "r1", "kind": "event", "status": "pending", "text": "synthetic",
            "provenance": "synthetic explicit user request", "alias": "work",
            "calendar_id": "primary", "event_id": "e1", "lead_minutes": 10,
            "late_grace_minutes": 10,
            "event_due": "2026-10-05T10:00:00+00:00",
            "trigger_at": "2026-10-05T09:50:00+00:00",
            "last_check_at": "", "event_version": "v1", "attempts": 0,
        }
        plan = R.plan_dispatch(reg, now=now, prefs={"paused": {"active": False}},
                               getter=getter)
        self.assertEqual(plan.messages, [])
        self.assertEqual(reg["reminders"]["r1"]["status"], "uncertain")


# --------------------------------------------------------------------------- #
# 5. registered scheduled bridge surface
# --------------------------------------------------------------------------- #


class ScheduledBridgeSurface(unittest.TestCase):
    def test_pipeline_command_is_registered_and_parses(self):
        parser = argparse.ArgumentParser(prog="hermes secretary")
        sc.register_cli(parser)
        ns = parser.parse_args(["pipeline", "--check"])
        self.assertEqual(ns.secretary_command, "pipeline")
        self.assertTrue(ns.check)

    def test_pipeline_in_dispatch(self):
        self.assertIn("pipeline", sc._DISPATCH)

    def test_configure_migrate_accepts_explicit_scope_flags(self):
        parser = argparse.ArgumentParser(prog="hermes secretary")
        sc.register_cli(parser)
        ns = parser.parse_args(["configure", "migrate", "--mail-account", "work",
                                "--calendar-id", "team@group.calendar.google.com"])
        self.assertEqual(ns.mail_account, ["work"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
