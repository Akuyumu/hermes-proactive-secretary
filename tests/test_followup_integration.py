"""Synthetic integration acceptance for the 1.0.2 follow-up slice.

These tests exercise the real root registration, the real ``pre_llm_call`` hook,
the real tool handlers and the real follow-up/portal transports. Only profile
paths, clock and the native cron execution/ledger are synthetic. Nothing here
reads private mail, sends a message, touches a live Desktop/Telegram session,
changes git, installs/releases or uses the network.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_secretary_followup_qa")
cron = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")
responsibility_surface = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_responsibilities")

from proactive_pilot import followups as F  # noqa: E402
from proactive_pilot import ownerpaths  # noqa: E402
from proactive_pilot import policy as policy_mod  # noqa: E402
from proactive_pilot import portal_notices as notices  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = "2026-11-18T09:00:00+00:00"


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    def __init__(self, *, profile_name="A", execution=None):
        self.profile_name = profile_name
        self.execution = execution
        self.state = MemoryState()
        self.tools = {}
        self.hooks = {}
        self.commands = {}

    def current_cron_execution(self):
        return self.execution

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema,
                            "handler": handler, "kwargs": kwargs}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler


class FollowupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.state = self.data / "var"

        self._env_keys = ("HERMES_HOME", paths.DATA_ENV)
        self._saved_env = {key: os.environ.get(key) for key in self._env_keys}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ[paths.DATA_ENV] = str(self.data)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

        # The host module is absent in this synthetic profile: use the explicit
        # per-profile data root, never the launch home.
        self._native = mock.patch.object(
            ownerpaths, "_native_constants", return_value=None)
        self._native.start()
        self.addCleanup(self._native.stop)

        self.clock = SeededClock(T0, timezone_name="UTC")
        self._clock = mock.patch(
            "proactive_pilot.clock.Clock", return_value=self.clock)
        self._clock.start()
        self.addCleanup(self._clock.stop)

        self.job_id = "job-portal-followup"
        self._seed_ledger()

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_ledger(self, *, job_id=None, name=None):
        job_name = name or cron.PORTAL_JOB_NAME
        cron.save_job_ownership(
            paths.job_ownership_path(),
            {
                "schema": 1,
                "plugin": cron.OWNED_PLUGIN,
                "jobs": {job_name: {"id": job_id or self.job_id, "name": job_name}},
            },
        )

    def _execution(self, *, execution_id="exec-1", job_id=None, job_name=None,
                   profile="A"):
        return types.SimpleNamespace(
            job_id=self.job_id if job_id is None else job_id,
            job_name=cron.PORTAL_JOB_NAME if job_name is None else job_name,
            execution_id=execution_id,
            profile=profile,
        )

    def _foreground(self):
        ctx = Ctx(execution=None)
        result = _PKG._register_conversation(ctx)
        self.assertTrue(result.get("followup_tool"), result)
        self.assertIn("secretary_followup", ctx.tools)
        return ctx, ctx.tools["secretary_followup"]["handler"]

    def _portal(self, **kwargs):
        ctx = Ctx(profile_name=kwargs.pop("profile_name", "A"),
                  execution=self._execution(**kwargs))
        result = _PKG._register_conversation(ctx)
        self.assertTrue(result.get("followup_tool"), result)
        return ctx, ctx.tools["secretary_followup"]["handler"]

    @staticmethod
    def _call(handler, payload):
        return json.loads(handler(payload))

    def _human(self, ctx, text, *, session="s-1", platform="telegram",
               chat_type="dm"):
        hook = ctx.hooks["pre_llm_call"]
        return hook(platform=platform, user_message=text, session_id=session,
                    chat_type=chat_type)

    def _create(self, ctx, handler, *, text="harbour tide", quote=None,
                kind="ongoing", session="s-1",
                hint="check the harbour tide level"):
        quote = quote or "Please keep an eye on the harbour tide."
        self._human(ctx, quote, session=session)
        return self._call(handler, {
            "action": "create",
            "kind": kind,
            "session_id": session,
            "evidence_quote": quote,
            "text": text,
            "check_hint": hint,
        })

    def _pending(self):
        path = self.state / notices.PORTAL_NOTICES_ARTIFACT
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["pending"]

    def _dispatch_emitted(self):
        store = StateStore(self.state, self.clock)
        prefs = json.loads(json.dumps(policy_mod.DEFAULT_PREFERENCES))
        value = json.loads(json.dumps(policy_mod.DEFAULT_POLICY))
        value.update({
            "timezone": "UTC",
            "quiet_start_hour": 0,
            "quiet_end_hour": 0,
            "cooldown_minutes": 0,
            "daily_normal_cap": None,
        })

        def job_reader(job_id):
            return ({"id": self.job_id, "name": cron.PORTAL_JOB_NAME,
                     "state": "active"}, True)

        return notices.dispatch_notice(
            store, self.clock, policy=value, prefs=prefs,
            state=store.load_notify_state(),
            native_ctx=Ctx(profile_name="A"), job_reader=job_reader)

    def test_desktop_and_telegram_create_and_read_back_through_real_hook_tool(self):
        for platform in ("desktop", "telegram"):
            with self.subTest(platform=platform):
                ctx, handler = self._foreground()
                quote = f"Please keep an eye on the {platform} harbour tide."
                self._human(ctx, quote, session=f"{platform}-1",
                            platform=platform)
                created = self._call(handler, {
                    "action": "create",
                    "kind": "ongoing",
                    "session_id": f"{platform}-1",
                    "evidence_quote": quote,
                    "text": f"{platform} harbour tide",
                    "check_hint": "harbour page",
                })
                self.assertTrue(created["ok"], created)
                self.assertEqual(created["followup"]["status"], "active")
                fid = created["followup"]["id"]
                self.assertTrue(fid.startswith("fu_"))

                readback = self._call(handler, {"action": "list"})
                self.assertTrue(readback["ok"], readback)
                ids = {row["id"] for row in readback["followups"]}
                self.assertIn(fid, ids)
                self.assertGreaterEqual(readback["summary"]["active"], 1)

                # A fresh StateStore/foreground handler reads the same profile data.
                fresh_ctx, fresh_handler = self._foreground()
                again = self._call(fresh_handler, {"action": "inspect",
                                                   "followup_id": fid})
                self.assertEqual(again["followup"]["text"], f"{platform} harbour tide")
                self.assertEqual(F.get(StateStore(self.state, self.clock), fid)["id"], fid)

    def test_foreground_create_rejects_background_shared_stale_and_untrusted(self):
        ctx, handler = self._foreground()
        quote = "Please keep an eye on the harbour tide."
        self._human(ctx, quote, session="s-ok")

        # A background turn is never stored as eligible human evidence.
        self._human(ctx, quote, session="s-bg", platform="background")
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-bg",
            "evidence_quote": quote, "text": "bg tide"})["ok"])

        # A shared/group turn is also ineligible.
        self._human(ctx, quote, session="s-group", platform="telegram",
                    chat_type="group")
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-group",
            "evidence_quote": quote, "text": "group tide"})["ok"])

        # A turn from another session is not current evidence.
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-other",
            "evidence_quote": quote, "text": "other tide"})["ok"])

        # A stale turn (past the existing TTL) is not current evidence.
        responsibility_surface.remember_human_turn(
            ctx, session_id="s-stale", user_message=quote, platform="telegram",
            at=time.time() - responsibility_surface.TURN_TTL_SECONDS - 5)
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-stale",
            "evidence_quote": quote, "text": "stale tide"})["ok"])

        # Quoted/reported and conditional text cannot authorise creation.
        reported = 'She said "please keep an eye on the harbour tide"'
        self._human(ctx, reported, session="s-reported")
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-reported",
            "evidence_quote": reported, "text": "reported tide"})["ok"])

        conditional = "If I have time, I will keep an eye on the harbour tide."
        self._human(ctx, conditional, session="s-cond")
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-cond",
            "evidence_quote": conditional, "text": "conditional tide"})["ok"])

        # A model-invented quote is refused.
        self.assertFalse(self._call(handler, {
            "action": "create", "session_id": "s-ok",
            "evidence_quote": "the assistant says this is important",
            "text": "invented tide"})["ok"])
        self.assertFalse((self.state / F.STORE).exists())

    def test_foreground_stop_requires_current_exact_quote_and_binding(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, text="harbour tide watch")
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        stop_quote = "Please stop the harbour tide watch now."
        self._human(ctx, stop_quote, session="s-1")
        stopped = self._call(handler, {
            "action": "stop", "session_id": "s-1", "followup_id": fid,
            "evidence_quote": stop_quote})
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(stopped["followup"]["status"], "done")
        self.assertEqual(stopped["followup"]["close_reason"], "owner_stopped")

        # The same close cannot be repeated, and another session cannot stop.
        again = self._call(handler, {
            "action": "stop", "session_id": "s-1", "followup_id": fid,
            "evidence_quote": stop_quote})
        self.assertFalse(again["ok"], again)

        other = self._create(ctx, handler, text="parcel watch", quote=(
            "Please keep an eye on the parcel."), session="s-2")
        self.assertTrue(other["ok"], other)
        # A stop needs a current turn in the named session; the earlier create
        # turn in s-2 is not silently reused for an unrelated s-3 session.
        denied = self._call(handler, {
            "action": "stop", "session_id": "s-3",
            "followup_id": other["followup"]["id"],
            "evidence_quote": "stop the parcel watch"})
        self.assertFalse(denied["ok"], denied)

    def test_empty_followups_add_no_work_or_writes(self):
        ctx, handler = self._foreground()
        listed = self._call(handler, {"action": "list"})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(listed["followups"], [])
        self.assertFalse((self.state / F.STORE).exists())

        pctx, phandler = self._portal()
        due = self._call(phandler, {"action": "due"})
        self.assertTrue(due["ok"], due)
        self.assertEqual(due["count"], 0)
        self.assertEqual(due["followups"], [])
        self.assertFalse((self.state / F.STORE).exists())
        self.assertFalse((self.state / notices.PORTAL_NOTICES_ARTIFACT).exists())

    def test_ongoing_survives_quiet_and_check_failures_and_notifies_once(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, text="harbour tide watch")
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        _, due_handler = self._portal(execution_id="exec-due")
        due = self._call(due_handler, {"action": "due"})
        self.assertTrue(due["ok"], due)
        self.assertEqual(due["count"], 1)
        self.assertNotIn("text", due["followups"][0])
        self.assertNotIn("harbour tide watch", json.dumps(due))

        _, portal_handler = self._portal(execution_id="exec-check-1")
        for result in ("no_match", "error", "blocked", "no_match"):
            checked = self._call(portal_handler, {
                "action": "record_check", "followup_id": fid, "result": result})
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(checked["followup"]["status"], "active")
            self.assertEqual(checked["followup"]["last_result"], result)

        matched = self._call(portal_handler, {
            "action": "record_check", "followup_id": fid, "result": "match",
            "fingerprint": "match-1"})
        self.assertEqual(matched["followup"]["status"], "active")

        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        queued = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "match-1", "expiry": expiry,
            "message": "The harbour tide changed.",
            "evidence_refs": ["source:harbour-page"]})
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["followup"]["status"], "active")
        self.assertEqual(queued["followup"]["notified_count"], 1)
        self.assertEqual(len(self._pending()), 1)

        # The same ongoing match is never queued twice.
        repeated = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "match-1", "expiry": expiry,
            "message": "The harbour tide changed.",
            "evidence_refs": ["source:harbour-page"]})
        self.assertFalse(repeated["ok"], repeated)
        self.assertEqual(len(self._pending()), 1)

        # Another quiet check still leaves the ongoing watch alive.
        checked = self._call(portal_handler, {
            "action": "record_check", "followup_id": fid, "result": "no_match"})
        self.assertEqual(checked["followup"]["status"], "active")

    def test_twenty_watches_are_due_in_bounded_wakes(self):
        ctx, handler = self._foreground()
        created_ids = []
        for index in range(20):
            created = self._create(
                ctx, handler,
                text=f"batch watch {index}",
                quote=f"Please keep an eye on batch watch {index}.",
                session=f"s-batch-{index}",
                hint=f"check batch gauge {index}",
            )
            self.assertTrue(created["ok"], created)
            created_ids.append(created["followup"]["id"])
        self.assertEqual(len(set(created_ids)), 20)

        _, first_handler = self._portal(execution_id="exec-batch-1")
        first = self._call(first_handler, {"action": "due"})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["count"], F.MAX_WAKE_ENTRIES)
        first_ids = {row["id"] for row in first["followups"]}
        self.assertEqual(len(first_ids), F.MAX_WAKE_ENTRIES)
        self.assertTrue(first_ids <= set(created_ids))

        for fid in first_ids:
            checked = self._call(first_handler, {
                "action": "record_check", "followup_id": fid,
                "result": "no_match"})
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(checked["followup"]["check_count"], 1)

        _, second_handler = self._portal(execution_id="exec-batch-2")
        remaining = len(created_ids) - F.MAX_WAKE_ENTRIES
        second = self._call(second_handler, {"action": "due",
                                             "max_entries": remaining})
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["count"], remaining)
        second_ids = {row["id"] for row in second["followups"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, set(created_ids))

    def test_missing_or_blank_hint_never_writes_and_legacy_row_needs_clarification(self):
        ctx, handler = self._foreground()
        quote = "Please keep an eye on the legacy harbour tide."
        self._human(ctx, quote, session="s-hint")
        for hint in (None, "", "   ", "\t\n"):
            refused = self._call(handler, {
                "action": "create",
                "kind": "ongoing",
                "session_id": "s-hint",
                "evidence_quote": quote,
                "text": "legacy harbour tide",
                "check_hint": hint,
            })
            self.assertFalse(refused["ok"], refused)
            self.assertFalse((self.state / F.STORE).exists())

        stamp = self.clock.now().isoformat()
        legacy = {
            "id": "fu_legacy00000000",
            "text": "legacy blank hint watch",
            "kind": "ongoing",
            "status": "active",
            "created_at": stamp,
            "updated_at": stamp,
            "source": "legacy",
            "check_hint": "",
            "last_checked_at": "",
            "last_result": "",
            "check_count": 0,
            "notified_count": 0,
            "seen": [],
            "notice_reservations": [],
            "notice_event_key": "",
            "notice_status": "",
            "closed_at": "",
            "close_reason": "",
        }
        data = F.empty_store()
        data["entries"][legacy["id"]] = legacy
        StateStore(self.state, self.clock).save(F.STORE, data)

        listed = self._call(handler, {"action": "list"})
        self.assertTrue(listed["ok"], listed)
        row = next(row for row in listed["followups"]
                   if row["id"] == legacy["id"])
        self.assertTrue(row["needs_clarification"], row)
        self.assertEqual(row["clarification"], F.NEEDS_CLARIFICATION_TEXT)

        inspected = self._call(handler, {
            "action": "inspect", "followup_id": legacy["id"]})
        self.assertEqual(inspected["followup"]["id"], legacy["id"])
        self.assertEqual(inspected["followup"]["check_hint"], "")

        _, portal_handler = self._portal(execution_id="exec-legacy-row")
        due = self._call(portal_handler, {"action": "due"})
        self.assertTrue(due["ok"], due)
        self.assertEqual(due["count"], 1)
        self.assertTrue(due["followups"][0]["needs_clarification"], due)
        self.assertEqual(due["followups"][0]["clarification"],
                         F.NEEDS_CLARIFICATION_TEXT)

    def test_stop_refuses_wrong_id_and_shared_topic_then_stops_unique_and_reopens(self):
        ctx, handler = self._foreground()
        alpha = self._create(ctx, handler, text="alpha lighthouse watch",
                             quote="Please keep an eye on the alpha lighthouse.",
                             session="s-alpha", hint="check alpha beacon")
        beta = self._create(ctx, handler, text="beta parcel watch",
                            quote="Please keep an eye on the beta parcel.",
                            session="s-beta", hint="check beta tracker")
        north = self._create(ctx, handler, text="harbour tide north",
                             quote="Please keep an eye on the northern harbour tide.",
                             session="s-north", hint="check north tide gauge")
        south = self._create(ctx, handler, text="harbour tide south",
                             quote="Please keep an eye on the southern harbour tide.",
                             session="s-south", hint="check south tide gauge")
        for created in (alpha, beta, north, south):
            self.assertTrue(created["ok"], created)
        ids = {
            "alpha": alpha["followup"]["id"],
            "beta": beta["followup"]["id"],
            "north": north["followup"]["id"],
            "south": south["followup"]["id"],
        }
        store_path = self.state / F.STORE

        wrong_quote = "Please stop the wrong id fu_0000000000000000 now."
        self._human(ctx, wrong_quote, session="s-stop-wrong")
        before = store_path.read_bytes()
        wrong = self._call(handler, {
            "action": "stop", "session_id": "s-stop-wrong",
            "followup_id": ids["alpha"], "evidence_quote": wrong_quote})
        self.assertFalse(wrong["ok"], wrong)
        self.assertEqual(wrong["status"], "needs_clarification")
        self.assertEqual(store_path.read_bytes(), before)

        ambiguous_quote = "Please stop the harbour tide watch."
        self._human(ctx, ambiguous_quote, session="s-stop-ambiguous")
        before = store_path.read_bytes()
        ambiguous = self._call(handler, {
            "action": "stop", "session_id": "s-stop-ambiguous",
            "evidence_quote": ambiguous_quote})
        self.assertFalse(ambiguous["ok"], ambiguous)
        self.assertEqual(ambiguous["status"], "needs_clarification")
        self.assertEqual(store_path.read_bytes(), before)

        unique_quote = "Please stop the alpha lighthouse watch."
        self._human(ctx, unique_quote, session="s-stop-unique")
        stopped = self._call(handler, {
            "action": "stop", "session_id": "s-stop-unique",
            "evidence_quote": unique_quote})
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(stopped["followup"]["id"], ids["alpha"])
        self.assertEqual(stopped["followup"]["status"], "done")
        self.assertEqual(stopped["followup"]["close_reason"], "owner_stopped")

        _fresh_ctx, fresh_handler = self._foreground()
        reopened = self._call(fresh_handler, {
            "action": "inspect", "followup_id": ids["alpha"]})
        self.assertEqual(reopened["followup"]["status"], "done")
        self.assertEqual(reopened["followup"]["close_reason"], "owner_stopped")
        listed = self._call(fresh_handler, {"action": "list"})
        active_ids = {row["id"] for row in listed["followups"]}
        self.assertEqual(active_ids,
                         {ids["beta"], ids["north"], ids["south"]})
        summary = F.summary(StateStore(self.state, self.clock))
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["active"], 3)

    def test_one_shot_uncertainty_never_completes_or_retries(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, kind="one_shot",
                               text="parcel delivery watch",
                               quote="Please keep an eye on the parcel delivery.")
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        _, portal_handler = self._portal(execution_id="exec-oneshot")
        self.assertTrue(self._call(portal_handler, {
            "action": "record_check", "followup_id": fid,
            "result": "match", "fingerprint": "parcel-1"})["ok"])
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        queued = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "parcel-1", "expiry": expiry,
            "message": "The parcel delivery changed.",
            "evidence_refs": ["source:parcel-tracker"]})
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["followup"]["status"], "awaiting_delivery")
        self.assertTrue(queued["frozen"])
        self.assertEqual(len(self._pending()), 1)

        for status in ("emitted", "attempted", "unknown"):
            resolved = self._call(portal_handler, {
                "action": "resolve_notice", "followup_id": fid,
                "notice_status": status})
            self.assertTrue(resolved["ok"], resolved)
            self.assertEqual(resolved["followup"]["status"], "awaiting_delivery")
            self.assertEqual(resolved["followup"]["notice_status"], status)
            self.assertNotEqual(resolved["followup"]["status"], "done")

        # No verified receipt exists, so emitted-unconfirmed is never delivered.
        denied = self._call(portal_handler, {
            "action": "resolve_notice", "followup_id": fid,
            "notice_status": "delivered"})
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(F.get(StateStore(self.state, self.clock), fid)["status"],
                         "awaiting_delivery")

        # A later run with a different execution id refuses to resubmit.
        _, restarted = self._portal(execution_id="exec-restarted")
        replay = self._call(restarted, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "parcel-1", "expiry": expiry,
            "message": "The parcel delivery changed.",
            "evidence_refs": ["source:parcel-tracker"]})
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["status"], "already_queued")
        self.assertTrue(replay["frozen"])
        self.assertEqual(len(self._pending()), 1)

        # Checks are frozen for the one-shot, even if the model tries again.
        check = self._call(restarted, {
            "action": "record_check", "followup_id": fid,
            "result": "no_match"})
        self.assertFalse(check["ok"], check)

    def test_ambiguous_enqueue_stays_frozen_and_is_not_blindly_retried(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, kind="one_shot",
                               text="ambiguous watch",
                               quote="Please keep an eye on the ambiguous parcel.")
        fid = created["followup"]["id"]
        _, portal_handler = self._portal(execution_id="exec-ambiguous")
        self.assertTrue(self._call(portal_handler, {
            "action": "record_check", "followup_id": fid,
            "result": "match", "fingerprint": "amb-1"})["ok"])
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()

        with mock.patch.object(notices, "submit_notice",
                               side_effect=RuntimeError("synthetic ambiguity")) as submit:
            first = self._call(portal_handler, {
                "action": "queue_notice", "followup_id": fid,
                "fingerprint": "amb-1", "expiry": expiry,
                "message": "The ambiguous parcel changed.",
                "evidence_refs": ["source:ambiguous-parcel"]})
            self.assertFalse(first["ok"], first)
            self.assertEqual(first["status"], "enqueue_unknown")
            self.assertTrue(first["frozen"])
            self.assertEqual(first["followup"]["status"], "awaiting_delivery")
            self.assertEqual(submit.call_count, 1)

            second = self._call(portal_handler, {
                "action": "queue_notice", "followup_id": fid,
                "fingerprint": "amb-1", "expiry": expiry,
                "message": "The ambiguous parcel changed.",
                "evidence_refs": ["source:ambiguous-parcel"]})
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["status"], "already_frozen")
            self.assertTrue(second["frozen"])
            self.assertEqual(submit.call_count, 1)
        self.assertFalse((self.state / notices.PORTAL_NOTICES_ARTIFACT).exists())

    def test_one_shot_restart_and_duplicate_resistance(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, kind="one_shot",
                               text="durable ack watch",
                               quote="Please keep an eye on the durable ack.")
        fid = created["followup"]["id"]
        _, portal_handler = self._portal(execution_id="exec-durable-1")
        self.assertTrue(self._call(portal_handler, {
            "action": "record_check", "followup_id": fid,
            "result": "match", "fingerprint": "dur-1"})["ok"])
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        self.assertTrue(self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "dur-1", "expiry": expiry,
            "message": "The durable ack changed.",
            "evidence_refs": ["source:durable-ack"]})["ok"])

        # Fresh context + fresh registration + fresh StateStore, same profile.
        _, restarted = self._portal(execution_id="exec-durable-2")
        replay = self._call(restarted, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "dur-1", "expiry": expiry,
            "message": "The durable ack changed.",
            "evidence_refs": ["source:durable-ack"]})
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["status"], "already_queued")
        self.assertEqual(len(self._pending()), 1)
        queue = json.loads((self.state / notices.PORTAL_NOTICES_ARTIFACT)
                           .read_text(encoding="utf-8"))
        self.assertEqual(queue["seen"][replay["event_key"]]["status"], "pending")

    def test_composed_foreground_queue_and_dispatch_gate_is_offline_only(self):
        """One composed offline path: human create -> owned portal enqueue ->
        real durable queue -> real ``dispatch_notice`` gate.

        ``dispatch_notice`` is called directly in-process.  This is not a native
        scheduler invocation, not real Desktop attribution, and not Telegram
        delivery.  Only the clock and the host job-identity read are synthetic;
        the queue, ``seen`` ledger, policy gate and deduplication are real.
        """
        ctx, handler = self._foreground()
        created = self._create(
            ctx, handler, kind="one_shot", session="s-dispatch",
            text="dispatch harbour tide watch",
            quote="Please keep an eye on the dispatch harbour tide.",
            hint="check the dispatch harbour tide gauge",
        )
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        # The portal action is taken under a fake *owned* native execution
        # identity.  It writes the real durable queue through submit_notice.
        _, portal_handler = self._portal(execution_id="exec-dispatch-1")
        checked = self._call(portal_handler, {
            "action": "record_check", "followup_id": fid,
            "result": "match", "fingerprint": "tide-1"})
        self.assertTrue(checked["ok"], checked)

        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        message = "The dispatch harbour tide changed."
        queued = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "tide-1", "expiry": expiry,
            "topic": "Harbour tide",
            "message": message,
            "evidence_refs": ["source:harbour-page"]})
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(queued["frozen"])
        self.assertEqual(queued["followup"]["status"], "awaiting_delivery")
        event_key = queued["event_key"]

        queue_path = self.state / notices.PORTAL_NOTICES_ARTIFACT
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(len(queue["pending"]), 1)
        item = queue["pending"][0]
        self.assertEqual(item["event_key"], event_key)
        self.assertEqual(item["message"], message)
        self.assertEqual(item["origin"]["job_id"], self.job_id)
        self.assertEqual(item["origin"]["job_name"], cron.PORTAL_JOB_NAME)
        self.assertEqual(item["origin"]["profile"], "A")
        self.assertEqual(queue["seen"][event_key]["status"], "pending")

        store = StateStore(self.state, self.clock)
        prefs = json.loads(json.dumps(policy_mod.DEFAULT_PREFERENCES))

        def policy(**overrides):
            value = json.loads(json.dumps(policy_mod.DEFAULT_POLICY))
            value.update({
                "timezone": "UTC",
                "quiet_start_hour": 0,
                "quiet_end_hour": 0,
                "cooldown_minutes": 0,
            })
            value.update(overrides)
            return value

        # Fake only the native live-job read.  The real gate still proves the
        # durable queue origin before touching queue or notify state.
        def job_reader(job_id):
            return ({"id": self.job_id, "name": cron.PORTAL_JOB_NAME,
                     "state": "active"}, True)

        # An explicit policy cap blocks attributable output before mutation.
        blocked = notices.dispatch_notice(
            store, self.clock, policy=policy(daily_normal_cap=0), prefs=prefs,
            state=store.load_notify_state(),
            native_ctx=Ctx(profile_name="A"), job_reader=job_reader)
        self.assertEqual(blocked["message"], "")
        self.assertEqual(blocked["status"], "blocked")
        blocked_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(len(blocked_queue["pending"]), 1)
        self.assertEqual(blocked_queue["seen"][event_key]["status"], "pending")
        self.assertFalse((self.state / notices.NOTIFY_STATE_ARTIFACT).exists())

        allowed = policy(daily_normal_cap=None)
        emitted = notices.dispatch_notice(
            store, self.clock, policy=allowed, prefs=prefs,
            state=store.load_notify_state(),
            native_ctx=Ctx(profile_name="A"), job_reader=job_reader)
        self.assertEqual(emitted["message"], message)
        self.assertEqual(emitted["status"], "emitted-unconfirmed")
        self.assertNotIn(emitted["status"],
                         {"delivered", "confirmed", "completed"})
        self.assertEqual(emitted["event_key"], event_key)
        self.assertEqual(emitted["id"], item["id"])

        emitted_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(emitted_queue["pending"], [])
        self.assertEqual(emitted_queue["seen"][event_key]["status"],
                         "emitted-unconfirmed")
        notify = json.loads(
            (self.state / notices.NOTIFY_STATE_ARTIFACT)
            .read_text(encoding="utf-8"))
        self.assertTrue(notify.get("delivered"))
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), fid)["status"],
            "awaiting_delivery")

        # A fresh store/ledger replay is silent and cannot duplicate the event.
        restarted = StateStore(self.state, self.clock)
        replayed = notices.dispatch_notice(
            restarted, self.clock, policy=allowed, prefs=prefs,
            state=restarted.load_notify_state(strict=True),
            native_ctx=Ctx(profile_name="A"), job_reader=job_reader)
        self.assertEqual(replayed["message"], "")
        self.assertNotEqual(replayed["status"], "emitted-unconfirmed")
        self.assertEqual(replayed["status"], "empty")
        final_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(final_queue["pending"], [])
        self.assertEqual(list(final_queue["seen"]), [event_key])
        self.assertEqual(final_queue["seen"][event_key]["status"],
                         "emitted-unconfirmed")
        final_notify = json.loads(
            (self.state / notices.NOTIFY_STATE_ARTIFACT)
            .read_text(encoding="utf-8"))
        delivered = final_notify.get("delivered")
        if isinstance(delivered, list):
            self.assertEqual(len(delivered), 1)

    def test_owner_receipt_requires_exact_emitted_notice_and_current_ack(self):
        ctx, handler = self._foreground()
        created = self._create(
            ctx, handler, kind="one_shot", session="s-owner-receipt",
            text="owner receipt parcel",
            quote="Please keep an eye on the owner receipt parcel.",
            hint="check the owner receipt parcel tracker",
        )
        self.assertTrue(created["ok"], created)
        fid = created["followup"]["id"]

        _, portal_handler = self._portal(execution_id="exec-owner-receipt")
        self.assertTrue(self._call(portal_handler, {
            "action": "record_check", "followup_id": fid,
            "result": "match", "fingerprint": "owner-receipt-1"})["ok"])
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()
        queued = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "owner-receipt-1", "expiry": expiry,
            "message": "The owner receipt parcel changed.",
            "evidence_refs": ["source:owner-receipt-parcel"]})
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(queued["status"], "queued")
        event_key = queued["event_key"]

        # Merely queued is not emitted evidence: no reference is exposed and a
        # guessed reference cannot close the watch.
        inspected = self._call(handler, {"action": "inspect", "followup_id": fid})
        self.assertNotIn("owner_receipt", inspected["followup"])
        pending_ack = "I received the notice notice-receipt:" + "0" * 24 + "."
        self._human(ctx, pending_ack, session="s-owner-ack")
        before_emit = self._call(handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": pending_ack,
            "notice_ref": "notice-receipt:" + "0" * 24})
        self.assertFalse(before_emit["ok"], before_emit)
        self.assertEqual(before_emit["status"], "receipt_not_available")
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), fid)["status"],
            "awaiting_delivery")

        emitted = self._dispatch_emitted()
        self.assertEqual(emitted["status"], "emitted-unconfirmed")
        queue_path = self.state / notices.PORTAL_NOTICES_ARTIFACT
        queue_before = queue_path.read_bytes()

        inspected = self._call(handler, {"action": "inspect", "followup_id": fid})
        receipt = inspected["followup"]["owner_receipt"]
        self.assertEqual(receipt["status"], "emitted-unconfirmed")
        self.assertEqual(receipt["event_key"], event_key)
        self.assertTrue(receipt["ref"].startswith("notice-receipt:"))

        # A portal/cron execution can never confirm an owner receipt.
        portal_denied = self._call(portal_handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": pending_ack,
            "notice_ref": receipt["ref"]})
        self.assertFalse(portal_denied["ok"], portal_denied)
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), fid)["status"],
            "awaiting_delivery")

        # Generic thanks is not an explicit receipt acknowledgment.
        self._human(ctx, "Thanks!", session="s-owner-ack")
        denied = self._call(handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": "Thanks!",
            "notice_ref": receipt["ref"]})
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), fid)["status"],
            "awaiting_delivery")

        # An explicit acknowledgment of a guessed reference is refused.
        wrong = "I received the notice notice-receipt:" + "f" * 24 + "."
        self._human(ctx, wrong, session="s-owner-ack")
        denied = self._call(handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": wrong,
            "notice_ref": "notice-receipt:" + "f" * 24})
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(denied["status"], "receipt_mismatch")
        self.assertEqual(queue_path.read_bytes(), queue_before)
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), fid)["status"],
            "awaiting_delivery")

        # The exact reference in the current authenticated owner turn closes it.
        ack = f"I received the notice {receipt['ref']}."
        self._human(ctx, ack, session="s-owner-ack")
        confirmed = self._call(handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": ack,
            "notice_ref": receipt["ref"]})
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(confirmed["status"], "owner_confirmed")
        self.assertEqual(confirmed["delivery"],
                         "owner_confirmed_not_transport_verified")
        self.assertEqual(confirmed["followup"]["status"], "done")
        self.assertEqual(confirmed["followup"]["close_reason"], "owner_confirmed")
        self.assertEqual(confirmed["followup"]["notice_status"], "owner_confirmed")
        self.assertNotEqual(confirmed["followup"]["notice_status"], "delivered")
        self.assertNotEqual(confirmed["followup"]["close_reason"], "notified")
        self.assertEqual(queue_path.read_bytes(), queue_before)
        self.assertEqual(self._pending(), [])
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertEqual(queue["seen"][event_key]["status"], "emitted-unconfirmed")

        # Replaying the same receipt is idempotent and resends nothing.
        replayed = self._call(handler, {
            "action": "confirm_receipt", "followup_id": fid,
            "session_id": "s-owner-ack", "evidence_quote": ack,
            "notice_ref": receipt["ref"]})
        self.assertTrue(replayed["ok"], replayed)
        self.assertEqual(replayed["status"], "already_owner_confirmed")
        self.assertEqual(queue_path.read_bytes(), queue_before)
        self.assertEqual(
            F.summary(StateStore(self.state, self.clock))["awaiting_delivery"], 0)

        # A confirmed one-shot cannot be requeued or re-labeled as delivered.
        self.assertFalse(self._call(portal_handler, {
            "action": "queue_notice", "followup_id": fid,
            "fingerprint": "owner-receipt-1", "expiry": expiry,
            "message": "The owner receipt parcel changed.",
            "evidence_refs": ["source:owner-receipt-parcel"]})["ok"])
        self.assertFalse(self._call(portal_handler, {
            "action": "resolve_notice", "followup_id": fid,
            "notice_status": "delivered"})["ok"])
        self.assertEqual(queue_path.read_bytes(), queue_before)

    def test_two_one_shot_watches_in_one_wake_release_only_the_unsubmitted_one(self):
        ctx, handler = self._foreground()
        alpha = self._create(ctx, handler, kind="one_shot", text="alpha one shot",
                             quote="Please keep an eye on the alpha parcel.",
                             session="s-alpha")
        beta = self._create(ctx, handler, kind="one_shot", text="beta one shot",
                            quote="Please keep an eye on the beta parcel.",
                            session="s-beta")
        self.assertTrue(alpha["ok"], alpha)
        self.assertTrue(beta["ok"], beta)
        alpha_id = alpha["followup"]["id"]
        beta_id = beta["followup"]["id"]

        _, portal_handler = self._portal(execution_id="exec-two-watch")
        for fid, fp in ((alpha_id, "alpha-1"), (beta_id, "beta-1")):
            checked = self._call(portal_handler, {
                "action": "record_check", "followup_id": fid,
                "result": "match", "fingerprint": fp})
            self.assertTrue(checked["ok"], checked)
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()

        first = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": alpha_id,
            "fingerprint": "alpha-1", "expiry": expiry,
            "message": "The alpha parcel changed.",
            "evidence_refs": ["source:alpha-parcel"]})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["status"], "queued")
        self.assertEqual(len(self._pending()), 1)

        # One native execution may submit only one event.  The producer proves
        # the second attempt wrote nothing, so it must be restored rather than
        # being stranded awaiting delivery forever.
        second = self._call(portal_handler, {
            "action": "queue_notice", "followup_id": beta_id,
            "fingerprint": "beta-1", "expiry": expiry,
            "message": "The beta parcel changed.",
            "evidence_refs": ["source:beta-parcel"]})
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "execution_used")
        self.assertFalse(second["frozen"])
        self.assertEqual(second["followup"]["status"], "active")
        self.assertEqual(len(self._pending()), 1)

        data = json.loads((self.state / F.STORE).read_text(encoding="utf-8"))
        beta_entry = data["entries"][beta_id]
        self.assertEqual(beta_entry["status"], "active")
        self.assertEqual(beta_entry["notice_event_key"], "")
        self.assertNotIn("beta-1", beta_entry["seen"])
        self.assertNotIn(second["event_key"], data["dedupe"])

        # The failed second watch is still due in this wake...
        due = self._call(portal_handler, {"action": "due"})
        self.assertEqual([row["id"] for row in due["followups"]], [beta_id])

        # ...and a subsequent wake with a fresh execution can enqueue it.
        _, next_wake = self._portal(execution_id="exec-two-watch-next")
        retried = self._call(next_wake, {
            "action": "queue_notice", "followup_id": beta_id,
            "fingerprint": "beta-1", "expiry": expiry,
            "message": "The beta parcel changed.",
            "evidence_refs": ["source:beta-parcel"]})
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(len(self._pending()), 2)
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), beta_id)["status"],
            "awaiting_delivery")

    def test_awaiting_capacity_is_separate_and_defers_before_dispatch(self):
        ctx, handler = self._foreground()
        store = StateStore(self.state, self.clock)
        stamp = self.clock.now()
        awaiting_ids = []
        for index in range(F.MAX_AWAITING_FOLLOWUPS):
            created = self._create(
                ctx, handler, kind="one_shot",
                text=f"awaiting capacity {index}",
                quote=f"Please keep an eye on awaiting capacity {index}.",
                session=f"s-awaiting-cap-{index}",
                hint=f"check awaiting capacity gauge {index}",
            )
            self.assertTrue(created["ok"], created)
            fid = created["followup"]["id"]
            F.record_check(store, fid, "match",
                           fingerprint=f"await-cap-{index}", now=stamp)
            F.queue_notice(store, fid, f"await-cap-evt-{index}",
                           fingerprint=f"await-cap-{index}", now=stamp)
            awaiting_ids.append(fid)

        summary = F.summary(store)
        self.assertEqual(summary["awaiting_delivery"], F.MAX_AWAITING_FOLLOWUPS)
        self.assertEqual(summary["active"], 0)

        extra = self._create(
            ctx, handler, kind="one_shot", text="extra deferred watch",
            quote="Please keep an eye on the extra deferred parcel.",
            session="s-extra-deferred", hint="check the extra deferred parcel")
        self.assertTrue(extra["ok"], extra)
        extra_id = extra["followup"]["id"]

        _, portal_handler = self._portal(execution_id="exec-pending-cap")
        self.assertTrue(self._call(portal_handler, {
            "action": "record_check", "followup_id": extra_id,
            "result": "match", "fingerprint": "extra-fp"})["ok"])
        expiry = (self.clock.now() + timedelta(hours=1)).isoformat()

        with mock.patch.object(notices, "submit_notice") as submit:
            deferred = self._call(portal_handler, {
                "action": "queue_notice", "followup_id": extra_id,
                "fingerprint": "extra-fp", "expiry": expiry,
                "message": "The extra parcel changed.",
                "evidence_refs": ["source:extra-parcel"]})
            self.assertFalse(deferred["ok"], deferred)
            self.assertEqual(deferred["status"], "deferred")
            self.assertTrue(deferred["deferred"])
            self.assertFalse(deferred["frozen"])
            self.assertEqual(deferred["capacity"], "pending")
            self.assertEqual(submit.call_count, 0)
        self.assertEqual(F.get(store, extra_id)["status"], "active")
        self.assertNotIn("extra-fp", F.get(store, extra_id)["seen"])

        # A restart keeps both the full pending set and the deferred active watch.
        _, restarted = self._portal(execution_id="exec-pending-restart")
        self.assertEqual(
            F.summary(StateStore(self.state, self.clock))["awaiting_delivery"],
            F.MAX_AWAITING_FOLLOWUPS)
        self.assertEqual(
            F.get(StateStore(self.state, self.clock), extra_id)["status"],
            "active")
        with mock.patch.object(notices, "submit_notice") as submit:
            again = self._call(restarted, {
                "action": "queue_notice", "followup_id": extra_id,
                "fingerprint": "extra-fp", "expiry": expiry,
                "message": "The extra parcel changed.",
                "evidence_refs": ["source:extra-parcel"]})
            self.assertEqual(again["status"], "deferred")
            self.assertEqual(submit.call_count, 0)

        # The existing owner-bound foreground flow can explicitly stop an
        # awaiting one-shot, freeing one pending slot for the deferred watch.
        stop_quote = f"Please stop awaiting watch {awaiting_ids[0]} now."
        self._human(ctx, stop_quote, session="s-stop-awaiting")
        stopped = self._call(handler, {
            "action": "stop", "session_id": "s-stop-awaiting",
            "followup_id": awaiting_ids[0], "evidence_quote": stop_quote})
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(stopped["followup"]["status"], "done")
        self.assertEqual(stopped["followup"]["close_reason"], "owner_stopped")

        _, freed = self._portal(execution_id="exec-pending-freed")
        queued = self._call(freed, {
            "action": "queue_notice", "followup_id": extra_id,
            "fingerprint": "extra-fp", "expiry": expiry,
            "message": "The extra parcel changed.",
            "evidence_refs": ["source:extra-parcel"]})
        self.assertTrue(queued["ok"], queued)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["followup"]["status"], "awaiting_delivery")
        self.assertEqual(
            F.summary(StateStore(self.state, self.clock))["awaiting_delivery"],
            F.MAX_AWAITING_FOLLOWUPS)

    def test_portal_actions_require_verified_owned_execution_identity(self):
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, text="identity watch")
        fid = created["followup"]["id"]

        variants = [
            ("unsupported-host", Ctx(profile_name="A")),
            ("not-cron", Ctx(profile_name="A", execution=None)),
            ("foreign-job", Ctx(profile_name="A",
                                execution=self._execution(job_name="other-job"))),
            ("profile-mismatch", Ctx(profile_name="A",
                                     execution=self._execution(profile="B"))),
            ("ownership-mismatch", Ctx(profile_name="A",
                                       execution=self._execution(job_id="other-id"))),
            ("missing-profile", Ctx(profile_name=None,
                                    execution=self._execution())),
        ]
        for label, bad in variants:
            with self.subTest(label=label):
                result = _PKG._register_conversation(bad)
                self.assertTrue(result.get("followup_tool"), result)
                bad_handler = bad.tools["secretary_followup"]["handler"]
                refused = self._call(bad_handler, {
                    "action": "record_check", "followup_id": fid,
                    "result": "match", "fingerprint": "bad-fp"})
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(F.get(StateStore(self.state, self.clock), fid)["check_count"], 0)

        # Foreground create/stop is never available from a portal execution.
        _, portal_handler = self._portal(execution_id="exec-fg-denied")
        denied = self._call(portal_handler, {
            "action": "create", "session_id": "s-1",
            "evidence_quote": "Please keep an eye on the harbour tide.",
            "text": "portal-created"})
        self.assertFalse(denied["ok"], denied)

    def test_static_portal_prompt_mentions_tool_without_raw_watch_text(self):
        marker = "SECRET-RAW-WATCH-MARKER"
        ctx, handler = self._foreground()
        created = self._create(ctx, handler, text=marker,
                               quote="Please keep an eye on the secret raw watch marker.")
        self.assertTrue(created["ok"], created)
        prompt = portal.PROMPT
        self.assertIn("secretary_followup", prompt)
        self.assertIn("original initiative", prompt)
        self.assertIn("never grant new authority", prompt)
        self.assertIn("never add a schedule", prompt)
        self.assertNotIn(marker, prompt)
        self.assertNotIn("harbour page", prompt)

    def test_empty_followups_prompt_is_unchanged_and_schema_is_typed(self):
        ctx, _handler = self._foreground()
        before = portal.PROMPT
        tool = ctx.tools["secretary_followup"]
        self.assertEqual(tool["schema"], importlib.import_module(
            f"{_PKG.__name__}.secretary.secretary_followups").TOOL_SCHEMA)
        self.assertEqual(tool["schema"]["type"], "object")
        self.assertIs(tool["schema"]["additionalProperties"], False)
        self.assertIn("action", tool["schema"]["required"])
        self.assertEqual(before, portal.PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
