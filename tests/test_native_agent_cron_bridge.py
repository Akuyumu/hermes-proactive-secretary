"""Offline tests for the normal-agent native cron bridge.

No live scheduler is touched: command capture proves the argv shape, and an
in-memory backend proves ownership/readback semantics. Script/reminder jobs are
kept on their original ``--script --no-agent`` argv.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_native_agent_cron"
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


def _agent_spec(**overrides):
    values = dict(
        name="hermes-proactive-secretary-agent",
        schedule="*/5 * * * *",
        prompt="Summarise my day and report only actionable items.",
    )
    values.update(overrides)
    return cron_mod.NormalAgentJobSpec(**values)


class _Capture:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.fail = False

    def __call__(self, argv):
        self.argvs.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "fake failure")
        return subprocess.CompletedProcess(argv, 0, "", "")


class AgentArgvTests(unittest.TestCase):
    def test_true_normal_agent_argv_has_no_script_or_no_agent(self):
        capture = _Capture()
        backend = cron_mod.HermesCronBackend(runner=capture)
        backend.add_job(_agent_spec(deliver="local"))

        argv = capture.argvs[0]
        self.assertEqual(argv[0], "hermes")
        self.assertEqual(argv[1:3], ["cron", "create"])
        self.assertEqual(argv[3], "*/5 * * * *")
        self.assertEqual(argv[4], "Summarise my day and report only actionable items.")
        self.assertNotIn("--no-agent", argv)
        self.assertNotIn("--script", argv)
        self.assertEqual(argv[argv.index("--name") + 1], "hermes-proactive-secretary-agent")
        self.assertEqual(argv[argv.index("--deliver") + 1], "local")

    def test_supplied_native_pins_are_individual_argv_tokens(self):
        capture = _Capture()
        backend = cron_mod.HermesCronBackend(runner=capture)
        spec = _agent_spec(
            model="native-model-x", provider="native-provider-y",
            reasoning="high", skills=["calendar", "email"],
            workdir="/plugins/hermes-proactive-secretary", deliver="origin",
        )
        backend.add_job(spec)

        argv = capture.argvs[0]
        for flag, value in (
            ("--model", "native-model-x"),
            ("--provider", "native-provider-y"),
            ("--reasoning-effort", "high"),
            ("--workdir", "/plugins/hermes-proactive-secretary"),
            ("--deliver", "origin"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        skill_positions = [i for i, arg in enumerate(argv) if arg == "--skill"]
        self.assertEqual([argv[i + 1] for i in skill_positions], ["calendar", "email"])
        self.assertNotIn("calendar,email", argv)
        self.assertNotIn("--skills", argv)
        self.assertNotIn("--reasoning", argv)
        self.assertFalse(any(" " in part and part.startswith("--") for part in argv))

    def test_omitted_pins_are_absent_so_native_defaults_inherit(self):
        capture = _Capture()
        cron_mod.HermesCronBackend(runner=capture).add_job(_agent_spec(deliver=""))
        argv = capture.argvs[0]
        for flag in ("--model", "--provider", "--reasoning-effort", "--skill",
                     "--workdir", "--deliver"):
            self.assertNotIn(flag, argv)

    def test_legacy_script_and_reminder_argv_are_unchanged(self):
        capture = _Capture()
        backend = cron_mod.HermesCronBackend(runner=capture)
        for spec in cron_mod.build_job_specs():
            backend.add_job(spec)
        for spec, argv in zip(cron_mod.build_job_specs(), capture.argvs):
            self.assertIn("--script", argv)
            self.assertIn("--no-agent", argv)
            self.assertNotIn("--model", argv)
            self.assertEqual(argv[0], "hermes")
            self.assertEqual(argv[1:3], ["cron", "create"])
            self.assertEqual(argv[3], spec.schedule)
            self.assertEqual(argv[4], "--name")


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.capture = _Capture()
        self.backend = cron_mod.HermesCronBackend(runner=self.capture)

    def test_empty_or_malformed_agent_fields_are_rejected_before_command(self):
        bad_specs = [
            _agent_spec(prompt=""),
            _agent_spec(prompt="   "),
            _agent_spec(name=""),
            _agent_spec(schedule=""),
            _agent_spec(prompt="first line\nsecond line"),
            _agent_spec(prompt="--no-agent"),
            _agent_spec(model="--provider"),
            _agent_spec(skills=["calendar", "\x00bad"]),
        ]
        for spec in bad_specs:
            with self.assertRaises(cron_mod.CronError):
                self.backend.add_job(spec)
        self.assertEqual(self.capture.argvs, [])

    def test_mixed_script_plus_agent_mode_is_refused_before_command(self):
        with self.assertRaises(cron_mod.CronError):
            self.backend.add_job(_agent_spec(script="runner.py"))
        with self.assertRaises(cron_mod.CronError):
            self.backend.add_job(cron_mod.CronJobSpec(
                name="mixed", schedule="* * * * *", script="runner.py",
                prompt="do work", model="m",
            ))
        with self.assertRaises(cron_mod.CronError):
            cron_mod.ensure_normal_agent_job(
                self.backend, cron_mod.CronJobSpec(
                    name="script-only", schedule="* * * * *", script="runner.py"))
        self.assertEqual(self.capture.argvs, [])

    def test_agent_pin_without_prompt_is_refused(self):
        with self.assertRaises(cron_mod.CronError):
            self.backend.add_job(cron_mod.CronJobSpec(
                name="pinned", schedule="* * * * *", model="m"))


class OwnershipReadbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "job_ownership.json"
        self.addCleanup(self._tmp.cleanup)

    def _ownership(self):
        return cron_mod.load_job_ownership(self.path)

    def test_creation_records_exact_new_id_and_available_agent_fields(self):
        backend = cron_mod.InMemoryCronBackend()
        ownership = self._ownership()
        result = cron_mod.ensure_normal_agent_job(
            backend, _agent_spec(model="m", provider="p", reasoning="high",
                                 skills=["calendar"]),
            ownership=ownership, ownership_path=self.path, now="2026-11-03T00:00:00+00:00",
        )
        self.assertEqual(result.action, "created")
        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "job-1")
        self.assertEqual(jobs[0]["mode"], "agent")
        self.assertTrue(result.detail.startswith("id=job-1; verification=full"))

        reloaded = cron_mod.load_job_ownership(self.path)
        record = reloaded["jobs"][_agent_spec().name]
        self.assertEqual(record["id"], "job-1")
        self.assertEqual(record["prompt"], _agent_spec().prompt)
        self.assertEqual(record["model"], "m")
        self.assertEqual(record["provider"], "p")
        self.assertEqual(record["reasoning"], "high")
        self.assertEqual(record["skills"], ["calendar"])
        self.assertEqual(record["mode"], "agent")

    def test_foreign_same_name_is_never_adopted_or_removed(self):
        foreign = {
            "id": "foreign-7", "name": _agent_spec().name,
            "schedule": "0 0 1 1 *", "script": "someone_else.py",
            "deliver": "origin", "workdir": "/elsewhere", "mode": "no-agent",
            "state": "active",
        }
        backend = cron_mod.InMemoryCronBackend([foreign])
        ownership = self._ownership()
        with self.assertRaises(cron_mod.CronError):
            cron_mod.ensure_normal_agent_job(
                backend, _agent_spec(), ownership=ownership, ownership_path=self.path)

        self.assertEqual(backend.list_jobs(), [foreign])
        self.assertEqual(cron_mod.load_job_ownership(self.path)["jobs"], {})

    def test_mismatched_existing_record_is_refused(self):
        backend = cron_mod.InMemoryCronBackend([{
            "id": "foreign-9", "name": _agent_spec().name,
            "schedule": "*/5 * * * *", "prompt": _agent_spec().prompt,
            "deliver": "local", "workdir": "", "mode": "agent", "state": "active",
        }])
        ownership = {"schema": 1, "plugin": cron_mod.OWNED_PLUGIN, "jobs": {
            _agent_spec().name: {"id": "owned-1", "name": _agent_spec().name},
        }}
        with self.assertRaises(cron_mod.CronError):
            cron_mod.ensure_normal_agent_job(
                backend, _agent_spec(), ownership=ownership, ownership_path=self.path)
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["foreign-9"])

    def test_existing_agent_without_ledger_is_not_adopted(self):
        backend = cron_mod.InMemoryCronBackend([{
            "id": "existing-1", "name": _agent_spec().name,
            "schedule": "*/5 * * * *", "prompt": _agent_spec().prompt,
            "deliver": "local", "workdir": "", "mode": "agent", "state": "active",
        }])
        with self.assertRaises(cron_mod.CronError):
            cron_mod.ensure_normal_agent_job(backend, _agent_spec())
        self.assertEqual([j["id"] for j in backend.list_jobs()], ["existing-1"])

    def test_inconclusive_readback_without_mode_fails(self):
        class _NoModeBackend(cron_mod.InMemoryCronBackend):
            def add_job(self, spec):
                job = super().add_job(spec)
                job.pop("mode", None)
                return job

        with self.assertRaises(cron_mod.CronError) as ctx:
            cron_mod.ensure_normal_agent_job(_NoModeBackend(), _agent_spec())
        self.assertIn("mode", str(ctx.exception))
        self.assertEqual(cron_mod.load_job_ownership(self.path)["jobs"], {})

    def test_inconclusive_readback_without_exact_id_fails(self):
        class _NoIdBackend(cron_mod.InMemoryCronBackend):
            def add_job(self, spec):
                job = super().add_job(spec)
                job["id"] = ""
                return job

        with self.assertRaises(cron_mod.CronError) as ctx:
            cron_mod.ensure_normal_agent_job(_NoIdBackend(), _agent_spec())
        self.assertIn("id", str(ctx.exception))

    def test_partial_text_readback_is_reported_honestly(self):
        class _NoPromptTextBackend(cron_mod.InMemoryCronBackend):
            def add_job(self, spec):
                job = super().add_job(spec)
                job.pop("prompt", None)
                job.pop("model", None)
                return job

        result = cron_mod.ensure_normal_agent_job(
            _NoPromptTextBackend(), _agent_spec(model="m"))
        self.assertIn("verification=partial", result.detail)
        self.assertIn("prompt", result.detail)
        self.assertIn("model", result.detail)
        self.assertNotIn("full", result.detail)


class ImportSideEffectTests(unittest.TestCase):
    def test_import_does_not_touch_the_cron_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "calls.jsonl"
            fake = bindir / "hermes"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['FAKE_CRON_LOG'], 'a') as fh:\n"
                "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            code = (
                "import importlib.util, sys, pathlib\n"
                f"root = pathlib.Path({str(ROOT)!r})\n"
                "spec = importlib.util.spec_from_file_location('cron_import_probe', root / 'cron.py')\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "sys.modules['cron_import_probe'] = mod\n"
                "spec.loader.exec_module(mod)\n"
            )
            env = {
                "PATH": str(bindir) + ":/usr/bin:/bin",
                "FAKE_CRON_LOG": str(log),
                "PYTHONPATH": str(ROOT),
            }
            subprocess.run([sys.executable, "-c", code], env=env, check=True,
                           capture_output=True, text=True)
            self.assertFalse(log.exists(), "importing cron.py must not run the scheduler CLI")
            if log.exists():
                self.assertEqual(log.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
