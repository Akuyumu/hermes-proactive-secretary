"""Setup can adopt a new portal prompt without changing any other job shape."""
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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_prompt_refresh"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")

PORTAL = cron_mod.PORTAL_JOB_NAME
OLD_PROMPT = "Old owner-specific portal prompt."


def _ok_checks(_fixture: str) -> dict:
    return {"self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
            "dry_run_rc": 0}


class PortalPromptRefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "home").mkdir()
        self.addCleanup(self._tmp.cleanup)
        self._saved = {key: os.environ.get(key)
                       for key in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(self.root / "data")
        os.environ["HERMES_HOME"] = str(self.root / "home")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _setup(self, *, schedule: str = "*/5 * * * *",
               context_from=("self",)):
        job = {
            "id": "job-portal-1",
            "name": PORTAL,
            "schedule": schedule,
            "prompt": OLD_PROMPT,
            "script": "",
            "deliver": "local",
            "workdir": "",
            "mode": "agent",
            "state": "active",
            "last_run": "",
            "model": "model-x",
            "provider": "provider-x",
            "reasoning": "low",
            "skills": ["alpha"],
            "context_from": list(context_from),
        }
        spec = cron_mod.NormalAgentJobSpec(
            name=PORTAL, schedule=schedule, prompt=OLD_PROMPT, deliver="local",
            context_from=list(context_from), model="model-x",
            provider="provider-x", reasoning="low", skills=["alpha"])
        ownership: dict = {}
        cron_mod.record_owned_job(ownership, spec, job)
        cron_mod.save_job_ownership(paths.job_ownership_path(), ownership)
        return cron_mod.InMemoryCronBackend(jobs=[job]), ownership

    def _ns(self, **overrides):
        base = dict(force=False, no_cron=False, json=True,
                    fixture="healthy_empty", deliver=None, data_dir=None,
                    import_from=None, import_dry_run=False,
                    with_portal=False, refresh_portal_prompt=True)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, ns, backend):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(ns, backend=backend)
        raw = buf.getvalue()
        payload = json.loads(raw) if raw.strip() else {}
        return rc, payload

    def test_refresh_updates_only_the_prompt(self):
        backend, ownership = self._setup()
        before = backend.list_jobs()[0]
        rc, payload = self._run(self._ns(), backend)
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "refreshed")
        self.assertEqual(payload["job_id"], "job-portal-1")
        self.assertEqual(payload["old_version"], "unversioned")
        self.assertEqual(payload["new_version"], portal.PROMPT_VERSION)
        after = backend.list_jobs()[0]
        self.assertEqual(after["prompt"], portal.PROMPT)
        for key in ("id", "name", "schedule", "context_from", "deliver",
                    "mode", "state", "model", "provider", "reasoning",
                    "skills", "workdir", "script"):
            self.assertEqual(after.get(key), before.get(key), key)
        saved = cron_mod.load_job_ownership(paths.job_ownership_path())
        record = saved["jobs"][PORTAL]
        self.assertEqual(record["prompt"], portal.PROMPT)
        self.assertEqual(record["id"], "job-portal-1")
        self.assertEqual(record["schedule"], "*/5 * * * *")
        self.assertEqual(record["context_from"], ["self"])

    def test_cadence_mismatch_refuses_without_a_write(self):
        backend, _ownership = self._setup()
        job = backend.jobs[0]
        job["schedule"] = "0 */3 * * *"
        rc, payload = self._run(self._ns(), backend)
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "mismatch")
        self.assertIn("cadence", payload["detail"])
        self.assertEqual(backend.list_jobs()[0]["prompt"], OLD_PROMPT)
        saved = cron_mod.load_job_ownership(paths.job_ownership_path())
        self.assertEqual(saved["jobs"][PORTAL]["prompt"], OLD_PROMPT)

    def test_context_mismatch_refuses_without_a_write(self):
        backend, _ownership = self._setup()
        backend.jobs[0]["context_from"] = ["other"]
        rc, payload = self._run(self._ns(), backend)
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "mismatch")
        self.assertIn("context", payload["detail"])
        self.assertEqual(backend.list_jobs()[0]["prompt"], OLD_PROMPT)

    def test_without_the_flag_the_prompt_is_untouched(self):
        backend, _ownership = self._setup()
        original = sc._run_offline_checks
        sc._run_offline_checks = _ok_checks
        self.addCleanup(setattr, sc, "_run_offline_checks", original)
        rc, _payload = self._run(
            self._ns(no_cron=True, refresh_portal_prompt=False), backend)
        self.assertEqual(rc, 0)
        self.assertEqual(backend.list_jobs()[0]["prompt"], OLD_PROMPT)
        saved = cron_mod.load_job_ownership(paths.job_ownership_path())
        self.assertEqual(saved["jobs"][PORTAL]["prompt"], OLD_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
