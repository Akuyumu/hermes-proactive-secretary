"""Real-settings seam repair: one validated owner, per-field native overlay,
explicit file compatibility and configure/migrate projection.

Offline only. The host-integration test runs the real Hermes launcher in a
synthetic HOME/HERMES_HOME and imports the installed host source READ ONLY; it
never touches the real profile, config, credentials or plugins.
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
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig as uc  # noqa: E402


def _load_plugin_package():
    name = "hermes_proactive_secretary_settings_seam_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


class FormCtx:
    """Mirror of the host's per-key ``get_config``/``set_config`` seam."""

    def __init__(self, settings=None):
        self.settings = copy.deepcopy(dict(settings or {}))
        self.writes: list[tuple[str, object]] = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self.settings[key] = copy.deepcopy(value)


def _file_config(cap=3):
    return {
        "schema": 1,
        "version": 1,
        "timezone": "Europe/Berlin",
        "notification": {
            "persona": "Ada",
            "notify_address": "owner",
            "destination": "platform:chat-7",
            "quiet_start_hour": 21,
            "quiet_end_hour": 7,
            "daily_normal_cap": cap,
            "cooldown_minutes": 15,
        },
        "sources": {"public_feeds": True, "obligations": True,
                    "mail": True, "calendar": True, "tasks": True},
        "mail": {"accounts": ["sample-work", "personal"],
                 "aliases": {"sample-work": "work"}},
        "calendar": {"accounts": ["work-cal"],
                     "calendars": ["primary", "team@group.calendar.google.com"]},
        "tasks": {"accounts": ["task-work"], "lists": ["My Tasks"], "adapter": "tasks"},
        "public_interests": ["distributed systems"],
        "public_sources": [{"kind": "github_releases",
                            "url": "https://api.github.com/repos/x/y/releases"}],
        "adaptive": {"mode": "shadow", "privacy_review": True, "inferred_goals": False},
        "health": {"critical_sources": ["mail:sample-work"]},
        "bounds": {"collector_budget_seconds": 45, "max_candidates_per_kind": 20,
                   "feed_max_sources_per_run": 1},
        "adapters": {"productivity_secretary": True, "gws_calendar": False},
        "advanced": {"legacy_policy": {"custom_legacy_field": "keep-me"}},
    }


