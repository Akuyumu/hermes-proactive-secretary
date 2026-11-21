"""Offline coverage for the real native-cron surface and runner ownership.

A fake ``hermes`` executable is placed on ``PATH``; it records every argv and
emits a realistic ``cron list`` text table. No network, no live scheduler, no
real cron job is created.
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


def _load_plugin_package():
    name = "hermes_proactive_secretary_cron_test"
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

SPEC = cron_mod.CronJobSpec(
    name="proactive-secretary-pilot",
    schedule="*/15 * * * *",
    script=cron_mod.PILOT_SCRIPT,
    deliver="origin",
    workdir="/plugins/hermes-proactive-secretary",
)

SAMPLE_LIST = """\
  job-7 [active]
    Name:      proactive-secretary-pilot
    Schedule:  */15 * * * *
    Next run:  2026-10-05T12:15:00+08:00
    Deliver:   origin
    Script:    proactive_secretary_runner.py
    Mode:      no-agent (script stdout delivered directly)
    Workdir:   /plugins/hermes-proactive-secretary
    Last run:  2026-10-05T12:00:00+08:00  ok
  job-9 [paused]
    Name:      proactive-secretary-reminders
    Schedule:  * * * * *
    Next run:
    Deliver:   local
    Script:    proactive_secretary_reminder_runner.py
    Mode:      no-agent (script stdout delivered directly)
    Workdir:   /plugins/hermes-proactive-secretary
    Last run:    never
"""

FAKE_HERMES = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_HERMES_STATE"])
log = Path(os.environ["FAKE_HERMES_LOG"])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")

jobs = json.loads(state.read_text()) if state.exists() else []
args = sys.argv[1:]
if len(args) < 2 or args[0] != "cron":
    sys.exit(1)
verb, rest = args[1], args[2:]

if verb == "--help":
    print("Usage: hermes cron ...")
    sys.exit(0)

if verb == "list":
    for j in jobs:
        print(f"  {j['id']} [{j.get('state', 'active')}]")
        print(f"    Name:      {j.get('name', '')}")
        print(f"    Schedule:  {j.get('schedule', '')}")
        print(f"    Next run:  {j.get('next_run', '')}")
        print(f"    Deliver:   {j.get('deliver', '')}")
        print(f"    Script:    {j.get('script', '')}")
        print("    Mode:      no-agent (script stdout delivered directly)")
        print(f"    Workdir:   {j.get('workdir', '')}")
        print(f"    Last run:  {j.get('last_run', '')}  ok")
    sys.exit(0)

if verb == "create":
    def opt(flag, default=""):
        return rest[rest.index(flag) + 1] if flag in rest else default
    job = {
        "id": f"job-{len(jobs) + 1}", "name": opt("--name"),
        "schedule": rest[0] if rest else "", "script": opt("--script"),
        "deliver": opt("--deliver"), "workdir": opt("--workdir"),
        "state": "active", "last_run": "",
    }
    jobs.append(job)
    state.write_text(json.dumps(jobs))
    print(f"Created job {job['id']}")
    sys.exit(0)

if verb == "remove":
    job_id = rest[0] if rest else ""
    jobs = [j for j in jobs if j["id"] != job_id]
    state.write_text(json.dumps(jobs))
    print(f"Removed job {job_id}")
    sys.exit(0)

sys.exit(1)
"""


class FakeHermesMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        self.bindir = root / "bin"
        self.bindir.mkdir()
        self.fake = self.bindir / "hermes"
        self.fake.write_text(FAKE_HERMES, encoding="utf-8")
        self.fake.chmod(0o755)
        self.state = root / "jobs.json"
        self.log = root / "argv.jsonl"
        managed = ("PATH", "FAKE_HERMES_STATE", "FAKE_HERMES_LOG", "HERMES_BIN",
                   "HERMES_CRON_CREATE_VERB", "HERMES_CRON_LIST_VERB", "HERMES_CRON_REMOVE_VERB")
        self._old = {k: os.environ.get(k) for k in managed}
        for key in ("HERMES_BIN", "HERMES_CRON_CREATE_VERB",
                    "HERMES_CRON_LIST_VERB", "HERMES_CRON_REMOVE_VERB"):
            os.environ.pop(key, None)
        os.environ["PATH"] = str(self.bindir) + os.pathsep + self._old["PATH"]
        os.environ["FAKE_HERMES_STATE"] = str(self.state)
        os.environ["FAKE_HERMES_LOG"] = str(self.log)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]


