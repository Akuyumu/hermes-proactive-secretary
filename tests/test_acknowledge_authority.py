"""The acknowledgement gate cannot be widened by the model or by a cron turn."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from secretary import secretary_acknowledge_tool as tool  # noqa: E402


class _Ctx:
    def __init__(self, execution=None) -> None:
        self._execution = execution

    def current_cron_execution(self):
        return self._execution


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patches = [
            mock.patch.object(tool.paths, "state_dir", return_value=root / "var"),
            mock.patch.object(tool.paths, "preferences_path",
                              return_value=root / "preferences.json"),
            mock.patch.object(tool.paths, "data_root", return_value=root),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def drive(self, *, turn_text: str, declared: str, execution=None, **extra):
        """Run the real handler with an attested turn and a stubbed store write."""
        applied: dict = {}

        def fake_attested(ctx, payload, *, host_session_id=None):
            return True, "", "", {"text": turn_text, "reply_to": "fu_pinned"}, \
                str(payload.get("evidence_quote") or "")

        def fake_apply(payload=None, *, now=None):
            applied["body"] = dict(payload or {})
            return {"ok": True, "status": "applied"}

        def fake_pinned(ctx, payload, turn, *, conversation, owner, store, now):
            return {"target_id": "followup:fu_pinned",
                    "target_type": "followup",
                    "target_label": "harbour tide",
                    "followup_id": "fu_pinned",
                    "pinned_by": "reply",
                    "aliases": {"fu_pinned", "followup:fu_pinned"}}, None

        original_attested, original_apply = tool._attested_owner_turn, tool._apply_unattested
        original_pinned = tool._pinned_target
        tool._attested_owner_turn = fake_attested
        tool._apply_unattested = fake_apply
        tool._pinned_target = fake_pinned
        try:
            handler = tool.tool_handler(_Ctx(execution))
            body = {"text": declared, "evidence_quote": turn_text,
                    "scope": "harbour tide"}
            body.update(extra)
            raw = handler(body, session_id="s1")
        finally:
            tool._attested_owner_turn = original_attested
            tool._apply_unattested = original_apply
            tool._pinned_target = original_pinned
        return json.loads(raw), applied

    def test_model_cannot_change_the_duration(self):
        result, applied = self.drive(turn_text="later 30", declared="later 2h")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"]["code"], "not_instruction")
        self.assertEqual(applied, {})

    def test_model_cannot_turn_a_refusal_into_an_acknowledgement(self):
        result, applied = self.drive(turn_text="not done", declared="done")
        self.assertFalse(result["ok"])
        self.assertEqual(applied, {})

    def test_the_owner_words_are_what_get_applied(self):
        result, applied = self.drive(turn_text="later 30", declared="later 30")
        self.assertTrue(result["ok"], result)
        self.assertEqual(applied["body"]["text"], "later 30")

    def test_a_cron_execution_cannot_reuse_a_cached_human_turn(self):
        result, applied = self.drive(turn_text="done", declared="done",
                                     execution=object())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"]["code"], "scheduled")
        self.assertEqual(applied, {})

    def test_later_uses_the_pinned_target_even_without_model_scope(self):
        result, applied = self.drive(turn_text="later 30", declared="later 30",
                                     scope="")
        self.assertTrue(result["ok"], result)
        self.assertEqual(applied["body"]["target_id"], "followup:fu_pinned")
        self.assertEqual(applied["body"]["scope"], "harbour tide")

    def test_kind_is_forwarded_to_the_store(self):
        result, applied = self.drive(turn_text="later 30", declared="later 30", kind="mail")
        self.assertTrue(result["ok"], result)
        self.assertEqual(applied["body"]["kind"], "mail")


if __name__ == "__main__":
    unittest.main()
