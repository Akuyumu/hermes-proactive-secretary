"""0.5.0 conversation/model/onboarding tests with strict-signature fakes."""
from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRATCH = tempfile.mkdtemp(prefix="secretary-050-")
os.environ["HERMES_HOME"] = os.path.join(_SCRATCH, "home")
os.environ["HERMES_PROACTIVE_SECRETARY_DATA"] = os.path.join(_SCRATCH, "data")
atexit.register(shutil.rmtree, _SCRATCH, True)
sys.path.insert(0, str(_ROOT / "src"))


def _load_package():
    name = "hermes_proactive_secretary_050"
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(_ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
onboarding = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_onboarding")
models = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_models")
conversation = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_conversation")


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class StrictCtx:
    def __init__(self, *, settings=None, state=None, strict_aux=False,
                 injection_grant=False):
        self.settings = dict(settings or {})
        self.state = state if state is not None else MemoryState()
        self.strict_aux = strict_aux
        self.injection_grant = injection_grant
        self.aux_calls = []
        self.tool_calls = []
        self.commands = {}
        self.hooks = {}
        self.saved_config = []
        self.injected = []
        self.plugin_id = "hermes-proactive-secretary"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value
        self.saved_config.append((key, value))

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None):
        self.aux_calls.append({"key": key, "display_name": display_name,
                               "description": description, "defaults": defaults,
                               "inherit_from": inherit_from})

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="", emoji="",
                      override=False):
        self.tool_calls.append({"name": name, "toolset": toolset, "schema": schema,
                                "handler": handler, "description": description})

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_skill(self, name, path, description=""):
        self.skills = getattr(self, "skills", {})
        self.skills[name] = (path, description)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands = getattr(self, "cli_commands", {})
        self.cli_commands[name] = (help, setup_fn, handler_fn, description)

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append({"content": content, "role": role,
                              "session_key": session_key, "origin": origin})
        return self.injection_grant

    def _gateway_injection_allowed(self):
        return self.injection_grant


class StrictAuxCtx(StrictCtx):
    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None,
                                fallback_policy=None):
        self.aux_calls.append({"key": key, "display_name": display_name,
                               "description": description, "defaults": defaults,
                               "inherit_from": inherit_from,
                               "fallback_policy": fallback_policy})


def _service(ctx=None, *, state=None):
    return onboarding.OnboardingService(ctx, state=state)


def _complete_state(service, *, destination="local"):
    service.update(settings={"timezone": "Europe/London", "daily_allowance": 2},
                   destination=destination, explicit_consent=True)


