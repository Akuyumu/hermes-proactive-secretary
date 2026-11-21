"""Integrated 0.5.0 seams: allowances, route safety, project, sessions, refs.

No network/model/live profile. Fakes model only the host facade; every guarded
path is exercised through the real plugin modules and real registration helper.
"""
from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = tempfile.mkdtemp(prefix="secretary-050-integration-")
os.environ.setdefault("HERMES_HOME", os.path.join(SCRATCH, "home"))
atexit.register(shutil.rmtree, SCRATCH, True)
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import evaluator as evaluator_mod  # noqa: E402
from proactive_pilot import intake as intake_mod  # noqa: E402
from proactive_pilot import interestproposal as ip  # noqa: E402
from proactive_pilot import native_runtime as nr  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.alertrefs import remember_alert  # noqa: E402
from proactive_pilot.planner import NativePlanner, PlannerError  # noqa: E402


def _load_package():
    name = "hermes_proactive_secretary_050_integration"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
onboarding = importlib.import_module(f"{PKG.__name__}.secretary.secretary_onboarding")
models = importlib.import_module(f"{PKG.__name__}.secretary.secretary_models")
conversation = importlib.import_module(f"{PKG.__name__}.secretary.secretary_conversation")


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeLlm:
    _ALLOWED = {"instructions", "input", "json_schema", "json_mode", "schema_name",
                "system_prompt", "temperature", "max_tokens", "timeout", "purpose"}

    def __init__(self, parsed=None):
        self.calls = []
        self.parsed = parsed or {"schema": 1, "action": "silence"}

    def complete_structured(self, **kwargs):
        unknown = set(kwargs) - self._ALLOWED
        if unknown:
            raise TypeError(f"unexpected kwargs: {sorted(unknown)}")
        self.calls.append(kwargs)

        class Result:
            text = ""
            parsed = self.parsed
        return Result()


class Ctx:
    def __init__(self, *, settings=None, strict=False, llm=None, fail_strict=False):
        self.settings = dict(settings or {})
        self.state = MemoryState()
        self.strict = strict
        self.fail_strict = fail_strict
        if llm is not None:
            self.llm = llm
        self.aux_calls = []
        self.commands = {}
        self.hooks = {}
        self.tools = []
        self.injected = []
        self.plugin_id = "hermes-proactive-secretary"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None):
        self.aux_calls.append({"key": key, "defaults": defaults, "kwargs": {}})

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append((name, handler))

    def register_skill(self, *a, **k):
        pass

    def register_cli_command(self, *a, **k):
        pass

    def inject_message(self, *a, **k):
        self.injected.append((a, k))
        return False


class StrictCtx(Ctx):
    def __init__(self, **kwargs):
        kwargs["strict"] = True
        super().__init__(**kwargs)

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None,
                                fallback_policy=None):
        self.aux_calls.append({"key": key, "defaults": defaults,
                               "kwargs": {"fallback_policy": fallback_policy}})
        if self.fail_strict and fallback_policy:
            raise RuntimeError("registry lookup failed")


ROUTES_OK = {"primary_provider": "provider-a", "primary_model": "model-a",
             "fallback_provider": "provider-b", "fallback_model": "model-b"}


