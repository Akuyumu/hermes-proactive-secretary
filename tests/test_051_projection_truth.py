"""0.5.1 live-readback regressions: configure project dry-run and config truth.

Synthetic configuration/state only. The production registered CLI parser and
handler are exercised in an isolated data directory; no live profile, source,
network, scheduler or installed plugin is touched.
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
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import config_backup  # noqa: E402
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402


def _load_package():
    name = "hermes_proactive_portal_051_projection"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
sc = importlib.import_module(f"{PKG.__name__}.portal.portal_cli")
paths = importlib.import_module(f"{PKG.__name__}.paths")
setup_actions = importlib.import_module("portal.portal_setup_actions")
cron_mod = importlib.import_module(f"{PKG.__name__}.cron")


class FormCtx:
    def __init__(self, settings=None):
        self.settings = copy.deepcopy(dict(settings or {}))
        self.writes = []
        self.plugin_id = "hermes-proactive-portal"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self.settings[key] = copy.deepcopy(value)


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = copy.deepcopy(value)


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def _configured(*, enabled=True, scoped=True):
    config = uc.default_config()
    config["timezone"] = "Europe/Berlin"
    config["notification"].update({
        "destination": "local",
        "daily_normal_cap": 4,
        "quiet_start_hour": 21,
        "quiet_end_hour": 7,
        "cooldown_minutes": 15,
    })
    config["sources"] = {
        "public_feeds": enabled,
        "obligations": enabled,
        "mail": enabled,
        "calendar": enabled,
        "tasks": enabled,
    }
    config["mail"] = {
        "accounts": ["synthetic-mail"] if scoped else [],
        "aliases": {},
    }
    config["calendar"] = {
        "accounts": ["synthetic-calendar"] if scoped else [],
        "calendars": ["synthetic-calendar-id"] if scoped else [],
    }
    config["tasks"] = {
        "accounts": ["synthetic-tasks"] if scoped else [],
        "lists": ["Synthetic Tasks"] if scoped else [],
        "adapter": "tasks",
    }
    config["public_interests"] = ["synthetic interest"] if scoped else []
    config["public_sources"] = ([{"kind": "feed", "url": "https://example.test/feed"}]
                                if scoped else [])
    config["adaptive"] = {
        "mode": "active" if enabled else "disabled",
        "privacy_review": True,
        "inferred_goals": False,
    }
    return uc.validate_config(config)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="secretary-051-projection-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._old = {key: os.environ.get(key) for key in (paths.DATA_ENV, "HERMES_HOME")}
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

    def _run_registered(self, argv):
        parser = argparse.ArgumentParser(prog="hermes portal")
        sc.register_cli(parser)
        args = parser.parse_args(argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.portal_cli_command(args)
        try:
            payload = json.loads(buf.getvalue())
        except ValueError:
            payload = {"raw": buf.getvalue()}
        return rc, payload


class ProjectDryRunTests(_Base):
    def _dry_run(self, ctx):
        before = paths.user_config_path().read_bytes()
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(config_backup, "create_backup") as backup, \
                mock.patch.object(sc, "_backup_before_change") as legacy_backup:
            rc, payload = self._run_registered(
                ["configure", "project", "--dry-run", "--json"])
        self.assertEqual(paths.user_config_path().read_bytes(), before)
        self.assertEqual(ctx.writes, [])
        backup.assert_not_called()
        legacy_backup.assert_not_called()
        return rc, payload, ctx

    def test_dry_run_reports_planned_changes_without_any_write_or_backup(self):
        _write_json(paths.user_config_path(), _configured())
        ctx = FormCtx()
        rc, payload, ctx = self._dry_run(ctx)
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["action"], "project")
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["changed"], sorted(payload["changed"]))
        self.assertEqual(payload["would_write"], payload["changed"])
        self.assertEqual(payload["native_projection"]["written"], [])
        self.assertFalse(payload["native_projection"]["noop"])
        self.assertIsNone(payload["native_projection"]["backup"])
        self.assertEqual(ctx.settings, {})

    def test_dry_run_empty_plan_is_a_noop(self):
        config = _configured()
        _write_json(paths.user_config_path(), config)
        ctx = FormCtx(uc.native_flat_values(config))
        before = copy.deepcopy(ctx.settings)
        rc, payload, ctx = self._dry_run(ctx)
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["changed"], [])
        self.assertTrue(payload["native_projection"]["noop"])
        self.assertEqual(payload["native_projection"]["written"], [])
        self.assertEqual(ctx.settings, before)

    def test_dry_run_malformed_native_fails_closed(self):
        _write_json(paths.user_config_path(), _configured())
        ctx = FormCtx({"user_config": "{not valid json", "daily_normal_cap": 9})
        rc, payload, ctx = self._dry_run(ctx)
        self.assertEqual(rc, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid native", payload["error"])
        self.assertEqual(ctx.settings, {"user_config": "{not valid json",
                                        "daily_normal_cap": 9})

    def test_init_and_migrate_dry_runs_keep_their_no_write_semantics(self):
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(config_backup, "create_backup") as backup:
            rc, payload = self._run_registered(
                ["configure", "init", "--dry-run", "--json"])
            self.assertEqual(rc, 0, payload)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(paths.user_config_path().exists())

            legacy = _write_json(self.root / "legacy_policy.json",
                                 {"timezone": "Asia/Singapore"})
            rc, payload = self._run_registered(
                ["configure", "migrate", "--from-policy", str(legacy),
                 "--dry-run", "--json"])
            self.assertEqual(rc, 0, payload)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(paths.user_config_path().exists())
        self.assertEqual(ctx.writes, [])
        backup.assert_not_called()

    def test_normal_project_snapshots_then_repeats_as_noop(self):
        _write_json(paths.user_config_path(), _configured())
        ctx = FormCtx()
        with mock.patch.object(sc, "get_native_context", return_value=ctx):
            rc, first = self._run_registered(["configure", "project", "--json"])
            self.assertEqual(rc, 0, first)
            self.assertEqual(first["status"], "projected")
            self.assertTrue(first["native_projection"]["backup"]["created"])
            self.assertTrue(ctx.writes)
            backups = len(config_backup.list_backups(paths.data_root()))
            rc, second = self._run_registered(["configure", "project", "--json"])
        self.assertEqual(rc, 0, second)
        self.assertEqual(second["status"], "unchanged")
        self.assertTrue(second["native_projection"]["noop"])
        self.assertEqual(len(config_backup.list_backups(paths.data_root())), backups)


class CapabilityTruthTests(_Base):
    def _fresh_state(self):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "source_health": {
                "stale_after_minutes": 45,
                "sources": {
                    "mail": {"last_success_at": now},
                    "calendar": {"last_success_at": now},
                    "tasks": {"last_success_at": now},
                    "obligation": {"last_success_at": now},
                    "feed:synthetic": {"last_success_at": now},
                },
            },
        }

    def _status(self, ctx, *, mail_available=True, src_available=True):
        src = self.root / "synthetic-secretary-src"
        if src_available:
            src.mkdir(exist_ok=True)
        _write_json(paths.state_dir() / "notify_state.json", self._fresh_state())
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(sc.shutil, "which",
                                  side_effect=lambda name: ("/usr/bin/himalaya"
                                                            if name == "himalaya" and mail_available
                                                            else None)), \
                mock.patch.object(sc, "_secretary_src_dir", return_value=src), \
                mock.patch.object(sc, "_secretary_src", return_value=str(src)), \
                mock.patch.object(sc, "_backend",
                                  return_value=cron_mod.InMemoryCronBackend()):
            return self._run_registered(["status", "--json"])

    def test_status_truth_uses_the_validated_nested_config_owner(self):
        _write_json(paths.user_config_path(), _configured())
        ctx = FormCtx()
        rc, payload = self._status(ctx)
        self.assertEqual(rc, 0, payload)
        self.assertTrue(all(payload["config"]["sources"].values()))
        self.assertEqual(payload["monitoring"], "full")
        self.assertEqual(payload["capabilities"]["monitoring"], "full")
        for name in ("mail", "calendar", "tasks", "public", "obligations"):
            row = payload["capabilities"]["truth"][name]
            self.assertTrue(row["configured"], name)
            self.assertTrue(row["authorized"], name)
            self.assertTrue(row["available"], name)
            self.assertTrue(row["verified"], name)
            self.assertTrue(row["fresh"], name)
            self.assertEqual(row["status"], "verified-fresh", name)
            self.assertEqual(row["remedy"], "", name)

    def test_disabled_sources_stay_off_even_with_scopes(self):
        _write_json(paths.user_config_path(), _configured(enabled=False))
        ctx = FormCtx()
        rc, payload = self._status(ctx)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["monitoring"], "public-only")
        for name in ("mail", "calendar", "tasks", "public", "obligations"):
            row = payload["capabilities"]["truth"][name]
            self.assertFalse(row["configured"], name)
            self.assertFalse(row["fresh"], name)
            self.assertEqual(row["status"], "disabled", name)
            self.assertTrue(row["remedy"], name)

    def test_enabled_but_unscoped_never_claims_coverage(self):
        _write_json(paths.user_config_path(), _configured(scoped=False))
        ctx = FormCtx()
        rc, payload = self._status(ctx)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["monitoring"], "public-only")
        for name in ("mail", "calendar", "tasks", "public", "obligations"):
            row = payload["capabilities"]["truth"][name]
            self.assertFalse(row["configured"], name)
            self.assertFalse(row["authorized"], name)
            self.assertEqual(row["status"], "enabled-unscoped", name)
            self.assertIn("explicit", row["remedy"].lower(), name)

    def test_unavailable_external_connector_has_a_remedy_not_fake_coverage(self):
        _write_json(paths.user_config_path(), _configured())
        ctx = FormCtx()
        rc, payload = self._status(ctx, mail_available=False, src_available=False)
        self.assertEqual(rc, 0, payload)
        for name in ("mail", "obligations", "calendar", "tasks"):
            row = payload["capabilities"]["truth"][name]
            self.assertFalse(row["available"], name)
            self.assertFalse(row["verified"], name)
            self.assertFalse(row["fresh"], name)
            self.assertEqual(row["status"], "unavailable", name)
            self.assertTrue(row["remedy"], name)

    def test_cli_and_page_source_flags_use_the_same_canonical_owner(self):
        _write_json(paths.user_config_path(), _configured())
        feedback.save_preferences_file(
            paths.preferences_path(), feedback.empty_preferences(),
            datetime.now(timezone.utc), lock=False)
        ctx = FormCtx()
        ctx.state = MemoryState()
        rc, cli_payload = self._status(ctx)
        self.assertEqual(rc, 0, cli_payload)
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(sc.shutil, "which",
                                  side_effect=lambda name: ("/usr/bin/himalaya"
                                                            if name == "himalaya" else None)), \
                mock.patch.object(sc, "_secretary_src_dir",
                                  return_value=self.root / "synthetic-secretary-src"), \
                mock.patch.object(sc, "_secretary_src",
                                  return_value=str(self.root / "synthetic-secretary-src")):
            page = setup_actions.SetupActions(
                ctx, package_name=PKG.__name__,
                config_path=paths.user_config_path(),
                preferences_path=paths.preferences_path(),
                backup_root=paths.data_root()).status()
        self.assertTrue(page["ok"], page)
        expected = {
            "mail": "mail", "calendar": "calendar", "tasks": "tasks",
            "public": "public", "obligations": "obligations",
        }
        page_status = {
            "disabled": "off", "enabled-unscoped": "unconfigured",
            "configured-unverified": "unverified", "configured-stale": "stale",
            "verified-fresh": "fresh", "unavailable": "unavailable",
        }
        for page_name, truth_name in expected.items():
            row = page["sources"][page_name]
            truth = cli_payload["capabilities"]["truth"][truth_name]
            self.assertEqual(row["status"], page_status[truth["status"]], page_name)
            self.assertEqual(row["enabled"],
                             cli_payload["config"]["sources"][
                                 "public_feeds" if page_name == "public" else page_name],
                             page_name)
            self.assertEqual(row["authorized"], truth["authorized"], page_name)
            self.assertEqual(row["verified"], truth["verified"], page_name)
            self.assertEqual(row["fresh"], truth["fresh"], page_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
