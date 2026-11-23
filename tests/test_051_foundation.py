"""0.5.1 foundation regressions: explicit projection, backups, onboarding sync.

Synthetic profiles/data only. No network, model, live config/state, scheduler or
installed plugin is touched. The production host form module is imported READ
ONLY when the isolated test runtime provides it.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import config_backup  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402


def _load_package():
    name = "hermes_proactive_secretary_051"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
sc = importlib.import_module(f"{PKG.__name__}.secretary.secretary_cli")
paths = importlib.import_module(f"{PKG.__name__}.paths")
onboarding = importlib.import_module(f"{PKG.__name__}.secretary.secretary_onboarding")
conversation = importlib.import_module(f"{PKG.__name__}.secretary.secretary_conversation")


class FormCtx:
    def __init__(self, settings=None):
        self.settings = copy.deepcopy(dict(settings or {}))
        self.writes = []
        self.plugin_id = "hermes-proactive-secretary"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self.settings[key] = copy.deepcopy(value)

    # Registration is not exercised for its output here; these no-ops let a
    # test prove that loading/enabling the plugin does not start an interview.
    def register_cli_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass

    def register_tool(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_auxiliary_task(self, *args, **kwargs):
        pass


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = copy.deepcopy(value)


class FileState:
    """Small JSON-backed facade that survives a simulated disable/enable."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, key, default=None):
        return self._read().get(key, default)

    def set(self, key, value):
        data = self._read()
        data[key] = copy.deepcopy(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _file_config(*, cap=3, explicit_empty=False):
    return {
        "schema": 1,
        "version": 1,
        "timezone": "Europe/Berlin",
        "notification": {"persona": "Ada", "notify_address": "owner",
                         "destination": "platform:chat-7", "quiet_start_hour": 21,
                         "quiet_end_hour": 7, "daily_normal_cap": cap,
                         "cooldown_minutes": 15},
        "sources": {"public_feeds": not explicit_empty, "obligations": True,
                    "mail": True, "calendar": True, "tasks": True},
        "mail": {"accounts": ["sample-work", "personal"],
                 "aliases": {"sample-work": "work"}},
        "calendar": {"accounts": ["work-cal"],
                     "calendars": ["primary", "team@group.calendar.google.com"]},
        "tasks": {"accounts": ["task-work"], "lists": ["My Tasks"], "adapter": "tasks"},
        "public_interests": [] if explicit_empty else ["distributed systems"],
        "public_sources": [] if explicit_empty else [{
            "kind": "github_releases",
            "url": "https://api.github.com/repos/example/project/releases"}],
        "adaptive": {"mode": "shadow", "privacy_review": True, "inferred_goals": False},
        "health": {"critical_sources": ["mail:sample-work"]},
        "bounds": {"collector_budget_seconds": 45, "max_candidates_per_kind": 20,
                   "feed_max_sources_per_run": 1},
        "adapters": {"productivity_secretary": True, "gws_calendar": False},
        "advanced": {"legacy_policy": {"custom_legacy_field": "keep-me"}},
    }


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class _DataDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="secretary-051-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = {key: os.environ.get(key) for key in (
            paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(self.root / "data")
        os.environ["HERMES_HOME"] = str(self.root / "home")
        paths.ensure_data_dirs()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _ns(self, action, **kw):
        defaults = {"action": action, "file": None, "from_policy": None,
                    "force": False, "dry_run": False, "json": True, "data_dir": None}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def _configure(self, action, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.configure_command(self._ns(action, **kw))
        return rc, json.loads(buf.getvalue())


class ProjectionRepairTests(_DataDir):
    def test_one_field_manual_save_and_project_preserve_scope_and_custom_url(self):
        payload = _file_config(cap=3)
        path = _write_json(paths.user_config_path(), payload)
        before = path.read_bytes()
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, result = self._configure("project")
            self.assertEqual(rc, 0, result)
            self.assertEqual(result["status"], "projected")
            self.assertTrue(result["native_projection"]["backup"]["created"])
        self.assertEqual(path.read_bytes(), before,
                         "projection must never rewrite the existing config file")
        # A real form save changes exactly one ordinary key.
        ctx.set_config("daily_normal_cap", 9)
        loaded = uc.load_user_config(path, native=ctx)
        cfg = loaded.config
        self.assertEqual(cfg["notification"]["daily_normal_cap"], 9)
        self.assertEqual(cfg["mail"]["accounts"], ["sample-work", "personal"])
        self.assertEqual(cfg["calendar"]["calendars"],
                         ["primary", "team@group.calendar.google.com"])
        self.assertEqual(cfg["tasks"]["lists"], ["My Tasks"])
        self.assertEqual(cfg["public_interests"], ["distributed systems"])
        self.assertEqual(cfg["public_sources"][0]["url"],
                         "https://api.github.com/repos/example/project/releases")
        self.assertEqual(cfg["advanced"]["legacy_policy"],
                         {"custom_legacy_field": "keep-me"})
        # Re-running the repair is a true no-op: no write and no extra backup.
        backups = len(config_backup.list_backups(paths.data_root()))
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, result = self._configure("project")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(ctx.writes[-1], ("daily_normal_cap", 9))
        self.assertEqual(len(config_backup.list_backups(paths.data_root())), backups)

    def test_explicit_empty_lists_stay_empty_and_do_not_inherit_seed_urls(self):
        policy = json.loads(paths.default_policy_path().read_text(encoding="utf-8"))
        policy["feed_seed_sources"] = [{"kind": "feed", "url": "https://example.test/seed"}]
        _write_json(paths.policy_path(), policy)
        path = _write_json(paths.user_config_path(), _file_config(explicit_empty=True))
        before = path.read_bytes()
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, result = self._configure("project")
        self.assertEqual(rc, 0, result)
        self.assertEqual(ctx.settings["public_interests"], [])
        self.assertEqual(ctx.settings["public_sources"], [])
        loaded = uc.load_user_config(path, native=ctx)
        self.assertEqual(loaded.config["public_interests"], [])
        self.assertEqual(loaded.config["public_sources"], [])
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_native_fails_closed_without_write_or_backup(self):
        path = _write_json(paths.user_config_path(), _file_config())
        before = path.read_bytes()
        ctx = FormCtx({"user_config": "{not valid json", "daily_normal_cap": 9})
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, result = self._configure("project")
        self.assertEqual(rc, 1, result)
        self.assertFalse(result["ok"])
        self.assertEqual(ctx.writes, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(config_backup.list_backups(paths.data_root()), [])

    def test_backup_failure_prevents_every_projection_write(self):
        path = _write_json(paths.user_config_path(), _file_config())
        before = path.read_bytes()
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(config_backup, "create_backup",
                                  side_effect=config_backup.BackupError("no space")):
            rc, result = self._configure("project")
        self.assertEqual(rc, 1, result)
        self.assertEqual(ctx.writes, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("BackupError", result["native_projection"]["error"])

    def test_backup_manifest_hash_parity_and_exact_record_scope(self):
        data = paths.data_root()
        _write_json(data / "user_config.json", _file_config())
        _write_json(data / "preferences.json", {"schema": 1, "explicit": []})
        (data / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
        _write_json(data / "auth.json", {"token": "do-not-copy"})
        _write_json(data / "host_memory.json", {"private": "do-not-copy"})
        _write_json(data / "unrelated_config.json", {"x": 1})
        ctx = FormCtx()
        backup = config_backup.create_backup(data, native=ctx,
                                             state=MemoryState(), reason="test")
        names = {row["name"] for row in backup["files"]}
        self.assertEqual(names, {"user_config.json", "preferences.json"})
        verified = config_backup.verify_backup(backup["path"])
        self.assertTrue(verified["ok"], verified)
        for row in backup["files"]:
            actual = (Path(backup["path"]) / row["name"]).read_bytes()
            self.assertEqual(__import__("hashlib").sha256(actual).hexdigest(),
                             row["sha256"])
            self.assertEqual(len(actual), row["bytes"])
        self.assertFalse(any("do-not-copy" in (Path(backup["path"]) / name).read_text(
            encoding="utf-8", errors="ignore") for name in names if name.endswith(".json")))

    def test_backup_is_owner_only_and_restorable(self):
        data = paths.data_root()
        config_path = _write_json(data / "user_config.json", _file_config(cap=3))
        prefs_path = _write_json(data / "preferences.json",
                                {"schema": 1, "explicit": [], "inferred": [],
                                 "snoozes": [], "paused": {"active": False}})
        ctx = FormCtx({"daily_normal_cap": 5, "public_interests": []})
        state = MemoryState()
        state.set("onboarding_state", {"status": "in_progress", "settings": {"timezone": "UTC"}})
        backup = config_backup.create_backup(data, native=ctx, state=state,
                                             reason="restore test")
        backup_dir = Path(backup["path"])
        self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
        for row in backup["files"]:
            self.assertEqual((backup_dir / row["name"]).stat().st_mode & 0o777, 0o600)
        # Change every relevant record, then restore the verified snapshot.
        _write_json(config_path, _file_config(cap=99))
        _write_json(prefs_path, {"schema": 1, "paused": {"active": True}})
        ctx.set_config("daily_normal_cap", 99)
        state.set("onboarding_state", {"status": "cancelled"})
        restored = config_backup.restore_backup(backup_dir, data, native=ctx, state=state)
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))
                         ["notification"]["daily_normal_cap"], 3)
        self.assertFalse(json.loads(prefs_path.read_text(encoding="utf-8"))
                         ["paused"]["active"])
        self.assertEqual(ctx.settings["daily_normal_cap"], 5)
        self.assertEqual(state.get("onboarding_state")["status"], "in_progress")

    def test_setup_kept_backfills_projection_without_rewriting_config(self):
        default_policy = json.loads(
            paths.default_policy_path().read_text(encoding="utf-8"))
        _write_json(paths.policy_path(), default_policy)
        path = _write_json(paths.user_config_path(), _file_config(cap=4))
        before = path.read_bytes()
        ctx = FormCtx()

        def checks(_fixture):
            return {"self_test_rc": 0, "dry_run_rc": 0,
                    "self_test": {"total": 1, "passed": 1, "failed": 0}}

        ns = sc.setup_command.__globals__  # keep patch scope explicit
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(sc, "_run_offline_checks", checks):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sc.setup_command(argparse.Namespace(
                    force=False, no_cron=True, json=True, fixture="healthy_empty",
                    deliver=None, data_dir=None, import_from=None,
                    import_dry_run=False, with_preparation=False), backend=None)
        self.assertEqual(rc, 0, buf.getvalue())
        result = json.loads(buf.getvalue())
        self.assertEqual(result["policy"]["action"], "kept")
        self.assertEqual(result["user_config"]["action"], "kept")
        self.assertTrue(result["native_projection"]["projected"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(ctx.settings["daily_normal_cap"], 4)
        self.assertEqual(ctx.settings["public_interests"], ["distributed systems"])


class OnboardingConvergenceTests(_DataDir):
    def _service(self, ctx, state=None):
        return onboarding.OnboardingService(ctx, state=state or ctx.state)

    def _ctx(self, settings=None, state=None):
        ctx = FormCtx(settings)
        ctx.state = state or MemoryState()
        return ctx

    def test_native_form_edit_updates_stale_onboarding_snapshot(self):
        ctx = self._ctx()
        svc = self._service(ctx)
        svc.update(settings={"timezone": "Europe/London", "daily_allowance": 2},
                   destination="local", grants={"mail": True},
                   source_scope={"mail_accounts": ["sample-work"]},
                   explicit_consent=True)
        svc.confirm(user_confirmed=True, apply_settings=True)
        # Manual native form edit after onboarding saved its snapshot.
        ctx.set_config("daily_normal_cap", 9)
        reopened = self._service(ctx)
        info = reopened.inspect()
        self.assertEqual(info["state"]["settings"]["daily_allowance"], 9)
        self.assertTrue(info["state"]["grants"]["mail"])
        self.assertEqual(info["state"]["source_scope"]["mail_accounts"], ["sample-work"])
        # A later onboarding change still preserves scope and the form value.
        reopened.update(settings={"quiet_start_hour": 22})
        reopened.confirm(user_confirmed=True, apply_settings=True)
        loaded = uc.load_user_config(native=ctx)
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)
        self.assertEqual(loaded.config["mail"]["accounts"], ["sample-work"])
        self.assertTrue(loaded.config["sources"]["mail"])
        self.assertEqual(loaded.config["notification"]["quiet_start_hour"], 22)

    def test_unsaved_onboarding_choice_is_not_clobbered_by_old_config(self):
        ctx = self._ctx({"user_config": _file_config()})
        svc = self._service(ctx)
        # The old file says platform:chat-7; the owner is choosing telegram and
        # has not confirmed yet, so a read must not silently revert the choice.
        svc.update(destination="telegram")
        reopened = self._service(ctx)
        self.assertEqual(reopened.inspect()["state"]["destination"]["target"], "telegram")

    def test_public_grant_uses_the_public_feeds_switch(self):
        ctx = self._ctx()
        svc = self._service(ctx)
        svc.update(settings={"timezone": "UTC"}, destination="local",
                   grants={"public_sources": True},
                   source_scope={"public_interests": ["marine biology"]},
                   explicit_consent=True)
        svc.confirm(user_confirmed=True, apply_settings=True)
        saved = ctx.settings["user_config"]
        self.assertTrue(saved["sources"]["public_feeds"])
        self.assertEqual(saved["public_interests"], ["marine biology"])
        self.assertNotIn("public_sources", saved["sources"])
        loaded = uc.load_user_config(native=ctx)
        self.assertTrue(loaded.config["sources"]["public_feeds"])

    def test_explicit_empty_scope_converges_after_form_edit(self):
        ctx = self._ctx()
        svc = self._service(ctx)
        svc.update(settings={"timezone": "UTC"}, destination="local",
                   grants={"mail": True},
                   source_scope={"mail_accounts": ["sample-work"]},
                   explicit_consent=True)
        svc.confirm(user_confirmed=True, apply_settings=True)
        payload = copy.deepcopy(ctx.settings["user_config"])
        payload["mail"]["accounts"] = []
        marker = payload.setdefault("advanced", {}).setdefault(
            "onboarding_provenance", {"schema": 1, "known_fields": []})
        marker.setdefault("known_fields", [])
        if "mail.accounts" not in marker["known_fields"]:
            marker["known_fields"].append("mail.accounts")
        ctx.set_config("user_config", payload)
        info = self._service(ctx).inspect()
        self.assertEqual(info["state"]["source_scope"]["mail_accounts"], [])
        self.assertIn("scope:mail_accounts", info["state"]["decided"])

    def test_persisted_onboarding_survives_reopen_without_auto_interview(self):
        state_path = self.root / "state.json"
        ctx = self._ctx(state=FileState(state_path))
        svc = self._service(ctx)
        self.assertIsNotNone(conversation.invitation_context(
            ctx, svc, platform="telegram", user_message="hello"))
        svc.update(settings={"timezone": "Europe/London"})
        before = json.loads(state_path.read_text(encoding="utf-8"))
        # Simulate disable -> enable: fresh context + service over the same
        # owner-only state file; registration alone must not start an interview.
        ctx2 = self._ctx(state=FileState(state_path))
        try:
            PKG.register(ctx2)
        finally:
            sc.set_native_context(None)
        after_register = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(before, after_register)
        svc2 = self._service(ctx2)
        info = svc2.inspect()
        self.assertEqual(info["state"]["status"], "in_progress")
        self.assertEqual(info["state"]["settings"]["timezone"], "Europe/London")
        self.assertIsNone(conversation.invitation_context(
            ctx2, svc2, platform="telegram", user_message="hello again"))

    def test_repeated_setup_pending_and_complete_never_resets_or_interviews(self):
        ctx = self._ctx()
        svc = self._service(ctx)
        svc.update(settings={"timezone": "UTC"}, destination="local")
        svc.confirm(user_confirmed=True, apply_settings=True)
        before = copy.deepcopy(svc.inspect()["state"])
        text = conversation.command_text(ctx, svc, "setup")
        self.assertIn("delivery verification is still pending", text)
        self.assertNotIn("sec-cont-", text)
        self.assertNotIn(conversation.PENDING_KEY, ctx.state.data)
        self.assertEqual(before, svc.inspect()["state"])
        # A second review is equally non-destructive.
        conversation.command_text(ctx, svc, "setup")
        self.assertEqual(before, svc.inspect()["state"])


class HostFormSchemaTests(unittest.TestCase):
    def test_production_host_fields_are_humanised_and_match_real_projection(self):
        try:
            from hermes_cli.plugins_settings import plugin_settings_fields
        except Exception as exc:  # noqa: BLE001 - host module is optional offline
            raise unittest.SkipTest(f"host settings module unavailable: {type(exc).__name__}")
        fields = plugin_settings_fields("hermes-proactive-secretary", ROOT)
        self.assertTrue(fields)
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(set(by_key), set(uc.NATIVE_FLAT_FIELDS) | {
            "primary_provider", "primary_model", "fallback_provider", "fallback_model",
            "portal_reasoning_effort", "portal_cadence"})
        import yaml
        spec = yaml.safe_load((ROOT / "plugin.yaml").read_text(
            encoding="utf-8"))["config_schema"]["portal_reasoning_effort"]
        self.assertEqual((spec["type"], spec["default"]), ("str", ""))
        cadence = yaml.safe_load((ROOT / "plugin.yaml").read_text(
            encoding="utf-8"))["config_schema"]["portal_cadence"]
        self.assertEqual((cadence["type"], cadence["default"]), ("str", "3h"))
        self.assertEqual(cadence["choices"], ["3h", "6h"])
        self.assertEqual(by_key["portal_cadence"]["default"], "3h")
        for field in fields:
            self.assertTrue(field["label"], field["key"])
            self.assertTrue(field["description"], field["key"])
            self.assertNotEqual(field["label"], field["key"], field["key"])
        self.assertIn("connected", by_key["mail_enabled"]["description"].lower())
        self.assertIn("account", by_key["calendar_enabled"]["description"].lower())
        self.assertIn("never a target", by_key["daily_normal_cap"]["description"].lower())
        # Real manifest defaults are neutral, and our projection agrees exactly.
        ordinary = {key: field.get("default") for key, field in by_key.items()
                    if key in uc.NATIVE_FLAT_FIELDS}
        self.assertEqual(ordinary, uc.native_flat_values(uc.default_config()))
        ctx = FormCtx()
        uc.project_native_settings(ctx, uc.default_config())
        self.assertEqual(ctx.settings, ordinary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