class OnboardingProgressTests(unittest.TestCase):
    def test_resumable_progress_and_cancellation(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        svc.update(settings={"timezone": "Europe/London"}, explicit_consent=False)
        self.assertEqual(svc.inspect()["state"]["status"], "in_progress")
        svc.cancel()
        self.assertEqual(svc.inspect()["state"]["status"], "cancelled")
        resumed = _service(ctx)
        self.assertEqual(resumed.inspect()["state"]["settings"]["timezone"], "Europe/London")
        resumed.update(settings={"daily_allowance": 4})
        self.assertEqual(resumed.inspect()["state"]["status"], "in_progress")

    def test_memory_proposals_are_not_source_consent(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        svc.propose_facts([{"key": "city", "value": "London",
                            "provenance": "foreground_memory", "confidence": "high"}])
        state = svc.inspect()["state"]
        self.assertEqual(state["proposals"][0]["value"], "London")
        self.assertFalse(any(state["grants"].values()))
        with self.assertRaises(onboarding.OnboardingError):
            svc.update(grants={"mail": True}, explicit_consent=False)
        self.assertFalse(svc.inspect()["state"]["grants"]["mail"])
        svc.update(grants={"mail": True}, explicit_consent=True)
        self.assertTrue(svc.inspect()["state"]["grants"]["mail"])

    def test_invalid_values_write_nothing(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        before = json.dumps(svc.inspect()["state"], sort_keys=True)
        for patch in ({"timezone": "not a timezone!"},
                      {"daily_allowance": -1},
                      {"daily_allowance": True},
                      {"quiet_start_hour": 24},
                      {"api_key": "secret"}):
            with self.assertRaises(onboarding.OnboardingError):
                svc.update(settings=patch)
        with self.assertRaises(onboarding.OnboardingError):
            svc.update(destination="bad\nnewline")
        self.assertEqual(before, json.dumps(svc.inspect()["state"], sort_keys=True))

    def test_confirm_changes_no_schedule_or_grants(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        _complete_state(svc, destination="local")
        before_grants = dict(svc.inspect()["state"]["grants"])
        result = svc.confirm(user_confirmed=True, apply_settings=True)
        self.assertEqual(result["status"], "pending_verification")
        self.assertFalse(result["schedule_armed"])
        self.assertFalse(result["grants_changed_by_confirmation"])
        self.assertEqual(before_grants, svc.inspect()["state"]["grants"])
        self.assertEqual(ctx.saved_config[0][0], "user_config")
        self.assertNotIn("cron", json.dumps(ctx.saved_config).lower())
        self.assertEqual(result["config_readback"]["notification.destination"], "local")
        self.assertFalse(result["live_destination_verified"])

    def test_pending_verification_has_actionable_delivery_next_step(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        _complete_state(svc, destination="telegram")
        result = svc.confirm(user_confirmed=True, apply_settings=True)
        self.assertEqual(result["status"], "pending_verification")
        info = svc.inspect()
        self.assertEqual(info["next_step"], "delivery_verification")
        self.assertIn("hermes secretary setup --deliver telegram",
                      info["next_question"])
        self.assertIn("NOT confirmed", info["next_question"])
        status = conversation.command_text(ctx, svc, "status")
        self.assertIn("delivery_verification", status)
        self.assertIn("hermes secretary setup --deliver telegram", status)
        setup = conversation.command_text(ctx, svc, "setup")
        self.assertIn("delivery verification is still pending", setup)
        self.assertIn("hermes secretary setup --deliver telegram", setup)

    def test_model_cannot_mark_destination_live_verified(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        _complete_state(svc, destination="local")
        pending = svc.request_destination_verification()
        self.assertEqual(pending["status"], "pending")
        with self.assertRaises(onboarding.OnboardingError):
            svc.confirm_destination(pending["challenge"], user_confirmed=False)
        confirmed = svc.confirm_destination(pending["challenge"], user_confirmed=True)
        self.assertEqual(confirmed["status"], "user_confirmed")
        self.assertIn("Not a live delivery verification", confirmed["note"])

    def test_exact_readback_names_affected_record(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        result = svc.update(settings={"timezone": "America/New_York"}, destination="local")
        self.assertEqual(result["readback"]["record"], onboarding.STATE_KEY)
        self.assertEqual(result["readback"]["settings"]["timezone"], "America/New_York")
        self.assertEqual(result["readback"]["destination"]["target"], "local")
        self.assertEqual(result["readback"]["destination"]["status"], "unverified")


class ConversationSurfaceTests(unittest.TestCase):
    def test_invitation_once_per_profile_and_never_cron(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        hook = conversation.make_pre_llm_hook(ctx, svc)
        first = hook(platform="telegram", user_message="hello",
                     is_first_turn=True, task_id="t1", parent_session_id="")
        self.assertIn("onboarding", first["context"].lower())
        second = hook(platform="telegram", user_message="hello again",
                      is_first_turn=True, task_id="t2", parent_session_id="")
        self.assertIsNone(second)
        other = StrictCtx()
        other_hook = conversation.make_pre_llm_hook(other, _service(other))
        self.assertIsNotNone(other_hook(platform="cli", user_message="hi"))
        cron_ctx = StrictCtx()
        cron_hook = conversation.make_pre_llm_hook(cron_ctx, _service(cron_ctx))
        self.assertIsNone(cron_hook(platform="cron", user_message="scheduled work"))
        self.assertIsNone(cron_hook(platform="a2a", user_message="peer message"))
        self.assertIsNone(cron_hook(platform="internal", user_message="system"))

    def test_profile_separation(self):
        a = StrictCtx()
        b = StrictCtx()
        ha = conversation.make_pre_llm_hook(a, _service(a))
        hb = conversation.make_pre_llm_hook(b, _service(b))
        self.assertIsNotNone(ha(platform="cli", user_message="hello"))
        self.assertIsNotNone(hb(platform="cli", user_message="hello"))
        self.assertIsNone(ha(platform="cli", user_message="again"))
        self.assertIsNone(hb(platform="cli", user_message="again"))

    def test_unrelated_chat_is_not_taken_over(self):
        ctx = StrictCtx()
        conversation._set(ctx, conversation.INVITATION_KEY,
                          {"version": 1, "invited_at": 1})
        hook = conversation.make_pre_llm_hook(ctx, _service(ctx))
        self.assertIsNone(hook(platform="telegram", user_message="what is the weather?"))
        self.assertIsNone(hook(platform="telegram", user_message="/secretary setup"))

    def test_missing_injection_grant_uses_pending_next_reply(self):
        ctx = StrictCtx(injection_grant=False)
        svc = _service(ctx)
        surfaces = conversation.register_conversation_surfaces(ctx, service=svc)
        self.assertTrue(surfaces["command"])
        self.assertTrue(surfaces["hook"])
        self.assertFalse(surfaces["injection_used"])
        text = ctx.commands["secretary"][0]("setup")
        self.assertIn("Start or resume", text)
        self.assertIn("timezone", text)
        self.assertEqual(ctx.injected, [])
        hook = ctx.hooks["pre_llm_call"]
        token = ctx.state.data[conversation.PENDING_KEY]["token"]
        continued = hook(platform="telegram",
                         user_message=f"Europe/London sec-cont-{token}")
        self.assertIn("continuation", continued["context"].lower())
        self.assertIsNone(hook(platform="telegram", user_message="Europe/London"))
        self.assertEqual(ctx.injected, [])

    def test_injection_grant_does_not_force_a_send(self):
        ctx = StrictCtx(injection_grant=True)
        svc = _service(ctx)
        text = conversation.make_command_handler(ctx, svc)("setup")
        self.assertIn("Start or resume", text)
        self.assertEqual(ctx.injected, [])
        self.assertEqual(svc.inspect()["capabilities"]["gateway_injection"], True)

    def test_slash_cancel_clears_pending_without_arming(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        handler = conversation.make_command_handler(ctx, svc)
        handler("setup")
        token = ctx.state.data[conversation.PENDING_KEY]["token"]
        self.assertIsNotNone(conversation._pending_record(ctx, token=token))
        out = handler("cancel")
        self.assertIn("cancelled", out)
        self.assertIsNone(conversation._pending_record(ctx, token=token))
        self.assertEqual(svc.inspect()["state"]["status"], "cancelled")

    def test_relevant_reference_context_and_acknowledgement_rule(self):
        ctx = StrictCtx()
        hook = conversation.make_pre_llm_hook(ctx, _service(ctx))
        out = hook(platform="telegram", user_message="why this alert sec-abc123?")
        self.assertIn("sec-abc123", out["context"])
        self.assertIn("not preferences", out["context"])
        self.assertIn("specific approval", out["context"])

    def test_tool_rejects_invalid_input_without_write(self):
        ctx = StrictCtx()
        svc = _service(ctx)
        registered = conversation.register_onboarding_tool(ctx, service=svc)
        self.assertTrue(registered["tool"])
        handler = ctx.tool_calls[0]["handler"]
        before = json.dumps(svc.inspect()["state"], sort_keys=True)
        result = json.loads(handler({"action": "update", "settings": {"daily_allowance": -1}}))
        self.assertFalse(result["ok"])
        self.assertEqual(before, json.dumps(svc.inspect()["state"], sort_keys=True))
        ok = json.loads(handler({"action": "inspect"}))
        self.assertTrue(ok["ok"])


class ModelRouteTests(unittest.TestCase):
    def test_strict_signature_detection_does_not_invent_kwargs(self):
        self.assertFalse(models.supports_strict_policy(StrictCtx().register_auxiliary_task))
        self.assertTrue(models.supports_strict_policy(StrictAuxCtx().register_auxiliary_task))

    def test_old_host_rejects_strict_capability_and_allows_no_call(self):
        ctx = StrictCtx(settings={
            "primary_provider": "provider-a", "primary_model": "model-a",
            "fallback_provider": "provider-b", "fallback_model": "model-b",
        })
        result = models.register_configured_models(ctx)
        self.assertTrue(result["registered"])
        self.assertFalse(result["strict_active"])
        self.assertNotIn("fallback_policy", ctx.aux_calls[0])
        self.assertFalse(result["status"]["configured_model_calls_allowed"])
        with self.assertRaises(models.ModelRouteUnsupported):
            models.require_configured_model_lane(result["status"])

    def test_supported_host_uses_explicit_strict_keyword(self):
        ctx = StrictAuxCtx(settings={
            "primary_provider": "provider-a", "primary_model": "model-a",
            "fallback_provider": "provider-b", "fallback_model": "model-b",
        })
        result = models.register_configured_models(ctx)
        self.assertTrue(result["strict_active"])
        self.assertEqual(ctx.aux_calls[0]["fallback_policy"], "configured_only")
        self.assertEqual(result["status"]["fallback"]["provider"], "provider-b")
        self.assertTrue(result["status"]["configured_model_calls_allowed"])

    def test_plugin_defaults_and_explicit_operator_precedence(self):
        settings = {
            "primary_provider": "provider-a", "primary_model": "model-a",
            "fallback_provider": "provider-b", "fallback_model": "model-b",
        }
        status = models.effective_route_status(
            settings=settings, auxiliary_reader=lambda _key: {},
            strict_active=True)
        self.assertEqual(status["primary"]["source"], "plugin_defaults")
        self.assertEqual(status["primary"]["provider"], "provider-a")
        explicit = models.effective_route_status(
            settings=settings,
            auxiliary_reader=lambda _key: {"provider": "operator-x", "model": "operator-y",
                                           "fallback_chain": [{"provider": "operator-fb"}]},
            strict_active=True)
        self.assertEqual(explicit["primary"]["source"], "explicit_auxiliary")
        self.assertEqual(explicit["primary"]["provider"], "operator-x")
        self.assertEqual(explicit["fallback"]["provider"], "operator-fb")
        inherited = models.effective_route_status(
            settings={}, auxiliary_reader=lambda _key: {}, strict_active=True)
        self.assertEqual(inherited["primary"]["source"], "chat_model_inheritance")
        self.assertIsNone(inherited["fallback"])


class RegistrationTests(unittest.TestCase):
    def test_register_wires_surfaces_without_effects(self):
        ctx = StrictCtx()
        _PKG.register(ctx)
        self.assertIn("secretary", ctx.commands)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertEqual(ctx.tool_calls[0]["name"], "secretary_onboarding")
        self.assertEqual(ctx.aux_calls[0]["key"], "proactive_secretary")
        self.assertEqual(ctx.injected, [])
        self.assertEqual(ctx.state.data, {})
        self.assertEqual(ctx.saved_config, [])


if __name__ == "__main__":
    unittest.main()
