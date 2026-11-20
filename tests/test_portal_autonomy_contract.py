"""Contract tests for the portal job's autonomous useful-work prompt.

These exercise the real lifecycle path (registered ``hermes secretary portal``
parser -> ``secretary_cli`` -> ``secretary_portal`` -> cron backend) and assert
the prompt that is actually persisted on the native job and in the ownership
ledger. They never touch the scheduler, network or a real profile: every test
uses an ambient-TMPDIR synthetic ``HERMES_HOME``/data root and an in-memory
backend, matching the sibling portal lifecycle tests.
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


def _load_plugin_package(name: str = "hermes_proactive_secretary_autonomy_qa"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
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

PORTAL = cron_mod.PORTAL_JOB_NAME


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


class _TouchBackend(cron_mod.InMemoryCronBackend):
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


class _RecordingBackend(_TouchBackend):
    """In-memory backend that also captures the production native create argv."""

    def __init__(self, jobs=None) -> None:
        super().__init__(jobs)
        self.argvs: list[list[str]] = []

    def _runner(self, argv):
        self.argvs.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def add_job(self, spec):
        self.calls.append("add")
        cron_mod.HermesCronBackend(hermes_bin="hermes", runner=self._runner).add_job(spec)
        return cron_mod.InMemoryCronBackend.add_job(self, spec)


class _Isolated(unittest.TestCase):
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

    def run_portal(self, backend, *argv: str):
        ns = _parse(*argv)
        return _capture_stdout(sc.portal_command, ns, backend=backend)

    def enable(self, backend, schedule: str = "*/5 * * * *", *extra: str):
        return self.run_portal(
            backend, "portal", "enable", "--json", "--schedule", schedule, *extra)

    def preview(self, backend, schedule: str = "*/5 * * * *", *extra: str):
        return self.run_portal(
            backend, "portal", "preview", "--json", "--schedule", schedule, *extra)

    def ledger(self) -> dict:
        return cron_mod.load_job_ownership(paths.job_ownership_path())


class PersistedWorkContractTests(_Isolated):
    def test_enable_persists_useful_work_contract_on_job_and_ledger(self):
        backend = _RecordingBackend()
        rc, payload = self.enable(backend)
        self.assertEqual(rc, 0, payload)

        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["name"], PORTAL)
        self.assertEqual(job["prompt"], portal.PROMPT)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["prompt"], portal.PROMPT)

        prompt = job["prompt"]
        required = (
            "accepted responsibilities",
            "authorised sources",
            "changed evidence",
            "read back the actual result",
            "native memory and existing work artifacts",
            "next due follow-up",
            "never create a parallel memory, preference, scheduling or permission engine",
            "explicit corrections",
            "temporary scope",
            "thanks, silence, or non-response",
            "approval-required action unexecuted",
            "pending blocker",
            "secretary_propose_notice",
            "Keep the native final local",
            "meaningful silence",
            "never grants new authority, sources, or spending",
            "not enforcement",
            "Native skills and workdir pins remain optional",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_persisted_prompt_is_the_actual_native_create_argument(self):
        backend = _RecordingBackend()
        rc, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(backend.argvs), 1)
        argv = backend.argvs[0]
        self.assertEqual(argv[1:3], ["cron", "create"])
        self.assertEqual(argv[4], portal.PROMPT)
        self.assertEqual(argv[argv.index("--deliver") + 1], "local")


def _self_context_spec(**overrides) -> "cron_mod.NormalAgentJobSpec":
    values = {
        "name": "hermes-proactive-secretary-portal",
        "schedule": "*/5 * * * *",
        "prompt": portal.PROMPT,
        "context_from": ["self"],
    }
    values.update(overrides)
    return cron_mod.NormalAgentJobSpec(**values)


def _capture_create_argv(spec):
    captured: list[list[str]] = []

    def runner(argv):
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    backend = cron_mod.HermesCronBackend(hermes_bin="hermes", runner=runner)
    backend.add_job(spec)
    return captured


class NativeSelfContextCliTests(_Isolated):
    def test_self_context_emits_continuity_once_and_never_context_from(self):
        argv = _capture_create_argv(_self_context_spec())[0]
        self.assertEqual(argv[:3], ["hermes", "cron", "create"])
        self.assertEqual(argv[4], portal.PROMPT)
        self.assertEqual(argv.count("--continuity"), 1)
        self.assertEqual(argv[-1], "--continuity")
        self.assertNotIn("--context-from", argv)

    def test_no_context_emits_neither_continuity_nor_context_from(self):
        argv = _capture_create_argv(_self_context_spec(context_from=[]))[0]
        self.assertNotIn("--continuity", argv)
        self.assertNotIn("--context-from", argv)

    def test_unsupported_non_self_context_fails_before_any_backend_mutation(self):
        captured: list[list[str]] = []

        def runner(argv):
            captured.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = cron_mod.HermesCronBackend(hermes_bin="hermes", runner=runner)
        with self.assertRaises(cron_mod.CronError):
            backend.add_job(_self_context_spec(context_from=["job-abc"]))
        self.assertEqual(captured, [])

        memory = cron_mod.InMemoryCronBackend()
        with self.assertRaises(cron_mod.CronError):
            memory.add_job(_self_context_spec(context_from=["job-abc"]))
        self.assertEqual(memory.list_jobs(), [])

    def test_persisted_self_context_round_trips_and_drift_is_reconciled(self):
        backend = cron_mod.InMemoryCronBackend()
        ownership = cron_mod.load_job_ownership(None)
        spec = _self_context_spec()

        created = cron_mod.ensure_normal_agent_job(
            backend, spec, ownership=ownership, ownership_path=None)
        self.assertEqual(created.action, "created")
        self.assertEqual(backend.list_jobs()[0]["context_from"], ["self"])
        self.assertEqual(
            ownership["jobs"][spec.name]["context_from"], ["self"])

        unchanged = cron_mod.ensure_normal_agent_job(
            backend, spec, ownership=ownership, ownership_path=None)
        self.assertEqual(unchanged.action, "already-present")

        backend.jobs[0]["context_from"] = []
        updated = cron_mod.ensure_normal_agent_job(
            backend, spec, ownership=ownership, ownership_path=None)
        self.assertEqual(updated.action, "updated")
        self.assertEqual(backend.list_jobs()[0]["context_from"], ["self"])
        self.assertEqual(
            ownership["jobs"][spec.name]["context_from"], ["self"])


class NativeParserConformanceTests(_Isolated):
    def _native_parser(self):
        try:
            from hermes_cli.subcommands import cron as native_cron
        except Exception as exc:  # noqa: BLE001 - optional installed SDK
            self.skipTest(f"installed native cron CLI is unavailable: {exc}")
        build = getattr(native_cron, "build_cron_parser", None)
        if not callable(build):
            self.skipTest("installed native cron CLI has no build_cron_parser")
        parser = argparse.ArgumentParser(prog="hermes")
        subparsers = parser.add_subparsers(dest="command")
        build(subparsers, cmd_cron=lambda *args, **kwargs: 0)
        return parser

    def test_emitted_self_context_argv_parses_against_installed_native_parser(self):
        parser = self._native_parser()
        argv = _capture_create_argv(_self_context_spec())[0]
        self.assertNotIn("--context-from", argv)
        # Drop the executable token: the real argv is "hermes cron create ...".
        parsed = parser.parse_args(argv[1:])
        self.assertEqual(getattr(parsed, "continuity", None), True)
        self.assertEqual(getattr(parsed, "prompt", None), portal.PROMPT)


class NegativeContractTests(_Isolated):
    def test_preview_does_not_activate_or_persist_anything(self):
        backend = _TouchBackend()
        rc, payload = self.preview(backend)
        calls = list(backend.calls)
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "would-create")
        self.assertEqual(calls, ["list"])
        self.assertFalse(any(call in {"add", "remove"} for call in calls))
        self.assertEqual(backend.list_jobs(), [])
        self.assertFalse(paths.job_ownership_path().exists())
        self.assertFalse(paths.state_dir().exists())
        self.assertFalse(paths.scripts_dir().exists())

    def test_enable_stays_local_native_agent_without_a_second_engine(self):
        backend = _RecordingBackend()
        rc, _ = self.enable(backend)
        self.assertEqual(rc, 0)

        argv = backend.argvs[0]
        self.assertNotIn("--script", argv)
        self.assertNotIn("--no-agent", argv)
        self.assertEqual(argv[argv.index("--deliver") + 1], "local")

        job = backend.list_jobs()[0]
        self.assertEqual(job["mode"], "agent")
        self.assertEqual(job["script"], "")
        self.assertEqual(job["deliver"], "local")
        # The work contract reuses native memory/artifacts; lifecycle writes only
        # the ownership ledger and never invents state or script engines.
        self.assertFalse(paths.state_dir().exists())
        self.assertFalse(paths.scripts_dir().exists())
        self.assertEqual(set(self.ledger()["jobs"]), {PORTAL})

    def test_contract_forbids_fabricated_or_unverified_success(self):
        prompt = portal.PROMPT
        for forbidden in (
            "claim success without reading back",
            "grant yourself new authority",
            "create a parallel memory engine",
            "send notices directly",
            "bypass plugin controls",
            "ignore the monitoring gates",
        ):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("before claiming success", prompt)
        self.assertIn("never send directly", prompt)
        self.assertIn("never send directly or attempt to evade plugin controls", prompt)


class PromptReconciliationTests(_Isolated):
    def _set_prompt(self, backend, prompt: str) -> None:
        self.assertEqual(len(backend.jobs), 1)
        backend.jobs[0]["prompt"] = prompt

    def _set_ledger_prompt(self, prompt: str) -> None:
        ledger = self.ledger()
        ledger["jobs"][PORTAL]["prompt"] = prompt
        cron_mod.save_job_ownership(paths.job_ownership_path(), ledger)

    def test_preview_detects_changed_prompt_without_writes_then_enable_reconciles(self):
        backend = _RecordingBackend()
        rc, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        self.assertEqual(backend.jobs[0]["prompt"], portal.PROMPT)

        old_prompt = "old contract: send directly without reading back"
        self._set_prompt(backend, old_prompt)
        self._set_ledger_prompt(old_prompt)

        before = list(backend.calls)
        rc, preview = self.preview(backend)
        preview_calls = backend.calls[len(before):]
        self.assertEqual(rc, 0, preview)
        self.assertEqual(preview["status"], "would-update")
        self.assertEqual(preview_calls, ["list"])
        self.assertNotIn("prompt", preview["requested_pins"])
        self.assertEqual(backend.jobs[0]["prompt"], old_prompt)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["prompt"], old_prompt)

        before = list(backend.calls)
        rc, enabled = self.enable(backend)
        enable_calls = backend.calls[len(before):]
        self.assertEqual(rc, 0, enabled)
        self.assertEqual(enabled["status"], "updated")
        self.assertIn("remove", enable_calls)
        self.assertIn("add", enable_calls)
        self.assertEqual(len(backend.jobs), 1)
        self.assertEqual(backend.jobs[0]["prompt"], portal.PROMPT)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["prompt"], portal.PROMPT)
        self.assertEqual(backend.jobs[0]["id"], enabled["job_id"])
        self.assertEqual(backend.argvs[-1][4], portal.PROMPT)

    def test_repeated_enable_does_not_duplicate_or_churn(self):
        backend = _TouchBackend()
        rc, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        first_id = jobs[0]["id"]

        before = list(backend.calls)
        rc, payload = self.enable(backend)
        repeated_calls = backend.calls[len(before):]
        jobs = backend.list_jobs()
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "already-present")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], first_id)
        self.assertNotIn("add", repeated_calls)
        self.assertNotIn("remove", repeated_calls)

    def test_foreign_same_name_job_is_never_adopted_or_modified(self):
        foreign = {
            "id": "foreign-1", "name": PORTAL, "schedule": "*/5 * * * *",
            "prompt": "foreign prompt", "mode": "agent", "state": "active",
            "deliver": "local",
        }
        backend = _TouchBackend([foreign])
        rc, payload = self.enable(backend)
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["status"], "foreign")
        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "foreign-1")
        self.assertEqual(jobs[0]["prompt"], "foreign prompt")
        self.assertNotIn("add", backend.calls)
        self.assertNotIn("remove", backend.calls)
        self.assertFalse(paths.job_ownership_path().exists())

    def test_missing_live_prompt_with_stale_ledger_is_not_verified_current(self):
        backend = _TouchBackend()
        rc, _ = self.enable(backend)
        self.assertEqual(rc, 0)
        backend.jobs[0].pop("prompt", None)
        self._set_ledger_prompt("stale ledger prompt")

        rc, preview = self.preview(backend)
        self.assertNotEqual(rc, 0)
        self.assertEqual(preview["status"], "inconclusive")
        self.assertFalse(preview["ok"])

        before = list(backend.calls)
        rc, enabled = self.enable(backend)
        enable_calls = backend.calls[len(before):]
        self.assertNotEqual(rc, 0)
        self.assertEqual(enabled["status"], "inconclusive")
        self.assertNotIn("add", enable_calls)
        self.assertNotIn("remove", enable_calls)
        self.assertEqual(self.ledger()["jobs"][PORTAL]["prompt"], "stale ledger prompt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
