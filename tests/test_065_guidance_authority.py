"""End-to-end authority for the owner-guidance tool.

A mutation is now two-step: the first call proves the owner's turn and freezes
the exact payload server-side, then a later affirmative reply confirms that same
reference.  Each test records the turn through the real
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "3f0c2a9e-1b7d-4a63-9c2e-0e1f2a3b4c5d"


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
guidance = importlib.import_module(f"{_PKG.__name__}.src.proactive_pilot.guidance")
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

    def _call(self, payload, session_id=None, task_id=TASK_ID):
        return json.loads(self.handler(payload, session_id=session_id, task_id=task_id))

    def _propose_close(self, message, topics=("exams",), session_id="s-guidance",
                       **extra):
        self._remember(message, session_id=session_id)
        payload = {"action": "close", "topics": list(topics),
                   "evidence_quote": message}
        payload.update(extra)
        return self._call(payload, session_id=session_id)

    def _confirm(self, ref, reply="yes", session_id="s-guidance"):
        self._remember(reply, session_id=session_id)
        return self._call({"action": "confirm", "ref": ref,
                           "evidence_quote": reply}, session_id=session_id)

    def test_natural_close_needs_confirmation_then_writes_after_yes(self):
        message = "exams are over, close the exams topic"
        first = self._propose_close(message)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertTrue(first["ref"].startswith("gref-"), first)
        self.assertIn('"exams"', first["summary"])
        self.assertIn("reply yes", first["ask"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["status"], "closed")
        self.assertEqual(confirmed["confirmed_ref"], first["ref"])
        self.assertEqual(confirmed["entry"]["labels"], ["exams"])
        listed = self._call({"action": "list"})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["entries"][0]["labels"], ["exams"])

    def test_extra_topics_are_refused_by_label_binding(self):
        first = self._propose_close(
            "exams are over, close the exams topic", topics=("exams", "travel"))
        self.assertFalse(first["ok"], first)
        self.assertIn(first["status"], ("refused", "confirmation_required"))
        self.assertNotIn("ref", first)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_unrequested_expiry_is_ignored_and_default_is_written(self):
        message = "exams are over, close the exams topic"
        first = self._propose_close(message, until="2030-01-01")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertNotIn("2030", first["summary"])

        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["status"], "closed")
        until = datetime.fromisoformat(
            confirmed["entry"]["until"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        self.assertGreater(until, now + timedelta(days=6))
        self.assertLess(until, now + timedelta(days=8))
        self.assertNotEqual(until.year, 2030)

    def test_permanent_without_owner_words_does_not_create_permanent_pending(self):
        first = self._propose_close(
            "exams are over, close the exams topic", permanent=True)
        self.assertFalse(first["ok"], first)
        self.assertNotIn("ref", first)
        if "ref" in first:
            self.assertFalse(
                guidance.read_pending(self.data, first["ref"])["permanent"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_permanent_with_owner_words_is_proposed_and_confirmed(self):
        first = self._propose_close(
            "exams are over, close the exams topic permanently", permanent=True)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn("permanently", first["summary"])
        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertTrue(confirmed["entry"]["permanent"])
        self.assertEqual(confirmed["entry"]["until"], "")

    def test_confirm_from_another_session_is_refused(self):
        first = self._propose_close("exams are over, close the exams topic")
        other = self._confirm(first["ref"], session_id="s-other")
        self.assertFalse(other["ok"], other)
        self.assertEqual(other["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_confirm_after_consumption_is_refused(self):
        first = self._propose_close("exams are over, close the exams topic")
        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        again = self._confirm(first["ref"])
        self.assertFalse(again["ok"], again)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 1)

    def test_two_pending_replies_must_name_the_change(self):
        exams = self._propose_close(
            "exams are over, close the exams topic", topics=("exams",))
        travel = self._propose_close(
            "travel is over, close the travel topic", topics=("travel",))
        self.assertFalse(exams["ok"], exams)
        self.assertFalse(travel["ok"], travel)

        bare = self._confirm(exams["ref"], reply="yes")
        self.assertFalse(bare["ok"], bare)
        self.assertEqual(bare["status"], "refused")
        self.assertIn("which change", bare["error"].lower())
        self.assertIsNotNone(guidance.read_pending(self.data, exams["ref"]))
        self.assertIsNotNone(guidance.read_pending(self.data, travel["ref"]))

        named = self._confirm(exams["ref"], reply="yes, close exams")
        self.assertTrue(named["ok"], named)
        self.assertEqual(named["confirmed_ref"], exams["ref"])
        self.assertIsNone(guidance.read_pending(self.data, exams["ref"]))
        self.assertIsNotNone(guidance.read_pending(self.data, travel["ref"]))
        self.assertEqual(self._call({"action": "list"})["count"], 1)

    def test_one_turn_cannot_confirm_a_second_pending(self):
        exams = self._propose_close(
            "exams are over, close the exams topic", topics=("exams",))
        travel = self._propose_close(
            "travel is over, close the travel topic", topics=("travel",))
        reply = "yes, close exams"
        self._remember(reply)
        confirmed = self._call(
            {"action": "confirm", "ref": exams["ref"], "evidence_quote": reply},
            session_id="s-guidance")
        self.assertTrue(confirmed["ok"], confirmed)
        again = self._call(
            {"action": "confirm", "ref": travel["ref"], "evidence_quote": reply},
            session_id="s-guidance")
        self.assertFalse(again["ok"], again)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 1)
        self.assertIsNotNone(guidance.read_pending(self.data, travel["ref"]))

    def test_turn_at_the_proposal_instant_is_refused(self):
        first = self._propose_close("exams are over, close the exams topic")
        row = guidance.read_pending(self.data, first["ref"])
        self.assertIsNotNone(row)
        responsibilities.remember_human_turn(
            self.ctx, session_id="s-guidance", user_message="yes",
            platform="telegram", at=row["created_epoch"])
        result = self._call(
            {"action": "confirm", "ref": first["ref"], "evidence_quote": "yes"},
            session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 0)
        self.assertIsNotNone(guidance.read_pending(self.data, first["ref"]))

    def test_cancel_from_another_session_is_refused(self):
        first = self._propose_close("exams are over, close the exams topic")
        result = self._call(
            {"action": "cancel", "ref": first["ref"]}, session_id="s-other")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIsNotNone(guidance.read_pending(self.data, first["ref"]))
        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)

    def test_summary_uses_the_exact_date_not_a_day_count(self):
        first = self._propose_close(
            "Close the exams topic, it is over on 2026-12-24.")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn('close "exams" until 2026-12-24', first["summary"])
        self.assertNotIn("days", first["summary"])
        self.assertNotIn("for ", first["summary"])

    def test_reply_naming_both_pending_topics_is_refused(self):
        exams = self._propose_close("exams are over, close the exams topic")
        travel = self._propose_close("travel is booked, close the travel topic",
                                     topics=("travel",))
        self.assertIn("ref", exams)
        self.assertIn("ref", travel)
        reply = "yes, close exams and travel"
        self._remember(reply)
        result = self._call({"action": "confirm", "ref": exams["ref"],
                             "evidence_quote": reply}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertIn("more than one", result["error"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)
        self.assertIsNotNone(guidance.read_pending(self.data, exams["ref"]))
        self.assertIsNotNone(guidance.read_pending(self.data, travel["ref"]))

    def test_unrelated_affirmative_does_not_confirm(self):
        first = self._propose_close("exams are over, close the exams topic")
        reply = "yes i know, what is for lunch"
        self._remember(reply)
        result = self._call({"action": "confirm", "ref": first["ref"],
                             "evidence_quote": reply}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertIn("does not say which change", result["error"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_short_yes_still_confirms_one_pending(self):
        first = self._propose_close("exams are over, close the exams topic")
        result = self._confirm(first["ref"], reply="yes please")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._call({"action": "list"})["count"], 1)

    def test_until_with_a_date_is_a_duration_not_a_condition(self):
        first = self._propose_close(
            "exams are over, close the exams topic until 2026-12-24")
        self.assertTrue(first.get("ref"), first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn('close "exams" until 2026-12-24', first["summary"])


    def test_confirm_with_non_affirmative_reply_is_refused(self):
        first = self._propose_close("exams are over, close the exams topic")
        result = self._confirm(first["ref"], reply="maybe")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_negated_conditional_and_question_clips_are_refused_at_step_one(self):
        cases = [
            ("Please don't close the exams topic yet", "negat"),
            ("if the exams are over, close the exams topic", "conditional"),
            ("Should I close the exams topic?", "question"),
            ("Assuming exams end, close them", "conditional"),
        ]
        for message, reason in cases:
            with self.subTest(message=message):
                first = self._propose_close(message)
                self.assertFalse(first["ok"], first)
                self.assertEqual(first["status"], "refused")
                self.assertIn(reason, first["error"].lower())
                self.assertNotIn("ref", first)
                self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_clipped_evidence_cannot_hide_a_negated_instruction(self):
        turn = "Do not close the exams topic. Evidence: close the exams topic"
        self._remember(turn)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": "close the exams topic"}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("ref", result)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_quoted_turn_cannot_propose(self):
        message = '"exams are over, close it"'
        first = self._propose_close(message)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "refused")
        self.assertIn("quoted", first["error"].lower())
        self.assertNotIn("ref", first)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_distant_negation_is_refused(self):
        message = "Do not under any circumstances ever close the exams topic."
        first = self._propose_close(message)
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "refused")
        self.assertIn("negated", first["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_close_excludes_the_requested_target(self):
        first = self._propose_close("Close travel, not exams.")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "refused")
        self.assertNotIn("ref", first)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_close_label_is_not_a_raw_substring(self):
        first = self._propose_close("Close the cartoon topic.", topics=("art",))
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "refused")
        self.assertNotIn("ref", first)
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_reopen_is_proposed_with_the_entry_then_confirmed(self):
        closed = guidance_tool._apply_mutation(
            {"action": "close", "topics": ["exams"]})
        self.assertTrue(closed["ok"], closed)
        entry_id = closed["entry"]["id"]

        message = "reopen the exams topic"
        self._remember(message)
        first = self._call({
            "action": "reopen", "id": entry_id,
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn('"exams"', first["summary"])
        self.assertEqual(self._call({"action": "list"})["count"], 1)

        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["status"], "reopened")
        self.assertEqual(confirmed["confirmed_ref"], first["ref"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_reopen_target_must_match_the_stored_entry(self):
        closed = guidance_tool._apply_mutation(
            {"action": "close", "topics": ["exams"]})
        self.assertTrue(closed["ok"], closed)
        message = "reopen the travel topic"
        self._remember(message)
        result = self._call({
            "action": "reopen", "id": closed["entry"]["id"],
            "evidence_quote": message}, session_id="s-guidance")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("ref", result)
        self.assertEqual(self._call({"action": "list"})["count"], 1)

    def test_public_dispatch_cannot_mutate_or_confirm(self):
        first = self._propose_close("exams are over, close the exams topic")
        refused_close = guidance_tool.dispatch(
            {"action": "close", "topics": ["exams"]})
        self.assertFalse(refused_close["ok"], refused_close)
        self.assertEqual(refused_close["status"], "refused")
        refused_confirm = guidance_tool.dispatch(
            {"action": "confirm", "ref": first["ref"], "evidence_quote": "yes"})
        self.assertFalse(refused_confirm["ok"], refused_confirm)
        self.assertEqual(refused_confirm["status"], "refused")
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_missing_session_id_is_refused(self):
        message = "exams are over, close it"
        self._remember(message)
        result = self._call({
            "action": "close", "topics": ["exams"],
            "evidence_quote": message}, session_id=None)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("session_id", result["error"])
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_no_recorded_turn_cannot_propose(self):
        message = "exams are over, close it"
        result = self._call({
            "action": "close", "topics": ["exams"], "evidence_quote": message},
            session_id="s-no-turn")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "refused")
        self.assertIn("no current human conversation turn", result["error"].lower())
        self.assertEqual(self._call({"action": "list"})["count"], 0)

    def test_ordinary_foreground_task_id_does_not_block(self):
        first = self._propose_close("exams are over, close the exams topic")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn("ref", first)

    def test_natural_stop_instruction_binds_target(self):
        first = self._propose_close(
            "exams are over, stop notifying me about exams")
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["status"], "confirmation_required")
        self.assertIn('"exams"', first["summary"])
        confirmed = self._confirm(first["ref"])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["entry"]["labels"], ["exams"])

    def test_list_needs_no_authority(self):
        result = self._call({"action": "list"}, session_id=None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
