"""Production-seam checks: default dashboard service, real host loader and profile owner."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig  # noqa: E402
from secretary.secretary_setup_actions import SetupActionError  # noqa: E402

PLUGIN_NAME = "hermes-proactive-secretary"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _tree_digest(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


class _ProfileFixture:
    def __init__(self, *, enabled: bool = True):
        self.root = Path(tempfile.mkdtemp(
            prefix="secretary-051-seams-", dir=os.environ.get("TMPDIR") or None))
        self._finalizer = weakref.finalize(self, shutil.rmtree, self.root, True)
        self.a = self.root / "profile-a"
        self.b = self.root / "profile-b"
        for home in (self.a, self.b):
            home.mkdir(mode=0o700)
        self._write_profile(self.a, enabled=False)
        self._write_profile(self.b, enabled=enabled)
        config = userconfig.default_config()
        config.update({
            "timezone": "Europe/London",
            "sources": {"public_feeds": True, "obligations": False, "mail": True,
                        "calendar": False, "tasks": False},
            "mail": {"accounts": ["synthetic@example.test"], "aliases": {}},
            "public_interests": ["synthetic public"],
        })
        self.b_config = self.b / "plugin-data" / PLUGIN_NAME / "user_config.json"
        _write_json(self.b_config, userconfig.validate_config(config))
        self.b_prefs = self.b / "plugin-data" / PLUGIN_NAME / "preferences.json"
        _write_json(self.b_prefs, {
            "schema": 1, "version": 0, "updated_at": "",
            "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": True, "reason": "synthetic"},
        })
        # A sentinel in profile A proves the default resolver never falls back
        # to the launch profile or its data override.
        self.a_config = self.a / "plugin-data" / PLUGIN_NAME / "user_config.json"
        _write_json(self.a_config, {"sentinel": "profile-a-must-not-change"})
        self.a_override = self.a / "override-data"
        self.a_override.mkdir()
        (self.a_override / "sentinel.json").write_text('{"a": true}', encoding="utf-8")

    @staticmethod
    def _write_profile(home: Path, *, enabled: bool) -> None:
        enabled_line = f"    - {PLUGIN_NAME}\n" if enabled else "    []\n"
        (home / "config.yaml").write_text(
            "plugins:\n"
            "  enabled:\n" + enabled_line +
            f"  entries:\n    {PLUGIN_NAME}:\n      settings: {{}}\n",
            encoding="utf-8")

    def close(self) -> None:
        self._finalizer()

    def snapshot_a(self) -> dict[str, bytes]:
        return _tree_digest(self.a)


class _Dashboard:
    @classmethod
    def load(cls):
        spec = importlib.util.spec_from_file_location(
            "secretary_051_production_dashboard", ROOT / "dashboard" / "plugin_api.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


class DefaultProductionServiceTests(unittest.TestCase):
    def setUp(self):
        self.f = _ProfileFixture()
        self.addCleanup(self.f.close)
        self.api = _Dashboard.load()
        try:
            import hermes_cli.plugins as host_plugins
            import hermes_constants as constants
        except Exception as exc:  # noqa: BLE001 - provisioned host only
            self.skipTest(f"host plugin runtime unavailable: {type(exc).__name__}")
        self.host_plugins = host_plugins
        self.constants = constants
        self._saved_env = {key: os.environ.get(key) for key in
                           ("HERMES_HOME", "HERMES_PROACTIVE_SECRETARY_DATA")}
        os.environ["HERMES_HOME"] = str(self.f.a)
        os.environ["HERMES_PROACTIVE_SECRETARY_DATA"] = str(self.f.a_override)
        self.addCleanup(self._restore_env)
        self.token = constants.set_hermes_home_override(str(self.f.b))
        self.addCleanup(constants.reset_hermes_home_override, self.token)

        from hermes_cli.plugins import LoadedPlugin, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        self.manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
        self.assertIsNotNone(self.manifest)
        self.manager = PluginManager(scope_key=constants.hermes_home_key(self.f.b))
        module = self.manager._load_directory_module(self.manifest)
        self.assertIsNotNone(module)
        self.package = module.__name__
        key = self.manifest.key or self.manifest.name
        self.manager._plugins[key] = LoadedPlugin(
            manifest=self.manifest, module=module, enabled=True)
        patcher = mock.patch.object(host_plugins, "get_plugin_manager",
                                    return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_service_uses_real_owner_paths_and_host_context(self):
        before_a = self.f.snapshot_a()
        service = self.api._service()  # DEFAULT constructor: no path injection
        self.assertEqual(Path(service._config_path), self.f.b_config)
        self.assertEqual(Path(service._preferences_path), self.f.b_prefs)
        self.assertEqual(Path(service._backup_root),
                         self.f.b / "plugin-data" / PLUGIN_NAME)
        self.assertNotIn(str(self.f.a), str(service._config_path))
        self.assertEqual(service.package_name, self.package)

        status = service.status()
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["config"]["timezone"], "Europe/London")
        self.assertNotIn("synthetic@example.test", json.dumps(status))
        self.assertNotIn("synthetic public", json.dumps(status))

        read = service.settings()
        self.assertEqual(read["values"]["timezone"], "Europe/London")
        updated = service.update_settings(values={"timezone": "Asia/Tokyo"},
                                          revision=read["revision"])
        self.assertTrue(updated["changed"])
        self.assertEqual(updated["values"]["timezone"], "Asia/Tokyo")

        # Prepare/commit reset through the default owner paths and real state facade.
        plan = service.prepare_reset()
        state_path = Path(service.state.path)
        self.assertEqual(state_path.parent, self.f.b / "plugin-data" / state_path.parent.name)
        self.assertTrue(state_path.is_file())
        self.assertIsNotNone(service.state.get("setup_reset_challenge"))
        result = service.commit_reset(challenge=plan["challenge"], phrase="RESET",
                                      explicit=True)
        self.assertTrue(result["changed"])
        backup = (self.f.b / "plugin-data" / PLUGIN_NAME / "backups" / result["backup_id"])
        self.assertTrue(backup.is_dir(), backup)
        self.assertIsNone(service.state.get("onboarding_state"))
        self.assertEqual(_tree_digest(self.f.a), before_a)

    def test_foreign_path_modules_cannot_poison_owner_resolution(self):
        before_a = self.f.snapshot_a()
        fake = mock.Mock()
        fake.data_root.return_value = self.f.a / "plugin-data" / PLUGIN_NAME
        fake.user_config_path.return_value = self.f.a_config
        fake.preferences_path.return_value = self.f.a / "preferences.json"
        with mock.patch.dict(sys.modules, {"paths": fake,
                                           "proactive_pilot.paths": fake}):
            service = self.api._service()
            self.assertEqual(Path(service._config_path), self.f.b_config)
            self.assertEqual(Path(service._backup_root),
                             self.f.b / "plugin-data" / PLUGIN_NAME)
            self.assertEqual(service.settings()["values"]["timezone"], "Europe/London")
        self.assertEqual(_tree_digest(self.f.a), before_a)

    def test_actual_fastapi_mount_serves_owner_profile_status_and_settings(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(self.api.router, prefix=f"/api/plugins/{PLUGIN_NAME}")
        client = TestClient(app, raise_server_exceptions=False)
        status = client.get(f"/api/plugins/{PLUGIN_NAME}/setup/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["config"]["timezone"], "Europe/London")
        settings = client.get(f"/api/plugins/{PLUGIN_NAME}/settings")
        self.assertEqual(settings.status_code, 200, settings.text)
        self.assertEqual(settings.json()["values"]["timezone"], "Europe/London")
        preview = client.post(f"/api/plugins/{PLUGIN_NAME}/settings/preview", json={
            "revision": settings.json()["revision"],
            "values": {"timezone": "Asia/Tokyo"},
        })
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse(preview.json()["requires_confirmation"])

    def test_manager_from_another_profile_is_refused_not_used(self):
        before_a = self.f.snapshot_a()
        other = self.host_plugins.PluginManager(
            scope_key=self.constants.hermes_home_key(self.f.a))
        with mock.patch.object(self.host_plugins, "get_plugin_manager", return_value=other):
            with self.assertRaises(SetupActionError) as caught:
                self.api._service()
        self.assertEqual(caught.exception.code, "profile_scope_mismatch")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(_tree_digest(self.f.a), before_a)


if __name__ == "__main__":
    unittest.main()
