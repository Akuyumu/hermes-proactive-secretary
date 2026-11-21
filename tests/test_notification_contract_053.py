"""Synthetic regression coverage for the low-noise notification contract (0.5.3).

Everything runs against temporary stores and the actual ready-notice seam. No
live source, scheduler, delivery transport, credential or network is used.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import main as pipeline_main  # noqa: E402
from proactive_pilot import preparation as P  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot import source_health as SH  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.feedback import empty_preferences  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+00:00")
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)


def prov():
    return {"kind": "user_commitment", "source_ref": "conversation:turn-3",
            "conversation_ref": "chat:main", "at": T0.isoformat()}


def facts():
    # Conversation-scoped evidence needs no private source snapshot, so these
    # regressions exercise the notice seam rather than the source-consent gate.
    return [{"ref": "conversation:s1", "text": "Requirement A is due 2026-10-25.",
             "provenance": "conversation", "observed_at": T0.isoformat()}]


def notice_policy(**over):
    base = dict(DEFAULT_POLICY)
    base.update({
        "timezone": "UTC",
        "preparation_enabled": True,
        "preparation_scopes": ["conversation"],
        "preparation_source_consent": True,
        "quiet_start_hour": 0,
        "quiet_end_hour": 0,
        "cooldown_minutes": 0,
        "daily_normal_cap": 5,
        "_config_present": True,
    })
    base.update(over)
    return base


class NotificationContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        self.artifacts = self.root / "artifacts"
        self.state = {"schema": 1, "items": {}, "delivered": [], "health": {}}

    def accepted(self, **over):
        data = dict(
            desired_outcome="Prepare the application checklist",
            acceptance_criterion="Checklist covers every requirement with refs",
            provenance=prov(),
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:s1"],
            facts=facts())
        data.update(over)
        return R.create(self.store, **data)

    def attach(self, rec, *, content="draft", title="Private Alpha", now=T0):
        claim = R.claim_due(self.store, now=now)
        self.assertIsNotNone(claim)
        return P.write_artifact(
            self.store, responsibility_id=rec["id"],
            claim_token=claim["claim_token"], generation=claim["generation"],
            source_version=claim["source_version"], title=title, content=content,
            evidence_refs=["conversation:s1"], artifact_root=self.artifacts,
            gate_fn=lambda: True, now=now)

    def regenerate(self, rec, *, update=None, content="draft", now=T1):
        R.update(self.store, rec["id"], now=now, **(update or {"source_version": "v2"}))
        fresh = R.inspect(self.store, rec["id"])
        self.attach(fresh, content=content, now=now)
        return fresh

    def run_notice(self, *, policy=None, prefs=None, state=None, store=None,
                   emit=None):
        store = store or self.store
        state = self.state if state is None else state
        policy = policy or notice_policy()
        prefs = prefs if prefs is not None else empty_preferences()
        args = SimpleNamespace(native_ctx=None, check=False, dry_run=False)
        out = io.StringIO()
        if emit is not None:
            with patch.object(pipeline_main, "_emit", side_effect=emit):
                rc = pipeline_main._maybe_emit_ready_notice(
                    args, store, state, policy, prefs, store.clock)
            return rc, ""
        with contextlib.redirect_stdout(out):
            rc = pipeline_main._maybe_emit_ready_notice(
                args, store, state, policy, prefs, store.clock)
        return rc, out.getvalue()

    def reopen(self):
        store = StateStore(self.root / "state", SeededClock(T1.isoformat()))
        return store, store.load_notify_state(strict=True)

    def test_legacy_already_emitted_is_seeded_and_not_renotified(self):
        rec = self.accepted()
        first = self.attach(rec)
        self.state["delivered"].append({
            "at": T0.isoformat(),
            "signature": "preparation-ready:" + first["artifact_ref"].rsplit(":", 1)[-1],
            "urgent": False,
        })
        rc, text = self.run_notice()
        self.assertIsNone(rc)
        self.assertEqual(text, "")
        seeded = R.inspect(self.store, rec["id"])["attention"]
        self.assertEqual(seeded["status"], "legacy")

        reopened, state = self.reopen()
        self.state = state
        self.store = reopened
        self.regenerate(rec)
        rc, text = self.run_notice()
        self.assertIsNone(rc)
        self.assertEqual(text, "")
        self.assertFalse(first["artifact_ref"] in text)

    def test_legacy_row_for_a_different_artifact_does_not_suppress_a_new_outcome(self):
        """An old announcement of another artifact must not pre-suppress."""
        rec = self.accepted()
        current = self.attach(rec)
        self.state["delivered"].append({
            "at": T0.isoformat(),
            "signature": "preparation-ready:artifact:" + rec["id"] + ":old-doc.md",
            "urgent": False,
        })
        rc, text = self.run_notice()
        self.assertEqual(rc, 0, "a different artifact's announcement is not this outcome")
        self.assertIn("application checklist", text)
        self.assertEqual(R.inspect(self.store, rec["id"])["attention"]["status"], "delivered")
        self.assertNotEqual(current["artifact_ref"], "")

    def test_repeated_generations_and_restart_emit_once(self):
        rec = self.accepted()
        self.attach(rec)
        rc, first = self.run_notice()
        self.assertEqual(rc, 0)
        self.assertIn("application checklist", first)

        for now in (T1, T2):
            self.regenerate(rec, now=now)
            rc, text = self.run_notice()
            self.assertIsNone(rc)
            self.assertEqual(text, "")

        reopened, state = self.reopen()
        self.state = state
        self.store = reopened
        self.regenerate(rec, update={"source_version": "v3"}, now=T2 + timedelta(minutes=5))
        self.assertEqual(self.run_notice()[1], "")

    def test_genuine_material_change_rearms_but_evidence_churn_does_not(self):
        rec = self.accepted()
        self.attach(rec)
        first = self.run_notice()[1]
        self.assertIn("application checklist", first)

        self.regenerate(rec, update={"source_version": "v2"})
        self.assertEqual(self.run_notice()[1], "")

        self.regenerate(rec, update={
            "desired_outcome": "Prepare the supplier renewal decision brief",
            "acceptance_criterion": "Clearly names the decision and deadline"},
            content="new material", now=T2)
        rc, changed = self.run_notice()
        self.assertEqual(rc, 0)
        self.assertIn("supplier renewal decision brief", changed)
        self.assertNotIn("application checklist", changed)

    def test_quiet_cap_and_explicit_preference_suppress_without_consuming(self):
        rec = self.accepted()
        self.attach(rec)

        quiet = notice_policy(quiet_start_hour=0, quiet_end_hour=23)
        self.assertIsNone(self.run_notice(policy=quiet)[0])
        # Readback normalises the attention block; suppression means no material
        # identity was recorded and no claim status was written.
        self.assertEqual(R.inspect(self.store, rec["id"])["attention"].get("material", ""), "")
        self.assertEqual(R.inspect(self.store, rec["id"])["attention"].get("status", ""), "")
        self.assertEqual(
            R.inspect(self.store, rec["id"])["ready"]["notification"]["status"], "")

        capped_state = {"schema": 1, "items": {}, "health": {},
                        "delivered": [{"at": T0.isoformat(), "signature": f"x{i}"}
                                      for i in range(5)]}
        self.assertIsNone(self.run_notice(state=capped_state)[0])
        self.assertEqual(capped_state["delivered"][-1]["signature"], "x4")

        blocked_prefs = empty_preferences()
        blocked_prefs["explicit"] = [
            {"kind": "preparation", "suppress": True, "match": "application"}]
        self.assertIsNone(self.run_notice(prefs=blocked_prefs)[0])
        self.assertEqual(R.inspect(self.store, rec["id"])["attention"].get("material", ""), "")

        rc, text = self.run_notice()
        self.assertEqual(rc, 0)
        self.assertIn("application checklist", text)

    def test_unknown_emission_is_terminal_and_never_resent(self):
        rec = self.accepted()
        self.attach(rec)

        def boom(_text):
            raise RuntimeError("synthetic ambiguous transport")

        rc, _ = self.run_notice(emit=boom)
        self.assertEqual(rc, 1)
        after = R.inspect(self.store, rec["id"])
        self.assertEqual(after["attention"]["status"], "unknown")
        self.assertEqual(after["ready"]["notification"]["status"], "unknown")

        reopened, state = self.reopen()
        self.state = state
        self.store = reopened
        self.regenerate(rec)
        rc, text = self.run_notice()
        self.assertIsNone(rc)
        self.assertEqual(text, "")

    def test_rendered_notice_contains_no_raw_identifiers(self):
        rec = self.accepted(
            desired_outcome=("Prepare artifact:resp:secret.docx for mail:msg-1 "
                             "using /home/example/private/notes.md"),
            acceptance_criterion=("Meet https://example.test/private and cite "
                                  "conversation:turn-3"))
        artifact = self.attach(rec, title="Secret Title", content="Secret body")
        rc, text = self.run_notice()
        self.assertEqual(rc, 0)
        for raw in ("artifact:", "mail:", "conversation:", "https://",
                    "/home/", rec["id"], artifact["artifact_ref"],
                    "Secret Title", "Secret body", "No external action"):
            self.assertNotIn(raw, text)

    def test_two_scheduler_instances_claim_one_artifact(self):
        rec = self.accepted()
        self.attach(rec)
        one = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        two = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        first = P.claim_ready_notice(one, now=T0, attention_fn=lambda: True)
        second = P.claim_ready_notice(two, now=T0, attention_fn=lambda: True)
        self.assertTrue(first["eligible"])
        self.assertFalse(second["eligible"])
        P.mark_ready_notice_delivered(
            one, rec["id"], notice_token=first["notice_token"],
            generation=first["generation"], source_version=first["source_version"],
            artifact_ref=first["artifact_ref"], artifact_hash=first["hash"], now=T0)

        self.regenerate(rec)
        third = P.claim_ready_notice(two, now=T1, attention_fn=lambda: True)
        self.assertFalse(third["eligible"])


class SourceHealthContractProofTests(unittest.TestCase):
    def _failure(self, error="401 unauthorized"):
        return SourceResult(name="mail:critical", status="error", error=error,
                            attempted=True)

    def _policy(self):
        return dict(DEFAULT_POLICY, timezone="UTC", health_critical_sources=["mail:critical"],
                    source_health_failure_threshold=3,
                    source_health_persistent_minutes=0,
                    source_health_cooldown_minutes=1,
                    source_health_max_recovery_attempts=2)

    def _alert_ready_state(self, rec, start, error="401 unauthorized"):
        state = {"schema": 1, "items": {}, "delivered": [], "health": {}}
        policy = self._policy()
        t = start
        for _ in range(3):
            SH.record_observations(state, [self._failure(error)], t, policy)
            t += timedelta(minutes=2)
        for _ in range(2):
            SH.dispatch_gate(state, "mail:critical", t, policy)
            t += timedelta(minutes=2)
            SH.record_observations(state, [self._failure(error)], t, policy)
            t += timedelta(minutes=2)
        return state, policy, t

    def test_same_unresolved_incident_alerts_once_then_recovery_rearms(self):
        state, policy, now = self._alert_ready_state(None, T0)
        first = SH.health_alert(state, now, policy, commit=True)
        self.assertIn("credentials", first)
        # Cooldown expiry and different exception wording must not re-notify.
        later = now + timedelta(hours=8)
        SH.record_observations(
            state, [self._failure("401 authorization revoked again")], later, policy)
        self.assertIsNone(SH.health_alert(state, later, policy, commit=False))

        # Authoritative recovery closes the incident internally.
        SH.record_observations(
            state, [SourceResult(name="mail:critical", status="ok",
                                 attempted=True)], later + timedelta(minutes=2), policy)
        recovered = SH.load_source_health(state)["sources"]["mail:critical"]
        self.assertEqual(recovered["failure_streak"], 0)
        state2, policy2, now2 = self._alert_ready_state(
            None, later + timedelta(hours=1), error="401 unauthorized")
        self.assertIn("credentials", SH.health_alert(state2, now2, policy2, commit=True))


if __name__ == "__main__":
    unittest.main()
