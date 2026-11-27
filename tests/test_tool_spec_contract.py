"""Every registered tool must expose a complete function-level schema.

The host builds a served tool definition as ``{**entry.schema, "name":
entry.name}`` and reads ``entry.schema.get("parameters")`` for validation.  A
bare JSON-Schema parameter object therefore registers a tool with no
``parameters`` key at all, leaving the model with a zero-argument tool.  This
test registers the real package and inspects the schema argument of every
``register_tool`` call (positional or keyword).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"


def _load_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _MemoryState:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _RegistrationCtx:
    def __init__(self):
        self.plugin_id = "hermes-proactive-secretary"
        self.settings = {}
        self.state = _MemoryState()
        self.tool_calls = []
        self.cli_names = []

    def get_config(self, key, default=None):
        return default

    def register_auxiliary_task(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tool_calls.append({
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "kwargs": kwargs,
        })

    def register_hook(self, *args, **kwargs):
        pass

    def current_cron_execution(self):
        return None

    def register_skill(self, *args, **kwargs):
        pass

    def register_cli_command(self, name, help="", setup_fn=None, handler_fn=None,
                             description=""):
        self.cli_names.append(name)

    def inject_message(self, *args, **kwargs):
        return False


def _declared_tools() -> list[str]:
    import yaml

    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return list(manifest.get("provides_tools") or [])


class ToolSpecContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = _load_package("hermes_proactive_secretary_tool_spec_contract")
        with tempfile.TemporaryDirectory(prefix="tool-spec-contract-") as scratch:
            env = {
                "HERMES_HOME": os.path.join(scratch, "home"),
                "HERMES_PROACTIVE_SECRETARY_DATA": os.path.join(scratch, "data"),
            }
            try:
                with mock.patch.dict(os.environ, env):
                    cls.ctx = _RegistrationCtx()
                    package.register(cls.ctx)
            finally:
                try:
                    from hermes_proactive_secretary_tool_spec_contract.secretary import (
                        secretary_cli,
                    )
                    secretary_cli.set_native_context(None)
                except Exception:  # noqa: BLE001 - cleanup only
                    pass

    def _schemas(self):
        for call in self.ctx.tool_calls:
            yield call["name"], call["schema"]

    def test_every_tool_registers_a_function_schema(self):
        seen = []
        for name, schema in self._schemas():
            with self.subTest(tool=name):
                self.assertIsInstance(schema, dict)
                self.assertIn("parameters", schema)
                self.assertIn("name", schema)
                self.assertIn("description", schema)
                self.assertEqual(schema["name"], name)
                self.assertTrue(str(schema["name"]).strip())
                self.assertTrue(str(schema["description"]).strip())
                parameters = schema["parameters"]
                self.assertIsInstance(parameters, dict)
                self.assertEqual(parameters.get("type"), "object")
            seen.append(name)
        self.assertTrue(seen)

    def test_registered_names_are_exactly_the_declared_seven(self):
        registered = sorted(
            call["name"] for call in self.ctx.tool_calls
            if isinstance(call["name"], str))
        declared = sorted(_declared_tools())
        self.assertEqual(len(declared), 8, declared)
        self.assertEqual(registered, declared)

    def test_no_registration_passes_a_bare_schema(self):
        for name, schema in self._schemas():
            with self.subTest(tool=name):
                self.assertIsInstance(schema, dict)
                self.assertIn(
                    "parameters", schema,
                    f"{name} passed a bare parameter object with no parameters key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
