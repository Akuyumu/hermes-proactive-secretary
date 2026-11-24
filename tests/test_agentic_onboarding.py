"""Conversational onboarding, bounded preparation and exact approval seams."""
from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SCRATCH = tempfile.mkdtemp(prefix="secretary-agentic-")
os.environ["HERMES_HOME"] = os.path.join(_SCRATCH, "home")
os.environ["HERMES_PROACTIVE_SECRETARY_DATA"] = os.path.join(_SCRATCH, "profile-a")
atexit.register(shutil.rmtree, _SCRATCH, True)
sys.path.insert(0, str(_ROOT / "src"))


def _load_package():
    name = "hermes_proactive_secretary_agentic"
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(_ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
onboarding = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_onboarding")
conversation = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_conversation")
responsibilities = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_responsibilities")
approvals = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_approvals")
userconfig = importlib.import_module("proactive_pilot.userconfig")
native_runtime = importlib.import_module("proactive_pilot.native_runtime")


class _FakeRoute:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed)


class _FakeLane:
    available = True

    def __init__(self, parsed):
        self.route = _FakeRoute(parsed)


def _synthetic_lane(text="Synthetic bounded draft: renew the permit before Friday."):
    return _FakeLane({
        "title": "Renewal checklist draft",
        "content": text,
        "format": "markdown",
        "evidence_refs": [],
    })


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class StrictCtx:
    def __init__(self, *, config=None, state=None):
        self.settings = {}
        if config is not None:
            self.settings["user_config"] = json.loads(json.dumps(config))
        self.state = state if state is not None else MemoryState()
        self.tool_calls = []
        self.commands = {}
        self.hooks = {}
        self.saved_config = []
        self.plugin_id = "hermes-proactive-secretary"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value
        self.saved_config.append((key, value))

    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="", emoji="",
                      override=False):
        self.tool_calls.append({"name": name, "schema": schema, "handler": handler,
                                "description": description})

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_skill(self, name, path, description=""):
        self.skills = {name: (path, description)}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands = {name: (help, setup_fn, handler_fn, description)}


def _registered(config=None):
    ctx = StrictCtx(config=config)
    _PKG._register_conversation(ctx)
    tools = {row["name"]: row["handler"] for row in ctx.tool_calls}
    return ctx, tools


def _tool(tools, name, payload):
    return json.loads(tools[name](payload))


def _config(**overrides):
    config = userconfig.default_config()
    config["timezone"] = overrides.pop("timezone", "Europe/London")
    config["notification"]["destination"] = overrides.pop("destination", "")
    config["sources"].update(overrides.pop("sources", {}))
    preparation = overrides.pop("preparation", None)
    if preparation is not None:
        config["preparation"].update(preparation)
    if overrides:
        config.update(overrides)
    return userconfig.validate_config(config)


