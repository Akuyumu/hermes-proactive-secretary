"""Independent actual-CLI lifecycle tests for the portal cron job.

These tests exercise the registered ``hermes secretary portal`` parser and the
real CLI wrapper. They never invoke the native scheduler, the network, or any
other production mutator: the injected backend is the module's in-memory
backend (or a thin recording/failure subclass of it), and every data/home root
is a ``TemporaryDirectory`` under the ambient ``TMPDIR``. Nothing is written
under the repository.

The expectations are the user contract, not a snapshot of current internals.
In particular they deliberately expect repeated ``--skill`` values to survive
as an exact case-preserved list, malformed/flag-like pins to be refused before
any mutation, and model/workdir comparisons to remain case-sensitive. Those
expectations fail against the source bugs noted in the build brief until the
source is fixed; the tests do not encode the buggy behaviour.
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

# Importing the package executes dataclasses; keep the repository free of
# __pycache__ as a side effect of this test module.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package(name: str = "hermes_proactive_secretary_portal_qa"):
    """Load the plugin exactly like the sibling tests, registering first."""
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    # Dataclasses resolve their module through sys.modules during exec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")

PORTAL = cron_mod.PORTAL_JOB_NAME
MONITORING = cron_mod.MONITORING_JOB_NAME
REMINDER = cron_mod.REMINDER_JOB_NAME


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


def _fast_checks(_fixture: str) -> dict:
    return {"self_test_rc": 0,
            "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}


def _setup_namespace(*, no_cron: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        force=False, no_cron=no_cron, json=True, fixture="healthy_empty",
        deliver=None, data_dir=None, import_from=None, import_dry_run=False,
        with_preparation=False,
    )


def _foreign_job(name: str = PORTAL, job_id: str = "foreign-7",
                 script: str = "someone_elses_runner.py") -> dict:
    return {
        "id": job_id, "name": name, "schedule": "0 0 1 1 *",
        "script": script, "deliver": "origin", "workdir": "/elsewhere",
        "mode": "no-agent", "state": "active", "last_run": "",
    }


class _TouchBackend(cron_mod.InMemoryCronBackend):
    """In-memory backend that records every backend operation."""

    def __init__(self, jobs=None) -> None:
        super().__init__(jobs)
        self.calls: list[str] = []

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
        return super().pause_job(job_id)

    def resume_job(self, job_id):
        self.calls.append("resume")
        return super().resume_job(job_id)


class _RecordingBackend(_TouchBackend):
    """In-memory backend that also records the real native create argv."""

    def __init__(self, jobs=None) -> None:
        super().__init__(jobs)
        self.argvs: list[list[str]] = []

    def _runner(self, argv):
        self.argvs.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def add_job(self, spec):
        self.calls.append("add")
        # Use the production argv builder, but capture instead of executing it.
        cron_mod.HermesCronBackend(hermes_bin="hermes", runner=self._runner).add_job(spec)
        return cron_mod.InMemoryCronBackend.add_job(self, spec)


class _PauseFailBackend(cron_mod.InMemoryCronBackend):
    def pause_job(self, job_id):
        raise RuntimeError("synthetic pause failure")


class _Isolated(unittest.TestCase):
    """Ambient-TMPDIR isolation with a synthetic HERMES_HOME and data root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._keys = ("HERMES_HOME", paths.DATA_ENV)
        self._saved = {key: os.environ.get(key) for key in self._keys}
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.profile("a")

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
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

    def snapshot(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        roots = (
            ("home", Path(os.environ["HERMES_HOME"])),
            ("data", Path(os.environ[paths.DATA_ENV])),
        )
        for label, root in roots:
            if not root.exists():
                entries[label] = "<absent>"
                continue
            for path in sorted(root.rglob("*")):
                rel = f"{label}/{path.relative_to(root)}"
                if path.is_file():
                    entries[rel] = path.read_text(encoding="utf-8", errors="replace")
                else:
                    entries[rel] = "<dir>"
        return entries

    def run_portal(self, backend, *argv: str):
        ns = _parse(*argv)
        rc, payload = _capture_stdout(sc.portal_command, ns, backend=backend)
        return rc, payload, ns

    def enable(self, backend, schedule: str = "*/5 * * * *", *extra: str):
        return self.run_portal(
            backend, "portal", "enable", "--json", "--schedule", schedule, *extra)

    def preview(self, backend, schedule: str = "*/5 * * * *", *extra: str):
        return self.run_portal(
            backend, "portal", "preview", "--json", "--schedule", schedule, *extra)

    def status(self, backend):
        return self.run_portal(backend, "portal", "status", "--json")

    def pause(self, backend):
        return self.run_portal(backend, "portal", "pause", "--json")

    def resume(self, backend):
        return self.run_portal(backend, "portal", "resume", "--json")

    def remove(self, backend):
        return self.run_portal(backend, "portal", "remove", "--json")

    def ledger(self) -> dict:
        return cron_mod.load_job_ownership(paths.job_ownership_path())

    def _setup(self, backend=None, *, no_cron: bool = False):
        original = sc._run_offline_checks
        sc._run_offline_checks = _fast_checks
        self.addCleanup(setattr, sc, "_run_offline_checks", original)
        rc, payload = _capture_stdout(
            sc.setup_command, _setup_namespace(no_cron=no_cron), backend=backend)
        return rc, payload


class ParserAndPreviewTests(_Isolated):
    def test_repeated_skill_parser_preserves_exact_case_list(self):
        ns = _parse("portal", "enable", "--schedule", "*/5 * * * *",
                    "--skill", "alpha", "--skill", "Beta")
        self.assertEqual(ns.skill, ["alpha", "Beta"])

    def test_preview_and_status_do_not_create_jobs_ledger_state_or_runners(self):
        backend = _TouchBackend()
        before = self.snapshot()

        rc, preview, _ = self.preview(
            backend, "*/5 * * * *", "--model", "Model-X",
            "--skill", "alpha", "--skill", "Beta")
        self.assertEqual(rc, 0)
        self.assertEqual(preview["status"], "would-create")
        self.assertEqual(preview["action"], "preview")

        rc, status, _ = self.status(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(status["status"], "missing")

        # No job, ledger, state or runner directory was created.
        self.assertEqual(backend.list_jobs(), [])
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)
        self.assertNotIn("pause", backend.calls)
        self.assertNotIn("resume", backend.calls)
        self.assertFalse(paths.job_ownership_path().exists())
        self.assertFalse(paths.state_dir().exists())
        self.assertFalse(paths.scripts_dir().exists())
        self.assertEqual(self.snapshot(), before)

    def test_module_import_creates_no_jobs_ledger_state_or_runners(self):
        self.profile("import")
        _load_plugin_package("hermes_proactive_secretary_portal_import_qa")
        self.assertFalse(paths.job_ownership_path().exists())
        self.assertFalse(paths.state_dir().exists())
        self.assertFalse(paths.scripts_dir().exists())

    def test_enable_argv_exact_native_pins_and_case_preserved_workdir_model(self):
        backend = _RecordingBackend()
        rc, payload, _ = self.enable(
            backend, "*/5 * * * *",
            "--model", "Model-X", "--provider", "Provider-Y",
            "--reasoning-effort", "High",
            "--skill", "alpha", "--skill", "Beta",
            "--workdir", "/Work/Dir", "--deliver", "local",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["effective_pins"]["reasoning_effort"], "high")
        self.assertEqual(len(backend.argvs), 1)
        argv = backend.argvs[0]

        self.assertEqual(argv[0], "hermes")
        self.assertEqual(argv[1:3], ["cron", "create"])
        self.assertEqual(argv[3], "*/5 * * * *")
        self.assertEqual(argv[4], portal.PROMPT)
        self.assertNotIn("--script", argv)
        self.assertNotIn("--no-agent", argv)

        for flag, value in (
            ("--model", "Model-X"),
            ("--provider", "Provider-Y"),
            ("--reasoning-effort", "high"),
            ("--workdir", "/Work/Dir"),
            ("--deliver", "local"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)

        skill_positions = [i for i, token in enumerate(argv) if token == "--skill"]
        self.assertEqual([argv[i + 1] for i in skill_positions],
                         ["alpha", "Beta"])
        self.assertNotIn("['alpha', 'Beta']", argv)

        job = backend.list_jobs()[0]
        self.assertEqual(job["mode"], "agent")
        self.assertEqual(job["script"], "")
        self.assertEqual(job["model"], "Model-X")
        self.assertEqual(job["provider"], "Provider-Y")
        self.assertEqual(job["reasoning"], "high")
        self.assertEqual(job["skills"], ["alpha", "Beta"])
        self.assertEqual(job["workdir"], "/Work/Dir")
        record = self.ledger()["jobs"][PORTAL]
        self.assertEqual(record["skills"], ["alpha", "Beta"])
        self.assertEqual(record["model"], "Model-X")
        self.assertEqual(record["workdir"], "/Work/Dir")

    def test_omitted_pins_are_absent_and_inherit(self):
        backend = _RecordingBackend()
        rc, _, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        argv = backend.argvs[0]
        for flag in ("--model", "--provider", "--reasoning-effort", "--skill",
                     "--workdir"):
            self.assertNotIn(flag, argv)
        # Portal policy is local-only delivery; it is not an inherited agent pin.
        self.assertEqual(argv[argv.index("--deliver") + 1], "local")

        job = backend.list_jobs()[0]
        self.assertEqual(job["model"], "")
        self.assertEqual(job["provider"], "")
        self.assertEqual(job["reasoning"], "")
        self.assertEqual(job["skills"], [])
        self.assertEqual(job["workdir"], "")
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["mode"], "agent")
        self.assertEqual(job["script"], "")


class EnableValidationTests(_Isolated):
    def test_enable_requires_explicit_schedule_before_mutation(self):
        backend = _TouchBackend()
        rc, payload, _ = self.run_portal(backend, "portal", "enable", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "error")
        self.assertIn("schedule", payload["detail"].lower())
        self.assertEqual(backend.calls, [])
        self.assertFalse(paths.job_ownership_path().exists())

    def test_non_local_delivery_rejected_before_mutation(self):
        for bad in ("origin", "platform:chat#1"):
            with self.subTest(deliver=bad):
                backend = _TouchBackend()
                rc, payload, _ = self.enable(
                    backend, "*/5 * * * *", "--deliver", bad)
                self.assertEqual(rc, 1)
                self.assertEqual(payload["status"], "rejected")
                self.assertIn("local", payload["detail"].lower())
                self.assertEqual(backend.calls, [])
                self.assertFalse(paths.job_ownership_path().exists())

        backend = _TouchBackend()
        rc, _, _ = self.enable(backend, "*/5 * * * *", "--deliver", "local")
        self.assertEqual(rc, 0)
        self.assertEqual(len(backend.list_jobs()), 1)

    def test_parser_has_no_script_or_no_agent_for_portal(self):
        for extra in (("--script", "runner.py"), ("--no-agent",)):
            with self.subTest(extra=extra):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        _parse("portal", "enable", "--schedule", "*/5 * * * *", *extra)

    def test_malformed_control_and_flag_like_pins_refused_before_mutation(self):
        cases = (
            ("--schedule", "bad\nschedule"),
            ("--model", "bad\nmodel"),
            ("--model", "bad\x00model"),
            ("--workdir", "--model"),
            ("--provider", "--model"),
            ("--skill", "-bad"),
        )
        for action in ("preview", "enable"):
            for flag, value in cases:
                with self.subTest(action=action, flag=flag, value=value):
                    backend = _TouchBackend()
                    if flag == "--schedule":
                        argv = ["portal", action, "--json",
                                f"--schedule={value}"]
                    else:
                        argv = ["portal", action, "--json",
                                "--schedule", "*/5 * * * *",
                                f"{flag}={value}"]
                    rc, payload, _ = self.run_portal(backend, *argv)
                    self.assertEqual(rc, 1, payload)
                    self.assertEqual(backend.calls, [])
                    self.assertFalse(paths.job_ownership_path().exists())

    def test_preview_validates_malformed_pins_too(self):
        for flag, value in (("--model", "bad\nmodel"),
                            ("--workdir", "--model")):
            with self.subTest(flag=flag):
                backend = _TouchBackend()
                rc, payload, _ = self.preview(
                    backend, "*/5 * * * *", f"{flag}={value}")
                self.assertEqual(rc, 1, payload)
                self.assertEqual(backend.calls, [])
                self.assertFalse(paths.job_ownership_path().exists())

    def test_changed_case_pins_are_not_normalized_equal(self):
        backend = _TouchBackend()
        rc, _, _ = self.enable(
            backend, "*/5 * * * *",
            "--model", "Model-7", "--workdir", "/Work/7")
        self.assertEqual(rc, 0)
        job_before = dict(backend.list_jobs()[0])

        backend.calls.clear()
        rc, preview, _ = self.preview(
            backend, "*/5 * * * *",
            "--model", "model-7", "--workdir", "/work/7")
        self.assertEqual(rc, 0)
        # A case-only difference is still an explicit user change.
        self.assertEqual(preview["status"], "would-update",
                         "case-only model/workdir changes must not compare equal")
        self.assertEqual(backend.list_jobs()[0], job_before)
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)


