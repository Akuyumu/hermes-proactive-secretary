"""Private preparation: gates, claims, artifacts, fencing, runner wake gate."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import preparation as P  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.userconfig import default_config  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")
RUNNER = ROOT / "runners" / "proactive_portal_preparation.py"


def prov():
    return {"kind": "user_commitment", "source_ref": "conversation:turn-3",
            "conversation_ref": "chat:main", "at": T0.isoformat()}


def facts():
    return [
        {"ref": "mail:msg-1", "text": "Requirement A is due 2026-10-25.",
         "provenance": "authorized mail", "observed_at": T0.isoformat()},
        {"ref": "https://example.test/page", "text": "See https://example.test/page for the form.",
         "provenance": "supplied source", "observed_at": T0.isoformat()},
    ]


def policy(**over):
    base = {"preparation_enabled": True,
            "preparation_scopes": ["conversation", "mail", "public", "tasks"],
            "preparation_source_consent": True}
    base.update(over)
    return base


def prefs(paused=False):
    return {"schema": 1, "version": 0, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": paused}}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        self.artifacts = self.root / "artifacts"
        self.addCleanup(self._tmp.cleanup)

    def accepted(self, **over):
        data = dict(desired_outcome="Prepare the application checklist",
                    acceptance_criterion="Checklist covers every requirement with refs",
                    provenance=prov(),
                    due_at=(T0 - timedelta(minutes=1)).isoformat(),
                    evidence_refs=["mail:msg-1", "https://example.test/page"],
                    facts=facts(),
                    external_refs=[{"kind": "task", "id": "task:abc", "version": "v1"}])
        data.update(over)
        return R.create(self.store, **data)

    def claim(self, rec):
        return R.claim_due(self.store, now=T0)

    def test_gates_block_before_source_loader(self):
        calls = []
        def loader(brief):
            calls.append(brief)
            return facts()

        cases = [
            (policy(preparation_enabled=False), {}, True, "preparation disabled"),
            (policy(), prefs(paused=True), True, "paused"),
            (policy(preparation_scopes=[]), {}, True, "no approved preparation scope"),
            (policy(preparation_source_consent=False), {}, True, "source consent missing"),
            (policy(), {}, False, "configuration missing"),
        ]
        for pol, pf, present, reason in cases:
            out = P.preflight(self.store, policy=pol, prefs=pf, config_present=present,
                              now=T0, source_loader=loader)
            self.assertFalse(out["wake"], reason)
            self.assertEqual(out["reason"], reason)
        self.assertEqual(calls, [])

    def test_no_due_work_is_silent(self):
        self.accepted(status=R.TENTATIVE, provenance={
            "kind": "opportunity", "source_ref": "feed:1", "conversation_ref": "", "at": T0.isoformat()})
        out = P.preflight(self.store, policy=policy(), prefs=prefs(), config_present=True, now=T0)
        self.assertFalse(out["wake"])
        self.assertEqual(out["reason"], "no due accepted responsibility")

    def test_due_work_wakes_without_source_change_and_context_is_untrusted(self):
        rec = self.accepted()
        out = P.preflight(self.store, policy=policy(), prefs=prefs(), config_present=True, now=T0)
        self.assertTrue(out["wake"])
        self.assertEqual(out["claim"]["id"], rec["id"])
        ctx = out["context"]
        self.assertIn("untrusted DATA", ctx["instructions"])
        self.assertEqual(ctx["data"]["facts"][0]["trust"], "untrusted_source_data")
        self.assertEqual(ctx["allowed_tools"], ["preparation_read_brief", "preparation_write_artifact"])

    def test_accepted_to_artifact_to_ready_to_foreground_handoff(self):
        rec = self.accepted()
        claim = self.claim(rec)
        out = P.write_artifact(
            self.store,
            responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="Application checklist", content=(
                "# Checklist\n\n- [ ] Requirement A (mail:msg-1)\n"
                "- [ ] Form: https://example.test/page\n"),
            evidence_refs=["mail:msg-1", "https://example.test/page"],
            artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        self.assertTrue(out["queued"])
        self.assertFalse(out["completed"])
        read = R.inspect(self.store, rec["id"])
        self.assertEqual(read["status"], R.ACCEPTED)
        self.assertEqual(read["ready"]["status"], "queued")
        ready = P.list_ready(self.store)
        self.assertEqual(len(ready), 1)
        handoff = P.foreground_handoff(self.store, rec["id"], artifact_root=self.artifacts, now=T0)
        self.assertEqual(handoff["execution"], "not_performed")
        self.assertEqual(handoff["target"], {"kind": "task", "ref": "task:abc"})
        self.assertEqual(handoff["approval"], "exact_current_foreground_approval_required")
        self.assertTrue(handoff["draft_is_untrusted_data"])

    def test_cancel_or_source_change_during_generation_rejects_artifact(self):
        rec = self.accepted()
        claim = self.claim(rec)
        R.update(self.store, rec["id"], source_version="v2", now=T0)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(
                self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
                generation=claim["generation"], source_version=claim["source_version"],
                title="stale", content="stale", evidence_refs=["mail:msg-1"],
                artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        self.assertEqual(P.list_ready(self.store), [])

        rec2 = self.accepted(desired_outcome="Another unit")
        claim2 = self.claim(rec2)
        R.cancel(self.store, rec2["id"], now=T0)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(
                self.store, responsibility_id=rec2["id"], claim_token=claim2["claim_token"],
                generation=claim2["generation"], source_version=claim2["source_version"],
                title="canceled", content="canceled", evidence_refs=["mail:msg-1"],
                artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        self.assertEqual(P.list_ready(self.store), [])

    def test_content_safety_oversize_urls_and_active_html(self):
        rec = self.accepted()
        claim = self.claim(rec)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(self.store, responsibility_id=rec["id"],
                             claim_token=claim["claim_token"], generation=claim["generation"],
                             source_version=claim["source_version"], title="big",
                             content="x" * (P.MAX_ARTIFACT_BYTES + 1),
                             evidence_refs=["mail:msg-1"], artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(self.store, responsibility_id=rec["id"],
                             claim_token=claim["claim_token"], generation=claim["generation"],
                             source_version=claim["source_version"], title="html",
                             content="<script>alert(1)</script>",
                             evidence_refs=["mail:msg-1"], artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(self.store, responsibility_id=rec["id"],
                             claim_token=claim["claim_token"], generation=claim["generation"],
                             source_version=claim["source_version"], title="url",
                             content="Invented https://not-supplied.test/x",
                             evidence_refs=["mail:msg-1"], artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(self.store, responsibility_id=rec["id"],
                             claim_token=claim["claim_token"], generation=claim["generation"],
                             source_version=claim["source_version"], title="evidence",
                             content="valid", evidence_refs=["mail:not-supplied"],
                             artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)

    def test_scope_mismatch_unknown_kind_and_revocation_deny_before_claim(self):
        rec = self.accepted()
        for scopes, reason_part in ((["public"], "not approved"),
                                    (["public", "conversation"], "not approved"),
                                    (["public", "bogus"], "unsupported"),
                                    ([], "no approved")):
            out = P.preflight(self.store, policy=policy(preparation_scopes=scopes),
                              prefs=prefs(), config_present=True, now=T0)
            self.assertFalse(out["wake"], scopes)
            self.assertIn(reason_part, out["reason"])
            read = R.inspect(self.store, rec["id"], include_claim=True)
            self.assertEqual(read["claim"]["token"], "")
        # Broad public consent cannot silently grant mail.
        out = P.preflight(self.store, policy=policy(preparation_scopes=["conversation", "public"]),
                          prefs=prefs(), config_present=True, now=T0)
        self.assertFalse(out["wake"])
        self.assertIn("mail", out["reason"])
        # Revocation after a previously broad approval also denies.
        self.assertTrue(P.preflight(self.store, policy=policy(), prefs=prefs(),
                                    config_present=True, now=T0)["wake"])

        rec2 = self.accepted(desired_outcome="Second scoped unit")
        out = P.preflight(self.store, policy=policy(preparation_scopes=["public"]),
                          prefs=prefs(), config_present=True, now=T0)
        self.assertFalse(out["wake"])
        self.assertEqual(R.inspect(self.store, rec2["id"], include_claim=True)["claim"]["token"], "")

    def test_external_source_loader_requires_authorised_evidence(self):
        self.accepted()
        calls = []
        out = P.preflight(self.store, policy=policy(), prefs=prefs(),
                          config_present=True, now=T0,
                          source_loader=lambda rec: calls.append(rec) or [])
        self.assertFalse(out["wake"])
        self.assertIn("needs authorised evidence", out["reason"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(out["claim"])

    def test_loader_snapshot_replaces_stale_source_rows(self):
        rec = self.accepted(
            evidence_refs=["mail:msg-1"], external_refs=[], facts=[
                {"ref": "conversation:turn-3", "text": "Please prepare it.",
                 "provenance": "conversation", "observed_at": T0.isoformat()},
                {"ref": "mail:msg-1", "text": "OLD STALE FACT",
                 "provenance": "cached mail", "observed_at": T0.isoformat()},
            ])
        fresh = [{"ref": "mail:msg-1", "text": "NEW CORRECT FACT",
                  "provenance": "authorised_cache", "observed_at": T0.isoformat()}]
        out = P.preflight(self.store, policy=policy(), prefs=prefs(),
                          config_present=True, now=T0,
                          source_loader=lambda rec: fresh)
        self.assertTrue(out["wake"], out.get("reason"))
        rows = [f for f in out["context"]["data"]["facts"] if f["ref"] == "mail:msg-1"]
        self.assertEqual([f["text"] for f in rows], ["NEW CORRECT FACT"])
        self.assertNotIn("OLD STALE FACT", json.dumps(out["context"]))
        brief = R.claimed_brief(self.store, rec["id"],
                                out["context"]["data"]["claim_token"], now=T0)
        self.assertNotIn("OLD STALE FACT", json.dumps(brief))
        self.assertIn("mail:msg-1", brief["evidence_refs"])

    def test_revoked_source_consent_blocks_cached_private_facts(self):
        rec = self.accepted(evidence_refs=["mail:msg-1"], external_refs=[], facts=[
            {"ref": "conversation:turn-3", "text": "Please prepare it.",
             "provenance": "conversation", "observed_at": T0.isoformat()}])
        fresh = [{"ref": "mail:msg-1", "text": "PRIVATE MAIL FACT",
                  "provenance": "authorised_cache", "observed_at": T0.isoformat()}]
        first = P.preflight(self.store, policy=policy(), prefs=prefs(),
                            config_present=True, now=T0,
                            source_loader=lambda r: fresh)
        self.assertTrue(first["wake"])
        R.release_claim(self.store, rec["id"],
                        claim_token=first["context"]["data"]["claim_token"],
                        generation=first["context"]["data"]["generation"],
                        source_version=first["context"]["data"]["source_version"], now=T0)
        denied = P.preflight(self.store, policy=policy(preparation_source_consent=False),
                             prefs=prefs(), config_present=True, now=T0,
                             source_loader=None)
        self.assertFalse(denied["wake"])
        self.assertEqual(denied["reason"], "source consent missing")
        self.assertNotIn("PRIVATE MAIL FACT", json.dumps(denied))
        # Conversation-only work does not require source consent.
        R.cancel(self.store, rec["id"], now=T0)
        conv = R.create(
            self.store, desired_outcome="Draft a note",
            acceptance_criterion="Short note",
            provenance=prov(),
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:turn-3"],
            facts=[{"ref": "conversation:turn-3", "text": "Please draft a note.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}])
        allowed = P.preflight(self.store, policy=policy(preparation_source_consent=False),
                              prefs=prefs(), config_present=True, now=T0,
                              source_loader=None)
        self.assertTrue(allowed["wake"], allowed.get("reason"))
        self.assertTrue(any(f["ref"] == "conversation:turn-3"
                            for f in allowed["context"]["data"]["facts"]))
        self.assertEqual(conv["id"], allowed["claim"]["id"])

    def test_conversation_only_with_consent_and_empty_loader_still_wakes(self):
        rec = R.create(
            self.store, desired_outcome="Draft a note",
            acceptance_criterion="Short note",
            provenance=prov(),
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:turn-3"],
            facts=[{"ref": "conversation:turn-3", "text": "Please draft a note.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}])
        out = P.preflight(self.store, policy=policy(), prefs=prefs(),
                          config_present=True, now=T0,
                          source_loader=lambda r: [])
        self.assertTrue(out["wake"], out.get("reason"))
        self.assertEqual(rec["id"], out["claim"]["id"])
        self.assertTrue(any(f["ref"] == "conversation:turn-3"
                            for f in out["context"]["data"]["facts"]))
        self.assertNotIn("needs authorised evidence", out["reason"])

    def test_citation_and_active_content_allowlist(self):
        rec = self.accepted()
        claim = self.claim(rec)
        for content, label in (
            ("[[ref: mail:invented]]", "citation"),
            ("Bare mail:invented reference", "token"),
            ("![x](https://example.test/page)", "image"),
            ("<svg onload=alert(1)></svg>", "svg"),
            ("<img src=x onerror=alert(1)>", "event"),
            ("[bad](javascript:alert(1))", "scheme"),
            ("[bad](data:text/html;base64,PHNjcmlwdD4=)", "data"),
            ("[bad](file:///etc/passwd)", "file"),
        ):
            with self.assertRaises(R.ResponsibilityError, msg=label):
                P.write_artifact(
                    self.store, responsibility_id=rec["id"],
                    claim_token=claim["claim_token"], generation=claim["generation"],
                    source_version=claim["source_version"], title=label,
                    content=content, evidence_refs=["mail:msg-1"],
                    artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        safe = P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="safe", content=(
                "Requirement A [[ref: mail:msg-1]]\n\n1 < 2 comparison stays text.\n"),
            evidence_refs=["mail:msg-1"], artifact_root=self.artifacts,
            gate_fn=lambda: True, now=T0)
        self.assertTrue(safe["queued"])

    def test_gate_is_rechecked_before_commit(self):
        rec = self.accepted()
        claim = self.claim(rec)
        verdicts = [True, False]

        def gate():
            return verdicts.pop(0)

        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(
                self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
                generation=claim["generation"], source_version=claim["source_version"],
                title="revoked", content="safe", evidence_refs=["mail:msg-1"],
                artifact_root=self.artifacts, gate_fn=gate, now=T0)
        self.assertEqual(P.list_ready(self.store), [])
        self.assertFalse((self.artifacts.exists() and any(self.artifacts.iterdir())))

        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(
                self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
                generation=claim["generation"], source_version=claim["source_version"],
                title="no gate", content="safe", evidence_refs=["mail:msg-1"],
                artifact_root=self.artifacts, gate_fn=None, now=T0)

    def test_stale_mirror_cannot_override_authoritative_ready(self):
        rec = self.accepted()
        claim = self.claim(rec)
        out = P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="authoritative", content="draft", evidence_refs=["mail:msg-1"],
            artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        mirror = P.load_prep(self.store)
        mirror["ready"][rec["id"]]["generation"] = 999
        mirror["ready"][rec["id"]]["hash"] = "0" * 64
        self.store.save(P.PREP_STATE, mirror)
        ready = P.list_ready(self.store)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["artifact_ref"], out["artifact_ref"])
        self.assertEqual(ready[0]["hash"], out["hash"])
        self.assertEqual(ready[0]["mirror"], "stale_ignored")
        (self.root / "state" / P.PREP_STATE).write_text("{not json", encoding="utf-8")
        ready = P.list_ready(self.store)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["artifact_ref"], out["artifact_ref"])
        self.assertEqual(ready[0]["mirror"], "corrupt_ignored")

    def test_ready_queue_retains_just_queued_item_on_ties(self):
        old_max = P.MAX_READY
        P.MAX_READY = 2
        self.addCleanup(setattr, P, "MAX_READY", old_max)
        for index in range(3):
            rec = {"id": f"resp_tie_{index}", "generation": 1, "source_version": "v1",
                   "external_refs": [], "ready": {}}
            item = P._queue_ready(
                self.store, rec=rec, artifact_ref=f"artifact:resp_tie_{index}:x.md",
                artifact_hash="a" * 64, now=T0)
            self.assertEqual(item["responsibility_id"], rec["id"])
        queued = P.load_prep(self.store)["ready"]
        self.assertIn("resp_tie_2", queued)
        self.assertEqual(len(queued), 2)

    def test_ready_notification_claim_dedupe_and_stale_receipts(self):
        rec = self.accepted()
        claim = self.claim(rec)
        out = P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="notify", content="draft", evidence_refs=["mail:msg-1"],
            artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        blocked = P.claim_ready_notice(self.store, now=T0,
                                       attention_fn=lambda: (False, "quiet hours"))
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["reason"], "quiet hours")
        no_gate = P.claim_ready_notice(self.store, now=T0)
        self.assertFalse(no_gate["eligible"])
        first = P.claim_ready_notice(self.store, now=T0, attention_fn=lambda: True)
        self.assertTrue(first["eligible"])
        token = first["notice_token"]
        self.assertEqual(first["hash"], out["hash"])
        self.assertFalse(P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True)["eligible"])
        with self.assertRaises(R.ResponsibilityError):
            P.mark_ready_notice_delivered(
                self.store, rec["id"], notice_token="stale", generation=first["generation"],
                source_version=first["source_version"], artifact_ref=first["artifact_ref"],
                artifact_hash=first["hash"], now=T0)
        P.mark_ready_notice_unknown(
            self.store, rec["id"], notice_token=token, generation=first["generation"],
            source_version=first["source_version"], artifact_ref=first["artifact_ref"],
            artifact_hash=first["hash"], now=T0)
        self.assertFalse(P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True)["eligible"])
        # A receipt from an older artifact cannot mark a newer generation.
        rec2 = self.accepted(desired_outcome="Newer unit")
        claim2 = self.claim(rec2)
        P.write_artifact(
            self.store, responsibility_id=rec2["id"], claim_token=claim2["claim_token"],
            generation=claim2["generation"], source_version=claim2["source_version"],
            title="notify2", content="draft2", evidence_refs=["mail:msg-1"],
            artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        second = P.claim_ready_notice(self.store, now=T0, attention_fn=lambda: True)
        self.assertTrue(second["eligible"])
        with self.assertRaises(R.ResponsibilityError):
            P.mark_ready_notice_delivered(
                self.store, rec["id"], notice_token=token, generation=first["generation"],
                source_version=first["source_version"], artifact_ref=first["artifact_ref"],
                artifact_hash=first["hash"], now=T0)
        P.mark_ready_notice_delivered(
            self.store, rec2["id"], notice_token=second["notice_token"],
            generation=second["generation"], source_version=second["source_version"],
            artifact_ref=second["artifact_ref"], artifact_hash=second["hash"], now=T0)
        self.assertFalse(P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True)["eligible"])

    def test_foreground_handoff_rejects_replaced_symlink_and_oversize(self):
        for mode in ("replaced", "symlink", "oversize"):
            rec = self.accepted(desired_outcome=f"Handoff {mode}")
            claim = self.claim(rec)
            out = P.write_artifact(
                self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
                generation=claim["generation"], source_version=claim["source_version"],
                title=mode, content="original", evidence_refs=["mail:msg-1"],
                artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
            path = P.artifact_path(self.artifacts, out["artifact_ref"])
            if mode == "replaced":
                path.write_text("replaced content", encoding="utf-8")
            elif mode == "oversize":
                path.write_bytes(b"x" * (P.MAX_ARTIFACT_BYTES + 1))
            else:
                target = self.root / "outside.txt"
                target.write_text("outside", encoding="utf-8")
                path.unlink()
                path.symlink_to(target)
            with self.assertRaises(R.ResponsibilityError, msg=mode):
                P.foreground_handoff(self.store, rec["id"], artifact_root=self.artifacts, now=T0)
            R.cancel(self.store, rec["id"], now=T0)

    def test_traversal_title_stays_inside_root_and_symlink_root_rejected(self):
        rec = self.accepted()
        claim = self.claim(rec)
        out = P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="../../escape", content="bounded",
            evidence_refs=["mail:msg-1"], artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        path = P.artifact_path(self.artifacts, out["artifact_ref"])
        self.assertEqual(path.parent, self.artifacts.resolve())
        with self.assertRaises(R.ResponsibilityError):
            P.artifact_path(self.artifacts, "artifact:x:../../etc/passwd")

        link = self.root / "linked-artifacts"
        link.symlink_to(self.artifacts, target_is_directory=True)
        with self.assertRaises(R.ResponsibilityError):
            P.write_artifact(
                self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
                generation=claim["generation"], source_version=claim["source_version"],
                title="symlink", content="bounded", evidence_refs=["mail:msg-1"],
                artifact_root=link, gate_fn=lambda: True, now=T0)

    def test_source_change_after_queue_invalidates_handoff(self):
        rec = self.accepted()
        claim = self.claim(rec)
        P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="Checklist", content="draft", evidence_refs=["mail:msg-1"],
            artifact_root=self.artifacts, gate_fn=lambda: True, now=T0)
        self.assertEqual(len(P.list_ready(self.store)), 1)
        R.update(self.store, rec["id"], source_version="v2", now=T0)
        self.assertEqual(P.list_ready(self.store), [])
        with self.assertRaises(R.ResponsibilityError):
            P.foreground_handoff(self.store, rec["id"], artifact_root=self.artifacts, now=T0)
        self.assertEqual(R.inspect(self.store, rec["id"])["ready"]["status"], "invalidated")

    def test_stale_completion_fence(self):
        rec = self.accepted()
        claim = self.claim(rec)
        with self.assertRaises(R.ResponsibilityError):
            R.complete(self.store, rec["id"], claim_token="wrong",
                       generation=claim["generation"],
                       source_version=claim["source_version"],
                       completion_evidence=["mail:msg-1"], now=T0)
        with self.assertRaises(R.ResponsibilityError):
            R.complete(self.store, rec["id"], claim_token=claim["claim_token"],
                       generation=claim["generation"] + 1,
                       source_version=claim["source_version"],
                       completion_evidence=["mail:msg-1"], now=T0)


class RunnerTests(unittest.TestCase):
    """Run the public script with an isolated HOME/HERMES_HOME/data root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.data = self.root / "plugin-data"
        self.home.mkdir(parents=True)
        self.data.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({
            "plugins": {"enabled": ["hermes-proactive-portal"]}
        }), encoding="utf-8")
        self.write_configs()
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.home),
            "HERMES_HOME": str(self.home),
            "HERMES_PROACTIVE_PORTAL_DATA": str(self.data),
        })
        self.addCleanup(self._tmp.cleanup)

    def write_configs(self, prep=True):
        pol = policy(preparation_enabled=prep)
        (self.data / "policy.json").write_text(json.dumps(pol), encoding="utf-8")
        (self.data / "preferences.json").write_text(json.dumps(prefs()), encoding="utf-8")
        cfg = default_config()
        cfg["preparation"] = {
            "enabled": prep,
            "source_consent": True,
            "scopes": ["conversation", "mail", "public", "tasks"] if prep else [],
        }
        (self.data / "user_config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def run_runner(self, *extra):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--seeded-clock", T0.isoformat(), *extra],
            cwd=str(ROOT), env=self.env, capture_output=True, text=True, timeout=30)
        return proc

    def test_no_work_emits_native_silent_json(self):
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})

    def test_disabled_gate_emits_silent_json(self):
        self.write_configs(prep=False)
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})

    def test_time_due_unchanged_wakes_without_source_change(self):
        store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        R.create(store, desired_outcome="Prepare checklist",
                 acceptance_criterion="Covers requirements",
                 provenance=prov(),
                 due_at=(T0 - timedelta(minutes=1)).isoformat(),
                 evidence_refs=["mail:msg-1"], facts=facts())
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        payload = json.loads(last)
        self.assertTrue(payload["wakeAgent"])
        self.assertEqual(payload["context"]["data"]["responsibility_id"].startswith("resp_"), True)
        self.assertEqual(payload["context"]["trust"]["facts"], "untrusted_source_data")
        self.assertNotIn("claim_token", payload["context"]["data"])
        self.assertTrue(payload["context"]["claim_token_redacted"])

    def test_canceled_work_does_not_wake(self):
        store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        rec = R.create(store, desired_outcome="Prepare checklist",
                       acceptance_criterion="Covers requirements", provenance=prov(),
                       due_at=(T0 - timedelta(minutes=1)).isoformat(),
                       evidence_refs=["mail:msg-1"], facts=facts())
        R.cancel(store, rec["id"], now=T0)
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})

    def test_expired_claim_recovers_and_wakes(self):
        store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        rec = R.create(store, desired_outcome="Prepare checklist",
                       acceptance_criterion="Covers requirements", provenance=prov(),
                       due_at=(T0 - timedelta(minutes=1)).isoformat(),
                       evidence_refs=["mail:msg-1"], facts=facts())
        R.claim_due(store, now=T0, lease_minutes=1)
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--seeded-clock",
             (T0 + timedelta(minutes=5)).isoformat()],
            cwd=str(ROOT), env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertTrue(json.loads(last)["wakeAgent"])
        read = R.inspect(store, rec["id"], include_claim=True)
        self.assertEqual(read["status"], R.ACCEPTED)
        self.assertTrue(read["recovered_at"])

    def test_corrupt_state_fails_closed(self):
        (self.data / "var").mkdir(parents=True, exist_ok=True)
        (self.data / "var" / "responsibilities.json").write_text("{not json", encoding="utf-8")
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 1)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})
        self.assertIn("preparation-state-invalid", proc.stderr)

    def test_shallow_corrupt_state_fails_closed_without_raw_state(self):
        (self.data / "var").mkdir(parents=True, exist_ok=True)
        bad = {"schema": 1, "version": 1, "records": {
            "resp_bad": {"id": "resp_bad", "status": "accepted", "generation": 1,
                         "claim": {}, "provenance": {"kind": "user_commitment",
                                                     "source_ref": "conversation:1",
                                                     "at": T0.isoformat()}}}}
        (self.data / "var" / "responsibilities.json").write_text(
            json.dumps(bad), encoding="utf-8")
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 1)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})
        self.assertIn("preparation-state-invalid", proc.stderr)
        self.assertNotIn("resp_bad", proc.stderr)
        self.assertNotIn("claim", proc.stderr.lower())

    def test_missing_config_is_gated_before_work(self):
        (self.data / "user_config.json").unlink()
        store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        R.create(store, desired_outcome="Prepare checklist",
                 acceptance_criterion="Covers requirements", provenance=prov(),
                 due_at=(T0 - timedelta(minutes=1)).isoformat(),
                 evidence_refs=["mail:msg-1"], facts=facts())
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0)
        last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
        self.assertEqual(json.loads(last), {"wakeAgent": False})
        self.assertIn("configuration missing", proc.stderr)


if __name__ == "__main__":
    unittest.main()