class AllowanceAndSettingsTests(unittest.TestCase):
    def test_allowance_is_optional_nonnegative_integer(self):
        for value in (0, 5, 1000, 10 ** 9):
            svc = onboarding.OnboardingService(Ctx())
            svc.update(settings={"daily_allowance": value})
            self.assertEqual(svc.inspect()["state"]["settings"]["daily_allowance"], value)
        # The canonical string control accepts digit text, blank and None.
        for raw, expected in (("5", 5), (" 0 ", 0), ("", None), ("   ", None),
                              (None, None)):
            svc = onboarding.OnboardingService(Ctx())
            svc.update(settings={"daily_allowance": raw})
            self.assertEqual(
                svc.inspect()["state"]["settings"]["daily_allowance"], expected)
        # Booleans, negatives, fractions and non-numeric text stay rejected.
        for bad in (-1, True, False, 1.5, "1.5", "many"):
            svc = onboarding.OnboardingService(Ctx())
            with self.assertRaises(onboarding.OnboardingError):
                svc.update(settings={"daily_allowance": bad})

    def test_timezone_unknown_setting_and_route_pairs(self):
        svc = onboarding.OnboardingService(Ctx())
        for patch in ({"timezone": "Mars/Olympus"},
                      {"timezone": "not a timezone!"},
                      {"unknown_control": 1},
                      {"primary_provider": "p"},
                      {"fallback_provider": "f", "fallback_model": "m"}):
            with self.assertRaises(onboarding.OnboardingError, msg=str(patch)):
                svc.update(settings=patch)
        svc.update(settings=ROUTES_OK)
        self.assertEqual(svc.inspect()["state"]["settings"]["primary_model"], "model-a")

    def test_manifest_route_fields_are_separate_and_validated(self):
        import yaml
        schema = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
        self.assertEqual(set(schema) - set(uc.NATIVE_FLAT_FIELDS),
                         set(models.ROUTE_SETTING_KEYS) | {"portal_reasoning_effort"})
        for key in models.ROUTE_SETTING_KEYS:
            self.assertEqual(schema[key]["default"], "")
        self.assertEqual(
            (schema["portal_reasoning_effort"]["type"],
             schema["portal_reasoning_effort"]["default"]), ("str", ""))
        _clean, errors = models.validate_route_settings({"primary_provider": "p"})
        self.assertTrue(any("together" in error for error in errors))
        _clean, errors = models.validate_route_settings(
            {"primary_provider": "p", "primary_model": "m",
             "fallback_provider": "f", "fallback_model": "f"})
        self.assertEqual(errors, [])


class ModelRouteGuardTests(unittest.TestCase):
    def test_kwargs_is_not_strict_capability_proof(self):
        class Swallow:
            def register_auxiliary_task(self, key, **kwargs):
                return None
        self.assertFalse(models.supports_strict_policy(Swallow().register_auxiliary_task))
        self.assertTrue(models.supports_strict_policy(StrictCtx().register_auxiliary_task))

    def test_old_host_denies_every_model_lane_before_dispatch(self):
        llm = FakeLlm()
        ctx = Ctx(llm=llm, settings=ROUTES_OK)
        result = models.register_configured_models(ctx)
        self.assertFalse(result["strict_active"])
        self.assertFalse(result["status"]["configured_model_calls_allowed"])

        packet = evaluator_mod.fixture_packet("evt:1")
        verdict = evaluator_mod.NativeLlmEvaluator(ctx, {}).evaluate(packet, {"_preferences": {}})
        self.assertEqual(verdict.action, "error")

        called = []
        planner = NativePlanner(ctx=ctx, resolver=lambda *a, **k: called.append("resolver"))
        with self.assertRaises(PlannerError):
            planner.plan({"id": "w1", "public_topic": "quantum", "scope": "public",
                          "include": [], "exclude": [], "origin": "explicit"}, {})
        self.assertEqual(called, [])
        with self.assertRaises(intake_mod.IntakeError):
            intake_mod.NativeIntakeModel(ctx=ctx, resolver=lambda *a, **k: called.append("resolver")
                                        ).extract("watch quantum", {})
        with self.assertRaises(ip.InferredProposalError):
            ip.NativeInterestProposer(ctx=ctx, resolver=lambda *a, **k: called.append("resolver")
                                      ).propose(["context"], {})
        with self.assertRaises(ip.PrivacyReviewError):
            ip.NativePrivacyVerifier(ctx=ctx, resolver=lambda *a, **k: called.append("resolver")
                                     ).review("topic", [], [], ["ctx"], {})
        self.assertEqual(llm.calls, [])
        self.assertEqual(called, [])

    def test_strict_registration_failure_never_falls_back_to_native(self):
        llm = FakeLlm()
        ctx = StrictCtx(llm=llm, fail_strict=True, settings=ROUTES_OK)
        result = models.register_configured_models(ctx)
        self.assertFalse(result["registered"])
        self.assertFalse(result["strict_active"])
        self.assertEqual(len(ctx.aux_calls), 1)
        self.assertEqual(ctx.aux_calls[0]["kwargs"], {"fallback_policy": "configured_only"})
        allowed, _reason = nr.model_lane_allowed(ctx)
        self.assertFalse(allowed)
        verdict = evaluator_mod.NativeLlmEvaluator(ctx, {}).evaluate(
            evaluator_mod.fixture_packet("evt:2"), {"_preferences": {}})
        self.assertEqual(verdict.action, "error")
        self.assertEqual(llm.calls, [])

    def test_strict_host_valid_route_dispatches_and_extra_lanes_block(self):
        llm = FakeLlm({"schema": 1, "action": "silence"})
        ctx = StrictCtx(llm=llm, settings=ROUTES_OK)
        result = models.register_configured_models(ctx)
        self.assertTrue(result["strict_active"])
        self.assertEqual(ctx.aux_calls[0]["kwargs"], {"fallback_policy": "configured_only"})
        verdict = evaluator_mod.NativeLlmEvaluator(ctx, {}).evaluate(
            evaluator_mod.fixture_packet("evt:3"), {"_preferences": {}})
        self.assertEqual(verdict.route, "ctx.llm")
        self.assertEqual(len(llm.calls), 1)

        blocked_ctx = StrictCtx(llm=FakeLlm(), settings=ROUTES_OK)
        blocked = models.register_configured_models(
            blocked_ctx,
            auxiliary_reader=lambda _key: {"provider": "op", "model": "op-m",
                                           "fallback_chain": [{"provider": "a", "model": "a"},
                                                              {"provider": "b", "model": "b"}]})
        self.assertTrue(blocked["status"]["blockers"])
        self.assertFalse(blocked["status"]["configured_model_calls_allowed"])
        allowed, _reason = nr.model_lane_allowed(blocked_ctx)
        self.assertFalse(allowed)

    def test_wrong_context_cannot_borrow_another_route(self):
        allowed_ctx = StrictCtx(llm=FakeLlm(), settings=ROUTES_OK)
        models.register_configured_models(allowed_ctx)
        other = StrictCtx(llm=FakeLlm(), settings=ROUTES_OK)
        self.assertTrue(nr.model_lane_allowed(allowed_ctx)[0])
        self.assertFalse(nr.model_lane_allowed(other)[0])
        verdict = evaluator_mod.NativeLlmEvaluator(other, {}).evaluate(
            evaluator_mod.fixture_packet("evt:4"), {"_preferences": {}})
        self.assertEqual(verdict.action, "error")
        self.assertEqual(other.llm.calls, [])


