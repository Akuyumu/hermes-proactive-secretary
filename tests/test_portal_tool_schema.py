"""Provider-facing schema acceptance for the portal notice tool.

These tests capture the real schema passed to ``ctx.register_tool``.  The native
registry only injects a name and wraps a function around the supplied schema, so
the registered value must be a complete function-level envelope with bounded
``parameters``.  The exported ``TOOL_SCHEMA`` remains the bare parameter object
for compatibility with existing callers and tests.

No registry, network, live cron job, profile store or file is touched.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_secretary_notices_tool_schema_qa")
notices = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_portal_notices"
)

ALLOWED = {"event_key", "topic", "message", "evidence_refs", "expiry"}
FORBIDDEN = (
    "job",
    "job_id",
    "job_name",
    "profile",
    "owner",
    "authority",
    "target",
    "destination",
    "cap",
    "caps",
    "urgency",
)


class _CaptureCtx:
    """Minimal host context that records the real registration call."""

    def __init__(self):
        self.profile_name = "A"
        self.registered = []

    def current_cron_execution(self):
        return object()

    def register_tool(self, name, category, schema, handler, **kwargs):
        self.registered.append({
            "name": name,
            "category": category,
            "schema": schema,
            "handler": handler,
            "kwargs": kwargs,
        })


class PortalToolSchemaTests(unittest.TestCase):
    def _captured(self):
        ctx = _CaptureCtx()
        result = notices.register_tool(ctx)
        self.assertTrue(result["portal_notice_tool"])
        self.assertEqual(len(ctx.registered), 1)
        return ctx.registered[0]

    def test_registered_schema_is_native_function_envelope(self):
        entry = self._captured()
        schema = entry["schema"]

        self.assertEqual(set(schema), {"name", "description", "parameters"})
        self.assertEqual(entry["category"], "secretary")
        self.assertEqual(schema["name"], "secretary_propose_notice")
        self.assertEqual(schema["name"], entry["name"])
        self.assertEqual(schema["description"], notices.TOOL_DESCRIPTION)
        self.assertEqual(schema["description"], entry["kwargs"]["description"])
        self.assertIs(schema["parameters"], notices.TOOL_SCHEMA)

        # It is not the bare parameter object that the registry would silently
        # fail to expose as ``function.parameters``.
        self.assertNotIn("type", schema)
        self.assertNotIn("properties", schema)

    def test_registered_parameters_are_exactly_the_allowed_fields(self):
        schema = self._captured()["schema"]
        parameters = schema["parameters"]
        properties = parameters["properties"]

        self.assertEqual(parameters["type"], "object")
        self.assertEqual(set(properties), ALLOWED | {"action"})
        self.assertEqual(parameters["required"], [])
        self.assertEqual(properties["action"]["enum"], ["submit", "budget"])
        self.assertIs(parameters["additionalProperties"], False)

        self.assertEqual(properties["event_key"]["type"], "string")
        self.assertEqual(properties["event_key"]["maxLength"], 160)
        self.assertEqual(properties["topic"]["type"], "string")
        self.assertEqual(properties["topic"]["maxLength"], 80)
        self.assertEqual(properties["message"]["type"], "string")
        self.assertEqual(properties["message"]["maxLength"], 350)

        evidence = properties["evidence_refs"]
        self.assertEqual(evidence["type"], "array")
        self.assertEqual(evidence["items"], {"type": "string", "maxLength": 240})
        self.assertEqual(evidence["minItems"], 1)
        self.assertEqual(evidence["maxItems"], 8)

        expiry = properties["expiry"]
        self.assertEqual(expiry["type"], "string")
        self.assertEqual(expiry["format"], "date-time")
        self.assertEqual(expiry["maxLength"], 64)

    def test_authority_fields_cannot_enter_schema_or_arguments(self):
        entry = self._captured()
        parameters = entry["schema"]["parameters"]

        for field in FORBIDDEN:
            with self.subTest(field=field):
                self.assertNotIn(field, parameters["properties"])
                self.assertNotIn(field, parameters["required"])

        proposal = {
            "event_key": "event-1",
            "topic": "Topic",
            "message": "A bounded notice",
            "evidence_refs": ["evidence-1"],
            "expiry": "2026-10-20T13:00:00+00:00",
        }
        for field in FORBIDDEN:
            with self.subTest(field=field):
                payload = dict(proposal)
                payload[field] = "attempted authority escalation"
                result = json.loads(entry["handler"](payload))
                self.assertFalse(result["ok"])
                self.assertFalse(result["accepted"])
                self.assertEqual(result["status"], "invalid_args")

    def test_exported_tool_schema_remains_bare_parameter_object(self):
        schema = notices.TOOL_SCHEMA

        self.assertEqual(
            set(schema),
            {"type", "properties", "required", "anyOf", "additionalProperties"},
        )
        self.assertEqual(schema["type"], "object")
        self.assertEqual(set(schema["properties"]), ALLOWED | {"action"})
        self.assertEqual(schema["required"], [])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(notices.PROPOSAL_KEYS), ALLOWED)

        for field in FORBIDDEN:
            self.assertNotIn(field, schema["properties"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
