"""The cadence control must exist where the owner actually looks.

Two defects this pins down: the Portal cadence was absent from the plugin's own
settings page, and Autonomy mode was parked under "Advanced" instead of the
Setup block. The tests read the shipped desktop page and exercise the REST
handlers with an injected backend, so no live profile is touched.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import types
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_package(name: str = "hermes_proactive_secretary_cadence_ui"):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_package()
JS = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


class DesktopSurfaceTests(unittest.TestCase):
    def test_cadence_control_is_on_the_settings_page(self):
        self.assertIn("How often the portal runs", JS)
        self.assertIn("Apply cadence", JS)
        self.assertIn("cadenceView", JS)
        self.assertIn("/cadence", JS)
        # Options are rendered from the interval list, so assert the list itself.
        self.assertIn("const CADENCE_OPTIONS", JS)
        for minutes in ("180", "360", "540", "720", "1440"):
            self.assertIn(minutes, JS)
        self.assertIn("Every ${minutes / 60} hour", JS)

    def test_cadence_uses_local_state_not_the_config_form(self):
        # The interval is applied through the gate endpoint, never smuggled into
        # the validated config patch (which would look saved but change nothing).
        self.assertIn("cadenceChoice", JS)
        self.assertNotIn("set('portal_cadence'", JS)

    def test_autonomy_mode_is_a_dropdown_in_the_setup_block(self):
        self.assertEqual(JS.count("title: 'Autonomy mode'"), 1)
        autonomy_at = JS.index("title: 'Autonomy mode'")
        # The control right after it must be a real select, not a text input.
        self.assertIn("jsx('select'", JS[autonomy_at:autonomy_at + 900])
        # And it must sit before the Advanced section, not inside it.
        self.assertLess(autonomy_at, JS.index("children: 'Advanced'"))

    def test_manifest_still_points_at_this_page(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "hermes-proactive-secretary")


class CadenceRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.api = importlib.import_module(f"{_PKG.__name__}.dashboard.plugin_api")
        except Exception as exc:  # noqa: BLE001 - absent host deps skip honestly
            raise unittest.SkipTest(f"plugin API unavailable: {type(exc).__name__}")

    def test_routes_are_registered(self):
        paths = {getattr(route, "path", "") for route in self.api.router.routes}
        self.assertIn("/cadence", paths)
        methods = set()
        for route in self.api.router.routes:
            if getattr(route, "path", "") == "/cadence":
                methods.update(getattr(route, "methods", set()) or set())
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)

    def _request(self):
        return types.SimpleNamespace(headers={"host": "127.0.0.1:9119"})

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path as _Path
        paths = importlib.import_module(f"{_PKG.__name__}.paths")
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        root = _Path(self._tmp.name)
        (root / "home").mkdir()
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", paths.DATA_ENV)}
        os.environ["HERMES_HOME"] = str(root / "home")
        os.environ[paths.DATA_ENV] = str(root / "data")
        self.addCleanup(self._tmp.cleanup)

        def restore():
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)

    def test_post_stores_without_touching_the_job(self):
        # apply=False only writes the plugin interval; the job store is never read
        # for a mutation, so no backend stub is needed here.
        response = self.api.cadence_write({"interval": "6h", "apply": False},
                                          self._request())
        body = json.loads(bytes(response.body).decode("utf-8"))
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["status"], "stored")
        self.assertEqual(body["interval_minutes"], 360)

    def test_invalid_interval_is_refused_cleanly(self):
        import importlib as _il
        cli = _il.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
        original = cli._backend
        cli._backend = lambda: types.SimpleNamespace(list_jobs=lambda: [])
        try:
            response = self.api.cadence_write({"interval": "not-a-duration", "apply": True},
                                              self._request())
        finally:
            cli._backend = original
        self.assertIn(response.status_code, (409, 503))
        body = json.loads(bytes(response.body).decode("utf-8"))
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