class ProjectionIntegrationTests(unittest.TestCase):
    def test_onboarding_preserves_scope_and_projects_flat_and_route_fields(self):
        opaque = {
            "schema": 1, "version": 1, "timezone": "Europe/Berlin",
            "notification": {"daily_normal_cap": 3, "destination": "old"},
            "sources": {"mail": True, "calendar": True},
            "mail": {"accounts": ["sample-work"], "aliases": {"sample-work": "work"}},
            "calendar": {"accounts": ["work-cal"], "calendars": ["primary"]},
            "tasks": {"accounts": ["task-work"], "lists": ["My Tasks"]},
            "bounds": {"max_candidates_per_kind": 20},
        }
        ctx = Ctx(settings={"user_config": opaque})
        svc = onboarding.OnboardingService(ctx)
        svc.update(settings={"timezone": "UTC", "daily_allowance": 9, **ROUTES_OK},
                   destination="local", explicit_consent=True)
        result = svc.confirm(user_confirmed=True, apply_settings=True)
        self.assertEqual(result["status"], "pending_verification")
        self.assertFalse(result["live_destination_verified"])

        saved = ctx.settings["user_config"]
        self.assertEqual(saved["mail"]["accounts"], ["sample-work"])
        self.assertEqual(saved["mail"]["aliases"], {"sample-work": "work"})
        self.assertEqual(saved["calendar"]["accounts"], ["work-cal"])
        self.assertEqual(saved["tasks"]["lists"], ["My Tasks"])
        self.assertTrue(saved["sources"]["mail"])
        self.assertEqual(saved["notification"]["daily_normal_cap"], 9)
        self.assertEqual(saved["notification"]["destination"], "local")
        self.assertEqual(saved["bounds"]["max_candidates_per_kind"], 20)
        # Ordinary native flat controls carry the same values (no second store).
        self.assertEqual(ctx.settings["daily_normal_cap"], 9)
        self.assertEqual(ctx.settings["timezone"], "UTC")
        self.assertTrue(ctx.settings["mail_enabled"])
        # Route fields go to their own native controls.
        for key in models.ROUTE_SETTING_KEYS:
            self.assertEqual(ctx.settings[key], ROUTES_OK[key])
        loaded = uc.load_user_config(native=ctx)
        self.assertEqual(loaded.config["mail"]["accounts"], ["sample-work"])
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)