def _write_config(root: Path, payload) -> Path:
    path = root / "user_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class NativeOverlayReproTests(unittest.TestCase):
    def test_flat_edit_over_file_preserves_unrelated_scope(self):
        # Parent repro 1: native={daily_normal_cap:9} over a file with mail
        # sample-work enabled must not erase the file's accounts/mail flag.
        file_cfg = _file_config(cap=3)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = _write_config(Path(tmp), file_cfg)
            for native in ({"daily_normal_cap": 9}, FormCtx({"daily_normal_cap": 9})):
                loaded = uc.load_user_config(path, native=native)
                cfg = loaded.config
                self.assertEqual(loaded.source, "native")
                self.assertEqual(cfg["notification"]["daily_normal_cap"], 9)
                self.assertEqual(cfg["mail"]["accounts"], ["sample-work", "personal"])
                self.assertEqual(cfg["mail"]["aliases"], {"sample-work": "work"})
                self.assertTrue(cfg["sources"]["mail"])
                self.assertTrue(cfg["sources"]["calendar"])
                self.assertTrue(cfg["sources"]["tasks"])
                self.assertEqual(cfg["calendar"]["calendars"],
                                 ["primary", "team@group.calendar.google.com"])
                self.assertEqual(cfg["tasks"]["lists"], ["My Tasks"])
                self.assertEqual(cfg["tasks"]["adapter"], "tasks")
                self.assertEqual(cfg["bounds"]["collector_budget_seconds"], 45)
                self.assertEqual(cfg["bounds"]["max_candidates_per_kind"], 20)
                self.assertEqual(cfg["notification"]["destination"], "platform:chat-7")
                overrides = uc.policy_overrides(cfg)
                self.assertEqual(overrides["daily_normal_cap"], 9)
                self.assertEqual(overrides["mail_accounts"], ["sample-work", "personal"])
                self.assertEqual(overrides["custom_legacy_field"], "keep-me")

    def test_flat_edit_wins_over_opaque_native_without_erasing_it(self):
        # Parent repro 2: the real form writes flat keys; an older opaque
        # user_config must not shadow that edit or lose its unrelated fields.
        opaque = _file_config(cap=5)
        payload = {"user_config": opaque, "daily_normal_cap": 9,
                   "timezone": "Asia/Tokyo"}
        for native in (payload, FormCtx(payload)):
            loaded = uc.load_user_config(None, native=native)
            cfg = loaded.config
            self.assertEqual(loaded.source, "native")
            self.assertEqual(cfg["notification"]["daily_normal_cap"], 9)
            self.assertEqual(cfg["timezone"], "Asia/Tokyo")
            self.assertEqual(cfg["mail"]["accounts"], ["sample-work", "personal"])
            self.assertTrue(cfg["sources"]["mail"])
            self.assertTrue(cfg["sources"]["calendar"])
            self.assertTrue(cfg["sources"]["tasks"])
            self.assertEqual(cfg["tasks"]["lists"], ["My Tasks"])
            self.assertEqual(cfg["bounds"]["max_candidates_per_kind"], 20)
            self.assertEqual(cfg["advanced"]["legacy_policy"],
                             {"custom_legacy_field": "keep-me"})

    def test_opaque_native_still_preferred_over_file_for_unedited_fields(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = _write_config(Path(tmp), _file_config(cap=3))
            native = FormCtx({"user_config": {
                "schema": 1, "version": 1, "timezone": "America/New_York",
                "notification": {"daily_normal_cap": 5},
                "mail": {"accounts": ["native-mail"]},
            }})
            loaded = uc.load_user_config(path, native=native)
            self.assertEqual(loaded.source, "native")
            self.assertEqual(loaded.config["timezone"], "America/New_York")
            self.assertEqual(loaded.config["mail"]["accounts"], ["native-mail"])
            self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 5)

    def test_malformed_authoritative_native_payload_still_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = _write_config(Path(tmp), _file_config())
            for native in (FormCtx({"user_config": "{not json", "daily_normal_cap": 9}),
                           FormCtx({"user_config": [1, 2]})):
                with self.assertRaises(uc.ConfigError):
                    uc.load_user_config(path, native=native)

    def test_malformed_flat_control_fails_closed_even_with_opaque(self):
        native = FormCtx({"user_config": _file_config(cap=5), "daily_normal_cap": -1})
        with self.assertRaises(uc.ConfigError):
            uc.load_user_config(None, native=native)

    def test_flat_edit_never_silently_migrates_a_stale_file(self):
        stale = _file_config(cap=3)
        stale["version"] = 0
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = _write_config(Path(tmp), stale)
            with self.assertRaises(uc.ConfigError) as caught:
                uc.load_user_config(path, native={"daily_normal_cap": 9})
            self.assertIn("migrate", str(caught.exception))
            loaded = uc.load_user_config(path, native={"daily_normal_cap": 9},
                                         migrate=True)
            self.assertTrue(loaded.migrated)
            self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)
            self.assertEqual(loaded.config["mail"]["accounts"],
                             ["sample-work", "personal"])

    def test_flat_edit_never_silently_migrates_a_stale_opaque_payload(self):
        stale = _file_config(cap=5)
        stale["version"] = 0
        native = FormCtx({"user_config": stale, "daily_normal_cap": 9})
        with self.assertRaises(uc.ConfigError) as caught:
            uc.load_user_config(None, native=native)
        self.assertIn("migrate", str(caught.exception))
        loaded = uc.load_user_config(None, native=native, migrate=True)
        self.assertTrue(loaded.migrated)
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)

    def test_absent_native_and_file_are_neutral_not_account_traps(self):
        loaded = uc.load_user_config(None, native=FormCtx({}))
        self.assertFalse(loaded.present)
        self.assertEqual(loaded.source, "defaults")
        self.assertEqual(loaded.config["mail"]["accounts"], [])
        self.assertFalse(loaded.config["sources"]["mail"])


