"""End-to-end authority for the owner-guidance tool.

The tool must accept exactly the same current private human turn evidence that
the follow-up lane accepts, and refuse quoted, reported, negated or conditional
turns.  Each test records the turn through the real
``responsibility_context`` hook path, then drives the real tool handler.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def _load_package(name: str = "hermes_proactive_secretary_guidance_authority_qa"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
paths = importlib.import_module(f"{_PKG.__name__}.paths")
responsibilities = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_responsibilities")
guidance_tool = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_guidance_tool")


class _MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _Ctx:
    def __init__(self):
        self.state = _MemoryState()


class GuidanceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

        self._saved = {key: os.environ.get(key) for key in (
            "HERMES_HOME", paths.DATA_ENV)}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ[paths.DATA_ENV] = str(self.data)
        self.addCleanup(self._restore)

        self.ctx = _Ctx()
        self.handler = guidance_tool.tool_handler(self.ctx)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _remember(self, text, session_id="s-guidance"):
        responsibilities.responsibility_context(
            self.ctx, platform="telegram", user_message=text,
            session_id=session_id)

    def _call(self, payload, session_id=None, task_id=None):
        return json.loads(self.handler(payload, session_id=session_id, task_id=task_id))

    def test_owner_close_records_an_entry(self):
        message = "exams are over, close it"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance",
            task_id="3f0c2a9e-1b7d-4a63-9c2e-0e1f2a3b4c5d")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["entry"]["labels"], ["exams"])
        listed = self._call({"action": "list"})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(listed["count"], 1)

    def test_negated_turn_cannot_close(self):
        message = "Please don't close the exams topic yet"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("negated", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_quoted_turn_cannot_close(self):
        message = '"exams are over, close it"'
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("quoted", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_missing_session_id_is_refused(self):
        message = "exams are over, close it"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("session_id", result["error"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_host_kwarg_alone_authorises(self):
        message = "exams are over, close it"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")

    def test_delegated_task_cannot_close(self):
        message = "exams are over, close it"
        # A task id is not proof on its own: with no recorded human turn the
        # tool still refuses. Task ids are no longer used as a foreground gate.
        result = json.loads(self.handler(
            {"action": "close", "topics": ["exams"], "evidence_quote": message},
            session_id="s-no-turn", task_id="task-1"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("no current human conversation turn", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_ordinary_foreground_task_id_does_not_block(self):
        message = "exams are over, close it"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance",
            task_id="3f0c2a9e-1b7d-4a63-9c2e-0e1f2a3b4c5d")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")

    def test_natural_stop_instruction_closes(self):
        message = "exams are over, stop notifying me about exams"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")

    def test_negated_instruction_is_refused(self):
        message = "don't close the exams topic yet"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("negated", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_conditional_instruction_is_refused(self):
        message = "if the exams are over, close the topic"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("conditional", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_unrelated_question_requires_confirmation(self):
        message = "What time is lunch?"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["travel"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertIn("ask", result)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_distant_negation_is_refused(self):
        message = "Do not under any circumstances ever close the exams topic."
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("negated", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_once_conditional_is_refused(self):
        message = "Once the results arrive, close the exams topic."
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("conditional", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_permanent_close_without_owner_words_requires_confirmation(self):
        message = "exams are over, close the exams topic"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"], "permanent": True,
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_permanent_close_with_owner_words_closes(self):
        message = "exams are over, close the exams topic permanently"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"], "permanent": True,
            "evidence_quote": message}, session_id="s-guidance")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result["entry"]["permanent"])

    def test_natural_stop_instruction_binds_target(self):
        message = "exams are over, stop notifying me about exams"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["entry"]["labels"], ["exams"])

    def test_short_negated_instruction_is_refused(self):
        message = "don't close the exams topic yet"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("negated", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_reopen_with_owner_words_reopens_the_entry(self):
        close_message = "exams are over, close the exams topic"
        self._remember(close_message)
        closed = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": close_message}, session_id="s-guidance")
        self.assertTrue(closed["ok"], closed)
        reopen_message = "reopen the exams topic"
        self._remember(reopen_message)
        reopened = self._call({
            "action": "reopen", "id": closed["entry"]["id"],
            "evidence_quote": reopen_message}, session_id="s-guidance")
        self.assertTrue(reopened["ok"], reopened)
        self.assertEqual(reopened["status"], "reopened")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_public_dispatch_cannot_mutate(self):
        message = "exams are over, close it"
        self._remember(message)
        result = guidance_tool.dispatch({"action": "close", "topics": ["exams"]})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_list_needs_no_authority(self):
        result = self._call({"action": "list"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
