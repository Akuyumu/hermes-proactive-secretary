"""Parity: the manifest must declare exactly the tools the plugin registers.

The host's plugin doctor warns when registration adds a tool that
``provides_tools`` does not list
(``registration adds tool 'X' not listed in provides_tools``).  That warning is
a release blocker, not cosmetic: the declaration is what the host and the
plugin's own capability summaries read.  A release that adds a tool without
updating the manifest therefore ships green tests and a WARN in the doctor.

This test registers the real package against a recording host context and
compares the names it registered with the manifest's own lists, in both
directions, so a tool can neither be registered-but-undeclared nor
declared-but-never-registered.
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


class _RecordingCtx:
    """Minimal host context: records every registration it receives."""

    def __init__(self):
        self.plugin_id = "hermes-proactive-secretary"
        self.tools: list[str] = []
        self.hooks: list[str] = []
        self.commands: list[str] = []
        self.cli_commands: list[str] = []
        self.skills: list[str] = []
        self.settings = {}
        self.state = _MemoryState()

    # Host surfaces the plugin may rely on.
    def get_config(self, key, default=None):
        return default

    def set_config(self, *args, **kwargs):
        return True

    def register_tool(self, name=None, *args, **kwargs):
        self.tools.append(name or kwargs.get("name"))

    def register_hook(self, name=None, *args, **kwargs):
        self.hooks.append(name or kwargs.get("name"))

    def register_command(self, name=None, *args, **kwargs):
        self.commands.append(name or kwargs.get("name"))

    def register_cli_command(self, name=None, *args, **kwargs):
        self.cli_commands.append(name or kwargs.get("name"))

    def register_skill(self, name=None, *args, **kwargs):
        self.skills.append(name or kwargs.get("name"))

    def register_auxiliary_task(self, *args, **kwargs):
        pass

    def current_cron_execution(self):
        return None

    def inject_message(self, *args, **kwargs):
        return False


def _declared(section: str) -> list[str]:
    import yaml

    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return list(manifest.get(section) or [])


class ManifestParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = _load_package("hermes_proactive_secretary_manifest_parity")
        with tempfile.TemporaryDirectory(prefix="manifest-parity-") as scratch:
            env = {
                "HERMES_HOME": os.path.join(scratch, "home"),
                "HERMES_PROACTIVE_SECRETARY_DATA": os.path.join(scratch, "data"),
            }
            try:
                with mock.patch.dict(os.environ, env):
                    cls.ctx = _RecordingCtx()
                    package.register(cls.ctx)
            finally:
                try:
                    from hermes_proactive_secretary_manifest_parity.secretary import (
                        secretary_cli,
                    )
                    secretary_cli.set_native_context(None)
                except Exception:  # noqa: BLE001 - cleanup only
                    pass

    def test_registered_tools_match_provides_tools_exactly(self):
        declared = _declared("provides_tools")
        registered = [name for name in self.ctx.tools if name]
        self.assertEqual(
            sorted(registered), sorted(declared),
            "plugin.yaml provides_tools must list exactly the tools register() "
            "adds; the host doctor fails the release on a mismatch",
        )

    def test_provides_tools_has_no_duplicates(self):
        declared = _declared("provides_tools")
        self.assertEqual(len(declared), len(set(declared)), declared)

    def test_registered_hooks_match_provides_hooks_exactly(self):
        declared = _declared("provides_hooks")
        registered = [name for name in self.ctx.hooks if name]
        self.assertEqual(sorted(registered), sorted(declared))

    def test_guidance_tool_is_declared(self):
        # Named explicitly: the tool was added in 1.1.8 while the manifest
        # still listed six tools, and every test stayed green.
        self.assertIn("secretary_guidance", _declared("provides_tools"))
        self.assertIn("secretary_guidance", self.ctx.tools)


if __name__ == "__main__":
    unittest.main(verbosity=2)