class ProjectionTests(unittest.TestCase):
    def test_projection_covers_every_manifest_control_and_preserves_scope(self):
        cfg = uc.validate_config(_file_config(cap=3))
        ctx = FormCtx()
        written = uc.project_native_settings(ctx, cfg)
        self.assertEqual(set(written), set(uc.NATIVE_FLAT_FIELDS))
        self.assertEqual(set(ctx.settings), set(uc.NATIVE_FLAT_FIELDS))
        self.assertEqual(ctx.settings["daily_normal_cap"], 3)
        # A real form save changes one key only; the file still supplies all
        # unrelated authorised scope/preferences/bounds.
        ctx.set_config("daily_normal_cap", 9)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = _write_config(Path(tmp), _file_config(cap=3))
            loaded = uc.load_user_config(path, native=ctx)
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)
        self.assertEqual(loaded.config["mail"]["accounts"], ["sample-work", "personal"])
        self.assertEqual(loaded.config["calendar"]["accounts"], ["work-cal"])
        self.assertEqual(loaded.config["tasks"]["accounts"], ["task-work"])
        self.assertEqual(loaded.config["bounds"]["max_candidates_per_kind"], 20)
        self.assertEqual(loaded.config["advanced"]["legacy_policy"],
                         {"custom_legacy_field": "keep-me"})

    def test_projection_is_idempotent_and_writes_only_changes(self):
        cfg = uc.validate_config(_file_config(cap=3))
        ctx = FormCtx()
        self.assertTrue(uc.project_native_settings(ctx, cfg))
        first = list(ctx.writes)
        self.assertEqual(uc.project_native_settings(ctx, cfg), [])
        self.assertEqual(ctx.writes, first)

    def test_projection_validates_before_any_write(self):
        ctx = FormCtx()
        bad = _file_config(cap=-1)
        with self.assertRaises(uc.ConfigError):
            uc.project_native_settings(ctx, bad)
        self.assertEqual(ctx.settings, {})
        self.assertEqual(ctx.writes, [])

    def test_projection_does_not_widen_scope_or_add_secrets(self):
        ctx = FormCtx()
        uc.project_native_settings(ctx, uc.validate_config(_file_config()))
        self.assertEqual(set(ctx.settings), set(uc.NATIVE_FLAT_FIELDS))
        self.assertFalse(any("account" in key or "alias" in key for key in ctx.settings))
        self.assertNotIn("api_key", ctx.settings)
        self.assertNotIn("token", ctx.settings)


class ManifestSeamTests(unittest.TestCase):
    def test_manifest_uses_host_field_mapping_shape_and_defaults(self):
        import yaml
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest["config_schema"]
        route_fields = {"primary_provider", "primary_model",
                        "fallback_provider", "fallback_model"}
        self.assertEqual(set(schema), set(uc.NATIVE_FLAT_FIELDS) | route_fields
                         | {"portal_reasoning_effort", "portal_cadence"})
        self.assertEqual(
            (schema["portal_reasoning_effort"]["type"],
             schema["portal_reasoning_effort"]["default"]), ("str", ""))
        self.assertEqual(schema["portal_cadence"]["default"], "3h")
        self.assertEqual(schema["portal_cadence"]["choices"], ["3h", "6h"])
        self.assertNotIn("properties", schema)
        for key, spec in schema.items():
            self.assertIsInstance(spec, dict)
            self.assertIn(str(spec.get("type")).lower(),
                          {"str", "int", "bool", "array"})
            self.assertIn("default", spec, key)
        defaults = {key: spec["default"] for key, spec in schema.items()}
        ordinary = {key: value for key, value in defaults.items()
                    if key in uc.NATIVE_FLAT_FIELDS}
        self.assertEqual(ordinary, uc.native_flat_values(uc.default_config()))
        self.assertEqual(defaults["destination"], "")
        self.assertEqual(defaults["daily_normal_cap"], "")
        self.assertEqual({key: defaults[key] for key in route_fields},
                         {key: "" for key in route_fields})


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(paths.DATA_ENV)
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        os.environ[paths.DATA_ENV] = str(Path(self._tmp.name) / "data")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop(paths.DATA_ENV, None)
        else:
            os.environ[paths.DATA_ENV] = self._old


