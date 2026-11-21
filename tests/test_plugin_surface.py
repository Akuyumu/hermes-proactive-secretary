"""Tests for the hermes-proactive-secretary plugin surface.

Covers CLI wiring, one-command setup idempotency and safety, pause stopping
collection, disable removing this plugin's own jobs, the cron job specs and
truthful job removal. No network, no live sources, no live scheduler.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin_package():
    name = "hermes_proactive_secretary_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
runner_install = importlib.import_module(f"{_PKG.__name__}.runner_install")


def _fast_checks(_fixture: str) -> dict:
    return {"self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}


def _setup_ns(*, force=False, no_cron=False, json_out=False, fixture="healthy_empty", deliver=None):
    return argparse.Namespace(force=force, no_cron=no_cron, json=json_out, fixture=fixture,
                              deliver=deliver, data_dir=None)


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(paths.DATA_ENV)
        self._old_home = os.environ.get("HERMES_HOME")
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        (root / "home").mkdir(parents=True, exist_ok=True)
        os.environ[paths.DATA_ENV] = str(root / "data")
        os.environ["HERMES_HOME"] = str(root / "home")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop(paths.DATA_ENV, None)
        else:
            os.environ[paths.DATA_ENV] = self._old
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home


class CliWiringTests(unittest.TestCase):
    def test_cli_tree_exposes_every_subcommand(self):
        parser = argparse.ArgumentParser(prog="hermes secretary")
        sc.register_cli(parser)
        args = parser.parse_args(["status", "--json"])
        self.assertIs(args.func, sc.secretary_command)
        self.assertEqual(args.secretary_command, "status")
        self.assertTrue(args.json)
        for name in ("setup", "status", "pause", "resume", "disable", "uninstall",
                     "self-test", "run-once", "watch", "feedback", "doctor"):
            sub = parser.parse_args([name] + (["add", "topic"] if name == "watch" else []))
            self.assertEqual(sub.secretary_command, name)

    def test_unknown_subcommand_is_nonzero(self):
        ns = argparse.Namespace(secretary_command=None, data_dir=None, json=False)
        self.assertEqual(sc.secretary_command(ns), 2)


class JobSpecTests(unittest.TestCase):
    def test_two_jobs_with_expected_schedules(self):
        specs = cron_mod.build_job_specs(deliver="local", workdir="/p")
        names = {s.name: s for s in specs}
        self.assertEqual(set(names), set(cron_mod.OWNED_JOB_NAMES))
        self.assertEqual(names[cron_mod.PILOT_JOB_NAME].schedule, "*/15 * * * *")
        self.assertEqual(names[cron_mod.REMINDER_JOB_NAME].schedule, "* * * * *")
        self.assertEqual(names[cron_mod.PILOT_JOB_NAME].script, cron_mod.PILOT_SCRIPT)
        self.assertEqual(names[cron_mod.REMINDER_JOB_NAME].script, cron_mod.REMINDER_SCRIPT)
        for spec in specs:
            self.assertTrue(spec.script.endswith(".py"))
            self.assertEqual(spec.deliver, "local")
            self.assertEqual(spec.workdir, "/p")


class CronBackendTests(unittest.TestCase):
    def test_ensure_job_is_idempotent(self):
        backend = cron_mod.InMemoryCronBackend()
        spec = cron_mod.CronJobSpec(name="j", schedule="* * * * *", script="run.py")
        first = cron_mod.ensure_job(backend, spec)
        second = cron_mod.ensure_job(backend, spec)
        self.assertEqual(first.action, "created")
        self.assertEqual(second.action, "already-present")
        self.assertEqual(len(backend.list_jobs()), 1)

    def test_remove_job_is_verified_and_idempotent(self):
        backend = cron_mod.InMemoryCronBackend()
        spec = cron_mod.CronJobSpec(name="j", schedule="* * * * *", script="run.py")
        cron_mod.ensure_job(backend, spec)
        self.assertEqual(cron_mod.remove_job(backend, "j").action, "removed")
        self.assertEqual(cron_mod.remove_job(backend, "j").action, "missing")


class SetupTests(_DataDirMixin):
    def setUp(self):
        super().setUp()
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = _fast_checks
        self.addCleanup(self._restore_checks)

    def _restore_checks(self):
        sc._run_offline_checks = self._orig_checks

    def test_setup_creates_jobs_and_is_idempotent(self):
        backend = cron_mod.InMemoryCronBackend()
        self.assertEqual(sc.setup_command(_setup_ns(), backend=backend), 0)
        names = {j["name"] for j in backend.list_jobs()}
        self.assertEqual(names, set(sc.OWNED_JOB_NAMES))
        self.assertTrue(paths.policy_path().is_file())
        # Re-run: no overwrite, no duplicate jobs.
        self.assertEqual(sc.setup_command(_setup_ns(), backend=backend), 0)
        self.assertEqual(len(backend.list_jobs()), 2)
        result = json.loads(self._setup_json(backend))
        self.assertEqual(result["policy"]["action"], "kept")

    def _setup_json(self, backend):
        ns = _setup_ns(json_out=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.setup_command(ns, backend=backend)
        return buf.getvalue()

    def test_setup_does_not_overwrite_policy_without_force(self):
        backend = cron_mod.InMemoryCronBackend()
        sc.setup_command(_setup_ns(), backend=backend)
        sentinel = json.dumps({"timezone": "UTC", "marker": "user-edit"})
        paths.policy_path().write_text(sentinel, encoding="utf-8")
        sc.setup_command(_setup_ns(force=False), backend=backend)
        self.assertEqual(paths.policy_path().read_text(encoding="utf-8"), sentinel)
        sc.setup_command(_setup_ns(force=True), backend=backend)
        rewritten = json.loads(paths.policy_path().read_text(encoding="utf-8"))
        self.assertNotIn("marker", rewritten)

    def test_setup_rolls_back_jobs_on_failure(self):
        class FailingBackend(cron_mod.InMemoryCronBackend):
            def add_job(self, spec):
                if spec.name == cron_mod.REMINDER_JOB_NAME:
                    raise cron_mod.CronError("boom")
                return super().add_job(spec)

        backend = FailingBackend()
        rc = sc.setup_command(_setup_ns(), backend=backend)
        self.assertEqual(rc, 1)
        self.assertEqual(backend.list_jobs(), [])

    def test_offline_checks_really_run(self):
        backend = cron_mod.InMemoryCronBackend()
        sc._run_offline_checks = self._orig_checks
        rc = sc.setup_command(_setup_ns(), backend=backend)
        self.assertEqual(rc, 0, "offline self-test + dry-run must pass")

    def test_setup_installs_owned_runners(self):
        backend = cron_mod.InMemoryCronBackend()
        self.assertEqual(sc.setup_command(_setup_ns(), backend=backend), 0)
        expected = set(runner_install.RUNNER_FILENAMES)
        for name in expected:
            self.assertTrue((paths.scripts_dir() / name).is_file(), name)
        owned = {Path(p).name for p in runner_install.owned_files(paths.runner_ownership_path())}
        self.assertEqual(owned, expected)

    def test_setup_defaults_deliver_to_local_with_warning(self):
        backend = cron_mod.InMemoryCronBackend()
        out = self._setup_json(backend)
        result = json.loads(out)
        self.assertEqual(result["deliver"], "local")
        self.assertTrue(any("deliver" in w and "local" in w for w in result["warnings"]))
        self.assertTrue(all(j["deliver"] == "local" for j in result["jobs"]))

    def test_setup_user_deliver_is_passed_through(self):
        backend = cron_mod.InMemoryCronBackend()
        sc.setup_command(_setup_ns(deliver="platform:chat-1"), backend=backend)
        self.assertTrue(all(j["deliver"] == "platform:chat-1" for j in backend.list_jobs()))


class PauseDisableTests(_DataDirMixin):
    def setUp(self):
        super().setUp()
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = _fast_checks
        self.addCleanup(lambda: setattr(sc, "_run_offline_checks", self._orig_checks))

    def test_pause_stops_collection_and_disable_removes_jobs(self):
        backend = cron_mod.InMemoryCronBackend()
        self.assertEqual(sc.setup_command(_setup_ns(), backend=backend), 0)
        orig_backend = sc._backend
        sc._backend = lambda: backend
        self.addCleanup(lambda: setattr(sc, "_backend", orig_backend))

        ns = argparse.Namespace(json=True, data_dir=None)
        self.assertEqual(sc.pause_command(ns), 0)
        self.assertTrue(sc._is_paused())

        # The paused production run must read nothing and emit nothing to stdout.
        from proactive_pilot.main import build_parser, run_once
        argv = ["--state-dir", str(paths.state_dir()), "--policy", str(paths.policy_path()),
                "--preferences", str(paths.preferences_path())]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run_once(build_parser().parse_args(argv))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "")

        # Disable must pause and remove exactly this plugin's jobs and runners.
        self.assertEqual(sc.disable_command(ns, uninstall=False), 0)
        self.assertEqual(backend.list_jobs(), [])
        for name in runner_install.RUNNER_FILENAMES:
            self.assertFalse((paths.scripts_dir() / name).exists(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
