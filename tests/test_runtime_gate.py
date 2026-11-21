"""Fail-closed native-toggle gate: unit + installed-rendered runner tests.

No network, no live profile, no scheduler: every profile is a fake tree under the
workspace and the pipeline is a spy that records whether it was imported/called.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_gate import plugin_enabled  # noqa: E402

PLUGIN = "hermes-proactive-secretary"
RUNNERS = ROOT / "runners"
_MISSING = object()


def _config(enabled=None, disabled=None):
    plugins = {}
    if enabled is not None:
        plugins["enabled"] = enabled
    if disabled is not None:
        plugins["disabled"] = disabled
    return {"plugins": plugins}


class GateUnitTests(unittest.TestCase):
    def test_explicitly_enabled_is_allowed(self):
        self.assertTrue(plugin_enabled(ROOT, config=_config(enabled=[PLUGIN])))

    def test_disabled_deny_list_wins(self):
        cfg = _config(enabled=[PLUGIN, "other"], disabled=[PLUGIN])
        self.assertFalse(plugin_enabled(ROOT, config=cfg))

    def test_absent_enabled_list_fails_closed(self):
        self.assertFalse(plugin_enabled(ROOT, config=_config(disabled=[])))

    def test_empty_enabled_list_fails_closed(self):
        self.assertFalse(plugin_enabled(ROOT, config=_config(enabled=[])))

    def test_malformed_enabled_list_fails_closed(self):
        for bad in ("hermes-proactive-secretary", 3, {"a": 1}, [1, 2], [None]):
            self.assertFalse(plugin_enabled(ROOT, config={"plugins": {"enabled": bad}}), bad)

    def test_malformed_disabled_list_fails_closed(self):
        cfg = {"plugins": {"enabled": [PLUGIN], "disabled": "hermes-proactive-secretary"}}
        self.assertFalse(plugin_enabled(ROOT, config=cfg))

    def test_missing_plugins_section_fails_closed(self):
        self.assertFalse(plugin_enabled(ROOT, config={}))

    def test_unreadable_config_fails_closed(self):
        self.assertFalse(plugin_enabled(ROOT, config=[]))
        self.assertFalse(plugin_enabled(ROOT, config="not-a-mapping"))

    def test_missing_plugin_root_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            gone = Path(tmp) / "removed-plugin"
            self.assertFalse(plugin_enabled(gone, config=_config(enabled=[PLUGIN])))


class GateProfileFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, data):
        (self.home / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_enabled_config_file_allows(self):
        self._write(_config(enabled=[PLUGIN]))
        self.assertTrue(plugin_enabled(ROOT, hermes_home=self.home))

    def test_disabled_config_file_blocks(self):
        self._write(_config(enabled=[PLUGIN], disabled=[PLUGIN]))
        self.assertFalse(plugin_enabled(ROOT, hermes_home=self.home))

    def test_malformed_config_file_blocks(self):
        (self.home / "config.json").write_text("{ not json", encoding="utf-8")
        self.assertFalse(plugin_enabled(ROOT, hermes_home=self.home))

    def test_missing_config_file_blocks(self):
        self.assertFalse(plugin_enabled(ROOT, hermes_home=self.home))


class _SpyMain(types.ModuleType):
    def __init__(self, name, spy, kind):
        super().__init__(name)
        self._spy = spy
        self._kind = kind

    def __getattr__(self, attr):
        if attr == "main":
            self._spy["imported"].append(self._kind)

            def _main(argv):
                self._spy["called"].append((self._kind, list(argv)))
                return 0
            return _main
        raise AttributeError(attr)


class RenderedRunnerGateTests(unittest.TestCase):
    """Run the installed-rendered scripts; a spy proves the pipeline is untouched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _render(self, name, plugin_root=ROOT):
        text = (RUNNERS / name).read_text(encoding="utf-8")
        assert "{{PLUGIN_ROOT}}" in text
        text = text.replace("{{PLUGIN_ROOT}}", str(plugin_root))
        target = self.scripts / name
        target.write_text(text, encoding="utf-8")
        return target

    def _write_config(self, data):
        (self.home / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def _run(self, script):
        spy = {"imported": [], "called": []}
        saved_modules = {}
        for name, module in (
            ("proactive_pilot", self._pkg()),
            ("proactive_pilot.main", _SpyMain("proactive_pilot.main", spy, "pilot")),
            ("proactive_pilot.reminders", _SpyMain("proactive_pilot.reminders", spy, "reminder")),
        ):
            saved_modules[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = module
        saved_env = dict(os.environ)
        saved_argv = list(sys.argv)
        os.environ["HERMES_HOME"] = str(self.home)
        sys.argv = [
            str(script),
            "--state-dir", str(self.root / "state"),
            "--policy", str(self.root / "policy.json"),
            "--preferences", str(self.root / "preferences.json"),
            "dispatch",
        ]
        out, err = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    runpy.run_path(str(script), run_name="__main__")
                except SystemExit as exc:
                    code = exc.code
        finally:
            sys.argv = saved_argv
            os.environ.clear()
            os.environ.update(saved_env)
            for name, old in saved_modules.items():
                if old is _MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old
        return code, out.getvalue(), err.getvalue(), spy

    @staticmethod
    def _pkg():
        pkg = types.ModuleType("proactive_pilot")
        pkg.__path__ = []
        return pkg

    def _assert_blocked(self, script_name):
        script = self._render(script_name)
        code, out, _err, spy = self._run(script)
        self.assertEqual(code, 0, (script_name, code))
        self.assertEqual(out, "", "a disabled runner must emit empty stdout")
        self.assertEqual(spy["imported"], [], f"{script_name} must not import the pipeline")
        self.assertEqual(spy["called"], [], f"{script_name} must not run the pipeline")

    def test_disabled_blocks_both_runners(self):
        self._write_config(_config(enabled=["something-else"]))
        self._assert_blocked("proactive_secretary_runner.py")
        self._assert_blocked("proactive_secretary_reminder_runner.py")

    def test_deny_list_blocks_both_runners(self):
        self._write_config(_config(enabled=[PLUGIN], disabled=[PLUGIN]))
        self._assert_blocked("proactive_secretary_runner.py")
        self._assert_blocked("proactive_secretary_reminder_runner.py")

    def test_malformed_config_blocks_both_runners(self):
        (self.home / "config.json").write_text("{ broken", encoding="utf-8")
        self._assert_blocked("proactive_secretary_runner.py")
        self._assert_blocked("proactive_secretary_reminder_runner.py")

    def test_removed_plugin_root_blocks(self):
        gone = self.root / "removed"
        script = self._render("proactive_secretary_runner.py", plugin_root=gone)
        self._write_config(_config(enabled=[PLUGIN]))
        code, out, _err, spy = self._run(script)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(spy["imported"], [])

    def test_enabled_allows_both_runners(self):
        self._write_config(_config(enabled=[PLUGIN]))
        for name, kind in (("proactive_secretary_runner.py", "pilot"),
                           ("proactive_secretary_reminder_runner.py", "reminder")):
            script = self._render(name)
            code, out, _err, spy = self._run(script)
            self.assertEqual(code, 0, (name, code))
            self.assertEqual(out, "")
            self.assertEqual(spy["imported"], [kind], name)
            self.assertEqual([c[0] for c in spy["called"]], [kind], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
