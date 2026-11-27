"""Native runtime bridge: ctx.llm routing, trust shape, model-free paths.

No real model call is ever made; the fake context records the exact kwargs the
route would pass, proving no provider/model/agent/profile override is sent.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import native_runtime as nr  # noqa: E402
from proactive_pilot.evaluator import NativeLlmEvaluator, fixture_packet  # noqa: E402


@dataclass
class _Structured:
    text: str
    parsed: object = None
    content_type: str = "json"


class FakeLlm:
    def __init__(self, result=None, *, raise_exc=None):
        self.calls = []
        self.result = result or _Structured(text="{}", parsed={"schema": 1, "action": "silence"})
        self.raise_exc = raise_exc

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class FakeCtx:
    def __init__(self, llm=None, settings=None):
        if llm is not None:
            self.llm = llm
        self._settings = settings or {}
        if llm is not None:
            nr.bind_model_lane(self, allowed=True, reason="test host lane")

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


class RoutingTests(unittest.TestCase):
    def test_no_context_is_unavailable(self):
        route = nr.build_llm_route(None)
        self.assertFalse(route.available)
        self.assertEqual(route.kind, "unsupported")

    def test_context_with_llm_is_available(self):
        route = nr.evaluator_route(FakeCtx(FakeLlm()))
        self.assertTrue(route.available)
        self.assertEqual(route.kind, "ctx.llm")

    def test_model_free_paths_never_route(self):
        ctx = FakeCtx(FakeLlm())
        for path in ("reminder", "disabled", "paused", "public-only"):
            route = nr.build_llm_route(ctx, path=path)
            self.assertFalse(route.available, path)
            self.assertEqual(route.kind, "unsupported", path)

    def test_reminder_route_is_always_model_free(self):
        route = nr.reminder_route()
        self.assertFalse(route.available)
        with self.assertRaises(nr.LlmUnavailable):
            route.complete_structured(instructions="x", input=[{"type": "text", "text": "y"}])
        self.assertEqual(route.calls, 0)

    def test_read_native_settings_uses_get_config(self):
        ctx = FakeCtx(settings={"k": "v"})
        self.assertEqual(nr.read_native_settings(ctx, "k"), "v")
        self.assertEqual(nr.read_native_settings(ctx, "missing", "d"), "d")
        self.assertEqual(nr.read_native_settings(None, "k", "d"), "d")

    def test_capabilities_report(self):
        caps = nr.native_capabilities(FakeCtx(FakeLlm()))
        self.assertTrue(caps["llm"])
        self.assertTrue(caps["plugin_settings"])
        self.assertFalse(nr.native_capabilities(None)["llm"])


class TrustShapeTests(unittest.TestCase):
    def test_structured_call_sends_no_overrides(self):
        llm = FakeLlm()
        route = nr.evaluator_route(FakeCtx(llm))
        route.complete_structured(instructions="i", input=[{"type": "text", "text": "t"}],
                                  max_tokens=64, timeout=5)
        self.assertEqual(route.calls, 1)
        call = llm.calls[0]
        self.assertEqual(call["task"], nr.AUX_TASK_KEY)
        for forbidden in ("provider", "model", "agent_id", "profile"):
            self.assertNotIn(forbidden, call)
        self.assertEqual(call["purpose"], "secretary.evaluate")
        self.assertEqual(call["max_tokens"], 64)

    def test_an_unknown_aux_slot_is_denied_by_the_host_not_silently_widened(self):
        class _NoTaskFacade:
            def __init__(self):
                self.calls = 0

            def complete_structured(self, *, instructions, input, json_schema=None,
                                    json_mode=False, schema_name=None, system_prompt=None,
                                    temperature=None, max_tokens=None, timeout=None,
                                    purpose=None):
                # No ``task`` parameter and no **kwargs: the keyword is refused by
                # the signature, so the body (and any provider call) never runs.
                self.calls += 1
                return None

        facade = _NoTaskFacade()
        route = nr.evaluator_route(FakeCtx(facade))
        with self.assertRaises(nr.LlmUnavailable):
            route.complete_structured(instructions="i", input=[{"type": "text", "text": "t"}])
        # The façade must never be reached: the keyword is refused before it runs.
        self.assertEqual(facade.calls, 0)

    def test_route_failure_is_sanitized(self):
        route = nr.evaluator_route(FakeCtx(FakeLlm(raise_exc=RuntimeError("token=SECRET"))))
        with self.assertRaises(nr.LlmUnavailable) as ctx:
            route.complete_structured(instructions="i", input=[{"type": "text", "text": "t"}])
        self.assertNotIn("SECRET", str(ctx.exception))


class NativeLlmEvaluatorTests(unittest.TestCase):
    def test_parsed_notify_is_validated(self):
        packet = fixture_packet("evt:test:1")
        payload = {
            "schema": 1, "action": "notify", "kind": "calendar", "key": "evt:test:1",
            "message": "Synthetic notice", "urgency": "normal",
        }
        llm = FakeLlm(_Structured(text="", parsed=payload))
        evaluator = NativeLlmEvaluator(FakeCtx(llm), {})
        result = evaluator.evaluate(packet, {"_preferences": {}})
        self.assertEqual(result.action, "notify")
        self.assertEqual(result.route, "ctx.llm")
        self.assertEqual(result.tools, 0)
        self.assertNotIn("provider", llm.calls[0])

    def test_unavailable_context_fails_closed(self):
        packet = fixture_packet("evt:test:1")
        evaluator = NativeLlmEvaluator(None, {})
        result = evaluator.evaluate(packet, {"_preferences": {}})
        self.assertEqual(result.action, "error")
        self.assertEqual(result.route, "ctx.llm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