class SessionScopedConversationTests(unittest.TestCase):
    def _service(self, ctx):
        return onboarding.OnboardingService(ctx)

    def test_pending_is_scoped_to_session_and_expires_or_cancels(self):
        ctx = Ctx()
        svc = self._service(ctx)
        conversation.set_pending(ctx, session_id="s1")
        hook = conversation.make_pre_llm_hook(ctx, svc)
        self.assertIsNone(hook(platform="telegram", user_message="hello", session_id="s2"))
        continued = hook(platform="telegram", user_message="Europe/London", session_id="s1")
        self.assertIn("continuation", continued["context"].lower())
        self.assertIsNone(hook(platform="telegram", user_message="again", session_id="s1"))

        conversation.set_pending(ctx, session_id="s1")
        ctx.state.data[conversation.PENDING_KEY]["created_at"] = time.time() - 10_000
        self.assertIsNone(hook(platform="telegram", user_message="late", session_id="s1"))

        conversation.set_pending(ctx, session_id="s1")
        handler = conversation.make_command_handler(ctx, svc)
        self.assertIn("cancelled", handler("cancel"))
        self.assertIsNone(conversation._pending_record(ctx, "s1"))

    def test_unbound_slash_pending_uses_next_human_turn_only(self):
        ctx = Ctx()
        svc = self._service(ctx)
        pending = conversation.set_pending(ctx, session_id="")
        hook = conversation.make_pre_llm_hook(ctx, svc)
        # An unrelated session cannot consume the unbound pending setup.
        self.assertIsNone(hook(platform="cli", user_message="UTC", session_id="one"))
        first = hook(platform="cli",
                     user_message=f"UTC sec-cont-{pending['token']}", session_id="two")
        self.assertIn("continuation", first["context"].lower())
        self.assertIsNone(hook(platform="cli", user_message="UTC", session_id="two"))

    def test_shared_chat_and_nonhuman_surfaces_are_skipped(self):
        ctx = Ctx()
        hook = conversation.make_pre_llm_hook(ctx, self._service(ctx))
        for kwargs in (
                {"platform": "telegram", "user_message": "hello", "chat_type": "group"},
                {"platform": "telegram", "user_message": "hello", "is_shared": True},
                {"platform": "telegram-group", "user_message": "hello"},
                {"platform": "cron", "user_message": "scheduled"},
                {"platform": "a2a", "user_message": "peer"},
                {"platform": "internal", "user_message": "system"}):
            self.assertIsNone(hook(**kwargs), kwargs)
        self.assertIsNone(conversation._invitation_record(ctx))


class AlertReferenceTests(unittest.TestCase):
    def test_emitted_refs_are_bounded_and_used_in_context(self):
        ctx = Ctx()
        for index in range(7):
            remember_alert(
                ctx.state, signature=f"sig-{index}", kind="calendar",
                candidate_refs=[{"ref": f"evt:{index}", "version": f"v{index}",
                                 "source": "calendar"}],
                destination="platform:chat-1", emitted_at=f"2026-10-20T0{index}:00:00Z")
        rows = ctx.state.get("alert_references")
        self.assertEqual(len(rows), 5)
        newest = rows[-1]
        hook = conversation.make_pre_llm_hook(ctx, onboarding.OnboardingService(ctx))
        out = hook(platform="telegram", user_message=f"why this {newest['ref']}?",
                   session_id="s1")
        self.assertIn(newest["ref"], out["context"])
        self.assertIn("evt:6@v6", out["context"])
        self.assertIn("platform:chat-1", out["context"])
        missing = hook(platform="telegram", user_message="why this sec-deadbeefdead?",
                       session_id="s1")
        self.assertIn("No matching emitted alert reference", missing["context"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
