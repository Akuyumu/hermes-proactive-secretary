"""Deterministic owner-guidance closures and their notice-boundary enforcement.

No model, no live profile: a temporary data root drives the store, and the
notice queue is exercised only through its public submit path with fake
identity/clock objects.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def _load_package(name: str = "hermes_proactive_secretary_guidance_qa"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
guidance = importlib.import_module(f"{_PKG.__name__}.src.proactive_pilot.guidance")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


class _Rooted(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "data"
        self.root.mkdir(parents=True)


class StoreTests(_Rooted):
    def test_close_defaults_to_finite_and_expires(self):
        result = guidance.close(self.root, ["Exams"], source="test")
        self.assertTrue(result["ok"], result)
        entry = result["entry"]
        self.assertFalse(entry["permanent"])
        self.assertTrue(entry["until"])
        later = datetime.now(timezone.utc) + timedelta(days=8)
        self.assertEqual(guidance.list_active(self.root, later)["entries"], [])

    def test_permanent_requires_explicit_flag(self):
        result = guidance.close(self.root, ["Exams"], permanent=True)
        self.assertTrue(result["entry"]["permanent"])
        later = datetime.now(timezone.utc) + timedelta(days=400)
        self.assertEqual(len(guidance.list_active(self.root, later)["entries"]), 1)

    def test_matching_is_exact_not_fuzzy(self):
        guidance.close(self.root, ["exams"])
        self.assertTrue(guidance.suppressed(self.root, labels=["  EXAMS "]))
        # Exact matching only: a singular, an extension or a neighbour is not covered.
        self.assertEqual(guidance.suppressed(self.root, labels=["Exam"]), "")
        self.assertEqual(guidance.suppressed(self.root, labels=["exam revision"]), "")
        self.assertEqual(guidance.suppressed(self.root, labels=["notes"]), "")

    def test_key_match_is_exact(self):
        guidance.close(self.root, [], keys=["cal:event:123"])
        self.assertTrue(guidance.suppressed(self.root, keys=["cal:event:123"]))
        self.assertEqual(guidance.suppressed(self.root, keys=["cal:event:1234"]), "")

    def test_reopen_restores_eligibility(self):
        entry = guidance.close(self.root, ["exams"])["entry"]
        self.assertTrue(guidance.suppressed(self.root, labels=["exams"]))
        self.assertTrue(guidance.reopen(self.root, entry["id"])["ok"])
        self.assertEqual(guidance.suppressed(self.root, labels=["exams"]), "")

    def test_corrupt_store_is_reported_not_reset(self):
        path = guidance.store_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertFalse(guidance.read_store(self.root)["ok"])
        self.assertFalse(guidance.close(self.root, ["exams"])["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_store_file_is_owner_only(self):
        guidance.close(self.root, ["exams"])
        self.assertEqual(guidance.store_path(self.root).stat().st_mode & 0o777, 0o600)


class NoticeBoundaryTests(_Rooted):
    """A closed topic is refused at submit, not merely discouraged in prose."""
    def _save_env(self):
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", paths.DATA_ENV)}
        self.addCleanup(self._restore)

    def setUp(self):
        super().setUp()
        self._save_env()
        os.environ["HERMES_HOME"] = str(self.root / "home")
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        os.environ[paths.DATA_ENV] = str(self.root)
        self.portal_notices = importlib.import_module(
            f"{_PKG.__name__}.src.proactive_pilot.portal_notices")
        state_mod = importlib.import_module(f"{_PKG.__name__}.src.proactive_pilot.state")

        class Clock:
            def now(self):
                return datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

        class Store:
            def __init__(self, root):
                self.root = root
                self._inner = state_mod.StateStore(root, Clock())
                self.state = self._inner

            def lock(self):
                return self._inner.lock()

            def load(self, *args, **kwargs):
                return self._inner.load(*args, **kwargs)

            def set(self, *args, **kwargs):
                return self._inner.set(*args, **kwargs)

        self.store = Store(self.root)
        self.clock = Clock()
        self.identity = {"execution_id": "exec-1", "job_id": "job-1",
                         "job_name": "hermes-proactive-secretary-portal",
                         "profile": "default"}

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _proposal(self, topic="exams", event="evt-1"):
        return {"event_key": event, "topic": topic,
                "message": "Maps are done; one past paper remains.",
                "evidence_refs": ["ref-1"], "expiry": "2026-11-26T12:00:00Z"}

    def test_closed_topic_is_refused_at_submit(self):
        guidance.close(self.root, ["exams"])
        receipt = self.portal_notices.submit_notice(
            self.store, self.clock, identity=self.identity, proposal=self._proposal())
        self.assertFalse(receipt.get("accepted"), receipt)
        self.assertEqual(receipt.get("status"), "suppressed", receipt)

    def test_changed_event_key_still_blocked_by_topic(self):
        guidance.close(self.root, ["exams"])
        receipt = self.portal_notices.submit_notice(
            self.store, self.clock, identity=self.identity,
            proposal=self._proposal(event="evt-brand-new"))
        self.assertEqual(receipt.get("status"), "suppressed", receipt)

    def test_unrelated_topic_is_not_blocked(self):
        guidance.close(self.root, ["exams"])
        receipt = self.portal_notices.submit_notice(
            self.store, self.clock, identity=self.identity,
            proposal=self._proposal(topic="library holds"))
        self.assertNotEqual(receipt.get("status"), "suppressed", receipt)

    def test_unknown_key_does_not_block(self):
        guidance.close(self.root, [], keys=["cal:event:123"])
        receipt = self.portal_notices.submit_notice(
            self.store, self.clock, identity=self.identity,
            proposal=self._proposal(topic="something else", event="evt-2"))
        self.assertNotEqual(receipt.get("status"), "suppressed", receipt)


class ToolTests(_Rooted):
    """The conversational capture path: model interprets, tool writes, store gates."""

    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", paths.DATA_ENV)}

        def restore():
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ["HERMES_HOME"] = str(self.root / "home")
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        os.environ[paths.DATA_ENV] = str(self.root)
        self.tool = importlib.import_module(
            f"{_PKG.__name__}.secretary.secretary_guidance_tool")

    def test_conversational_close_then_list_then_reopen(self):
        closed = self.tool.dispatch({
            "action": "close", "topics": ["Exams"],
            "reason": "yikes i dont need that cheatsheet actl"})
        self.assertTrue(closed["ok"], closed)
        self.assertIn("note", closed)
        listed = self.tool.dispatch({"action": "list"})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["entries"][0]["labels"], ["Exams"])
        self.assertIn("cheatsheet", listed["entries"][0]["source"])
        reopened = self.tool.dispatch({"action": "reopen",
                                       "id": listed["entries"][0]["id"]})
        self.assertTrue(reopened["ok"], reopened)
        self.assertEqual(self.tool.dispatch({"action": "list"})["count"], 0)

    def test_close_requires_a_topic_or_key(self):
        self.assertFalse(self.tool.dispatch({"action": "close"})["ok"])

    def test_handler_never_raises(self):
        handler = self.tool.tool_handler(None)
        payload = json.loads(handler({"action": "list"}))
        self.assertTrue(payload["ok"], payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
