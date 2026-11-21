"""Regression: the plugin CLI group must be ``secretary``, never ``portal``.

The host already owns a `portal` command group for Nous Portal, so a plugin
group with that name is silently shadowed. These checks read the real shipped
registration and text so the collision cannot return unnoticed.
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
SKILL = ROOT / "skills" / "proactive-secretary" / "SKILL.md"
CLI = ROOT / "secretary" / "secretary_cli.py"
FORBIDDEN = " ".join(("hermes", "portal"))


def _load_package():
    name = "hermes_proactive_secretary_cli_name"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _MemoryState:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


class _RegistrationCtx:
    def __init__(self):
        self.cli_names = []
        self.plugin_id = "hermes-proactive-secretary"
        self.settings = {}
        self.state = _MemoryState()

    def get_config(self, key, default=None):
        return default

    def register_auxiliary_task(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass

    def register_cli_command(self, name, help="", setup_fn=None, handler_fn=None,
                             description=""):
        self.cli_names.append(name)

    def inject_message(self, *args, **kwargs):
        return False


class CliGroupNameTests(unittest.TestCase):
    def test_registered_cli_command_group_is_secretary(self):
        package = _load_package()
        with tempfile.TemporaryDirectory(prefix="portal-cli-name-") as scratch:
            env = {
                "HERMES_HOME": os.path.join(scratch, "home"),
                "HERMES_PROACTIVE_SECRETARY_DATA": os.path.join(scratch, "data"),
            }
            try:
                with mock.patch.dict(os.environ, env):
                    ctx = _RegistrationCtx()
                    package.register(ctx)
            finally:
                try:
                    from hermes_proactive_secretary_cli_name.secretary import secretary_cli
                    secretary_cli.set_native_context(None)
                except Exception:  # noqa: BLE001 - cleanup only
                    pass
        self.assertEqual(ctx.cli_names, ["secretary"])

    def test_manifest_declares_secretary_cli_group(self):
        lines = (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
        start = lines.index("provides_cli_commands:")
        declared = []
        for line in lines[start + 1:]:
            if not line.startswith("  - "):
                break
            declared.append(line[4:].strip())
        self.assertEqual(declared, ["secretary"])

    def test_cli_and_skill_never_claim_the_shadowed_group(self):
        for path in (CLI, SKILL):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(FORBIDDEN, text, path)

    def test_no_shipped_python_or_markdown_claims_the_shadowed_group(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            parts = set(path.parts)
            if ".git" in parts or "__pycache__" in parts or "var" in parts:
                continue
            if path == Path(__file__).resolve():
                continue
            if FORBIDDEN in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