class LifecycleIdempotencyTests(_Isolated):
    def test_enable_twice_creates_one_job_and_one_ledger_record(self):
        backend = _RecordingBackend()
        rc1, first, _ = self.enable(backend)
        rc2, second, _ = self.enable(backend)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(first["job_id"], "job-1")
        self.assertEqual(second["status"], "already-present")
        self.assertEqual(len(backend.list_jobs()), 1)
        self.assertEqual(len(backend.argvs), 1)
        ledger = self.ledger()["jobs"]
        self.assertEqual(set(ledger), {PORTAL})
        self.assertEqual(ledger[PORTAL]["id"], "job-1")

    def test_pause_resume_readback_and_identical_enable_preserves_pause(self):
        backend = _TouchBackend()
        self.assertEqual(self.enable(backend)[0], 0)

        rc, paused, _ = self.pause(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(backend.list_jobs()[0]["state"], "paused")

        rc, readback, _ = self.status(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(readback["status"], "paused")
        self.assertEqual(readback["state"], "paused")

        rc, reenable, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(reenable["state"], "paused")
        self.assertIn("left paused", reenable["detail"])
        self.assertEqual(backend.list_jobs()[0]["state"], "paused")
        self.assertEqual(len(backend.list_jobs()), 1)

        rc, resumed, _ = self.resume(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(backend.list_jobs()[0]["state"], "active")
        self.assertEqual(self.status(backend)[1]["status"], "active")

    def test_remove_deletes_only_exact_owned_job(self):
        backend = _TouchBackend()
        self.assertEqual(self.enable(backend)[0], 0)
        backend.jobs.append({"id": "other-1", "name": "other-job",
                             "schedule": "* * * * *", "state": "active"})

        rc, removed, _ = self.remove(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(removed["status"], "removed")
        self.assertEqual([job["id"] for job in backend.list_jobs()],
                         ["other-1"])
        self.assertEqual(self.ledger()["jobs"], {})

    def test_disable_removes_owned_portal_but_leaves_foreign_twin(self):
        backend = _TouchBackend()
        rc, setup = self._setup(no_cron=True)
        self.assertEqual(rc, 0, setup)
        self.assertNotIn(PORTAL, self.ledger()["jobs"])

        self.assertEqual(self.enable(backend)[0], 0)
        backend.jobs.append(_foreign_job(job_id="foreign-twin"))

        original = sc._backend
        sc._backend = lambda: backend
        try:
            rc, result = _capture_stdout(
                sc.disable_command,
                argparse.Namespace(json=True, data_dir=None, purge=False))
        finally:
            sc._backend = original

        self.assertEqual(rc, 0, result)
        actions = {row["name"]: row["action"] for row in result["removed"]}
        self.assertEqual(actions[PORTAL], "removed")
        self.assertEqual([job["id"] for job in backend.list_jobs()],
                         ["foreign-twin"])
        self.assertNotIn(PORTAL, self.ledger()["jobs"])

    def test_setup_does_not_default_create_portal_job(self):
        backend = _TouchBackend()
        rc, result = self._setup(backend, no_cron=False)
        self.assertEqual(rc, 0, result)
        names = {job["name"] for job in backend.list_jobs()}
        self.assertEqual(names, {MONITORING, REMINDER})
        self.assertNotIn(PORTAL, names)
        self.assertEqual(set(self.ledger()["jobs"]), {MONITORING, REMINDER})


class OwnershipAndFailureTests(_Isolated):
    def test_foreign_same_name_is_refused_and_untouched(self):
        foreign = _foreign_job()
        backend = _TouchBackend([foreign])
        rc, payload, _ = self.enable(backend)
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "foreign")
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual(self.ledger()["jobs"], {})
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)

    def test_mismatched_recorded_id_is_refused_and_untouched(self):
        cron_mod.save_job_ownership(
            paths.job_ownership_path(),
            {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN,
             "jobs": {PORTAL: {"id": "owned-1", "name": PORTAL}}})
        foreign = _foreign_job(job_id="foreign-9")
        backend = _TouchBackend([foreign])

        rc, payload, _ = self.enable(backend)
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "foreign")
        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual(self.ledger()["jobs"][PORTAL]["id"], "owned-1")
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)

    def test_changed_pin_with_missing_readback_is_inconclusive_and_not_deleted(self):
        backend = _TouchBackend()
        self.assertEqual(
            self.enable(backend, "*/5 * * * *", "--model", "old")[0], 0)
        owned_id = backend.list_jobs()[0]["id"]
        # The native readback does not expose the model field at all.
        backend.jobs[0].pop("model", None)
        backend.jobs.append({"id": "other-1", "name": "other-job",
                             "schedule": "* * * * *", "state": "active"})

        backend.calls.clear()
        rc, preview, _ = self.preview(
            backend, "*/5 * * * *", "--model", "new")
        self.assertEqual(rc, 1)
        self.assertEqual(preview["status"], "inconclusive")
        self.assertIn("model", preview["detail"])

        rc, enable, _ = self.enable(
            backend, "*/5 * * * *", "--model", "new")
        self.assertEqual(rc, 1)
        self.assertEqual(enable["status"], "inconclusive")
        self.assertIn("model", enable["detail"])

        ids = [job["id"] for job in backend.list_jobs()]
        self.assertEqual(ids, [owned_id, "other-1"])
        self.assertEqual(self.ledger()["jobs"][PORTAL]["model"], "old")
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)

    def test_positive_enabled_flag_without_known_state_is_not_active(self):
        backend = _TouchBackend()
        self.assertEqual(self.enable(backend)[0], 0)
        backend.jobs[0].pop("state", None)
        backend.jobs[0]["enabled"] = True

        rc, payload, _ = self.status(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "unknown")
        self.assertEqual(payload["status"], "present")
        self.assertNotEqual(payload["status"], "active")

    def test_remove_mutation_failure_leaves_job_and_ledger(self):
        backend = _TouchBackend()
        self.assertEqual(self.enable(backend)[0], 0)
        backend.jobs.append({"id": "other-1", "name": "other-job",
                             "schedule": "* * * * *", "state": "active"})
        backend.fail_remove = True

        rc, payload, _ = self.remove(backend)
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(len(backend.list_jobs()), 2)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["id"], "job-1")

    def test_pause_mutation_failure_leaves_owned_and_other_jobs(self):
        backend = _PauseFailBackend()
        self.assertEqual(self.enable(backend)[0], 0)
        backend.jobs.append({"id": "other-1", "name": "other-job",
                             "schedule": "* * * * *", "state": "active"})

        rc, payload, _ = self.pause(backend)
        self.assertEqual(rc, 1)
        self.assertEqual(payload["status"], "error")
        states = {job["name"]: job["state"] for job in backend.list_jobs()}
        self.assertEqual(states[PORTAL], "active")
        self.assertEqual(states["other-job"], "active")
        self.assertEqual(self.ledger()["jobs"][PORTAL]["id"], "job-1")


