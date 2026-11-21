"""Offline tests for the owner-only pending-approval draft tree."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_secretary_drafts_qa")
cron = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
draft_surface = importlib.import_module(
    f"{_PKG.__name__}.secretary.secretary_draft")

from proactive_pilot import drafts as D  # noqa: E402
from proactive_pilot import ownerpaths  # noqa: E402


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

    def current_cron_execution(self):
        return self.execution

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema,
                            "handler": handler, "kwargs": kwargs}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class DraftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir(parents=True)
        self.data.mkdir(parents=True)

        self._env_keys = ("HERMES_HOME", paths.DATA_ENV)
        self._saved_env = {key: os.environ.get(key) for key in self._env_keys}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ[paths.DATA_ENV] = str(self.data)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

        self._native = mock.patch.object(
            ownerpaths, "_native_constants", return_value=None)
        self._native.start()
        self.addCleanup(self._native.stop)

        self.job_id = "job-drafts"
        cron.save_job_ownership(
            paths.job_ownership_path(),
            {"schema": 1, "plugin": cron.OWNED_PLUGIN,
             "jobs": {cron.PORTAL_JOB_NAME: {
                 "id": self.job_id, "name": cron.PORTAL_JOB_NAME}}},
        )
        self.base = paths.drafts_dir()
        paths.ensure_data_dirs()

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _execution(self, execution_id="exec-drafts"):
        return types.SimpleNamespace(
            job_id=self.job_id,
            job_name=cron.PORTAL_JOB_NAME,
            execution_id=execution_id,
            profile="A",
        )

    def _portal(self, execution_id="exec-drafts"):
        ctx = Ctx(execution=self._execution(execution_id))
        result = _PKG._register_conversation(ctx)
        self.assertTrue(result.get("draft_tool"), result)
        self.assertIn("secretary_draft", ctx.tools)
        return ctx, ctx.tools["secretary_draft"]["handler"]

    def _foreground(self):
        ctx = Ctx(execution=None)
        result = _PKG._register_conversation(ctx)
        self.assertTrue(result.get("draft_tool"), result)
        return ctx, ctx.tools["secretary_draft"]["handler"]

    @staticmethod
    def _call(handler, payload):
        return json.loads(handler(payload))

    def _stage(self, handler, **overrides):
        payload = {
            "action": "stage",
            "intent": "email",
            "title": "Reply to the harbour office",
            "to": "harbour@example.test",
            "subject": "Tide table",
            "reason": "The owner asked for a reply after the tide change.",
            "body": "# Draft\n\nA short plain-text reply.",
            "evidence": ["source:harbour-page"],
        }
        payload.update(overrides)
        return self._call(handler, payload)

    def _manifest(self, stage, draft_id):
        return json.loads((self.base / stage / draft_id / "manifest.json")
                          .read_text(encoding="utf-8"))

    def test_stage_list_show_approve_idempotent_and_discard(self):
        _, portal = self._portal("exec-lifecycle")
        staged = self._stage(portal)
        self.assertTrue(staged["ok"], staged)
        self.assertFalse(staged["executed"])
        manifest = staged["draft"]
        fid = manifest["id"]
        self.assertTrue(fid.startswith("dr_"))
        self.assertEqual(manifest["status"], "pending")
        self.assertEqual(manifest["filed_by"], "agent")
        self.assertEqual(manifest["body_ref"], "body.md")
        self.assertEqual(
            datetime.fromisoformat(manifest["expires_at"]),
            datetime.fromisoformat(manifest["created_at"]) + timedelta(days=7))
        self.assertTrue((self.base / "pending" / fid / "body.md").is_file())
        mode = stat.S_IMODE(os.stat(self.base / "pending" / fid / "body.md").st_mode)
        self.assertEqual(mode, 0o600)

        pending = self._call(portal, {"action": "list_pending"})
        self.assertTrue(pending["ok"], pending)
        self.assertEqual(pending["drafts"], [
            {"id": fid, "title": "Reply to the harbour office"}])
        self.assertEqual(set(pending["drafts"][0]), {"id", "title"})

        _, owner = self._foreground()
        listed = self._call(owner, {"action": "list"})
        self.assertEqual([row["id"] for row in listed["pending"]], [fid])
        self.assertEqual(listed["expired"], [])

        shown = self._call(owner, {"action": "show", "draft_id": fid})
        self.assertTrue(shown["ok"], shown)
        self.assertTrue(shown["draft_is_untrusted_data"])
        self.assertIn("short plain-text reply", shown["body"])
        self.assertEqual(shown["manifest"]["id"], fid)

        approved = self._call(owner, {"action": "approve", "draft_id": fid})
        self.assertTrue(approved["ok"], approved)
        self.assertEqual(approved["approval"], "owner_confirmed_foreground")
        self.assertFalse(approved["executed"])
        self.assertEqual(approved["receipt"], fid)
        self.assertEqual(approved["intent"], "email")
        self.assertEqual(approved["to"], "harbour@example.test")
        self.assertEqual(approved["subject"], "Tide table")
        self.assertTrue(Path(approved["body_ref"]).is_file())
        self.assertFalse((self.base / "pending" / fid).exists())
        self.assertTrue((self.base / "approved" / fid / "manifest.json").is_file())
        self.assertEqual(self._manifest("approved", fid)["status"], "approved")

        again = self._call(owner, {"action": "approve", "draft_id": fid})
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["already"])
        self.assertEqual(again["receipt"], fid)
        self.assertFalse(again["executed"])

        second = self._stage(portal, title="Second staged draft",
                             body="A different body.")
        self.assertTrue(second["ok"], second)
        sid = second["draft"]["id"]
        discarded = self._call(owner, {
            "action": "discard", "draft_id": sid, "note": "not needed"})
        self.assertTrue(discarded["ok"], discarded)
        self.assertEqual(discarded["status"], "discarded")
        self.assertTrue((self.base / "discarded" / sid).is_dir())
        repeat = self._call(owner, {"action": "discard", "draft_id": sid})
        self.assertTrue(repeat["ok"], repeat)
        self.assertTrue(repeat["already"])
        self.assertEqual(
            self._manifest("discarded", sid)["resolution_note"], "not needed")

    def test_portal_sweeps_expired_and_foreground_lists_it_separately(self):
        _, portal = self._portal("exec-expiry")
        past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        staged = self._stage(portal, expires_at=past)
        self.assertTrue(staged["ok"], staged)
        fid = staged["draft"]["id"]

        _, owner = self._foreground()
        before = self._call(owner, {"action": "list"})
        self.assertEqual(before["pending"], [])
        self.assertEqual([row["id"] for row in before["expired"]], [fid])

        swept = self._call(portal, {"action": "list_pending"})
        self.assertTrue(swept["ok"], swept)
        self.assertEqual(swept["expired_count"], 1)
        self.assertEqual(swept["drafts"], [])
        self.assertTrue((self.base / "expired" / fid).is_dir())
        self.assertFalse((self.base / "pending" / fid).exists())
        self.assertEqual(self._manifest("expired", fid)["status"], "expired")

        after = self._call(owner, {"action": "list"})
        self.assertEqual([row["id"] for row in after["expired"]], [fid])
        denied = self._call(owner, {"action": "approve", "draft_id": fid})
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(denied["status"], "expired")

    def test_caps_duplicate_and_refusals(self):
        _, first = self._portal("exec-cap-one")
        for index in range(3):
            created = self._stage(first, title=f"Capped draft {index}",
                                  body=f"Body {index}")
            self.assertTrue(created["ok"], created)
        fourth = self._stage(first, title="Capped draft fourth",
                             body="Body fourth")
        self.assertFalse(fourth["ok"], fourth)
        self.assertEqual(fourth["status"], "capacity")
        self.assertEqual(fourth["capacity"], "execution")

        # Duplicate intent/title/body within 24 hours is refused.
        _, dup = self._portal("exec-dup")
        original = self._stage(dup, title="Duplicate watch", body="Same body")
        self.assertTrue(original["ok"], original)
        repeated = self._stage(dup, title="Duplicate watch", body="Same body")
        self.assertFalse(repeated["ok"], repeated)
        self.assertEqual(repeated["status"], "duplicate")

        # Fill the pending directory directly through the store, then the tool
        # must refuse an eleventh pending draft.
        for index in range(6):
            D.stage(self.base, intent="other", title=f"Filler draft {index}",
                    body=f"Filler body {index}", reason="filler reason")
        _, eleventh = self._portal("exec-cap-eleven")
        full = self._stage(eleventh, title="Eleventh pending draft",
                           body="Eleventh body")
        self.assertFalse(full["ok"], full)
        self.assertEqual(full["status"], "capacity")
        self.assertEqual(full["capacity"], "pending")

        _, refusals = self._portal("exec-refusals")
        cases = [
            ("oversize", {"body": "x" * (D.MAX_BODY_BYTES + 1)}),
            ("html", {"body": "<p>active markup</p>"}),
            ("intent", {"intent": "spam"}),
            ("reason", {"reason": "   "}),
            ("body_ref", {"body_ref": "payload.html"}),
        ]
        for label, override in cases:
            with self.subTest(label=label):
                payload = {"title": f"Refuse {label}", "body": "ordinary body"}
                payload.update(override)
                refused = self._stage(refusals, **payload)
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(refused["status"], "invalid_args")

    def test_path_escape_and_symlink_refusal(self):
        _, owner = self._foreground()
        escaped = self._call(owner, {
            "action": "show", "draft_id": "../../etc/passwd"})
        self.assertFalse(escaped["ok"], escaped)

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        fake_id = "dr_" + "0" * 16
        link = self.base / "pending" / fake_id
        link.symlink_to(outside, target_is_directory=True)
        try:
            refused = self._call(owner, {"action": "show", "draft_id": fake_id})
            self.assertFalse(refused["ok"], refused)
        finally:
            link.unlink()

        _, portal = self._portal("exec-symlink-body")
        staged = self._stage(portal, body_ref="notes.md")
        self.assertTrue(staged["ok"], staged)
        fid = staged["draft"]["id"]
        body_file = self.base / "pending" / fid / "notes.md"
        body_file.unlink()
        body_file.symlink_to(ROOT / "README.md")
        shown = self._call(owner, {"action": "show", "draft_id": fid})
        self.assertFalse(shown["ok"], shown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
