"""Actual production owner callers under two synthetic native profiles.

The native binding is the real installed public ``hermes_constants`` module.
Only TMPDIR profiles are created; no live profile, credential, connector, token
or plugin state is read. If the host module is genuinely absent, the native
cases are explicitly skipped and the fail-closed/standalone cases remain.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

try:
    import hermes_constants as NATIVE
except ModuleNotFoundError as exc:
    if exc.name != "hermes_constants":
        raise
    NATIVE = None

import paths  # noqa: E402
import runtime_gate  # noqa: E402
import portal.portal_work as portal_work  # noqa: E402
from proactive_pilot import collectors  # noqa: E402
from proactive_pilot import evaluator  # noqa: E402
from proactive_pilot import gws_calendar  # noqa: E402
from proactive_pilot import ownerpaths  # noqa: E402
from proactive_pilot import preparation  # noqa: E402

PLUGIN = runtime_gate.PLUGIN_ID
_NATIVE_REQUIRED = unittest.skipUnless(
    NATIVE is not None, "actual hermes_constants is not installed")
_ENV_KEYS = ("HOME", "HERMES_HOME", paths.DATA_ENV, "SECRETARY_SRC",
             "PRODUCTIVITY_SECRETARY_SRC", "HERMES_AGENT_SRC")


@contextlib.contextmanager
def _override(home):
    token = NATIVE.set_hermes_home_override(home)
    try:
        yield
    finally:
        NATIVE.reset_hermes_home_override(token)


def _data(profile):
    return profile / "plugin-data" / ownerpaths.PLUGIN_NAME


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="secretary-profile-integration-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.a = self.root / "profile-a"
        self.b = self.root / "profile-b"
        self.c = self.root / "profile-c"
        self.a_data = self.root / "profile-a-data"
        for directory in (self.home, self.a, self.b, self.c):
            directory.mkdir()
        self._old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        os.environ["HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.a)
        for key in _ENV_KEYS[2:]:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@_NATIVE_REQUIRED
class ActualOwnerCallersTests(Workspace):
    def test_every_owner_reads_profile_b_and_never_creates_a(self):
        with _override(self.b):
            b_data = _data(self.b)
            self.assertEqual(portal_work._data_root(), b_data)
            self.assertEqual(preparation._plugin_data_root(), b_data)
            self.assertEqual(preparation.default_artifact_root(),
                             b_data / "preparation" / "artifacts")
            self.assertEqual(runtime_gate._hermes_home(), self.b)
            self.assertEqual(collectors.secretary_src_candidates()[0],
                             str(self.b / "productivity-secretary" / "src"))
            self.assertEqual(evaluator._default_agent_src(),
                             str(self.b / "hermes-agent"))
            self.assertEqual(gws_calendar._hermes_home(), self.b)
            self.assertEqual(ownerpaths.data_root(), b_data)
        for name in ("plugin-data", "scripts", "hermes-agent",
                     "productivity-secretary"):
            self.assertFalse((self.a / name).exists(), name)

    def test_enabled_gate_reads_the_routed_profile_config(self):
        (self.a / "config.json").write_text(
            '{"plugins": {"enabled": ["%s"], "disabled": ["%s"]}}' % (PLUGIN, PLUGIN),
            encoding="utf-8")
        (self.b / "config.json").write_text(
            '{"plugins": {"enabled": ["%s"]}}' % PLUGIN, encoding="utf-8")
        with _override(self.b):
            self.assertTrue(runtime_gate.plugin_enabled(ROOT))
        self.assertFalse(runtime_gate.plugin_enabled(ROOT))

    def test_b_writes_do_not_touch_a_sentinels(self):
        a_data = _data(self.a)
        a_data.mkdir(parents=True)
        (a_data / "preferences.json").write_text("A-preferences-sentinel", encoding="utf-8")
        with _override(self.b):
            paths.ensure_data_dirs()
            paths.preferences_path().write_text("B-preferences-sentinel", encoding="utf-8")
            store = portal_work._store()
            self.assertEqual(store.root, _data(self.b) / "var")
            artifact_root = preparation._ensure_owner_dir(
                preparation.default_artifact_root())
            (artifact_root / "canary.md").write_text("B-artifact", encoding="utf-8")
            (self.b / "google_token.json").write_text("{}", encoding="utf-8")
            gws_calendar.readiness(which=lambda _name: "/usr/bin/gws")
            self.assertEqual(
                (a_data / "preferences.json").read_text(encoding="utf-8"),
                "A-preferences-sentinel")
            self.assertFalse((a_data / "var").exists())
            self.assertFalse((a_data / "preparation").exists())

    def test_explicit_operator_paths_are_not_rewritten_to_profile_b(self):
        external_src = self.root / "external-secretary-src"
        external_src.mkdir()
        external_agent = self.root / "external-agent"
        os.environ["SECRETARY_SRC"] = str(external_src)
        os.environ["HERMES_AGENT_SRC"] = str(external_agent)
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with _override(self.b):
            candidates = collectors.secretary_src_candidates()
            self.assertEqual(candidates[0], str(external_src))
            self.assertEqual(collectors.resolve_secretary_src(), str(external_src))
            self.assertEqual(evaluator._default_agent_src(), str(external_agent))
            explicit_home = self.root / "explicit-gws-home"
            self.assertEqual(gws_calendar._hermes_home(explicit_home), explicit_home)
            self.assertEqual(portal_work._data_root(), _data(self.b))
        self.assertFalse(self.a_data.exists())

    def test_two_owning_contexts_route_actual_callers_across_thread_hops(self):
        with _override(self.b):
            context_b = contextvars.copy_context()
        with _override(self.c):
            context_c = contextvars.copy_context()

        results = {}
        errors = {}
        barrier = threading.Barrier(2, timeout=5)

        def snapshot():
            return (paths.hermes_home(),
                    portal_work._data_root(),
                    preparation.default_artifact_root(),
                    collectors.secretary_src_candidates()[0],
                    evaluator._default_agent_src(),
                    gws_calendar._hermes_home(),
                    runtime_gate._hermes_home())

        def worker(label, context):
            try:
                barrier.wait()
                results[label] = context.run(snapshot)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors[label] = exc

        threads = [threading.Thread(target=worker, args=("b", context_b)),
                   threading.Thread(target=worker, args=("c", context_c))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, {})
        for label, profile in (("b", self.b), ("c", self.c)):
            self.assertEqual(results[label], (
                profile,
                _data(profile),
                _data(profile) / "preparation" / "artifacts",
                str(profile / "productivity-secretary" / "src"),
                str(profile / "hermes-agent"),
                profile,
                profile,
            ))
        self.assertEqual(paths.hermes_home(), self.a)


@_NATIVE_REQUIRED
class FailClosedOwnerTests(Workspace):
    def test_each_active_home_owner_fails_closed_without_env_fallback(self):
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with mock.patch.object(NATIVE, "get_hermes_home",
                               side_effect=RuntimeError("scoped home unavailable")):
            for call in (paths.hermes_home,
                         portal_work._data_root,
                         preparation.default_artifact_root,
                         collectors.secretary_src_candidates,
                         evaluator._default_agent_src,
                         gws_calendar._hermes_home,
                         ownerpaths.data_root):
                with self.subTest(call=call.__module__ + "." + call.__name__):
                    with self.assertRaises(paths.ProfileResolutionError):
                        call()
            self.assertFalse(runtime_gate.plugin_enabled(ROOT))
        self.assertFalse(self.a_data.exists())
        self.assertFalse((self.a / "plugin-data").exists())

    def test_data_override_owners_fail_closed_when_launch_scope_is_unknown(self):
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with _override(self.b):
            with mock.patch.object(
                    NATIVE, "get_routing_process_hermes_home",
                    side_effect=RuntimeError("pinned launch unavailable")):
                for call in (paths.data_root,
                             portal_work._data_root,
                             preparation.default_artifact_root,
                             ownerpaths.data_root):
                    with self.subTest(call=call.__module__ + "." + call.__name__):
                        with self.assertRaises(paths.ProfileResolutionError):
                            call()
        self.assertFalse(self.a_data.exists())


if __name__ == "__main__":
    unittest.main()
