"""Routing contract for the bundled proactive-portal skill.

Asserts from the real shipped ``skills/proactive-portal/SKILL.md`` and from the
plugin registration that a follow-up/watch request surfaces the portal rather
than a cron job, and that the consent-gate handling is taught without bypassing
it.
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
SKILL = ROOT / "skills" / "proactive-portal" / "SKILL.md"
sys.path.insert(0, str(ROOT / "src"))


def _load_package():
    name = "hermes_proactive_portal_skill_routing"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _frontmatter(text):
    assert text.startswith("---")
    end = text.find("\n---", 3)
    assert end >= 0
    block = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            block[key.strip()] = value.strip()
    return block


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


class _MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _RegistrationCtx:
    def __init__(self):
        self.settings = {}
        self.state = _MemoryState()
        self.plugin_id = "hermes-proactive-portal"
        self.skills = {}
        self.tool_calls = []
        self.commands = {}
        self.hooks = {}
        self.injected = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_auxiliary_task(self, *args, **kwargs):
        pass

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_tool(self, name, *args, **kwargs):
        self.tool_calls.append(name)

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_skill(self, name, path, description=""):
        self.skills[name] = (path, description)

    def register_cli_command(self, *args, **kwargs):
        pass

    def inject_message(self, *args, **kwargs):
        self.injected.append(args)
        return False

    def _gateway_injection_allowed(self):
        return False


class SkillRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.frontmatter = _frontmatter(cls.text)
        cls.description = _unquote(cls.frontmatter.get("description", ""))
        cls.body = cls.text.split("\n---", 1)[1].lower()

    def test_skill_file_is_shipped_in_the_expected_path(self):
        self.assertTrue(SKILL.is_file())
        self.assertEqual(SKILL.relative_to(ROOT).as_posix(),
                         "skills/proactive-portal/SKILL.md")
        self.assertEqual(self.frontmatter.get("name"), "proactive-portal")
        self.assertEqual(self.frontmatter.get("version"), "1.0.5")

    def test_description_carries_the_routing_triggers(self):
        lowered = self.description.lower()
        for trigger in ("follow up", "watch", "track", "notify", "keep an eye"):
            self.assertIn(trigger, lowered)

    def test_body_routes_followups_to_portal_and_never_cron(self):
        self.assertIn("portal_followup", self.body)
        self.assertIn('action="create"', self.body)
        self.assertIn("do not create a cron job for a follow-up", self.body)
        self.assertIn("cron is not the portal's api", self.body)

    def test_body_teaches_one_confirming_question_without_bypasses(self):
        self.assertIn("one plain confirming question", self.body)
        self.assertIn("want me to file that as a follow-up?", self.body)
        self.assertIn("magic phrase", self.body)
        self.assertIn("hand-edit the store", self.body)
        for bypass in ("hand-edit the store", "substitute a cron job"):
            self.assertIn(bypass, self.body)

    def test_registration_passes_the_file_description(self):
        package = _load_package()
        with tempfile.TemporaryDirectory(prefix="portal-skill-routing-") as scratch:
            env = {
                "HERMES_HOME": os.path.join(scratch, "home"),
                "HERMES_PROACTIVE_PORTAL_DATA": os.path.join(scratch, "data"),
            }
            try:
                with mock.patch.dict(os.environ, env):
                    ctx = _RegistrationCtx()
                    package.register(ctx)
            finally:
                try:
                    from hermes_proactive_portal_skill_routing.portal import portal_cli
                    portal_cli.set_native_context(None)
                except Exception:  # noqa: BLE001 - cleanup only
                    pass
        self.assertIn("proactive-portal", ctx.skills)
        path, registered = ctx.skills["proactive-portal"]
        self.assertEqual(Path(path).resolve(), SKILL.resolve())
        self.assertEqual(registered, self.description)
        self.assertTrue(registered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
