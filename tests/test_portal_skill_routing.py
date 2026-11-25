"""Routing contract for the bundled proactive-secretary skill.

Asserts from the real shipped ``skills/proactive-secretary/SKILL.md`` and from the
plugin registration that a follow-up/watch request surfaces the secretary rather
than a cron job, and that the consent-gate handling is taught without bypassing
it.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "proactive-secretary" / "SKILL.md"
sys.path.insert(0, str(ROOT / "src"))


def _load_package():
    name = "hermes_proactive_secretary_skill_routing"
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
        self.plugin_id = "hermes-proactive-secretary"
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
                         "skills/proactive-secretary/SKILL.md")
        self.assertEqual(self.frontmatter.get("name"), "proactive-secretary")
        self.assertEqual(self.frontmatter.get("version"), "1.1.9")

    def test_description_carries_the_routing_triggers(self):
        lowered = self.description.lower()
        for trigger in ("follow up", "watch", "track", "notify", "keep an eye"):
            self.assertIn(trigger, lowered)

    def test_body_routes_followups_to_secretary_and_never_cron(self):
        self.assertIn("secretary_followup", self.body)
        self.assertIn('action="create"', self.body)
        self.assertIn("do not create a cron job for a follow-up", self.body)
        self.assertIn("cron is not the secretary's api", self.body)

    def test_body_teaches_one_confirming_question_without_bypasses(self):
        self.assertIn("one plain confirming question", self.body)
        self.assertIn("want me to file that as a follow-up?", self.body)
        self.assertIn("magic phrase", self.body)
        self.assertIn("hand-edit the store", self.body)
        for bypass in ("hand-edit the store", "substitute a cron job"):
            self.assertIn(bypass, self.body)

    def test_portal_prompt_adds_three_tiers_without_dropping_constraints(self):
        package = _load_package()
        portal = importlib.import_module(
            f"{package.__name__}.secretary.secretary_portal")
        prompt = portal.PROMPT
        for sentence in (
            "You may act on your own initiative inside these three tiers.",
            "at most three new watches per run, each with a reason and an expiry",
            "let expired ones close themselves",
            "as a draft with `secretary_draft` `stage`",
            "drafts sit in the pending directory until the owner approves them",
            "staging never executes anything",
            "Draft only when a message genuinely needs the owner's own words",
            "never for acknowledgements, receipts, newsletters, automated notices",
            "a staged draft announces itself to the owner",
            "a worthless draft costs attention",
            ("Check your budget with `secretary_propose_notice` `budget` "
             "before you choose."),
            ("It ranks what you send; it never lowers the bar — if nothing "
             "clears it, stay silent however much budget is left, and never "
             "send filler to use it up."),
            "Propose everything else and leave it unexecuted",
            "never copy credentials, account numbers or full brokerage positions",
        ):
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, prompt)
        for old in (
            "not only fixed collector candidates or accepted tasks",
            "never create a parallel memory, preference, ",
            "Leave every approval-required action unexecuted",
            "Prefer meaningful silence when no changed evidence warrants",
            "never spam or fill quotas",
            "This prompt is not enforcement or a sandbox",
        ):
            with self.subTest(old=old):
                self.assertIn(old, prompt)

    def test_registration_passes_the_file_description(self):
        package = _load_package()
        with tempfile.TemporaryDirectory(prefix="portal-skill-routing-") as scratch:
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
                    from hermes_proactive_secretary_skill_routing.secretary import secretary_cli
                    secretary_cli.set_native_context(None)
                except Exception:  # noqa: BLE001 - cleanup only
                    pass
        self.assertIn("proactive-secretary", ctx.skills)
        path, registered = ctx.skills["proactive-secretary"]
        self.assertEqual(Path(path).resolve(), SKILL.resolve())
        self.assertEqual(registered, self.description)
        self.assertTrue(registered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
