"""Production-path acceptance for the 0.5.0 main-candidate corrections.

No live profile, network, model, scheduler or installed plugin is touched. All
state is under TMPDIR with synthetic HOME/HERMES_HOME.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = tempfile.mkdtemp(prefix="secretary-main-final-")
os.environ.setdefault("HERMES_HOME", os.path.join(SCRATCH, "home"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import alertrefs  # noqa: E402
from proactive_pilot import main as pipeline_main  # noqa: E402
from proactive_pilot import evidence_cache  # noqa: E402
from proactive_pilot import modelhealth  # noqa: E402
from proactive_pilot import native_runtime  # noqa: E402
from proactive_pilot import preparation as P  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T12:00:00+00:00")


def _load_package():
    name = "hermes_proactive_secretary_main_final"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
conversation = importlib.import_module(f"{PKG.__name__}.secretary.secretary_conversation")
onboarding = importlib.import_module(f"{PKG.__name__}.secretary.secretary_onboarding")
sc = importlib.import_module(f"{PKG.__name__}.secretary.secretary_cli")
cron_mod = importlib.import_module(f"{PKG.__name__}.cron")
runner_install = importlib.import_module(f"{PKG.__name__}.runner_install")
responsibilities = importlib.import_module(f"{PKG.__name__}.secretary.secretary_responsibilities")
release_export = importlib.import_module("release_export")


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
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="", emoji="",
                      override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema,
                            "handler": handler, "override": override}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, **kwargs):
        self.commands[name] = fn


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self._old = {key: os.environ.get(key) for key in (
            "HERMES_PROACTIVE_SECRETARY_DATA", "HERMES_HOME")}
        os.environ["HERMES_PROACTIVE_SECRETARY_DATA"] = str(self.data)
        os.environ["HERMES_HOME"] = str(self.root / "home")
        (self.root / "home").mkdir()
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(self.data / "var", SeededClock(T0.isoformat()))

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ResponsibilityProductionTests(Workspace):
    def _ctx_and_hook(self):
        ctx = Ctx()
        registered = PKG._register_conversation(ctx)
        self.assertTrue(registered["tool"])
        self.assertTrue(registered["responsibility_tool"])
        service = onboarding.OnboardingService(ctx)
        hook = conversation.make_pre_llm_hook(ctx, service)
        return ctx, hook, ctx.tools["secretary_responsibility"]["handler"]

    def test_registered_hook_and_tool_capture_once_with_real_provenance(self):
        ctx, hook, handler = self._ctx_and_hook()
        message = "I will prepare the visa application by Friday"
        hint = hook(platform="telegram", user_message=message, session_id="s1")
        self.assertIn("secretary_responsibility", hint["context"])
        first = json.loads(handler({
            "action": "capture", "session_id": "s1", "evidence_quote": message,
            "desired_outcome": "prepare the visa application",
            "acceptance_criterion": "Application packet is complete",
        }))
        self.assertTrue(first["ok"])
        rec = first["responsibility"]
        self.assertEqual(rec["status"], R.ACCEPTED)
        self.assertTrue(rec["provenance"]["source_ref"].startswith("conversation:"))
        self.assertEqual(rec["provenance"]["conversation_ref"],
                         rec["provenance"]["source_ref"])
        second = json.loads(handler({
            "action": "capture", "session_id": "s1", "evidence_quote": message,
            "desired_outcome": "prepare the visa application",
        }))
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(json.loads(handler({"action": "list"}))["responsibilities"]), 1)

    def test_model_cannot_invent_quote_or_promote_opportunity(self):
        ctx, hook, handler = self._ctx_and_hook()
        hook(platform="telegram", user_message="I need to finish the report",
             session_id="s1")
        bad = json.loads(handler({
            "action": "capture", "session_id": "s1",
            "evidence_quote": "the assistant says this is important",
            "desired_outcome": "finish the report",
        }))
        self.assertFalse(bad["ok"])
        opportunity = json.loads(handler({
            "action": "capture", "session_id": "s1",
            "evidence_quote": "I need to finish the report",
            "desired_outcome": "maybe add a cover page", "kind": "opportunity",
        }))
        self.assertEqual(opportunity["responsibility"]["status"], R.TENTATIVE)

    def test_thanks_silence_and_unrelated_session_create_nothing(self):
        ctx, hook = self._ctx_and_hook()[0:2]
        self.assertIsNone(responsibilities.responsibility_context(
            ctx, platform="telegram", user_message="thanks!", session_id="s1"))
        self.assertIsNone(responsibilities.responsibility_context(
            ctx, platform="telegram", user_message="", session_id="s1"))
        hook(platform="telegram", user_message="I will send the form", session_id="s1")
        handler = ctx.tools["secretary_responsibility"]["handler"]
        denied = json.loads(handler({
            "action": "capture", "session_id": "other",
            "evidence_quote": "I will send the form", "desired_outcome": "send form"}))
        self.assertFalse(denied["ok"])
        self.assertEqual(json.loads(handler({"action": "list"}))["responsibilities"], [])

    def test_cli_create_list_update_cancel_and_handoff_are_foreground_only(self):
        parser = argparse.ArgumentParser()
        sc.register_cli(parser)

        def run(*argv):
            ns = parser.parse_args(["responsibilities", "--data-dir", str(self.data), *argv])
            return ns.func(ns)

        quote = "I will prepare the tax packet"
        self.assertEqual(run("capture", "--source-ref", "conversation:foreground-1",
                             "--conversation-ref", "conversation:foreground-1",
                             "--evidence-quote", quote,
                             "--desired-outcome", "prepare tax packet",
                             "--acceptance-criterion", "packet complete",
                             "--json"), 0)
        listed = R.list_records(self.store)
        self.assertEqual(len(listed), 1)
        rid = listed[0]["id"]
        self.assertEqual(run("update", "--id", rid, "--evidence-quote", quote,
                             "--due-at", (T0 + timedelta(days=2)).isoformat(),
                             "--json"), 0)
        self.assertEqual(R.inspect(self.store, rid)["generation"], 2)
        self.assertEqual(run("handoff", "--id", rid, "--json"), 1)
        self.assertEqual(run("cancel", "--id", rid, "--evidence-quote", quote,
                             "--reason", "user changed plans", "--json"), 0)
        self.assertEqual(R.inspect(self.store, rid)["status"], R.CANCELED)


class PreparationInstallTests(Workspace):
    def test_explicit_opt_in_installs_one_runner_and_one_job(self):
        scripts = self.root / "scripts"
        ownership = self.root / "runner_ownership.json"
        plain = runner_install.install_runners(
            plugin_root=ROOT, template_root=ROOT / "runners",
            scripts_dir=scripts, ownership_path=ownership)
        self.assertEqual(len(plain), 2)
        self.assertFalse((scripts / cron_mod.PREPARATION_SCRIPT).exists())
        self.assertEqual(len(cron_mod.build_job_specs()), 2)

        opted = runner_install.install_runners(
            plugin_root=ROOT, template_root=ROOT / "runners",
            scripts_dir=scripts, ownership_path=ownership,
            include_preparation=True)
        self.assertEqual(len(opted), 3)
        installed = scripts / cron_mod.PREPARATION_SCRIPT
        self.assertTrue(installed.exists())
        self.assertNotIn("{{PLUGIN_ROOT}}", installed.read_text(encoding="utf-8"))
        specs = cron_mod.build_job_specs(include_preparation=True)
        self.assertEqual(len(specs), 3)
        prep = [s for s in specs if s.name == cron_mod.PREPARATION_JOB_NAME]
        self.assertEqual(len(prep), 1)
        self.assertEqual(prep[0].script, cron_mod.PREPARATION_SCRIPT)
        self.assertEqual(prep[0].deliver, "local")

    def test_installed_runner_uses_fixed_native_argv_and_silent_stdout(self):
        scripts = self.root / "scripts"
        runner_install.install_runners(
            plugin_root=ROOT, template_root=ROOT / "runners",
            scripts_dir=scripts, ownership_path=self.root / "own.json",
            include_preparation=True)
        home = self.root / "home"
        (home / "config.json").write_text(json.dumps(
            {"plugins": {"enabled": ["hermes-proactive-secretary"]}}), encoding="utf-8")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "hermes"
        log = self.root / "argv.jsonl"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "open(os.environ['FAKE_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8")
        fake.chmod(0o700)
        env = dict(os.environ, HERMES_BIN=str(fake), FAKE_LOG=str(log),
                   HERMES_HOME=str(home),
                   HERMES_PROACTIVE_SECRETARY_DATA=str(self.data))
        proc = __import__("subprocess").run(
            [sys.executable, str(scripts / cron_mod.PREPARATION_SCRIPT)],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        argv = json.loads(log.read_text(encoding="utf-8").strip())
        self.assertEqual(argv[:3], ["secretary", "prepare", "--data-dir"])


class ReadyNoticeGateTests(Workspace):
    def _queued(self):
        rec = R.create(
            self.store, desired_outcome="Prepare checklist",
            acceptance_criterion="Checklist complete",
            provenance={"kind": "user_commitment",
                        "source_ref": "conversation:turn-1",
                        "conversation_ref": "conversation:turn-1",
                        "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:turn-1"],
            facts=[{"ref": "conversation:turn-1", "text": "User asked for a checklist.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}])
        claim = R.claim_due(self.store, now=T0)
        art = P.write_artifact(
            self.store, responsibility_id=rec["id"],
            claim_token=claim["claim_token"], generation=claim["generation"],
            source_version=claim["source_version"], title="Checklist",
            content="Checklist [[ref: conversation:turn-1]]",
            evidence_refs=["conversation:turn-1"],
            artifact_root=self.root / "artifacts", gate_fn=lambda: True, now=T0)
        return rec, art

    def test_scope_gate_blocks_then_allows_exactly_one_claim(self):
        rec, _ = self._queued()
        blocked = P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True,
            scope_fn=lambda item: False)
        self.assertFalse(blocked["eligible"])
        allowed = P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True,
            scope_fn=lambda item: True)
        self.assertTrue(allowed["eligible"])
        again = P.claim_ready_notice(
            self.store, now=T0, attention_fn=lambda: True,
            scope_fn=lambda item: True)
        self.assertFalse(again["eligible"])
        self.assertEqual(allowed["responsibility_id"], rec["id"])
        self.assertTrue(allowed["artifact_ref"].startswith("artifact:"))

    def test_disabled_preparation_gate_blocks_queued_notice(self):
        policy = dict(DEFAULT_POLICY, preparation_enabled=False,
                      preparation_scopes=["conversation"])
        gate = P.gate_status(policy, {}, config_present=True, now=T0)
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["reason"], "preparation disabled")


class OutageConsumerTests(Workspace):
    def _facts(self, primary="timeout", fallback="rate_limit"):
        return {
            "policy": "configured_only", "task": "proactive_secretary",
            "stop": "assigned_routes_exhausted", "native_fallback_attempted": False,
            "primary": {"provider": "p", "model": "m"},
            "attempts": [
                {"role": "primary", "provider": "p", "model": "m",
                 "result": primary, "reason": "bounded"},
                {"role": "fallback", "provider": "p2", "model": "m2",
                 "result": fallback, "reason": "bounded"},
            ],
        }

    def _run(self, facts):
        state = {}
        proposal = SimpleNamespace(route="ctx.llm", action="error", outcome=facts)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pipeline_main._maybe_model_outage_notice(
                SimpleNamespace(), self.store, state, dict(DEFAULT_POLICY), {},
                SeededClock(T0.isoformat()), proposal)
        return rc, out.getvalue()

    def test_typed_facts_preserved_through_llm_unavailable(self):
        class Llm:
            def complete_structured(self, **kwargs):
                exc = RuntimeError("provider secret https://example.test?key=SECRET")
                exc.aux_route_outcome = self.facts
                raise exc

        llm = Llm()
        llm.facts = self._facts()
        ctx = SimpleNamespace(llm=llm)
        native_runtime.bind_model_lane(ctx, allowed=True, reason="test")
        route = native_runtime.build_llm_route(ctx, path="evaluator")
        with self.assertRaises(native_runtime.LlmUnavailable) as caught:
            route.complete_structured(instructions="x", input=[])
        outcome = getattr(caught.exception, "aux_route_outcome")
        self.assertEqual(outcome["stop"], "assigned_routes_exhausted")
        self.assertNotIn("SECRET", json.dumps(outcome))

    def test_exhaustion_emits_one_fixed_notice_and_no_duplicate(self):
        rc, first = self._run(self._facts())
        self.assertEqual(rc, 0)
        self.assertIn(modelhealth.NOTICE_TEXT, first)
        rc2, second = self._run(self._facts())
        self.assertIsNone(rc2)
        self.assertEqual(second, "")
        status = modelhealth.status(self.store)
        self.assertEqual(status["notice"]["status"], "delivered")

    def test_primary_failure_fallback_success_is_no_incident(self):
        facts = self._facts(fallback="success")
        rc, out = self._run(facts)
        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertEqual(modelhealth.status(self.store)["notice"], None)

    def test_host_v1_stop_reason_and_route_shape_is_consumed(self):
        facts = {
            "version": 1, "policy": "configured_only", "task": "proactive_secretary",
            "stop_reason": "assigned_routes_exhausted",
            "route": {"provider": "strictprimary", "model": "primary-model"},
            "attempts": [
                {"role": "primary", "provider": "strictprimary",
                 "model": "primary-model", "result": "auth"},
                {"role": "fallback", "provider": "fallbackprov",
                 "model": "fallback-model", "result": "quarantined"},
            ],
        }
        rc, out = self._run(facts)
        self.assertEqual(rc, 0)
        self.assertIn(modelhealth.NOTICE_TEXT, out)

    def test_missing_or_malformed_metadata_never_fabricates_an_outage(self):
        for facts in (None, {"stop": "assigned_routes_exhausted"},
                      dict(self._facts(), native_fallback_attempted=True)):
            rc, out = self._run(facts)
            self.assertIsNone(rc)
            self.assertEqual(out, "")
        self.assertEqual(modelhealth.status(self.store)["notice"], None)


class OnboardingAndDiscoveryTests(Workspace):
    def test_gap_only_questions_and_duplicate_facts(self):
        state = onboarding.default_state()
        state["settings"]["timezone"] = "UTC"
        self.assertEqual(onboarding.next_step(state), "destination")
        state["destination"]["target"] = "local"
        self.assertEqual(onboarding.next_step(state), "allowance")
        state["decided"] = ["setting:daily_allowance"]
        self.assertEqual(onboarding.next_step(state), "quiet")
        state["decided"] += ["setting:quiet_start_hour", "setting:quiet_end_hour"]
        self.assertEqual(onboarding.next_step(state), "autonomy")
        state["settings"]["autonomy_mode"] = "review_only"
        state["decided"].append("setting:autonomy_mode")
        self.assertEqual(onboarding.next_step(state), "permissions")

        svc = onboarding.OnboardingService(Ctx())
        first = svc.propose_facts([{"key": "city", "value": "London"}])
        self.assertEqual(len(first["added"]), 1)
        duplicate = svc.propose_facts([{"key": "city", "value": "London"}])
        self.assertEqual(duplicate["added"], [])
        self.assertEqual(duplicate["skipped_existing"], ["city"])
        conflict = svc.propose_facts([{"key": "city", "value": "Paris"}])
        self.assertEqual(conflict["conflicts"], ["city"])
        self.assertTrue(conflict["added"][0]["conflict"])
        # Proposals are never grants.
        self.assertEqual(svc.inspect()["state"]["grants"], onboarding.default_state()["grants"])

    def test_mobile_followup_uses_exact_emitted_ref_not_unrelated_chat(self):
        ctx = Ctx()
        record = alertrefs.remember_alert(
            ctx.state, signature="preparation-ready:artifact:resp:doc.md",
            kind="preparation", candidate_refs=[{"ref": "resp_abc", "version": "2",
                                                 "source": "responsibility"}],
            destination="local", emitted_at=T0.isoformat(), source_version="v1")
        hook = conversation.make_pre_llm_hook(ctx, onboarding.OnboardingService(ctx))
        out = hook(platform="telegram", user_message=f"why this alert {record['ref']}?",
                   session_id="s1")
        self.assertIn("kind=preparation", out["context"])
        self.assertIn("resp_abc", out["context"])
        self.assertIn("emission only", out["context"])
        unrelated = hook(platform="telegram", user_message="what is the weather?",
                         session_id="s1")
        self.assertNotIn("Bounded emitted-alert", (unrelated or {}).get("context", ""))
        self.assertNotIn("resp_abc", (unrelated or {}).get("context", ""))

    def test_known_interests_drive_context_without_private_identifiers(self):
        policy = {"public_interests": [
            "marine biology", "mail me at person@example.test", "x" * 200,
            "open source robotics"]}
        context = pipeline_main._interest_context([], policy)
        self.assertIn("marine biology", context)
        self.assertIn("open source robotics", context)
        self.assertFalse(any("person@" in item for item in context))
        self.assertFalse(any(len(item) > 160 for item in context))


class PackagingTests(Workspace):
    def test_reproducible_code_only_package_and_doctor(self):
        out = self.root / "portal-1.1.11.tar.gz"
        result = release_export.build_package(out, root=ROOT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], "1.1.11")
        with tarfile.open(out) as archive:
            names = archive.getnames()
            self.assertTrue(any(name.endswith("plugin.yaml") for name in names))
            self.assertFalse(any(name.startswith(".build-context") for name in names))
            self.assertFalse(any(name.startswith("tests/") for name in names))
            install = self.root / "installed"
            install.mkdir()
            archive.extractall(install)
        doctor = release_export.doctor(install)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["version"], "1.1.11")
        self.assertEqual(doctor["identity"], "hermes-proactive-secretary")




class LastMileOutageRecoveryTests(Workspace):
    def _ctx(self, response=None, error=None):
        class Llm:
            def __init__(self):
                self.calls = []

            def complete_structured(self, **kwargs):
                self.calls.append(kwargs)
                if error is not None:
                    raise error
                return response

        ctx = SimpleNamespace(llm=Llm())
        native_runtime.bind_model_lane(ctx, allowed=True, reason="test")
        native_runtime.bind_health_store(ctx, self.store, SeededClock(T0.isoformat()))
        return ctx

    def _facts(self):
        return {
            "policy": "configured_only", "task": "proactive_secretary",
            "stop": "assigned_routes_exhausted", "native_fallback_attempted": False,
            "attempts": [
                {"role": "primary", "result": "timeout", "reason": "bounded"},
                {"role": "fallback", "result": "rate_limit", "reason": "bounded"},
            ],
        }

    def test_successful_route_recovers_then_new_outage_rearms_one_new_notice(self):
        first = modelhealth.consume_terminal_outcome(self.store, self._facts(), now=T0)
        first_id = first["incident"]["id"]
        claimed = modelhealth.claim_notice(self.store, now=T0, attention_fn=lambda: True)
        self.assertTrue(claimed["eligible"])
        modelhealth.mark_notice_delivered(
            self.store, incident_id=claimed["incident_id"],
            notice_token=claimed["notice_token"], now=T0)

        ctx = self._ctx(response=SimpleNamespace(
            parsed={"schema": 1, "action": "silence", "reason": "nothing material"}))
        from proactive_pilot.evaluator import NativeLlmEvaluator
        ev = NativeLlmEvaluator(ctx, dict(DEFAULT_POLICY))
        result = ev.evaluate(pipeline_main.fixture_packet("evt:x"), dict(DEFAULT_POLICY))
        self.assertEqual(result.action, "silence")
        recovered = modelhealth.status(self.store)
        self.assertEqual(recovered["incident"]["status"], "recovered")
        self.assertEqual(recovered["incident"]["recovery"]["success_role"], "route")

        second = modelhealth.consume_terminal_outcome(self.store, self._facts(), now=T0)
        self.assertNotEqual(second["incident"]["id"], first_id)
        self.assertEqual(second["notice"]["status"], "pending")
        new_claim = modelhealth.claim_notice(self.store, now=T0, attention_fn=lambda: True)
        self.assertTrue(new_claim["eligible"])
        self.assertEqual(new_claim["incident_id"], second["incident"]["id"])

    def test_planner_terminal_failure_feeds_the_common_incident(self):
        error = RuntimeError("provider secret")
        error.aux_route_outcome = self._facts()
        ctx = self._ctx(error=error)
        from proactive_pilot.planner import NativePlanner, PlannerError
        planner = NativePlanner(ctx=ctx)
        with self.assertRaises(PlannerError):
            planner.plan({"id": "w1", "version": 1, "public_topic": "robotics",
                          "scope": "public", "origin": "explicit"},
                         dict(DEFAULT_POLICY))
        state = modelhealth.status(self.store)
        self.assertEqual(state["incident"]["status"], "open")
        self.assertEqual(state["notice"]["status"], "pending")


class LastMileCliAcceptTests(Workspace):
    def test_registered_cli_accepts_a_tentative_opportunity_without_forgery(self):
        rec = R.create(
            self.store, desired_outcome="compare two suppliers",
            acceptance_criterion="Table is ready",
            provenance={"kind": "opportunity", "source_ref": "conversation:turn-9",
                        "conversation_ref": "conversation:turn-9", "at": T0.isoformat()},
            status=R.TENTATIVE, evidence_refs=["conversation:turn-9"],
            due_at=(T0 + timedelta(hours=1)).isoformat())
        out = responsibilities.dispatch({
            "action": "accept", "responsibility_id": rec["id"],
            "evidence_quote": "yes, please do that"})
        accepted = out["responsibility"]
        self.assertEqual(accepted["id"], rec["id"])
        self.assertEqual(accepted["status"], R.ACCEPTED)
        self.assertEqual(accepted["generation"], rec["generation"] + 1)
        self.assertEqual(accepted["provenance"]["kind"], "user_commitment")
        self.assertTrue(accepted["provenance"]["source_ref"].startswith("cli-confirmation:"))
        self.assertEqual(accepted["provenance"]["conversation_ref"], "")

    def test_cli_confirmation_scope_is_preparable_end_to_end(self):
        rec = R.create(
            self.store, desired_outcome="compare two suppliers",
            acceptance_criterion="Table is ready",
            provenance={"kind": "opportunity", "source_ref": "conversation:turn-9",
                        "conversation_ref": "conversation:turn-9", "at": T0.isoformat()},
            status=R.TENTATIVE, evidence_refs=["cli-confirmation:invocation"],
            due_at=(T0 - timedelta(minutes=1)).isoformat())
        out = responsibilities.dispatch({
            "action": "accept", "responsibility_id": rec["id"],
            "evidence_quote": "yes, please do that"})
        accepted = out["responsibility"]
        self.assertEqual(accepted["status"], R.ACCEPTED)
        policy = dict(DEFAULT_POLICY, preparation_enabled=True,
                      preparation_source_consent=False,
                      preparation_scopes=["conversation"])
        wake = P.preflight(self.store, policy=policy, prefs={}, config_present=True,
                           now=T0, source_loader=None)
        self.assertTrue(wake["wake"], wake.get("reason"))
        self.assertEqual(wake["claim"]["id"], rec["id"])


class LastMilePreparationFactsTests(Workspace):
    def _policy(self, **extra):
        return dict(DEFAULT_POLICY, preparation_enabled=True,
                    preparation_source_consent=True,
                    preparation_scopes=["conversation", "mail"],
                    mail_enabled=True, mail_accounts=["work"],
                    **extra)

    def _record(self, ref="mail:msg-1"):
        return R.create(
            self.store, desired_outcome="Summarise the renewal",
            acceptance_criterion="Summary ready",
            provenance={"kind": "user_commitment", "source_ref": "conversation:c1",
                        "conversation_ref": "conversation:c1", "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:c1", ref],
            facts=[{"ref": "conversation:c1", "text": "Please summarise it.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}])

    def test_authorised_cache_supplies_snapshot_and_citation_validation(self):
        result = SourceResult("mail:work", "ok", count=1, checked_at=T0.isoformat(), items=[{
            "ref": "mail:msg-1", "subject": "Renewal due", "version": "v1",
            "date": T0.isoformat()}])
        self.assertEqual(evidence_cache.record_results(
            self.store, [result], policy=self._policy(), now=T0), 1)
        rec = self._record()
        policy = self._policy()
        loader = lambda r: evidence_cache.read_facts(
            self.store, scopes={"mail"}, refs={"mail:msg-1"}, now=T0)
        out = P.preflight(self.store, policy=policy, prefs={}, config_present=True,
                          now=T0, source_loader=loader)
        self.assertTrue(out["wake"])
        brief = R.claimed_brief(self.store, rec["id"], out["claim"]["id"] if False else
                                out["context"]["data"]["claim_token"], now=T0)
        self.assertTrue(any(f["ref"] == "mail:msg-1" for f in brief["facts"]))
        written = P.write_artifact(
            self.store, responsibility_id=rec["id"],
            claim_token=out["context"]["data"]["claim_token"],
            generation=out["context"]["data"]["generation"],
            source_version=out["context"]["data"]["source_version"],
            title="Renewal summary", content="Due soon [[ref: mail:msg-1]]",
            evidence_refs=["mail:msg-1"], artifact_root=self.root / "artifacts",
            gate_fn=lambda: True, now=T0)
        self.assertTrue(written["artifact_ref"].startswith("artifact:"))

    def test_stale_and_disabled_sources_are_needs_evidence_with_zero_reads(self):
        stale = SourceResult("mail:work", "ok", count=1,
                             checked_at=(T0 - timedelta(days=3)).isoformat(), items=[{
                                 "ref": "mail:msg-1", "subject": "Renewal due",
                                 "date": (T0 - timedelta(days=3)).isoformat()}])
        evidence_cache.record_results(self.store, [stale], policy=self._policy(), now=T0)
        rec = self._record()
        out = P.preflight(
            self.store, policy=self._policy(), prefs={}, config_present=True, now=T0,
            source_loader=lambda r: evidence_cache.read_facts(
                self.store, scopes={"mail"}, refs={"mail:msg-1"}, now=T0))
        self.assertFalse(out["wake"])
        self.assertIn("needs authorised evidence", out["reason"])
        self.assertEqual(R.inspect(self.store, rec["id"])["status"], R.ACCEPTED)
        disabled = dict(self._policy(), mail_enabled=False)
        self.assertEqual(evidence_cache.scope_enabled(disabled, "mail"), False)
        self.assertEqual(evidence_cache.record_results(
            self.store, [stale], policy=disabled, now=T0), 0)


class LastMileInterestsTests(Workspace):
    def test_approved_interests_enter_real_planner_queries_and_cache(self):
        from proactive_pilot import adaptive
        from proactive_pilot.planner import FakePlanner
        calls = []

        def planner_factory(watch, policy):
            calls.append(dict(watch))
            return {"schema": 1, "queries": ["marine biology news"],
                    "scope": "public", "semantic": True, "provenance": "model"}

        planner = FakePlanner(planner_factory)
        policy = dict(DEFAULT_POLICY, adaptive_mode="active", feeds_enabled=True,
                      public_interests=["marine biology", "mail me at a@b.test"])
        first = adaptive.prepare_adaptive_plan(
            policy, SeededClock(T0.isoformat()), store=self.store, planner=planner, now=T0)
        self.assertEqual(first["origin"], "approved_interest")
        self.assertEqual(first["plan"]["queries"], ["marine biology news"])
        self.assertEqual(calls[0]["public_topic"], "marine biology")
        second = adaptive.prepare_adaptive_plan(
            policy, SeededClock(T0.isoformat()), store=self.store, planner=planner, now=T0)
        self.assertTrue(second.get("cached"))
        self.assertEqual(len(calls), 1)


class LastMileOnboardingControlTests(Workspace):
    def test_existing_effective_config_is_not_asked_again(self):
        cfg = {"schema": 1, "version": 1, "timezone": "Europe/Berlin",
               "notification": {"destination": "local", "daily_normal_cap": 7,
                                "quiet_start_hour": 22, "quiet_end_hour": 7},
               "sources": {"mail": True, "calendar": False, "tasks": False},
               "mail": {"accounts": ["work"]}}
        from proactive_pilot.userconfig import LoadedConfig, validate_config
        loaded = LoadedConfig(config=validate_config(cfg), present=True, source="native")
        svc = onboarding.OnboardingService(Ctx(), config_resolver=lambda: loaded)
        info = svc.inspect()
        self.assertEqual(info["next_step"], "autonomy")
        self.assertEqual(info["state"]["settings"]["timezone"], "Europe/Berlin")
        self.assertIn("grant:mail", info["state"]["decided"])

    def test_slash_setup_shows_the_real_question_and_mandatory_code(self):
        ctx = Ctx()
        svc = onboarding.OnboardingService(ctx)
        text = conversation.command_text(ctx, svc, "setup")
        self.assertIn("Next question:", text)
        self.assertIn("sec-cont-", text)
        self.assertIn("AND this continuation code", text)
        self.assertNotIn("Reply with your timezone to begin", text)

    def test_chat_pause_resume_and_cancel_setup_are_honest(self):
        ctx = Ctx()
        svc = onboarding.OnboardingService(ctx)
        paused = conversation.command_text(ctx, svc, "pause")
        self.assertIn("paused", paused.lower())
        self.assertIn("also paused", paused)
        self.assertNotIn("unaffected", paused)
        resumed = conversation.command_text(ctx, svc, "resume")
        self.assertIn("no longer paused", resumed.lower())
        self.assertIn("Delivery remains unconfirmed", resumed)
        cancelled = conversation.command_text(ctx, svc, "cancel-setup")
        self.assertIn("cancelled", cancelled.lower())
        self.assertIn("does not pause", conversation.command_text(ctx, svc, "help"))


class LastMileArtifactPrivacyTests(Workspace):
    def test_sensitive_title_never_reaches_filename_ref_or_public_handle(self):
        rec = R.create(
            self.store, desired_outcome="Prepare", acceptance_criterion="Ready",
            provenance={"kind": "user_commitment", "source_ref": "conversation:s1",
                        "conversation_ref": "conversation:s1", "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:s1"],
            facts=[{"ref": "conversation:s1", "text": "Prepare", "provenance": "conversation",
                    "observed_at": T0.isoformat()}])
        claim = R.claim_due(self.store, now=T0)
        out = P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="Acquisition of Secret Merger Alpha", content="Draft",
            evidence_refs=["conversation:s1"], artifact_root=self.root / "artifacts",
            gate_fn=lambda: True, now=T0)
        public = P.public_artifact_ref(out["artifact_ref"])
        self.assertNotIn("Secret", out["artifact_ref"])
        self.assertNotIn("Merger", out["artifact_ref"])
        self.assertNotIn("Secret", public)
        resolved = P.resolve_artifact_handle(self.store, public)
        self.assertEqual(resolved["artifact_ref"], out["artifact_ref"])
        self.assertEqual(resolved["hash"], out["hash"])

    def test_old_title_bearing_layout_remains_readable_but_public_handle_is_opaque(self):
        root = self.root / "old"
        root.mkdir()
        old = root / "resp_x-g1-private-title-0123456789abcdef.md"
        old.write_text("old draft", encoding="utf-8")
        path = P.artifact_path(root, f"artifact:resp_x:{old.name}")
        self.assertEqual(path.read_text(encoding="utf-8"), "old draft")
        handle = P.public_artifact_ref(f"artifact:resp_x:{old.name}")
        self.assertNotIn("private-title", handle)


class LastMilePackagingTruthTests(Workspace):
    def test_reproducible_archive_bytes_and_native_doctor(self):
        export = release_export
        first = self.root / "a.tar.gz"
        second = self.root / "b.tar.gz"
        one = export.build_package(first, root=ROOT)
        two = export.build_package(second, root=ROOT)
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(one["sha256"],
                         __import__("hashlib").sha256(first.read_bytes()).hexdigest())
        self.assertNotEqual(one["sha256"], one["contents_sha256"])
        install = self.root / "install"
        install.mkdir()
        import tarfile
        with tarfile.open(first) as archive:
            archive.extractall(install)
        doctor = export.doctor(install)
        self.assertTrue(doctor["ok"], doctor)
        self.assertTrue(doctor["entrypoint_import_registration"]["ok"])
        self.assertTrue(doctor["native_host_doctor"]["ok"])
        self.assertFalse((install / "FINALIZATION_PREPARATION.md").exists())
        self.assertFalse((install / "docs" / "FINALIZATION_PREPARATION.md").exists())

    def test_invalid_policy_timezone_falls_back_to_neutral_utc(self):
        from zoneinfo import ZoneInfo
        from proactive_pilot.policy import policy_zone
        self.assertEqual(policy_zone({"timezone": "Not/AZone"}), ZoneInfo("UTC"))


class LastMileReadyNoticeTruthTests(Workspace):
    def test_production_notice_uses_loaded_config_state_and_opaque_ref(self):
        rec = R.create(
            self.store, desired_outcome="Prepare", acceptance_criterion="Ready",
            provenance={"kind": "user_commitment", "source_ref": "conversation:s1",
                        "conversation_ref": "conversation:s1", "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:s1"],
            facts=[{"ref": "conversation:s1", "text": "Prepare", "provenance": "conversation",
                    "observed_at": T0.isoformat()}])
        claim = R.claim_due(self.store, now=T0)
        P.write_artifact(
            self.store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="Private Alpha", content="Draft [[ref: conversation:s1]]",
            evidence_refs=["conversation:s1"], artifact_root=self.root / "artifacts",
            gate_fn=lambda: True, now=T0)
        policy = dict(DEFAULT_POLICY, preparation_enabled=True,
                      preparation_scopes=["conversation"], _config_present=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pipeline_main._maybe_emit_ready_notice(
                SimpleNamespace(native_ctx=None, check=False, dry_run=False),
                self.store, {}, policy, {}, SeededClock(T0.isoformat()))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Prepare", text)
        self.assertIn("acceptance note", text)
        self.assertNotIn("artifact:", text)
        self.assertNotIn(rec["id"], text)
        self.assertNotIn("Private Alpha", text)
        self.assertNotIn("No external action", text)
        missing = dict(policy, _config_present=False)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            rc2 = pipeline_main._maybe_emit_ready_notice(
                SimpleNamespace(native_ctx=None, check=False, dry_run=False),
                self.store, {}, missing, {}, SeededClock(T0.isoformat()))
        self.assertIsNone(rc2)
        self.assertEqual(out2.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
