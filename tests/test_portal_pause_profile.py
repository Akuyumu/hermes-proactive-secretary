"""Focused invariants for the owned-profile cron env and global portal pause.

These tests exercise the real CLI consumers/backends with the plugin's actual
ownership ledger. They never invoke the native scheduler, the network, or any
production mutator: lifecycle tests inject the module's in-memory backend, and
the one transport test mocks only ``subprocess.run`` at the boundary while the
profile binding is the real public native context override (or, when the host
module is genuinely absent, the public ownerpaths resolver). Every root is a
temporary directory under the ambient TMPDIR.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_pause_profile_qa"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")
controls = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal_controls")
ownerpaths = paths._owner

PORTAL = cron_mod.PORTAL_JOB_NAME
MARKER = controls.GLOBAL_PAUSE_KEY


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    top = parser.add_subparsers(dest="command")
    secretary = top.add_parser("secretary")
    sc.register_cli(secretary)
    return parser


_PARSER = _build_parser()


def _parse(*argv: str) -> argparse.Namespace:
    return _PARSER.parse_args(["secretary", *argv])


def _capture_stdout(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    text = buf.getvalue().strip()
    return rc, (json.loads(text) if text else {})


class _Backend(cron_mod.InMemoryCronBackend):
    """In-memory backend that records every backend operation."""

    def __init__(self, jobs=None) -> None:
        super().__init__(jobs)
        self.calls: list[str] = []
        self.fail_pause = False
        self.fail_resume = False

    def list_jobs(self):
        self.calls.append("list")
        return super().list_jobs()

    def add_job(self, spec):
        self.calls.append("add")
        return super().add_job(spec)

    def remove_job(self, job_id):
        self.calls.append("remove")
        return super().remove_job(job_id)

    def pause_job(self, job_id):
        self.calls.append("pause")
        if self.fail_pause:
            raise RuntimeError("synthetic pause failure")
        return super().pause_job(job_id)

    def resume_job(self, job_id):
        self.calls.append("resume")
        if self.fail_resume:
            raise RuntimeError("synthetic resume failure")
        return super().resume_job(job_id)


def _foreign_portal(job_id: str = "foreign-7") -> dict:
    return {
        "id": job_id, "name": PORTAL, "schedule": "0 0 1 1 *",
        "script": "someone_elses_runner.py", "deliver": "origin",
        "workdir": "/elsewhere", "mode": "no-agent", "state": "active",
        "last_run": "",
    }


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved_env = {key: os.environ.get(key)
                           for key in ("HERMES_HOME", paths.DATA_ENV)}
        self._orig_backend = sc._backend
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)
        self.profile("a")

    def _restore(self) -> None:
        sc._backend = self._orig_backend
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def profile(self, name: str) -> tuple[Path, Path]:
        home = self.root / name / "home"
        data = self.root / name / "data"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(home)
        os.environ[paths.DATA_ENV] = str(data)
        return home, data

    def use_backend(self, backend) -> None:
        sc._backend = lambda: backend

    def run_portal(self, backend, *argv: str):
        return _capture_stdout(sc.portal_command, _parse(*argv), backend=backend)

    def enable(self, backend, schedule: str = "*/5 * * * *", *extra: str):
        return self.run_portal(backend, "portal", "enable", "--json",
                               "--schedule", schedule, *extra)

    def global_pause(self):
        return _capture_stdout(sc.pause_command, argparse.Namespace(json=True))

    def global_resume(self):
        return _capture_stdout(sc.resume_command, argparse.Namespace(json=True))

    def ledger_path(self) -> Path:
        return paths.job_ownership_path()

    def ledger(self) -> dict:
        return cron_mod.load_job_ownership(self.ledger_path())

    def record(self) -> dict:
        return self.ledger()["jobs"][PORTAL]

    def marker(self):
        return self.record().get(MARKER)

    def prefs_paused(self) -> bool:
        data = json.loads(paths.preferences_path().read_text(encoding="utf-8"))
        return bool((data.get("paused") or {}).get("active"))

    def portal_state(self, backend) -> str:
        row = next(j for j in backend.list_jobs() if j["name"] == PORTAL)
        return row["state"]

    def write_ledger(self, payload: dict) -> None:
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class GlobalPauseLifecycleTests(_Isolated):
    def test_global_pause_pauses_owned_portal_and_repeat_keeps_marker(self):
        backend = _Backend()
        rc, enabled = self.enable(backend)
        self.assertEqual(rc, 0, enabled)
        job_id = enabled["job_id"]
        self.use_backend(backend)

        rc, paused = self.global_pause()
        self.assertEqual(rc, 0, paused)
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["portal"]["portal"], "paused")
        self.assertEqual(self.portal_state(backend), "paused")
        self.assertTrue(self.prefs_paused())
        self.assertEqual(self.marker()["job_id"], job_id)

        rc, again = self.global_pause()
        self.assertEqual(rc, 0, again)
        self.assertEqual(again["portal"]["portal"], "already-paused")
        self.assertEqual(self.portal_state(backend), "paused")
        self.assertTrue(self.prefs_paused())
        self.assertEqual(self.marker()["job_id"], job_id)
        self.assertNotIn("resume", backend.calls)

    def test_global_resume_resumes_only_the_marked_portal(self):
        backend = _Backend()
        rc, enabled = self.enable(backend)
        self.assertEqual(rc, 0, enabled)
        job_id = enabled["job_id"]
        backend.jobs.append({"id": "other-1", "name": "other-job",
                             "schedule": "* * * * *", "state": "active"})
        self.use_backend(backend)
        self.assertEqual(self.global_pause()[0], 0)
        self.assertEqual(self.portal_state(backend), "paused")

        rc, resumed = self.global_resume()
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["portal"]["portal"], "resumed")
        self.assertFalse(self.prefs_paused())
        self.assertNotIn(MARKER, self.record())
        states = {j["name"]: j["state"] for j in backend.list_jobs()}
        self.assertEqual(states[PORTAL], "active")
        self.assertEqual(states["other-job"], "active")
        self.assertEqual(
            [j["id"] for j in backend.list_jobs() if j["name"] == PORTAL],
            [job_id])

    def test_manual_portal_pause_is_preserved_across_the_global_cycle(self):
        backend = _Backend()
        self.assertEqual(self.enable(backend)[0], 0)
        rc, manual = self.run_portal(backend, "portal", "pause", "--json")
        self.assertEqual(rc, 0, manual)
        self.assertEqual(self.portal_state(backend), "paused")
        self.use_backend(backend)

        rc, paused = self.global_pause()
        self.assertEqual(rc, 0, paused)
        self.assertEqual(paused["portal"]["portal"], "already-paused-manual")
        self.assertNotIn(MARKER, self.record())

        rc, resumed = self.global_resume()
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["portal"]["portal"], "not-global-paused")
        self.assertEqual(self.portal_state(backend), "paused")
        self.assertFalse(self.prefs_paused())

    def test_fresh_profile_global_pause_makes_no_job_or_backend_mutation(self):
        backend = _Backend()
        self.use_backend(backend)
        rc, paused = self.global_pause()
        self.assertEqual(rc, 0, paused)
        self.assertEqual(paused["portal"]["portal"], "not-opted-in")
        self.assertTrue(self.prefs_paused())
        self.assertEqual(backend.calls, [])
        self.assertFalse(self.ledger_path().exists())

        rc, resumed = self.global_resume()
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["portal"]["portal"], "not-opted-in")
        self.assertFalse(self.prefs_paused())
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("pause", backend.calls)
        self.assertNotIn("resume", backend.calls)
        self.assertFalse(self.ledger_path().exists())

    def test_pause_failure_keeps_deterministic_pause_and_reports_partial(self):
        backend = _Backend()
        self.assertEqual(self.enable(backend)[0], 0)
        backend.fail_pause = True
        self.use_backend(backend)

        rc, payload = self.global_pause()
        self.assertEqual(rc, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertIn("not confirmed stopped", payload["error"])
        self.assertTrue(self.prefs_paused())
        self.assertEqual(self.portal_state(backend), "active")
        self.assertNotIn(MARKER, self.record())

    def test_resume_failure_keeps_marker_and_reports_nonzero(self):
        backend = _Backend()
        self.assertEqual(self.enable(backend)[0], 0)
        self.use_backend(backend)
        self.assertEqual(self.global_pause()[0], 0)
        marker = dict(self.marker())
        backend.fail_resume = True

        rc, payload = self.global_resume()
        self.assertEqual(rc, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertIn("not confirmed resumed", payload["error"])
        self.assertEqual(self.portal_state(backend), "paused")
        self.assertEqual(self.marker(), marker)


class OwnershipFailureTests(_Isolated):
    def test_foreign_same_name_is_never_paused(self):
        foreign = _foreign_portal()
        backend = _Backend([dict(foreign)])
        self.write_ledger({"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                           "jobs": {PORTAL: {"id": "owned-1", "name": PORTAL}}})
        self.use_backend(backend)

        rc, payload = self.global_pause()
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["portal"]["portal"], "foreign")
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertNotIn(MARKER, self.record())
        self.assertNotIn("pause", backend.calls)

    def test_stale_recorded_id_is_missing_without_creating_or_pausing(self):
        backend = _Backend()
        self.write_ledger({"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                           "jobs": {PORTAL: {"id": "owned-missing", "name": PORTAL}}})
        self.use_backend(backend)

        rc, payload = self.global_pause()
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["portal"]["portal"], "missing")
        self.assertEqual(backend.list_jobs(), [])
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("pause", backend.calls)
        self.assertNotIn(MARKER, self.record())

    def test_corrupt_ledger_fails_without_foreign_mutation(self):
        foreign = _foreign_portal()
        backend = _Backend([dict(foreign)])
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        self.use_backend(backend)

        rc, payload = self.global_pause()
        self.assertEqual(rc, 1, payload)
        self.assertTrue(payload["paused"])
        self.assertIn("corrupt", payload["error"])
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertNotIn("pause", backend.calls)

    def test_symlink_ledger_fails_without_touching_target_or_foreign_job(self):
        foreign = _foreign_portal()
        backend = _Backend([dict(foreign)])
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "real-ledger.json"
        target.write_text(json.dumps({"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
                                      "jobs": {}}), encoding="utf-8")
        path.symlink_to(target)
        before = target.read_text(encoding="utf-8")
        self.use_backend(backend)

        rc, payload = self.global_pause()
        self.assertEqual(rc, 1, payload)
        self.assertIn("symlink", payload["error"])
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertTrue(path.is_symlink())
        self.assertNotIn("pause", backend.calls)


class GlobalPauseAdmissionTests(_Isolated):
    def test_enable_and_resume_refuse_while_global_pause_is_outstanding(self):
        backend = _Backend()
        self.assertEqual(self.enable(backend)[0], 0)
        self.use_backend(backend)
        self.assertEqual(self.global_pause()[0], 0)
        backend.calls.clear()

        rc, enable = self.enable(backend, "*/10 * * * *")
        self.assertEqual(rc, 1, enable)
        self.assertEqual(enable["status"], "globally-paused")
        self.assertIn("resume", enable["detail"])
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.jobs[0]["state"], "paused")

        rc, resume = self.run_portal(backend, "portal", "resume", "--json")
        self.assertEqual(rc, 1, resume)
        self.assertEqual(resume["status"], "globally-paused")
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.jobs[0]["state"], "paused")

    def test_enable_refuses_preferences_only_global_pause_before_any_job(self):
        backend = _Backend()
        self.use_backend(backend)
        self.assertEqual(self.global_pause()[0], 0)

        rc, payload = self.enable(backend)
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["status"], "globally-paused")
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.list_jobs(), [])
        self.assertFalse(self.ledger_path().exists())

    def test_read_only_and_stop_actions_stay_usable_while_globally_paused(self):
        backend = _Backend()
        self.assertEqual(self.enable(backend)[0], 0)
        self.use_backend(backend)
        self.assertEqual(self.global_pause()[0], 0)

        rc, status = self.run_portal(backend, "portal", "status", "--json")
        self.assertEqual(rc, 0, status)
        self.assertEqual(status["state"], "paused")

        rc, preview = self.run_portal(
            backend, "portal", "preview", "--json", "--schedule", "*/5 * * * *")
        self.assertEqual(rc, 0, preview)

        rc, pause = self.run_portal(backend, "portal", "pause", "--json")
        self.assertEqual(rc, 0, pause)
        self.assertEqual(self.portal_state(backend), "paused")

        self.assertEqual(self.global_resume()[0], 0)
        rc, removed = self.run_portal(backend, "portal", "remove", "--json")
        self.assertEqual(rc, 0, removed)
        self.assertEqual(backend.list_jobs(), [])


class _CronEnvIsolated(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        self._orig_run = cron_mod.subprocess.run
        self._orig_loader = cron_mod._load_served_profile_child_env
        self.calls: list[tuple[list[str], dict]] = []
        self.helper_calls: list[dict] = []
        self.helper_env = None
        self.addCleanup(setattr, cron_mod.subprocess, "run", self._orig_run)
        self.addCleanup(setattr, cron_mod, "_load_served_profile_child_env",
                        self._orig_loader)
        self._saved_credential = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "dummy-sentinel-not-a-real-credential"
        self.addCleanup(self._restore_credential)
        try:
            from tools.environments.local import (
                served_profile_child_env as native_helper)
        except Exception:  # noqa: BLE001 - host SDK genuinely absent
            native_helper = None

        def helper_seam(base=None, *, target_home=None,
                        inherit_credentials=False):
            self.helper_calls.append({
                "base": base,
                "target_home": target_home,
                "inherit_credentials": inherit_credentials,
            })
            if native_helper is not None:
                env = native_helper(
                    base, target_home=target_home,
                    inherit_credentials=inherit_credentials)
            else:
                env = self._faithful_helper(
                    base, target_home=target_home,
                    inherit_credentials=inherit_credentials)
            self.helper_env = env
            return env

        cron_mod._load_served_profile_child_env = lambda: helper_seam

    def _restore_credential(self) -> None:
        if self._saved_credential is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_credential

    def _faithful_helper(self, base=None, *, target_home=None,
                         inherit_credentials=False):
        """Bounded stand-in mirroring the native sanitizing contract."""
        env = dict(base) if base is not None else dict(os.environ)
        target = str(target_home or "")
        if target:
            env["HERMES_HOME"] = target
            env["TMPDIR"] = str(Path(target) / "tmp")
            if target != os.environ.get("HERMES_HOME"):
                for key in list(env):
                    if key.startswith("TERMINAL_"):
                        env.pop(key, None)
                env.pop(paths.DATA_ENV, None)
        if inherit_credentials:
            env["QA_DUMMY_CREDENTIAL"] = "dummy-sentinel-not-a-real-credential"
        else:
            for key in list(env):
                if "API_KEY" in key or "TOKEN" in key or "SECRET" in key:
                    env.pop(key, None)
        return env

    def fake_run(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class CronSubprocessProfileTests(_CronEnvIsolated):
    def _route_to(self, home: Path, launch: Path) -> None:
        try:
            import hermes_constants as native
        except Exception:  # noqa: BLE001 - host module genuinely absent
            native = None
        reset = getattr(native, "reset_hermes_home_override", None)
        set_override = getattr(native, "set_hermes_home_override", None)
        if callable(set_override) and callable(reset):
            token = set_override(str(home))
            self.addCleanup(reset, token)
            return
        original_home = ownerpaths.hermes_home
        original_launch = ownerpaths.launch_home
        ownerpaths.hermes_home = lambda: home
        ownerpaths.launch_home = lambda: launch
        self.addCleanup(setattr, ownerpaths, "hermes_home", original_home)
        self.addCleanup(setattr, ownerpaths, "launch_home", original_launch)

    def test_child_env_targets_routed_home_and_leaves_launch_env_unchanged(self):
        home_a, data_a = self.profile("a")
        home_b, _data_b = self.profile("b")
        # Back to launch A; the routed request is scoped to B.
        os.environ["HERMES_HOME"] = str(home_a)
        routed_data = str(data_a)
        os.environ[paths.DATA_ENV] = routed_data
        self._route_to(home_b, home_a)
        os.environ["HERMES_HOME"] = str(home_a)
        os.environ[paths.DATA_ENV] = routed_data
        launch_env = dict(os.environ)
        cron_mod.subprocess.run = self.fake_run

        backend = cron_mod.HermesCronBackend(hermes_bin="hermes")
        self.assertEqual(backend.list_jobs(), [])
        self.assertEqual(len(self.calls), 1)
        argv, kwargs = self.calls[0]
        self.assertEqual(argv, ["hermes", "cron", "list", "--all"])
        self.assertIs(kwargs["shell"], False)
        env = kwargs["env"]
        self.assertIs(env, self.helper_env)
        self.assertEqual(len(self.helper_calls), 1)
        helper_call = self.helper_calls[0]
        self.assertIsNone(helper_call["base"])
        self.assertIs(helper_call["inherit_credentials"], False)
        self.assertEqual(Path(helper_call["target_home"]).resolve(),
                         home_b.resolve())
        self.assertEqual(env["HERMES_HOME"], str(home_b))
        self.assertNotIn(paths.DATA_ENV, env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(os.environ["HERMES_HOME"], launch_env["HERMES_HOME"])
        self.assertEqual(os.environ[paths.DATA_ENV], launch_env[paths.DATA_ENV])
        self.assertEqual(os.environ["OPENAI_API_KEY"],
                         launch_env["OPENAI_API_KEY"])

    def test_same_profile_keeps_plugin_data_override(self):
        home_a, data_a = self.profile("a")
        self._route_to(home_a, home_a)
        os.environ["HERMES_HOME"] = str(home_a)
        os.environ[paths.DATA_ENV] = str(data_a)
        cron_mod.subprocess.run = self.fake_run

        backend = cron_mod.HermesCronBackend(hermes_bin="hermes")
        self.assertEqual(backend.list_jobs(), [])
        env = self.calls[0][1]["env"]
        self.assertEqual(env["HERMES_HOME"], str(home_a))
        self.assertEqual(env[paths.DATA_ENV], str(data_a))

    def test_missing_native_helper_aborts_before_spawning(self):
        home_a, _data_a = self.profile("a")
        self._route_to(home_a, home_a)
        cron_mod.subprocess.run = self.fake_run

        def missing_helper():
            raise ImportError("synthetic missing native served-profile helper")

        cron_mod._load_served_profile_child_env = missing_helper
        backend = cron_mod.HermesCronBackend(hermes_bin="hermes")
        with self.assertRaises(cron_mod.CronError):
            backend.list_jobs()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.helper_calls, [])

    def test_failing_native_helper_aborts_before_spawning(self):
        home_a, _data_a = self.profile("a")
        self._route_to(home_a, home_a)
        cron_mod.subprocess.run = self.fake_run

        def broken_helper(*, target_home=None, inherit_credentials=False):
            raise RuntimeError("synthetic native helper failure")

        cron_mod._load_served_profile_child_env = lambda: broken_helper
        backend = cron_mod.HermesCronBackend(hermes_bin="hermes")
        with self.assertRaises(cron_mod.CronError):
            backend.list_jobs()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.helper_calls, [])

    def test_resolution_failure_aborts_before_spawning(self):
        cron_mod.subprocess.run = self.fake_run
        original_home = ownerpaths.hermes_home
        ownerpaths.hermes_home = lambda: (_ for _ in ()).throw(
            ownerpaths.ProfileResolutionError("synthetic unreadable profile"))
        self.addCleanup(setattr, ownerpaths, "hermes_home", original_home)

        backend = cron_mod.HermesCronBackend(hermes_bin="hermes")
        with self.assertRaises(cron_mod.CronError):
            backend.list_jobs()
        self.assertEqual(self.calls, [])

    def test_injected_runner_keeps_argv_without_env_resolution(self):
        original_home = ownerpaths.hermes_home
        ownerpaths.hermes_home = lambda: (_ for _ in ()).throw(
            ownerpaths.ProfileResolutionError("must not be consulted"))
        self.addCleanup(setattr, ownerpaths, "hermes_home", original_home)
        seen: list[list[str]] = []

        def runner(argv):
            seen.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = cron_mod.HermesCronBackend(hermes_bin="hermes", runner=runner)
        self.assertEqual(backend.list_jobs(), [])
        self.assertEqual(seen, [["hermes", "cron", "list", "--all"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
