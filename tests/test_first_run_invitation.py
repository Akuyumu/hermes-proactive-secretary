"""Stage 2: the first-run invitation names one next step and an off switch."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_secretary_first_run_qa")
onboarding = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_onboarding")
conversation = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_conversation")


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    def __init__(self):
        self.state = MemoryState()

    def get_config(self, key, default=None):
        return default


class FirstRunInvitationTests(unittest.TestCase):
    def test_invitation_is_once_content_free_and_has_an_off_switch(self):
        ctx = Ctx()
        service = onboarding.OnboardingService(ctx)
        hook = conversation.make_pre_llm_hook(ctx, service)

        with mock.patch.object(conversation, "delivery_continuity_context",
                               return_value=None):
            first = hook(platform="telegram", user_message="hello")
            self.assertIsNotNone(first)
            text = first["context"]
            self.assertIn("what to watch", text.lower())
            self.assertIn("do not offer setup again", text.lower())
            for provider in ("mail", "calendar", "inbox", "tasks"):
                self.assertNotIn(provider, text.lower())

            second = hook(platform="telegram", user_message="hello again")
            self.assertIsNone(second)
        self.assertIn(conversation.INVITATION_KEY, ctx.state.data)

    def test_non_human_turns_never_get_the_invitation(self):
        ctx = Ctx()
        hook = conversation.make_pre_llm_hook(
            ctx, onboarding.OnboardingService(ctx))
        for platform in ("cron", "a2a", "internal", "scheduler"):
            self.assertIsNone(hook(platform=platform, user_message="scheduled"))
        self.assertNotIn(conversation.INVITATION_KEY, ctx.state.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
