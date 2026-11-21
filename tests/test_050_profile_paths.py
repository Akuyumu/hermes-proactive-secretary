"""Profile-state isolation for the 0.5.0 path owners.

Synthetic profiles only, created under TMPDIR. Native cases use the actual
installed public ``hermes_constants`` binding (never a copied SDK snapshot or a
fake module) and restore every context override and environment variable they
change. If the host module is genuinely absent, genuine standalone cases still
run and the native cases are explicitly skipped.
"""
from __future__ import annotations

import contextvars
import contextlib
import importlib
import importlib.util
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


def _load_package():
    name = "hermes_proactive_secretary_profile_paths"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
paths = importlib.import_module(f"{PKG.__name__}.paths")
onboarding = importlib.import_module(f"{PKG.__name__}.secretary.secretary_onboarding")

_NATIVE_REQUIRED = unittest.skipUnless(
    NATIVE is not None, "actual hermes_constants is not installed")
_ENV_KEYS = ("HOME", "HERMES_HOME", paths.DATA_ENV)


@contextlib.contextmanager
def _override(home):
    token = NATIVE.set_hermes_home_override(home)
    try:
        yield
    finally:
        NATIVE.reset_hermes_home_override(token)


@contextlib.contextmanager
def _absent_native():
    """Simulate a genuine standalone install without the host module."""
    saved = sys.modules.get("hermes_constants", _MISSING)
    sys.modules["hermes_constants"] = None
    try:
        yield
    finally:
        if saved is _MISSING:
            sys.modules.pop("hermes_constants", None)
        else:
            sys.modules["hermes_constants"] = saved


_MISSING = object()


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="secretary-profile-paths-")
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
        os.environ.pop(paths.DATA_ENV, None)
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def data_home(self, profile):
        return profile / "plugin-data" / paths.PLUGIN_NAME