class ConversationOnboardingTests(unittest.TestCase):
    def test_first_turn_starts_interview_and_resumes_without_commands(self):
        ctx, tools = _registered()
        hook = ctx.hooks["pre_llm_call"]
        first = hook(platform="telegram", user_message="hello", session_id="s-1")
        self.assertIn("Next question:", first["context"])
        self.assertIn("timezone", first["context"].lower())
        self.assertNotIn("/secretary setup", first["context"])
        self.assertIn("onboarding_invitation", ctx.state.data)

        # The same conversation continues with the next genuine question and
        # never asks the user to type a command.
        again = hook(platform="telegram", user_message="ok", session_id="s-1")
        self.assertIn("onboarding continuation", again["context"].lower())

        _tool(tools, "secretary_onboarding", {
            "action": "update", "settings": {"timezone": "Europe/London"},
            "explicit_consent": False})
        _tool(tools, "secretary_onboarding", {
            "action": "update", "destination": "telegram", "explicit_consent": False})
        consent_text = "Yes, enable private preparation for the conversation scope."
        hook(platform="telegram", user_message=consent_text, session_id="s-1",
             chat_type="dm")
        _tool(tools, "secretary_onboarding", {
            "action": "update", "session_id": "s-1",
            "evidence_quote": consent_text,
            "settings": {"daily_allowance": 3, "quiet_start_hour": 22,
                         "quiet_end_hour": 7, "autonomy_mode": "prepare_private",
                         "preparation_scopes": ["conversation"],
                         "preparation_source_consent": False},
            "explicit_consent": True})
        state = _tool(tools, "secretary_onboarding", {"action": "inspect"})["state"]
        self.assertEqual(state["settings"]["autonomy_mode"], "prepare_private")
        self.assertTrue(state["settings"]["preparation_scopes"])

        # A fresh profile-level registration does not invite again, and a
        # completed interview never reopens itself.
        confirm_text = "Yes, confirm and save the onboarding settings."
        hook(platform="telegram", user_message=confirm_text, session_id="s-1",
             chat_type="dm")
        result = _tool(tools, "secretary_onboarding", {
            "action": "confirm", "session_id": "s-1",
            "evidence_quote": confirm_text,
            "user_confirmed": True, "apply_settings": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(ctx.settings["user_config"]["preparation"]["enabled"], True)
        self.assertEqual(ctx.settings["user_config"]["preparation"]["scopes"],
                         ["conversation"])
        self.assertIsNone(hook(platform="telegram", user_message="hello again",
                               session_id="s-1"))

    def test_completed_or_disabled_profile_never_duplicates_invite(self):
        ctx, tools = _registered(_config())
        hook = ctx.hooks["pre_llm_call"]
        first = hook(platform="cli", user_message="hi", session_id="s-2")
        self.assertIsNotNone(first)
        self.assertIsNone(hook(platform="cli", user_message="hi", session_id="s-9"))
        ctx.state.data["onboarding_state"] = {
            **onboarding.default_state(),
            "status": "ready",
            "settings": {**onboarding.default_state()["settings"],
                         "timezone": "UTC"},
        }
        self.assertIsNone(hook(platform="cli", user_message="hi", session_id="s-2"))


class CaptureAndPreparationTests(unittest.TestCase):
    def setUp(self):
        self.lane = _synthetic_lane()
        self._lane_patch = mock.patch.object(
            native_runtime, "build_model_lane", return_value=self.lane)
        self._lane_patch.start()
        self.ctx, self.tools = _registered(_config(
            preparation={"enabled": True, "scopes": ["conversation"],
                         "source_consent": False}))

    def tearDown(self):
        self._lane_patch.stop()

    def _human(self, text, session="s-1"):
        hook = self.ctx.hooks["pre_llm_call"]
        return hook(platform="telegram", user_message=text, session_id=session,
                    chat_type="dm")

    def test_clear_foreground_task_is_captured_and_privately_prepared(self):
        text = "Please prepare the renewal checklist before Friday."
        self._human(text)
        result = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "commitment", "session_id": "s-1",
            "evidence_quote": text,
            "desired_outcome": "Prepare the renewal checklist",
            "acceptance_criterion": "Checklist covers every requirement"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["responsibility"]["status"], "accepted")
        self.assertEqual(result["responsibility"]["provenance"]["quote"], text)
        self.assertTrue(result["prepared"]["ok"], result["prepared"])
        self.assertTrue(result["prepared"]["queued"])
        self.assertEqual(result["prepared"]["external_effect"], "none")
        from proactive_pilot import preparation
        artifact = preparation.artifact_path(None, result["prepared"]["artifact_ref"])
        self.assertTrue(artifact.is_file())
        self.assertIn("Synthetic bounded draft",
                      artifact.read_text(encoding="utf-8"))
        self.assertEqual(len(self.lane.route.calls), 1)

    def test_thanks_and_questions_are_not_commitments(self):
        self._human("thanks!")
        context = self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message="what time is it?", session_id="s-1")
        self.assertTrue(context is None or "responsibility" not in context["context"].lower())
        failed = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "commitment", "session_id": "s-1",
            "evidence_quote": "thanks!", "desired_outcome": "Invented task"})
        self.assertFalse(failed["ok"])

    def test_inferred_opportunity_is_tentative_private_and_not_due(self):
        text = "The conference deadline is mentioned in this chat."
        self._human(text)
        result = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "opportunity", "session_id": "s-1",
            "evidence_quote": text,
            "desired_outcome": "Consider a conference checklist"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["responsibility"]["status"], "tentative")
        self.assertFalse(result["prepared"]["accepted_obligation"])
        self.assertTrue(result["prepared"]["private"])
        root = Path(_SCRATCH) / "profile-a" / "preparation" / "artifacts" / "tentative"
        files = list(root.glob("*.md")) if root.exists() else []
        self.assertTrue(files)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("TENTATIVE", body)
        self.assertIn("Synthetic bounded draft", body)
        from proactive_pilot import responsibilities as R
        from proactive_pilot.clock import Clock
        from proactive_pilot.state import StateStore
        store = StateStore(Path(_SCRATCH) / "profile-a" / "var", Clock())
        self.assertEqual(R.due_records(store), [])

    def test_no_consent_means_no_draft(self):
        ctx, tools = _registered(_config(
            preparation={"enabled": False, "scopes": [], "source_consent": False}))
        ctx.hooks["pre_llm_call"](platform="telegram",
                                  user_message="Please prepare the checklist.",
                                  session_id="s-3")
        result = _tool(tools, "secretary_responsibility", {
            "action": "capture", "kind": "commitment", "session_id": "s-3",
            "evidence_quote": "Please prepare the checklist.",
            "desired_outcome": "Prepare the checklist"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["prepared"].get("prepared", False))
        self.assertIn("preparation disabled", result["prepared"]["reason"])


class FinalIntegrationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._lane_patch = None
        import shutil as _shutil
        _shutil.rmtree(Path(_SCRATCH) / "profile-a" / "preparation" / "artifacts" / "tentative",
                       ignore_errors=True)

    def tearDown(self):
        if self._lane_patch is not None:
            self._lane_patch.stop()

    def _ctx(self, on_call=None):
        route = _FakeRoute({
            "title": "Draft title",
            "content": "Useful injected bounded draft body.",
            "format": "markdown",
            "evidence_refs": [],
        })
        if on_call is not None:
            original = route.complete_structured
            def call(**kwargs):
                original(**kwargs)
                on_call()
                return SimpleNamespace(parsed=route.parsed)
            route.complete_structured = call
        lane = _FakeLane({})
        lane.route = route
        self._lane_patch = mock.patch.object(
            native_runtime, "build_model_lane", return_value=lane)
        self._lane_patch.start()
        return _registered(_config(
            preparation={"enabled": True, "scopes": ["conversation"],
                         "source_consent": False})) + (route,)

    def test_tentative_request_is_json_mode_and_shared_bounded_budget(self):
        from secretary.secretary_preparation import DRAFT_MAX_TOKENS, DRAFT_TIMEOUT_SECONDS
        ctx, tools, route = self._ctx()
        text = "The conference deadline is mentioned in this chat."
        ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                  session_id="s-bound", chat_type="dm")
        result = _tool(tools, "secretary_responsibility", {
            "action": "capture", "kind": "opportunity", "session_id": "s-bound",
            "evidence_quote": text, "desired_outcome": "Consider a checklist"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["prepared"]["ok"], result["prepared"])
        call = route.calls[-1]
        self.assertNotIn("json_schema", call)
        self.assertEqual(call["max_tokens"], DRAFT_MAX_TOKENS)
        self.assertEqual(call["timeout"], DRAFT_TIMEOUT_SECONDS)
        self.assertEqual(call["schema_name"], "secretary_tentative_draft")

    def test_revoke_or_pause_during_model_call_denies_write(self):
        def revoke():
            config = json.loads(json.dumps(self.ctx.settings["user_config"]))
            config["preparation"]["enabled"] = False
            self.ctx.settings["user_config"] = config
        self.ctx, self.tools, _route = self._ctx(on_call=revoke)
        text = "The conference deadline is mentioned in this chat."
        self.ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                       session_id="s-revoke", chat_type="dm")
        result = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "opportunity", "session_id": "s-revoke",
            "evidence_quote": text, "desired_outcome": "Consider a checklist"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["prepared"].get("ok", False))
        self.assertNotIn("secretary_tentative_drafts", self.ctx.state.data)
        root = Path(_SCRATCH) / "profile-a" / "preparation" / "artifacts" / "tentative"
        self.assertEqual(list(root.glob("*.md")) if root.exists() else [], [])

    def test_changed_human_turn_during_model_call_denies_write(self):
        def change_turn():
            rows = self.ctx.state.data["responsibility_human_turn"]
            rows[-1]["text"] = "A completely different later message."
            rows[-1]["ref"] = "conversation:changed"
        self.ctx, self.tools, _route = self._ctx(on_call=change_turn)
        text = "The conference deadline is mentioned in this chat."
        self.ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                       session_id="s-turn", chat_type="dm")
        result = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "opportunity", "session_id": "s-turn",
            "evidence_quote": text, "desired_outcome": "Consider a checklist"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["prepared"].get("ok", False))
        self.assertNotIn("secretary_tentative_drafts", self.ctx.state.data)

    def test_malformed_tentative_index_fails_closed_before_model(self):
        self.ctx, self.tools, route = self._ctx()
        self.ctx.state.data["secretary_tentative_drafts"] = {"not": "a list"}
        text = "The conference deadline is mentioned in this chat."
        self.ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                       session_id="s-bad", chat_type="dm")
        result = _tool(self.tools, "secretary_responsibility", {
            "action": "capture", "kind": "opportunity", "session_id": "s-bad",
            "evidence_quote": text, "desired_outcome": "Consider a checklist"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["prepared"].get("ok", False))
        self.assertEqual(route.calls, [])

    def test_confirmed_conversation_setup_arms_via_existing_engine(self):
        ctx, tools = _registered()
        _tool(tools, "secretary_onboarding", {
            "action": "update", "settings": {"timezone": "UTC"},
            "explicit_consent": False})
        _tool(tools, "secretary_onboarding", {
            "action": "update", "destination": "telegram", "explicit_consent": False})
        consent_text = "Yes, enable private preparation for the conversation scope."
        hook = ctx.hooks["pre_llm_call"]
        hook(platform="telegram", user_message=consent_text, session_id="s-1",
             chat_type="dm")
        _tool(tools, "secretary_onboarding", {
            "action": "update", "session_id": "s-1",
            "evidence_quote": consent_text,
            "settings": {"daily_allowance": 3, "quiet_start_hour": 22,
                         "quiet_end_hour": 7, "autonomy_mode": "prepare_private",
                         "preparation_scopes": ["conversation"],
                         "preparation_source_consent": False},
            "explicit_consent": True})
        confirm_text = "Yes, confirm and save the onboarding settings."
        hook(platform="telegram", user_message=confirm_text, session_id="s-1",
             chat_type="dm")
        confirmed = _tool(tools, "secretary_onboarding", {
            "action": "confirm", "session_id": "s-1", "evidence_quote": confirm_text,
            "user_confirmed": True, "apply_settings": True})
        self.assertEqual(confirmed.get("status"), "pending_verification", confirmed)
        self.assertFalse(confirmed["schedule_armed"])
        cli = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
        arm_text = "Yes, arm the schedule now."
        hook(platform="telegram", user_message=arm_text, session_id="s-1",
             chat_type="dm")

        def fake_setup(args):
            print(json.dumps({"ok": True, "jobs": [
                {"name": "proactive-secretary-monitoring", "action": "kept"},
                {"name": "proactive-secretary-reminders", "action": "kept"}],
                "deliver": args.deliver, "preparation": args.with_preparation}))
            return 0
        with mock.patch.object(cli, "setup_command", side_effect=fake_setup) as setup:
            armed = _tool(tools, "secretary_onboarding", {
                "action": "arm", "session_id": "s-1", "evidence_quote": arm_text,
                "user_confirmed": True})
        self.assertTrue(armed["ok"], armed)
        self.assertTrue(armed["schedule_armed"])
        self.assertEqual(armed["status"], "ready")
        kwargs = setup.call_args.args[0]
        self.assertEqual(kwargs.deliver, "telegram")
        self.assertFalse(kwargs.no_cron)
        self.assertTrue(kwargs.with_preparation)

    @staticmethod
    def _sent_values(args):
        if hasattr(args, "__dict__"):
            return list(vars(args).values())
        return list(args.values())

    def _portal_onboarding(self, portal):
        ctx, tools = _registered()
        ctx.settings.update(primary_provider="custom-provider",
                            primary_model="custom-model",
                            portal_cadence="6h")
        _tool(tools, "secretary_onboarding", {
            "action": "update", "settings": {"timezone": "UTC"},
            "explicit_consent": False})
        _tool(tools, "secretary_onboarding", {
            "action": "update", "destination": "telegram",
            "explicit_consent": False})
        portal_text = "Yes, enable private preparation and Portal for this profile."
        hook = ctx.hooks["pre_llm_call"]
        hook(platform="telegram", user_message=portal_text, session_id="s-portal",
             chat_type="dm")
        _tool(tools, "secretary_onboarding", {
            "action": "update", "session_id": "s-portal",
            "evidence_quote": portal_text,
            "settings": {"daily_allowance": 3, "quiet_start_hour": 22,
                         "quiet_end_hour": 7, "autonomy_mode": "prepare_private",
                         "preparation_scopes": ["conversation"],
                         "preparation_source_consent": False,
                         "portal_enabled": True,
                         "primary_provider": "custom-provider",
                         "primary_model": "custom-model"},
            "explicit_consent": True})
        preview = _tool(tools, "secretary_onboarding", {
            "action": "preview", "session_id": "s-portal"})
        self.assertTrue(preview.get("accepted", preview.get("ok")), preview)
        review = preview.get("review") or {}
        review_ref = (preview.get("review_ref") or review.get("review_ref")
                      or review.get("ref") or preview.get("ref"))
        self.assertTrue(review_ref, preview)
        confirm_text = "Yes, confirm and save the portal onboarding settings."
        hook(platform="telegram", user_message=confirm_text,
             session_id="s-portal", chat_type="dm")
        confirmed = _tool(tools, "secretary_onboarding", {
            "action": "confirm", "session_id": "s-portal",
            "evidence_quote": confirm_text, "user_confirmed": True,
            "apply_settings": True, "review_ref": review_ref})
        self.assertTrue(confirmed.get("ok", True), confirmed)
        arm_text = "Yes, arm the schedule and portal now."
        hook(platform="telegram", user_message=arm_text,
             session_id="s-portal", chat_type="dm")
        cli = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")

        def fake_setup(args):
            print(json.dumps({
                "ok": True,
                "jobs": [
                    {"name": "proactive-secretary-monitoring", "action": "kept"},
                    {"name": "proactive-secretary-reminders", "action": "kept"}],
                "portal": portal}))
            return 0
        with mock.patch.object(cli, "setup_command", side_effect=fake_setup) as setup:
            armed = _tool(tools, "secretary_onboarding", {
                "action": "arm", "session_id": "s-portal",
                "evidence_quote": arm_text, "user_confirmed": True})
        self.assertIsNotNone(setup.call_args, armed)
        return preview, armed, setup.call_args.args[0]

    def test_portal_endpoint_reviews_owner_model_and_marks_ready(self):
        preview, armed, sent = self._portal_onboarding(
            {"ok": True, "state": "active", "job_id": "abc"})
        portal = preview["planned_effective_settings"]["portal"]
        self.assertEqual((portal["provider"], portal["model"]),
                         ("custom-provider", "custom-model"))
        self.assertEqual(portal["schedule"], "0 */6 * * *")
        self.assertEqual(sent.portal_review, portal)
        self.assertIs(sent.with_portal, True)
        self.assertTrue(armed["portal_armed"], armed)
        self.assertEqual(armed["status"], "ready")
        self.assertEqual(armed["portal"]["status"], "active")

    def test_portal_not_ok_does_not_mark_ready_even_when_active(self):
        _preview, armed, _sent = self._portal_onboarding(
            {"ok": False, "state": "active", "job_id": "abc"})
        self.assertFalse(armed["portal_armed"], armed)
        self.assertNotEqual(armed["status"], "ready")


class ExactApprovalTests(unittest.TestCase):
    def setUp(self):
        self.ctx, self.tools = _registered(_config(
            destination="telegram", sources={"mail": True}))
        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message="Please send the draft to the team.",
            session_id="s-1", chat_type="dm")

    def _request(self, effect="Send the private draft to the team chat"):
        return _tool(self.tools, "secretary_responsibility", {
            "action": "request_approval", "proposal_kind": "send",
            "proposal_effect": effect,
            "proposal_scope": "external mail", "ttl_minutes": 30,
            "session_id": "s-1"})

    def test_one_deduplicated_expiring_question_and_exact_yes(self):
        first = self._request()
        second = self._request()
        self.assertTrue(second["deduplicated"])
        ref = first["approval"]["ref"]
        self.assertEqual(second["approval"]["ref"], ref)

        silence = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": "yes"})
        self.assertFalse(silence["ok"])

        unrelated = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": "yes send the draft to the team"})
        self.assertFalse(unrelated["ok"])

        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message=f"yes {ref}",
            session_id="s-1", chat_type="dm")
        approved = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": f"yes {ref}"})
        self.assertTrue(approved["ok"], approved)
        self.assertEqual(approved["grants"], "this_proposal_only")
        self.assertEqual(approved["execution"], "not_performed")
        again = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": f"yes {ref}"})
        self.assertFalse(again["ok"])

    def test_expired_question_and_revoked_scope_have_zero_effect(self):
        first = self._request()
        ref = first["approval"]["ref"]
        with mock.patch.object(approvals.time, "time",
                               return_value=first["approval"]["expires_at"] + 1):
            expired = _tool(self.tools, "secretary_responsibility", {
                "action": "approve", "approval_ref": ref, "session_id": "s-1",
                "evidence_quote": f"yes {ref}"})
        self.assertFalse(expired["ok"])

        second = self._request()
        ref2 = second["approval"]["ref"]
        config = json.loads(json.dumps(self.ctx.settings["user_config"]))
        config["sources"]["mail"] = False
        self.ctx.settings["user_config"] = config
        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message=f"yes {ref2}",
            session_id="s-1", chat_type="dm")
        revoked = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref2, "session_id": "s-1",
            "evidence_quote": f"yes {ref2}"})
        self.assertFalse(revoked["ok"])


    def test_negated_quoted_conditional_and_unrelated_replies_deny(self):
        for text in (
                "Do not approve {ref}",
                "No, do not approve {ref}",
                '"yes {ref}"',
                "yes {ref} if you think it is safe",
                "yes {ref} but wait",
                "yes please"):
            first = self._request(effect=f"Proposal for {text[:20]}")
            ref = first["approval"]["ref"]
            self.ctx.hooks["pre_llm_call"](
                platform="telegram", user_message=text.format(ref=ref),
                session_id="s-1", chat_type="dm")
            denied = _tool(self.tools, "secretary_responsibility", {
                "action": "approve", "approval_ref": ref, "session_id": "s-1",
                "evidence_quote": text.format(ref=ref)})
            self.assertFalse(denied["ok"], (text, denied))

    def test_forged_reply_context_does_not_allow_a_plain_yes(self):
        first = self._request("Send the verified draft")
        ref = first["approval"]["ref"]
        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message="yes", session_id="s-1",
            chat_type="dm")
        denied = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": "yes",
            "reply_context": f"Approval needed: send the verified draft {ref}"})
        self.assertFalse(denied["ok"], denied)
        self.assertIn("reference", denied["error"])

    def test_different_session_old_turn_and_unrelated_ref_deny(self):
        first = self._request("Send proposal one")
        ref = first["approval"]["ref"]
        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message=f"yes {ref}", session_id="s-2",
            chat_type="dm")
        different = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-2",
            "evidence_quote": f"yes {ref}"})
        self.assertFalse(different["ok"])

        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message=f"yes {ref}", session_id="s-1",
            chat_type="dm")
        rows = self.ctx.state.data["responsibility_human_turn"]
        rows[-1]["at"] = float(first["approval"]["created_at"]) - 10
        old = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": f"yes {ref}"})
        self.assertFalse(old["ok"])

        second = self._request("Send proposal two")
        other_ref = second["approval"]["ref"]
        self.ctx.hooks["pre_llm_call"](
            platform="telegram", user_message=f"yes {other_ref}", session_id="s-1",
            chat_type="dm")
        unrelated = _tool(self.tools, "secretary_responsibility", {
            "action": "approve", "approval_ref": ref, "session_id": "s-1",
            "evidence_quote": f"yes {other_ref}", "reply_context": other_ref})
        self.assertFalse(unrelated["ok"])