class CreateArgvTests(FakeHermesMixin, unittest.TestCase):
    def test_create_uses_real_positional_shape(self):
        backend = cron_mod.HermesCronBackend()
        self.assertTrue(backend.is_available())
        backend.add_job(SPEC)
        argv = next(c for c in self.calls() if c[:2] == ["cron", "create"])
        self.assertEqual(argv[:2], ["cron", "create"])
        self.assertEqual(argv[2], "*/15 * * * *")
        for flag in ("--schedule", "--command", "--json"):
            self.assertNotIn(flag, argv)
        self.assertIn("--no-agent", argv)
        self.assertEqual(argv[argv.index("--name") + 1], SPEC.name)
        self.assertEqual(argv[argv.index("--script") + 1], cron_mod.PILOT_SCRIPT)
        self.assertEqual(argv[argv.index("--deliver") + 1], "origin")
        self.assertEqual(argv[argv.index("--workdir") + 1], SPEC.workdir)

    def test_create_without_workdir_omits_the_flag(self):
        backend = cron_mod.HermesCronBackend()
        backend.add_job(cron_mod.CronJobSpec(name="j", schedule="* * * * *", script="s.py"))
        argv = self.calls()[0]
        self.assertNotIn("--workdir", argv)
        self.assertEqual(argv[argv.index("--deliver") + 1], "local")

    def test_default_verbs_are_the_real_shape(self):
        backend = cron_mod.HermesCronBackend()
        self.assertEqual((backend.create_verb, backend.list_verb, backend.remove_verb),
                         ("create", "list", "remove"))

    def test_verbs_can_be_overridden_by_env(self):
        os.environ["HERMES_CRON_CREATE_VERB"] = "new"
        self.addCleanup(lambda: os.environ.pop("HERMES_CRON_CREATE_VERB", None))
        backend = cron_mod.HermesCronBackend()
        self.assertEqual(backend.create_verb, "new")


class ListParsingTests(FakeHermesMixin, unittest.TestCase):
    def test_list_text_blocks_parse_to_job_dicts(self):
        jobs = cron_mod.parse_cron_list(SAMPLE_LIST)
        self.assertEqual(len(jobs), 2)
        first, second = jobs
        self.assertEqual(first["id"], "job-7")
        self.assertEqual(first["state"], "active")
        self.assertEqual(first["name"], "proactive-secretary-pilot")
        self.assertEqual(first["schedule"], "*/15 * * * *")
        self.assertEqual(first["deliver"], "origin")
        self.assertEqual(first["script"], cron_mod.PILOT_SCRIPT)
        self.assertEqual(first["mode"], "no-agent (script stdout delivered directly)")
        self.assertEqual(first["workdir"], "/plugins/hermes-proactive-secretary")
        self.assertIn("2026-10-05T12:00:00", first["last_run"])
        self.assertEqual(second["id"], "job-9")
        self.assertEqual(second["state"], "paused")

    def test_backend_lists_from_fake_text_output(self):
        self.state.write_text(json.dumps([{
            "id": "job-3", "name": SPEC.name, "schedule": SPEC.schedule,
            "script": SPEC.script, "deliver": SPEC.deliver, "workdir": SPEC.workdir,
            "state": "active", "last_run": "2026-10-05T12:00:00+08:00",
        }]), encoding="utf-8")
        backend = cron_mod.HermesCronBackend()
        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], SPEC.name)
        self.assertEqual(self.calls()[0], ["cron", "list", "--all"])


class EnsureRemoveTests(FakeHermesMixin, unittest.TestCase):
    def test_ensure_then_remove_resolves_name_to_id_and_verifies(self):
        backend = cron_mod.HermesCronBackend()
        self.assertEqual(cron_mod.ensure_job(backend, SPEC).action, "created")
        self.assertEqual(cron_mod.ensure_job(backend, SPEC).action, "already-present")
        jobs = backend.list_jobs()
        self.assertEqual(len(jobs), 1)
        job_id = jobs[0]["id"]
        result = cron_mod.remove_job(backend, SPEC.name)
        self.assertEqual(result.action, "removed")
        self.assertEqual(backend.list_jobs(), [])
        removes = [c for c in self.calls() if c[:2] == ["cron", "remove"]]
        self.assertEqual(removes[0][2], job_id)

    def test_remove_missing_is_idempotent(self):
        backend = cron_mod.HermesCronBackend()
        self.assertEqual(cron_mod.remove_job(backend, SPEC.name).action, "missing")

    def test_remove_still_present_raises(self):
        backend = cron_mod.InMemoryCronBackend()
        cron_mod.ensure_job(backend, SPEC)
        backend.fail_remove = True
        with self.assertRaises(cron_mod.CronError):
            cron_mod.remove_job(backend, SPEC.name)


class RunnerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        self.scripts = root / "scripts"
        self.ownership = root / "runner_ownership.json"
        self.addCleanup(self._tmp.cleanup)

    def _install(self, **kwargs):
        return runner_install.install_runners(
            plugin_root=ROOT, template_root=ROOT / "runners",
            scripts_dir=self.scripts, ownership_path=self.ownership, **kwargs,
        )

    def test_install_records_ownership_and_is_idempotent(self):
        results = self._install(now="2026-10-05T00:00:00+00:00")
        self.assertEqual(len(results), len(runner_install.RUNNER_FILENAMES))
        for name in runner_install.RUNNER_FILENAMES:
            target = self.scripts / name
            self.assertTrue(target.is_file(), name)
            text = target.read_text(encoding="utf-8")
            self.assertNotIn(runner_install.PLACEHOLDER, text)
            self.assertIn(str(ROOT), text)
        self.assertEqual(set(Path(p).name for p in runner_install.owned_files(self.ownership)),
                         set(runner_install.RUNNER_FILENAMES))
        again = self._install()
        self.assertTrue(all(r["action"] == "unchanged" for r in again))

    def test_uninstall_removes_only_owned_files_and_reports(self):
        self._install()
        stray = self.scripts / "unrelated.py"
        stray.write_text("keep me", encoding="utf-8")
        results = runner_install.uninstall_runners(
            ownership_path=self.ownership, scripts_dir=self.scripts)
        actions = {Path(r["file"]).name: r["action"] for r in results}
        for name in runner_install.RUNNER_FILENAMES:
            self.assertEqual(actions[name], "removed", name)
            self.assertFalse((self.scripts / name).exists())
        self.assertTrue(stray.exists(), "an unowned file must never be removed")
        self.assertEqual(runner_install.owned_files(self.ownership), [])

    def test_uninstall_refuses_to_remove_modified_file(self):
        self._install()
        target = self.scripts / cron_mod.PILOT_SCRIPT
        target.write_text("user modified\n", encoding="utf-8")
        results = runner_install.uninstall_runners(
            ownership_path=self.ownership, scripts_dir=self.scripts)
        actions = {Path(r["file"]).name: r["action"] for r in results}
        self.assertEqual(actions[cron_mod.PILOT_SCRIPT], "kept")
        self.assertTrue(target.exists())

    def test_unrelated_preexisting_file_is_backed_up_not_clobbered(self):
        self.scripts.mkdir(parents=True, exist_ok=True)
        target = self.scripts / cron_mod.PILOT_SCRIPT
        target.write_text("someone else's runner\n", encoding="utf-8")
        self._install()
        backups = list(self.scripts.glob(cron_mod.PILOT_SCRIPT + runner_install.BACKUP_SUFFIX + "*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "someone else's runner\n")
        self.assertNotIn(runner_install.PLACEHOLDER, target.read_text(encoding="utf-8"))


class DeliverDefaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(self._tmp.name)
        (root / "home").mkdir(parents=True, exist_ok=True)
        self._old = {k: os.environ.get(k) for k in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(root / "data")
        os.environ["HERMES_HOME"] = str(root / "home")
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = lambda _fixture: {
            "self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0}, "dry_run_rc": 0,
        }
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        sc._run_offline_checks = self._orig_checks
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _ns(self, **overrides):
        base = dict(force=False, no_cron=False, json=True, fixture="healthy_empty",
                    deliver=None, data_dir=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _setup(self, ns):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(ns, backend=cron_mod.InMemoryCronBackend())
        return rc, json.loads(buf.getvalue())

    def test_default_deliver_is_local_with_explicit_warning(self):
        rc, result = self._setup(self._ns())
        self.assertEqual(rc, 0)
        self.assertEqual(result["deliver"], "local")
        self.assertTrue(any("local" in w and "not delivered" in w for w in result["warnings"]))
        self.assertTrue(all(j["deliver"] == "local" for j in result["jobs"]))

    def test_user_supplied_deliver_has_no_warning(self):
        rc, result = self._setup(self._ns(deliver="platform:chat-42"))
        self.assertEqual(rc, 0)
        self.assertEqual(result["deliver"], "platform:chat-42")
        self.assertEqual(result["warnings"], [])
        self.assertTrue(all(j["deliver"] == "platform:chat-42" for j in result["jobs"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