@_NATIVE_REQUIRED
class NativeContextPathTests(Workspace):
    def test_context_override_routes_every_owned_path_to_profile_b(self):
        with _override(self.b):
            self.assertEqual(Path(NATIVE.get_hermes_home()), self.b)
            self.assertEqual(paths.hermes_home(), self.b)
            data = self.data_home(self.b)
            expected = {
                "data_root": data,
                "state_dir": data / "var",
                "policy_path": data / "policy.json",
                "preferences_path": data / "preferences.json",
                "user_config_path": data / "user_config.json",
                "runner_ownership_path": data / "runner_ownership.json",
                "job_ownership_path": data / "cron_job_ownership.json",
                "migration_manifest_path": data / "migration_manifest.json",
                "scripts_dir": self.b / "scripts",
            }
            for name, value in expected.items():
                with self.subTest(path=name):
                    self.assertEqual(getattr(paths, name)(), value)
            self.assertFalse((self.a / "plugin-data").exists())
            self.assertFalse((self.a / "scripts").exists())
        self.assertEqual(paths.hermes_home(), self.a)

    def test_launch_data_override_is_not_inherited_by_routed_profile(self):
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with _override(self.b):
            own_data = self.data_home(self.b)
            self.assertEqual(paths.data_root(), own_data)
            self.assertEqual(paths.ensure_data_dirs(), own_data)
            self.assertTrue((own_data / "var").is_dir())
            self.assertFalse(self.a_data.exists())
        self.assertEqual(paths.data_root(), self.a_data)

    def test_scoped_writes_never_touch_profile_a_sentinels(self):
        a_data = self.data_home(self.a)
        a_data.mkdir(parents=True)
        (a_data / "preferences.json").write_text("A-preferences-sentinel", encoding="utf-8")
        (a_data / "user_config.json").write_text("A-config-sentinel", encoding="utf-8")
        with _override(self.b):
            paths.ensure_data_dirs()
            paths.preferences_path().write_text("B-preferences-sentinel", encoding="utf-8")
            paths.user_config_path().write_text("B-config-sentinel", encoding="utf-8")
            paths.runner_ownership_path().write_text("B-ownership-sentinel", encoding="utf-8")
            b_data = self.data_home(self.b)
            self.assertEqual((b_data / "preferences.json").read_text(encoding="utf-8"),
                             "B-preferences-sentinel")
            self.assertEqual((b_data / "user_config.json").read_text(encoding="utf-8"),
                             "B-config-sentinel")
            self.assertEqual((a_data / "preferences.json").read_text(encoding="utf-8"),
                             "A-preferences-sentinel")
            self.assertEqual((a_data / "user_config.json").read_text(encoding="utf-8"),
                             "A-config-sentinel")
            self.assertFalse((a_data / "runner_ownership.json").exists())

    def test_two_owning_contexts_survive_thread_hops(self):
        with _override(self.b):
            context_b = contextvars.copy_context()
        with _override(self.c):
            context_c = contextvars.copy_context()

        results = {}
        errors = {}
        barrier = threading.Barrier(2, timeout=5)

        def worker(label, context):
            try:
                barrier.wait()
                results[label] = context.run(
                    lambda: (NATIVE.get_hermes_home(), paths.hermes_home(),
                             paths.data_root(), paths.preferences_path()))
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors[label] = exc

        threads = [threading.Thread(target=worker, args=("b", context_b)),
                   threading.Thread(target=worker, args=("c", context_c))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, {})
        self.assertEqual(results["b"], (
            self.b, self.b, self.data_home(self.b),
            self.data_home(self.b) / "preferences.json"))
        self.assertEqual(results["c"], (
            self.c, self.c, self.data_home(self.c),
            self.data_home(self.c) / "preferences.json"))
        self.assertEqual(paths.hermes_home(), self.a)

    def test_native_getter_failure_fails_closed_without_env_fallback(self):
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with mock.patch.object(NATIVE, "get_hermes_home",
                               side_effect=RuntimeError("scoped home unavailable")):
            with self.assertRaises(paths.ProfileResolutionError):
                paths.hermes_home()
            with self.assertRaises(paths.ProfileResolutionError):
                paths.data_root()
        self.assertFalse(self.a_data.exists())
        self.assertFalse((self.a / "plugin-data").exists())

    def test_native_launch_scope_failure_fails_closed_without_env_fallback(self):
        os.environ[paths.DATA_ENV] = str(self.a_data)
        with _override(self.b):
            with mock.patch.object(
                    NATIVE, "get_routing_process_hermes_home",
                    side_effect=RuntimeError("pinned launch unavailable")):
                self.assertEqual(paths.hermes_home(), self.b)
                with self.assertRaises(paths.ProfileResolutionError):
                    paths.data_root()
        self.assertFalse(self.a_data.exists())


class StandaloneFallbackTests(Workspace):
    def test_no_host_module_keeps_env_and_data_fallback(self):
        with _absent_native():
            os.environ["HERMES_HOME"] = str(self.b)
            os.environ[paths.DATA_ENV] = str(self.a_data)
            self.assertEqual(paths.hermes_home(), self.b)
            self.assertEqual(paths.data_root(), self.a_data)
            self.assertEqual(paths.state_dir(), self.a_data / "var")

            os.environ.pop("HERMES_HOME", None)
            os.environ.pop(paths.DATA_ENV, None)
            self.assertEqual(paths.hermes_home(), self.home / ".hermes")
            self.assertEqual(paths.data_root(),
                             self.home / ".hermes" / "plugin-data" / paths.PLUGIN_NAME)


class _MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _Ctx:
    def __init__(self):
        self.state = _MemoryState()
        self.settings = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def _gateway_injection_allowed(self):
        return False


@_NATIVE_REQUIRED
class ProductionOnboardingStateTests(Workspace):
    def test_onboarding_state_stays_with_each_owning_context(self):
        a_ctx = _Ctx()
        b_ctx = _Ctx()
        onboarding.OnboardingService(a_ctx).update(
            settings={"timezone": "Europe/London"}, explicit_consent=False)

        with _override(self.b):
            onboarding.OnboardingService(b_ctx).update(
                settings={"timezone": "Asia/Tokyo"}, explicit_consent=False)
            self.assertEqual(
                b_ctx.state.get(onboarding.STATE_KEY)["settings"]["timezone"],
                "Asia/Tokyo")
            self.assertEqual(
                a_ctx.state.get(onboarding.STATE_KEY)["settings"]["timezone"],
                "Europe/London")


if __name__ == "__main__":
    unittest.main()