class ProfileIsolationTests(_Isolated):
    def test_two_profile_data_roots_isolate_jobs_and_ledgers(self):
        """Plain A/B isolation; no native owner/context helper was in read scope.

        The routed/native context is explicitly unqualified here. Isolation is
        proven through the synthetic HERMES_HOME and data override: switching
        profiles changes the ledger path, and the empty B backend cannot see A's
        recorded job.
        """
        backend_a = _TouchBackend()
        rc, _, _ = self.enable(backend_a)
        self.assertEqual(rc, 0)
        path_a = paths.job_ownership_path()
        ledger_a_bytes = path_a.read_text(encoding="utf-8")

        backend_b = _TouchBackend()
        self.profile("b")
        path_b = paths.job_ownership_path()
        self.assertNotEqual(path_a, path_b)

        rc, status_b, _ = self.status(backend_b)
        self.assertEqual(rc, 0)
        self.assertEqual(status_b["status"], "missing")
        self.assertEqual(status_b["job_id"], "")
        self.assertEqual(self.ledger()["jobs"], {})

        rc, _, _ = self.enable(backend_b)
        self.assertEqual(rc, 0)
        self.assertIn(PORTAL, self.ledger()["jobs"])
        self.assertTrue(path_b.is_file())
        # B's enable did not rewrite A's ledger.
        self.assertEqual(path_a.read_text(encoding="utf-8"), ledger_a_bytes)

        self.profile("a")
        rc, status_a, _ = self.status(backend_a)
        self.assertEqual(rc, 0)
        self.assertEqual(status_a["job_id"], "job-1")
        self.assertIn(PORTAL, self.ledger()["jobs"])

    def test_data_override_keeps_launch_home_unmutated(self):
        """Wrong launch HOME with B data root; native context is unqualified.

        No ownerpath test-context helper was available in the permitted read
        scope, so this is the plain launch-home/data override split.
        """
        home_a, data_a = self.profile("a")
        _home_b, data_b = self.profile("b")
        # Launch from A's home while the runtime data root is B's.
        os.environ["HERMES_HOME"] = str(home_a)
        os.environ[paths.DATA_ENV] = str(data_b)
        home_before = {path.name for path in home_a.rglob("*")}

        backend = _TouchBackend()
        rc, _, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        self.assertTrue((data_b / "cron_job_ownership.json").is_file())
        self.assertFalse((data_a / "cron_job_ownership.json").exists())
        self.assertEqual({path.name for path in home_a.rglob("*")}, home_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
