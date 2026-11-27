"""The acknowledgement tool is registered and fails closed without authority."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402


class _Ctx:
    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def register_tool(self, *args, **kwargs):
        self.registered.append((args, kwargs))


class RegistrationTests(unittest.TestCase):
    def test_registers_the_expected_name_and_schema(self):
        ctx = _Ctx()
        result = tool.register_tool(ctx)
        self.assertTrue(result["acknowledge_tool"])
        (args, kwargs) = ctx.registered[0]
        self.assertEqual(args[0], "secretary_acknowledge")
        self.assertEqual(kwargs["description"], tool.TOOL_DESCRIPTION)
        self.assertEqual(args[2]["parameters"]["required"], ["text", "evidence_quote"])
        self.assertFalse(args[2]["parameters"]["additionalProperties"])

    def test_missing_registrar_is_reported_not_raised(self):
        result = tool.register_tool(object())
        self.assertFalse(result["acknowledge_tool"])
        self.assertIn("register_tool unavailable", result["acknowledge_tool_error"])


class FailClosedTests(unittest.TestCase):
    def test_empty_text_is_refused(self):
        handler = tool.tool_handler(None)
        body = json.loads(handler({"text": "   "}))
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "refused")

    def test_no_host_authority_refuses_and_writes_nothing(self):
        handler = tool.tool_handler(None)
        body = json.loads(handler({"text": "done"}, session_id="s1"))
        self.assertFalse(body["ok"])
        self.assertIn(body["status"], {"refused", "unattested", "no_session"})


if __name__ == "__main__":
    unittest.main()
