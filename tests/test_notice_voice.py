"""Deterministic notice voice rules and the moved renderer behaviour."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.notice_voice import check_notice_text, render_ready_notice  # noqa: E402
from proactive_pilot import preparation  # noqa: E402


class NoticeVoiceRuleTests(unittest.TestCase):
    def test_too_long_is_refused(self):
        ok, rule = check_notice_text("A" * 481 + ".")
        self.assertFalse(ok)
        self.assertEqual(rule, "too_long")

    def test_hex_blob_is_refused(self):
        ok, rule = check_notice_text("Digest " + "a1" * 16 + " is attached.")
        self.assertFalse(ok)
        self.assertEqual(rule, "hex_blob")

    def test_hash_token_is_refused(self):
        ok, rule = check_notice_text(
            "The sha256:9f86d081884c7d659a2feaa0c55ad015 is attached.")
        self.assertFalse(ok)
        self.assertEqual(rule, "hash_token")

    def test_opaque_id_is_refused(self):
        for sample in ("See 0xdeadbeef for the record.",
                       "Receipt 123e4567-e89b-12d3-a456-426614174000 is ready."):
            with self.subTest(sample=sample):
                ok, rule = check_notice_text(sample)
                self.assertFalse(ok)
                self.assertEqual(rule, "opaque_id")

    def test_absolute_path_is_refused(self):
        ok, rule = check_notice_text("The file at /home/user/report.txt is ready.")
        self.assertFalse(ok)
        self.assertEqual(rule, "absolute_path")

    def test_slashed_date_is_not_an_absolute_path(self):
        ok, rule = check_notice_text("Print it before 27/11/2026.")
        self.assertTrue(ok, rule)

    def test_https_url_is_not_an_absolute_path(self):
        ok, rule = check_notice_text(
            "Join at https://meet.example.com/abc-defg-hij when the room opens.")
        self.assertTrue(ok, rule)

    def test_uppercase_hex_prefixed_id_is_refused(self):
        ok, rule = check_notice_text("Ref 0XDEADBEEF is attached to the case.")
        self.assertFalse(ok)
        self.assertEqual(rule, "opaque_id")

    def test_plugin_record_id_is_refused(self):
        for value in ("fu_9f2c1a", "resp_ab12cd34", "followup_7f3a2b1c"):
            with self.subTest(value=value):
                ok, rule = check_notice_text(f"Tracked under {value} for now.")
                self.assertFalse(ok)
                self.assertEqual(rule, "plugin_id")

    def test_bare_unix_root_is_an_absolute_path(self):
        for value in ("/etc", "/etc/passwd", "/home/owner/file.txt",
                      "/boot", "/private", "C:/config", "D:\\data\\x"):
            with self.subTest(value=value):
                ok, rule = check_notice_text(f"Saved at {value} earlier.")
                self.assertFalse(ok)
                self.assertEqual(rule, "absolute_path")

    def test_leading_timestamp_is_refused(self):
        ok, rule = check_notice_text("2026-11-27T09:00:00Z The draft is ready.")
        self.assertFalse(ok)
        self.assertEqual(rule, "leading_timestamp")

    def test_missing_sentence_end_is_refused(self):
        ok, rule = check_notice_text("The draft is ready for review")
        self.assertFalse(ok)
        self.assertEqual(rule, "no_sentence_end")

    def test_realistic_good_notice_passes_unchanged(self):
        notice = (
            'Your private draft for "Plan the trip" is ready to review. '
            'It was prepared against your acceptance note: "Covers the dates". '
            "Open it from your preparation queue when convenient."
        )
        self.assertEqual(check_notice_text(notice), (True, None))

    def test_rendered_ready_notice_is_a_good_notice(self):
        text = render_ready_notice({
            "desired_outcome": "Prepare the trip checklist",
            "acceptance_criterion": "Covers bookings and documents",
        })
        self.assertEqual(check_notice_text(text), (True, None))

    def test_preparation_keeps_the_public_imports(self):
        self.assertIs(preparation.render_ready_notice, render_ready_notice)
        self.assertTrue(callable(preparation._notice_fragment))


if __name__ == "__main__":
    unittest.main(verbosity=2)