def _ns(action, **kw):
    defaults = {"action": action, "file": None, "from_policy": None,
                "force": False, "dry_run": False, "json": True, "data_dir": None}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _json_out(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, json.loads(buf.getvalue())


class ConfigureProjectionTests(_DataDirMixin):
    def test_migrate_projects_form_values_then_ui_save_roundtrips(self):
        legacy = Path(self._tmp.name) / "legacy_policy.json"
        legacy.write_text(json.dumps({
            "timezone": "Asia/Singapore",
            "daily_normal_cap": 4,
            "feeds_enabled": False,
            "obligations_enabled": False,
            "quiet_start_hour": 22,
            "quiet_end_hour": 6,
            "mail_accounts": ["sample-work"],
            "mail_enabled": True,
            "calendar_accounts": ["work-cal"],
            "calendar_ids": ["primary"],
            "calendar_enabled": True,
            "task_accounts": ["task-work"],
            "task_lists": ["My Tasks"],
            "task_adapter": "tasks",
            "tasks_enabled": True,
            "custom_legacy_field": "keep-me",
        }), encoding="utf-8")
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, payload = _json_out(sc.configure_command,
                                    _ns("migrate", from_policy=str(legacy)))
            self.assertEqual(rc, 0, payload)
            self.assertEqual(payload["status"], "written")
            self.assertTrue(payload["native_projection"]["projected"])
            # The native form now shows migrated effective values, not neutral
            # manifest defaults.
            self.assertEqual(ctx.settings["timezone"], "Asia/Singapore")
            self.assertEqual(ctx.settings["daily_normal_cap"], 4)
            self.assertEqual(ctx.settings["quiet_start_hour"], 22)
            self.assertFalse(ctx.settings["public_feeds"])
            self.assertTrue(ctx.settings["mail_enabled"])
            self.assertTrue(ctx.settings["calendar_enabled"])
            self.assertTrue(ctx.settings["tasks_enabled"])

            # Real UI save: one ordinary field only.
            ctx.set_config("daily_normal_cap", 9)
            loaded = uc.load_user_config(paths.user_config_path(), native=ctx)
            cfg = loaded.config
            self.assertEqual(cfg["notification"]["daily_normal_cap"], 9)
            self.assertEqual(cfg["mail"]["accounts"], ["sample-work"])
            self.assertEqual(cfg["calendar"]["accounts"], ["work-cal"])
            self.assertEqual(cfg["tasks"]["accounts"], ["task-work"])
            self.assertEqual(cfg["tasks"]["lists"], ["My Tasks"])
            self.assertEqual(
                cfg["advanced"]["legacy_policy"]["custom_legacy_field"], "keep-me")
            overrides = uc.policy_overrides(cfg)
            self.assertEqual(overrides["mail_accounts"], ["sample-work"])
            self.assertEqual(overrides["custom_legacy_field"], "keep-me")

            # Form field rendering (same manifest + current settings) agrees.
            import yaml
            schema = yaml.safe_load(
                (ROOT / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
            rendered = {key: ctx.settings.get(key, spec["default"])
                        for key, spec in schema.items()}
            self.assertEqual(rendered["daily_normal_cap"], 9)
            self.assertEqual(rendered["timezone"], "Asia/Singapore")
            self.assertTrue(rendered["mail_enabled"])

            # A repeated migrate without --force keeps the file and does not
            # re-project or clobber the newer form value.
            ctx.writes.clear()
            rc, payload = _json_out(sc.configure_command,
                                    _ns("migrate", from_policy=str(legacy)))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "kept")
            self.assertFalse(payload["native_projection"]["projected"])
            self.assertEqual(ctx.writes, [])
            self.assertEqual(ctx.settings["daily_normal_cap"], 9)

    def test_init_projects_neutral_example(self):
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, payload = _json_out(sc.configure_command, _ns("init"))
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "written")
        self.assertEqual(ctx.settings["timezone"], "UTC")
        self.assertEqual(ctx.settings["daily_normal_cap"], "")
        self.assertEqual(ctx.settings["destination"], "local")
        self.assertFalse(ctx.settings["mail_enabled"])

    def test_migrate_existing_stale_file_in_place_and_projects(self):
        path = paths.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = _file_config(cap=3)
        stale["version"] = 0
        path.write_text(json.dumps(stale), encoding="utf-8")
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, payload = _json_out(sc.configure_command, _ns("migrate"))
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "written")
        self.assertEqual(payload["from_config"], str(path))
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["version"], uc.CONFIG_VERSION)
        self.assertEqual(written["mail"]["accounts"], ["sample-work", "personal"])
        self.assertEqual(ctx.settings["daily_normal_cap"], 3)
        loaded = uc.load_user_config(path, native=ctx)
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 3)
        self.assertEqual(loaded.config["mail"]["accounts"],
                         ["sample-work", "personal"])

    def test_migrate_existing_current_file_reprojects_form_values(self):
        path = paths.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _file_config(cap=3)
        path.write_text(json.dumps(current), encoding="utf-8")
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, payload = _json_out(sc.configure_command, _ns("migrate"))
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "written")
        self.assertEqual(payload["from_config"], str(path))
        self.assertEqual(ctx.settings["daily_normal_cap"], 3)
        self.assertEqual(ctx.settings["timezone"], "Europe/Berlin")
        self.assertTrue(ctx.settings["mail_enabled"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"],
                         uc.CONFIG_VERSION)

    def test_invalid_migrate_writes_no_file_and_no_native_settings(self):
        legacy = Path(self._tmp.name) / "bad_policy.json"
        legacy.write_text(json.dumps({"daily_normal_cap": -1}), encoding="utf-8")
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, payload = _json_out(sc.configure_command,
                                    _ns("migrate", from_policy=str(legacy)))
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertFalse(paths.user_config_path().exists())
        self.assertEqual(ctx.writes, [])


class RealHostFormTests(unittest.TestCase):
    """Exercise the installed Hermes form writer in a synthetic profile."""

    @staticmethod
    def _runtime():
        hermes = shutil.which("hermes")
        if not hermes:
            raise unittest.SkipTest("hermes launcher unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hh = Path(tmp) / "hermes-home"
            home.mkdir()
            hh.mkdir()
            env = dict(os.environ, HOME=str(home), HERMES_HOME=str(hh),
                       TMPDIR=str(Path(tmp) / "tmp"))
            Path(env["TMPDIR"]).mkdir()
            proc = subprocess.run([hermes, "--print-runtime-command"],
                                  env=env, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"runtime command unavailable: {proc.stderr.strip()[:160]}")
        command = json.loads(proc.stdout)
        joined = " ".join(command)
        found = re.search(r"sys\.path\.insert\(0, '([^']+)'\)", joined)
        if not found or not Path(found.group(1)).is_dir():
            raise unittest.SkipTest("host source root not discoverable")
        return command[0], found.group(1)

    def test_real_save_and_form_projection_feed_the_candidate_loader(self):
        python, host_root = self._runtime()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            home, hh = root / "home", root / "hermes-home"
            home.mkdir()
            hh.mkdir()
            script = f"""
import json, sys
sys.path.insert(0, {host_root!r})
from hermes_cli.plugins_settings import save_plugin_settings, plugin_settings_fields
pid = "hermes-proactive-secretary"
plugin_dir = {str(ROOT)!r}
before = {{f["key"]: f.get("value") for f in plugin_settings_fields(pid, plugin_dir)}}
written = save_plugin_settings(pid, plugin_dir, {{"daily_normal_cap": "9", "mail_enabled": True}})
after = {{f["key"]: f.get("value") for f in plugin_settings_fields(pid, plugin_dir)}}
from hermes_cli.config import load_config_readonly
payload = load_config_readonly()
settings = payload["plugins"]["entries"][pid]["settings"]
print("RESULT:" + json.dumps({{"before": before, "written": written,
                               "after": after, "settings": settings}}))
"""
            env = dict(os.environ, HOME=str(home), HERMES_HOME=str(hh),
                       TMPDIR=str(root / "tmp"))
            (root / "tmp").mkdir()
            proc = subprocess.run([python, "-c", script], env=env,
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            line = next(line for line in proc.stdout.splitlines()
                        if line.startswith("RESULT:"))
            result = json.loads(line.split("RESULT:", 1)[1])
            route_fields = {"primary_provider", "primary_model",
                            "fallback_provider", "fallback_model"}
            self.assertEqual(set(result["before"]), set(uc.NATIVE_FLAT_FIELDS)
                             | route_fields | {"portal_reasoning_effort",
                                               "portal_cadence"})
            self.assertEqual(result["before"]["portal_reasoning_effort"], "")
            self.assertEqual(result["before"]["portal_cadence"], "3h")
            self.assertEqual(result["before"]["daily_normal_cap"], "")
            self.assertEqual(result["before"]["destination"], "")
            self.assertEqual({key: result["before"][key] for key in route_fields},
                             {key: "" for key in route_fields})
            self.assertEqual(sorted(result["written"]), ["daily_normal_cap", "mail_enabled"])
            self.assertEqual(result["after"]["daily_normal_cap"], "9")
            self.assertTrue(result["after"]["mail_enabled"])
            self.assertEqual(result["settings"],
                             {"daily_normal_cap": "9", "mail_enabled": True})

            with tempfile.TemporaryDirectory(dir=ROOT) as cfg_tmp:
                path = _write_config(Path(cfg_tmp), _file_config(cap=3))
                loaded = uc.load_user_config(path, native=result["settings"])
            self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 9)
            self.assertTrue(loaded.config["sources"]["mail"])
            self.assertEqual(loaded.config["mail"]["accounts"],
                             ["sample-work", "personal"])
            self.assertEqual(loaded.config["calendar"]["calendars"],
                             ["primary", "team@group.calendar.google.com"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
