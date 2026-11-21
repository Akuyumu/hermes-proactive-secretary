"""Offline acceptance for silent delivery-continuity injection.

The hook must add only a content-free recency line on eligible, deictic (or
first) human turns. It must never expose notice text, topics, handles or paths.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_package():
    name = "hermes_proactive_portal_continuity_hook_qa"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
paths_mod = importlib.import_module(f"{PKG.__name__}.paths")
onboarding = importlib.import_module(f"{PKG.__name__}.portal.portal_onboarding")
conversation = importlib.import_module(
    f"{PKG.__name__}.portal.portal_conversation")

from proactive_pilot import deliveries  # noqa: E402
from proactive_pilot import main as main_mod  # noqa: E402
from proactive_pilot import portal_notices as core  # noqa: E402
from proactive_pilot.clock import Clock, SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

PRIVATE = ("Private cheatsheet for alice@example.com about Project X",)
SENSITIVE = ("Private cheatsheet", "alice@example.com", "Project X",
             "cheatsheet", "telegram:owner", "evt-private")


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class StrictCtx:
    def __init__(self):
        self.state = MemoryState()


def _service(ctx):
    return onboarding.OnboardingService(ctx)


class DeliveryContinuityHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "var"
        self._patch = mock.patch.object(paths_mod, "state_dir",
                                        return_value=self.state)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.store = StateStore(self.state, Clock())

    def _ctx(self):
        ctx = StrictCtx()
        ctx.state.data[onboarding.STATE_KEY] = {"status": "ready"}
        return ctx

    def _hook(self, ctx):
        return conversation.make_pre_llm_hook(ctx, _service(ctx))

    def _record(self, *, now=None, message=None, event_key="evt-private",
                notice_id="n-private"):
        deliveries.record_emission(
            self.store,
            message=(message or PRIVATE[0]),
            kind="portal-notice",
            event_key=event_key,
            notice_id=notice_id,
            evidence_refs=("evidence-private",),
            job_id="job-private",
            job_name="hermes-proactive-portal-portal",
            destination="telegram:owner",
            now=now or datetime.now(timezone.utc),
        )

    def test_deictic_message_gets_content_free_line(self):
        self._record()
        hook = self._hook(self._ctx())
        result = hook(platform="telegram", sender_id="owner",
                      user_message="Can you send me the cheatsheet you prepped?",
                      is_first_turn=False, session_id="s1")
        self.assertIsNotNone(result)
        context = result["context"]
        self.assertIn("Recent proactive notices in this profile: 1 delivered", context)
        self.assertIn("hermes portal deliveries --since", context)
        self.assertIn("--limit 5", context)
        for sensitive in SENSITIVE:
            self.assertNotIn(sensitive, context)
        self.assertNotIn(str(self.root), context)

    def test_first_turn_gets_the_line_without_a_deictic_cue(self):
        self._record()
        hook = self._hook(self._ctx())
        result = hook(platform="telegram", user_message="hello there",
                      is_first_turn=True, session_id="s2")
        self.assertIsNotNone(result)
        self.assertIn("Recent proactive notices", result["context"])

    def test_non_deictic_later_message_gets_nothing(self):
        self._record()
        hook = self._hook(self._ctx())
        result = hook(platform="telegram", user_message="what is the weather?",
                      is_first_turn=False, session_id="s3")
        self.assertIsNone(result)

    def test_no_ledger_row_gets_nothing(self):
        hook = self._hook(self._ctx())
        result = hook(platform="telegram",
                      user_message="send me the thing you mentioned earlier",
                      is_first_turn=False, session_id="s4")
        self.assertIsNone(result)

    def test_stale_row_older_than_24h_gets_nothing(self):
        self._record(now=datetime.now(timezone.utc) - timedelta(hours=25))
        hook = self._hook(self._ctx())
        result = hook(platform="telegram",
                      user_message="send me the thing you mentioned earlier",
                      is_first_turn=True, session_id="s5")
        self.assertIsNone(result)

    def test_cron_group_and_shared_turns_never_inject(self):
        self._record()
        hook = self._hook(self._ctx())
        cases = [
            dict(platform="cron", user_message="add these to the summary",
                 is_first_turn=True),
            dict(platform="a2a", user_message="add these to the summary",
                 is_first_turn=True),
            dict(platform="telegram:group", user_message="add these",
                 is_first_turn=True),
            dict(platform="telegram", chat_type="group", user_message="add these",
                 is_first_turn=True),
            dict(platform="telegram", is_shared=True, user_message="add these",
                 is_first_turn=True),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(hook(session_id="shared", **case))

    def test_pending_onboarding_keeps_priority_over_continuity(self):
        self._record()
        ctx = self._ctx()
        conversation.set_pending(ctx, action="setup", session_id="s6")
        hook = self._hook(ctx)
        result = hook(platform="telegram", user_message="add these details",
                      is_first_turn=False, session_id="s6")
        self.assertIsNotNone(result)
        self.assertIn("onboarding continuation", result["context"].lower())
        self.assertNotIn("Recent proactive notices", result["context"])

    def test_delivered_stdout_is_byte_identical_with_and_without_ledger(self):
        def queued(store, event_key, message):
            store.save(core.PORTAL_NOTICES_ARTIFACT, {
                "schema": core.QUEUE_SCHEMA,
                "pending": [{
                    "id": "notice-" + event_key,
                    "event_key": event_key,
                    "topic": "Topic",
                    "message": message,
                    "evidence_refs": ["evidence-1"],
                    "expiry": "2099-01-01T00:00:00+00:00",
                    "received_at": "2026-11-20T10:00:00+00:00",
                    "origin": {"job_id": "job-1",
                               "job_name": core.PORTAL_JOB_NAME,
                               "profile": "A"},
                }],
                "seen": {}, "executions": {},
            })

        def emit(store):
            clock = SeededClock("2026-11-20T10:05:00+00:00", timezone_name="UTC")
            queued(store, "evt-1", "The exact delivered text.")
            with mock.patch.object(core, "_authorize_pending", return_value=None), \
                    mock.patch.object(core, "gates_decide",
                                      return_value={"send": True}), \
                    mock.patch.object(core, "gates_record_delivery",
                                      return_value=None):
                result = core.dispatch_notice(
                    store, clock, policy={"deliver_target": "telegram:owner"},
                    prefs={}, state=store.load_notify_state())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main_mod._emit(result["message"])
            return out.getvalue()

        plain = StateStore(self.root / "plain" / "var", Clock())
        populated = StateStore(self.root / "populated" / "var", Clock())
        deliveries.record_emission(
            populated, message="An older separate notice", kind="alert",
            event_key="evt-old", notice_id="n-old",
            now=datetime.now(timezone.utc))
        self.assertEqual(emit(plain), emit(populated))
        self.assertEqual(emit(plain), "The exact delivered text.\n")


if __name__ == "__main__":
    unittest.main()
