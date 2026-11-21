"""Integrated private-preparation seam: native lane -> one draft -> writer.

These tests exercise the actual candidate command, the real StateStore, the
corrected responsibility/preparation modules and a fake host context. They do
not call a provider, source, network, scheduler or delivery path.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import importlib  # noqa: E402
import importlib.util  # noqa: E402

_PKG_NAME = "hermes_proactive_portal_050_prep_integration"
_spec = importlib.util.spec_from_file_location(
    _PKG_NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules[_PKG_NAME] = _pkg
_spec.loader.exec_module(_pkg)

cli = importlib.import_module(f"{_PKG_NAME}.portal.portal_cli")
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot import evidence_cache  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.native_runtime import bind_model_lane  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
main_mod = importlib.import_module(f"{_PKG_NAME}.src.proactive_pilot.main")

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")


class FakeLlm:
    def __init__(self, parsed=None, on_call=None):
        self.parsed = parsed or {
            "title": "Checklist",
            "content": "# Checklist\n\n- [ ] Requirement A [[ref: mail:msg-1]]\n",
            "format": "markdown",
            "evidence_refs": ["mail:msg-1"],
        }
        self.on_call = on_call
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)

        class Result:
            pass

        result = Result()
        result.parsed = self.parsed
        result.text = json.dumps(self.parsed)
        if self.on_call is not None:
            self.on_call()
        return result


class FakeCtx:
    def __init__(self, settings=None, llm=None):
        self.settings = dict(settings or {})
        self.llm = llm or FakeLlm()

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value


class PrepareCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "HERMES_HOME": str(self.home),
            "HERMES_PROACTIVE_PORTAL_DATA": str(self.data),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._tmp.cleanup)
        self._write_configs()
        self.store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        # The production loader reads only already-authorised cache rows. Seed
        # the record's mail ref so these tests exercise the real loader path.
        evidence_cache.record_results(
            self.store, [SourceResult(
                "mail:work", "ok", count=1, checked_at=T0.isoformat(),
                items=[{"ref": "mail:msg-1", "subject": "Requirement A.",
                        "version": "v1", "date": T0.isoformat()}])],
            policy={"preparation_enabled": True,
                    "preparation_source_consent": True,
                    "preparation_scopes": ["conversation", "mail"],
                    "mail_enabled": True, "mail_accounts": ["work"]},
            now=T0)
        self.rec = R.create(
            self.store,
            desired_outcome="Prepare checklist",
            acceptance_criterion="Covers requirements",
            provenance={
                "kind": "user_commitment",
                "source_ref": "conversation:1",
                "conversation_ref": "chat",
                "at": T0.isoformat(),
            },
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["mail:msg-1"],
            facts=[{
                "ref": "mail:msg-1",
                "text": "Requirement A.",
                "provenance": "authorized mail",
                "observed_at": T0.isoformat(),
            }],
        )
        self.addCleanup(cli.set_native_context, None)

    def _write_configs(self, scopes=None):
        config = uc.default_config()
        config["timezone"] = "Asia/Singapore"
        config["notification"]["quiet_start_hour"] = 0
        config["notification"]["quiet_end_hour"] = 0
        config["sources"]["mail"] = True
        config["mail"]["accounts"] = ["work"]
        config["preparation"] = {
            "enabled": True,
            "source_consent": True,
            "scopes": list(scopes if scopes is not None else ["conversation", "mail"]),
        }
        validated = uc.validate_config(config)
        (self.data / "user_config.json").write_text(
            json.dumps(validated), encoding="utf-8")
        (self.data / "preferences.json").write_text(
            json.dumps({"explicit": [], "inferred": [], "snoozes": [], "paused": {}}),
            encoding="utf-8")

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.prepare_command(argparse.Namespace(now=T0.isoformat()))
        return rc, out.getvalue(), err.getvalue()

    def test_prepare_success_recovers_common_incident(self):
        from proactive_pilot import modelhealth

        outcome = {
            "policy": "configured_only", "task": "proactive_secretary",
            "stop": "assigned_routes_exhausted", "native_fallback_attempted": False,
            "attempts": [
                {"role": "primary", "result": "timeout", "reason": "bounded"},
                {"role": "fallback", "result": "rate_limit", "reason": "bounded"},
            ],
        }
        opened = modelhealth.consume_terminal_outcome(self.store, outcome, now=T0)
        self.assertEqual(opened["incident"]["status"], "open")
        ctx = FakeCtx()
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=True, status={
            "strict_active": True, "configured_model_calls_allowed": True})
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(ctx.llm.calls), 1)
        state = modelhealth.status(self.store)
        self.assertEqual(state["incident"]["status"], "recovered")
        self.assertEqual(state["incident"]["recovery"]["success_role"], "route")

    def test_prepare_terminal_failure_feeds_common_incident(self):
        from proactive_pilot import modelhealth

        failure = RuntimeError("provider secret")
        failure.aux_route_outcome = {
            "policy": "configured_only", "task": "proactive_secretary",
            "stop": "assigned_routes_exhausted", "native_fallback_attempted": False,
            "attempts": [
                {"role": "primary", "result": "timeout", "reason": "bounded"},
                {"role": "fallback", "result": "rate_limit", "reason": "bounded"},
            ],
        }

        class FailingLlm(FakeLlm):
            def complete_structured(self, **kwargs):
                self.calls.append(kwargs)
                raise failure

        ctx = FakeCtx(llm=FailingLlm())
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=True, status={
            "strict_active": True, "configured_model_calls_allowed": True})
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        state = modelhealth.status(self.store)
        self.assertEqual(state["incident"]["status"], "open")
        self.assertEqual(state["notice"]["status"], "pending")
        self.assertNotIn("provider secret", json.dumps(state))

    def test_unsupported_host_never_claims_or_calls_model(self):
        ctx = FakeCtx()
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=False, reason="host lacks configured-only")
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(ctx.llm.calls, [])
        read = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(read["claim"]["token"], "")
        self.assertIn("model lane unavailable", err)
        self.assertNotIn("msg-1", err)

    def test_one_draft_writes_verified_artifact_without_stdout_message(self):
        ctx = FakeCtx()
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=True, status={
            "strict_active": True, "configured_model_calls_allowed": True})
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(len(ctx.llm.calls), 1)
        # The bearer claim token is never sent to the model.
        sent = json.dumps(ctx.llm.calls[0], ensure_ascii=False)
        read = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertNotIn('"claim_token":', sent)
        self.assertIn("claim_token_redacted", sent)
        self.assertEqual(read["ready"]["status"], "queued")
        self.assertTrue(read["ready"]["artifact_ref"].startswith("artifact:"))
        artifact = self.data / "preparation" / "artifacts" / \
            read["ready"]["artifact_ref"].rsplit(":", 1)[-1]
        self.assertTrue(artifact.is_file())
        self.assertEqual(oct(artifact.stat().st_mode & 0o777), "0o600")
        self.assertEqual(read["status"], R.ACCEPTED)

    def test_revocation_during_draft_aborts_before_commit(self):
        def revoke():
            self._write_configs(scopes=[])

        ctx = FakeCtx(llm=FakeLlm(on_call=revoke))
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=True, status={
            "strict_active": True, "configured_model_calls_allowed": True})
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        read = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(read["ready"]["status"], "")
        self.assertEqual(read["claim"]["token"], "")
        self.assertFalse((self.data / "preparation" / "artifacts").exists()
                         and any((self.data / "preparation" / "artifacts").iterdir()))

    def test_ready_artifact_uses_existing_attention_path_once(self):
        from proactive_pilot import preparation as prep
        from proactive_pilot.policy import DEFAULT_POLICY

        claim = R.claim_due(self.store, now=T0)
        # A source-scoped ready notice requires the current authorised source
        # generation, not only a historical evidence ref.
        R.attach_source_snapshot(
            self.store, self.rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            facts=[{"ref": "mail:msg-1", "text": "Requirement A.",
                    "provenance": "authorized mail", "observed_at": T0.isoformat()}],
            now=T0)
        prep.write_artifact(
            self.store, responsibility_id=self.rec["id"],
            claim_token=claim["claim_token"], generation=claim["generation"],
            source_version=claim["source_version"], title="Checklist",
            content="Draft [[ref: mail:msg-1]]", evidence_refs=["mail:msg-1"],
            gate_fn=lambda: True, now=T0)
        policy_path = self.data / "policy.json"
        policy_path.write_text(json.dumps(dict(DEFAULT_POLICY)), encoding="utf-8")
        prefs_path = self.data / "preferences.json"
        out, err = io.StringIO(), io.StringIO()
        args = main_mod.build_parser().parse_args([
            "--state-dir", str(self.data / "var"),
            "--policy", str(policy_path),
            "--preferences", str(prefs_path),
            "--run",
        ])
        args.require_config = True
        args.native_ctx = None
        args.clock = T0
        with mock.patch.object(main_mod, "default_sources", lambda *a, **k: []), \
                mock.patch.object(main_mod, "build_clock", lambda *a, **k: SeededClock(T0.isoformat())), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main_mod.run_once(args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
        self.assertEqual(rc, 0, err.getvalue())
        # 0.5.3 owner contract: the notice names the actual task and contains no
        # registry/artifact identifiers or generic status boilerplate.
        notice = out.getvalue()
        self.assertIn("draft for", notice.lower(), msg="OUT=" + notice + " ERR=" + err.getvalue())
        self.assertNotIn("artifact:", notice)
        self.assertNotIn("responsibility", notice.lower())
        self.assertNotIn("no external action", notice.lower())
        # A second run must not repeat the same notice.
        out2 = io.StringIO()
        with mock.patch.object(main_mod, "default_sources", lambda *a, **k: []), \
                mock.patch.object(main_mod, "build_clock", lambda *a, **k: SeededClock(T0.isoformat())), \
                contextlib.redirect_stdout(out2), contextlib.redirect_stderr(io.StringIO()):
            main_mod.run_once(args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
        self.assertNotIn("private secretary preparation draft", out2.getvalue().lower())
        self.assertEqual(out2.getvalue().strip(), "", "a repeated unchanged outcome must stay silent")

    def test_untrusted_model_output_cannot_escape_artifact_root(self):
        bad = FakeLlm(parsed={
            "title": "../../escape", "content": "<script>x</script>",
            "format": "markdown", "evidence_refs": ["mail:msg-1"],
        })
        ctx = FakeCtx(llm=bad)
        cli.set_native_context(ctx)
        bind_model_lane(ctx, allowed=True, status={
            "strict_active": True, "configured_model_calls_allowed": True})
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        read = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(read["ready"]["status"], "")
        self.assertEqual(read["claim"]["token"], "")


if __name__ == "__main__":
    unittest.main()