class GateFailClosedTests(unittest.TestCase):
    def setUp(self):
        prefs = Path(_SCRATCH) / "profile-a" / "preferences.json"
        prefs.unlink(missing_ok=True)
        self.addCleanup(prefs.unlink, missing_ok=True)
        self.lane = _synthetic_lane()
        self._lane_patch = mock.patch.object(
            native_runtime, "build_model_lane", return_value=self.lane)
        self._lane_patch.start()

    def tearDown(self):
        self._lane_patch.stop()

    def test_review_only_blocks_even_when_legacy_preparation_enabled(self):
        ctx, tools = _registered(_config(
            preparation={"enabled": True, "scopes": ["conversation"],
                         "source_consent": False, "mode": "review_only"}))
        text = "Please prepare the renewal checklist."
        ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                  session_id="s-r", chat_type="dm")
        result = _tool(tools, "secretary_responsibility", {
            "action": "capture", "kind": "commitment", "session_id": "s-r",
            "evidence_quote": text, "desired_outcome": "Prepare the checklist"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["prepared"].get("ok", False))
        self.assertIn("review-only", result["prepared"]["reason"])
        self.assertEqual(self.lane.route.calls, [])

    def test_corrupt_preferences_fail_closed_before_model(self):
        ctx, tools = _registered(_config(
            preparation={"enabled": True, "scopes": ["conversation"],
                         "source_consent": False}))
        prefs = Path(_SCRATCH) / "profile-a" / "preferences.json"
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text("{not valid json", encoding="utf-8")
        text = "Please prepare the renewal checklist."
        ctx.hooks["pre_llm_call"](platform="telegram", user_message=text,
                                  session_id="s-c", chat_type="dm")
        result = _tool(tools, "secretary_responsibility", {
            "action": "capture", "kind": "commitment", "session_id": "s-c",
            "evidence_quote": text, "desired_outcome": "Prepare the checklist"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["prepared"].get("ok", False))
        self.assertIn("FeedbackError", result["prepared"]["reason"])
        self.assertEqual(self.lane.route.calls, [])


class OrdinaryRunnerConsumerTests(unittest.TestCase):
    def test_preflight_uses_the_validated_config_and_gates_before_claim(self):
        from proactive_pilot import preparation, responsibilities as R
        from proactive_pilot.clock import SeededClock
        from proactive_pilot.policy import load_policy
        from proactive_pilot.state import StateStore
        root = Path(tempfile.mkdtemp(prefix="secretary-runner-", dir=_SCRATCH))
        clock = SeededClock("2026-10-21T10:00:00+00:00")
        config = userconfig.validate_config({
            "schema": 1, "version": 1, "preparation": {
                "enabled": True, "scopes": ["conversation"],
                "source_consent": False}})
        policy = userconfig.apply_user_config(load_policy(None), config)
        policy["_config_present"] = True
        prefs = {"explicit": [], "inferred": [], "snoozes": [],
                 "paused": {"active": False}}
        store = StateStore(root / "var", clock)
        R.create(
            store,
            desired_outcome="Prepare the checklist",
            acceptance_criterion="Checklist is complete",
            provenance={"kind": "user_commitment",
                        "source_ref": "conversation:turn-1",
                        "conversation_ref": "conversation:turn-1",
                        "at": clock.now().isoformat(),
                        "quote": "Please prepare the checklist."},
            status=R.ACCEPTED, now=clock.now())
        out = preparation.preflight(
            store, policy=policy, prefs=prefs, config_present=True,
            now=clock.now())
        self.assertTrue(out["wake"], out)
        self.assertEqual(out["context"]["responsibility_id"]
                         if "responsibility_id" in out["context"] else
                         out["claim"]["id"], out["claim"]["id"])

        disabled = userconfig.validate_config({
            "schema": 1, "version": 1, "preparation": {
                "enabled": False, "scopes": [], "source_consent": False}})
        policy2 = userconfig.apply_user_config(load_policy(None), disabled)
        policy2["_config_present"] = True
        store2 = StateStore(root / "var2", clock)
        R.create(
            store2,
            desired_outcome="Prepare another checklist",
            acceptance_criterion="Checklist is complete",
            provenance={"kind": "user_commitment",
                        "source_ref": "conversation:turn-2",
                        "conversation_ref": "conversation:turn-2",
                        "at": clock.now().isoformat(),
                        "quote": "Please prepare another checklist."},
            status=R.ACCEPTED, now=clock.now())
        blocked = preparation.preflight(
            store2, policy=policy2, prefs=prefs, config_present=True,
            now=clock.now())
        self.assertFalse(blocked["wake"])
        self.assertEqual(blocked["reason"], "preparation disabled")
        self.assertEqual(R.inspect(store2, R.list_records(store2)[0]["id"]).get(
            "claim", {}).get("token", ""), "")


if __name__ == "__main__":
    unittest.main()
