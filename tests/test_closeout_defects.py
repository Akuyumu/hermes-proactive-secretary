"""Focused regressions for the three remaining 0.5.0 closeout defects.

Synthetic HOME/HERMES_HOME/data only. No live profile, network, model, provider,
scheduler, message or installed plugin is touched.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = tempfile.mkdtemp(prefix="secretary-closeout-")
_ENV_KEYS = ("HOME", "HERMES_HOME", "HERMES_PROACTIVE_PORTAL_DATA", "TMPDIR")
_OLD_ENV: dict[str, str | None] = {}


def setUpModule():
    """Scope synthetic paths to this module instead of leaking them suite-wide."""
    for key in _ENV_KEYS:
        _OLD_ENV[key] = os.environ.get(key)
    os.environ["HOME"] = os.path.join(SCRATCH, "home")
    os.environ["HERMES_HOME"] = os.path.join(SCRATCH, "profile")
    os.environ["HERMES_PROACTIVE_PORTAL_DATA"] = os.path.join(SCRATCH, "data")
    os.environ["TMPDIR"] = SCRATCH
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["HERMES_HOME"]).mkdir(parents=True, exist_ok=True)


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    for key, value in _OLD_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import release_export  # noqa: E402
from proactive_pilot import modelhealth as M  # noqa: E402
from proactive_pilot import preparation as P  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T12:00:00+00:00")


def _load_package():
    name = "hermes_proactive_portal_closeout"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
conversation = importlib.import_module(f"{PKG.__name__}.portal.portal_conversation")
onboarding = importlib.import_module(f"{PKG.__name__}.portal.portal_onboarding")
models = importlib.import_module(f"{PKG.__name__}.portal.portal_models")
sc = importlib.import_module(f"{PKG.__name__}.portal.portal_cli")
cron_mod = importlib.import_module(f"{PKG.__name__}.cron")
status_mod = importlib.import_module(f"{PKG.__name__}.portal.portal_status")

ROUTES = {"primary_provider": "provider-a", "primary_model": "model-a",
          "fallback_provider": "provider-b", "fallback_model": "model-b"}


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class Ctx:
    def __init__(self, settings=None, *, strict_aux=False):
        self.settings = dict(settings or {})
        self.state = MemoryState()
        self.commands = {}
        self.hooks = {}
        self.strict_aux = strict_aux
        self.plugin_id = "hermes-proactive-portal"

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None,
                                fallback_policy=None):
        if not self.strict_aux and fallback_policy is not None:
            raise RuntimeError("unsupported")
        return None

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def _service(ctx):
    return onboarding.OnboardingService(ctx)


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class LicensePackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_archive_contains_the_actual_mit_license_bytes(self):
        out = self.root / "package.tar.gz"
        result = release_export.build_package(out, root=ROOT)
        self.assertEqual(result["file_count"], len(release_export._shipped_files(ROOT)))
        self.assertIn("LICENSE", result["files"] if "files" in result else
                      release_export.scan(ROOT)["files"])
        with tarfile.open(out) as archive:
            member = archive.extractfile("LICENSE")
            self.assertIsNotNone(member)
            self.assertEqual(member.read(), (ROOT / "LICENSE").read_bytes())
            names = archive.getnames()
        self.assertIn("LICENSE", names)
        self.assertFalse(any(name.startswith(".build-context") for name in names))
        self.assertFalse(any(name.startswith("tests/") for name in names))
        self.assertFalse(any(name.startswith("var/") for name in names))
        self.assertFalse(any(name.endswith(".pyc") for name in names))

    def test_doctor_rejects_an_extracted_package_without_license(self):
        out = self.root / "package.tar.gz"
        release_export.build_package(out, root=ROOT)
        install = self.root / "install"
        install.mkdir()
        with tarfile.open(out) as archive:
            archive.extractall(install)
        # The actual native host doctor is exercised by
        # test_main_final_paths/test_installed_lastmile. Here the package-local
        # checks and the fail-fast rule are the subject.
        fast_host = {"available": True, "ok": True, "reason": "covered elsewhere"}
        with mock.patch.object(release_export, "_native_host_doctor",
                               return_value=fast_host):
            self.assertTrue(release_export.doctor(install)["ok"])
        (install / "LICENSE").unlink()
        with mock.patch.object(release_export, "_native_host_doctor",
                               side_effect=AssertionError(
                                   "native doctor must not run for an incomplete package")):
            report = release_export.doctor(install)
        self.assertFalse(report["ok"])
        self.assertIn("LICENSE", report["missing"])
        self.assertTrue(any("LICENSE" in problem for problem in report["scan_problems"]))


class ChatStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self._old_data = os.environ["HERMES_PROACTIVE_PORTAL_DATA"]
        os.environ["HERMES_PROACTIVE_PORTAL_DATA"] = str(self.data)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        os.environ["HERMES_PROACTIVE_PORTAL_DATA"] = self._old_data

    def _handler(self, ctx, strict=True):
        if strict:
            models.register_configured_models(ctx)
        services = _service(ctx)
        conversation.register_conversation_surfaces(ctx, service=services)
        self.assertIn("portal", ctx.commands)
        return ctx.commands["portal"][0]

    def test_registered_handler_renders_real_profile_route_and_strict_state(self):
        ctx = Ctx({**ROUTES, "user_config": uc.validate_config(uc.default_config())},
                  strict_aux=True)
        handler = self._handler(ctx)
        text = handler("status")
        self.assertIn("provider-a", text)
        self.assertIn("model-a", text)
        self.assertIn("strict=active", text)
        self.assertNotIn("setup verified", text.lower())
        self.assertNotIn("localhost", text.lower())
        self.assertNotIn("http://", text.lower())

        unsupported = Ctx({"user_config": uc.validate_config(uc.default_config())})
        text = self._handler(unsupported, strict=False)("status")
        self.assertIn("strict=unsupported", text)

    def test_missing_config_is_honest_paused_and_none_owned(self):
        ctx = Ctx()
        # An empty ownership ledger needs no native CLI startup at all.
        with mock.patch.object(cron_mod, "HermesCronBackend",
                               side_effect=AssertionError("unexpected native probe")):
            handler = self._handler(ctx)
            text = handler("status")
            self.assertIn("configured=no", text)
            self.assertIn("delivery=not-configured", text)
            self.assertIn("0 owned (none recorded)", text)
            self.assertIn("active", text)
            handler("pause")
            paused = handler("status")
            self.assertIn("paused", paused)

    def test_owned_jobs_are_ids_only_and_health_incident_and_queue_render(self):
        ownership = {"jobs": {
            cron_mod.PILOT_JOB_NAME: {
                "id": "job-42", "name": cron_mod.PILOT_JOB_NAME,
                "created_at": T0.isoformat(), "schedule": "*/5 * * * *",
                "script": "proactive_portal_runner.py", "deliver": "local",
                "workdir": ""}}}
        cron_mod.save_job_ownership(self.data / "cron_job_ownership.json", ownership)
        store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        M.record_attempt(store, role="primary", outcome="failure",
                         failure_kind="timeout", now=T0)
        M.record_attempt(store, role="fallback", outcome="failure",
                         failure_kind="rate_limit", now=T0)
        rec = R.create(
            store, desired_outcome="Draft a short note",
            acceptance_criterion="One short note",
            provenance={"kind": "user_commitment", "source_ref": "conversation:c1",
                        "conversation_ref": "conversation:c1", "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:c1"],
            facts=[{"ref": "conversation:c1", "text": "Please draft a short note.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}],
            now=T0)
        claim = R.claim_due(store, now=T0)
        P.write_artifact(
            store, responsibility_id=rec["id"], claim_token=claim["claim_token"],
            generation=claim["generation"], source_version=claim["source_version"],
            title="Private title must not render", content="Draft",
            evidence_refs=["conversation:c1"], artifact_root=self.root / "artifacts",
            gate_fn=lambda: True, now=T0)

        ctx = Ctx({"user_config": uc.validate_config(uc.default_config())})
        backend = cron_mod.InMemoryCronBackend([{
            "id": "job-42", "name": cron_mod.PILOT_JOB_NAME, "state": "active",
            "next_run": "2026-10-20T12:15:00+00:00"}])
        with mock.patch.object(cron_mod, "HermesCronBackend",
                               lambda *args, **kwargs: backend):
            text = self._handler(ctx)("status")
        self.assertIn("1 owned", text)
        self.assertIn("job-42", text)
        self.assertIn("next=2026-10-20T12:15:00+00:00", text)
        self.assertNotIn("proactive-portal-pilot", text)
        self.assertIn("Health: open", text)
        self.assertIn("prepare queue=1 ready", text)
        self.assertIn(rec["id"], text)
        self.assertNotIn("Private title", text)
        self.assertNotIn("Draft", text)

    def _record_pilot_ownership(self):
        cron_mod.save_job_ownership(self.data / "cron_job_ownership.json", {"jobs": {
            cron_mod.PILOT_JOB_NAME: {
                "id": "job-42", "name": cron_mod.PILOT_JOB_NAME,
                "created_at": T0.isoformat(), "schedule": "*/15 * * * *",
                "script": cron_mod.PILOT_SCRIPT, "deliver": "local", "workdir": ""}}})

    def test_status_uses_inprocess_reader_instead_of_managed_cli(self):
        self._record_pilot_ownership()

        class StatusReader:
            timeout = 8
            def __init__(self):
                self.status_calls = 0
                self.list_calls = 0
            def list_jobs_for_status(self):
                self.status_calls += 1
                return [{"id": "job-42", "name": cron_mod.PILOT_JOB_NAME,
                         "state": "active",
                         "next_run": "2026-10-20T12:15:00+00:00"}]
            def list_jobs(self):
                self.list_calls += 1
                raise AssertionError("status must not spawn the managed hermes CLI")

        backend = StatusReader()
        result = status_mod._owned_jobs(backend, probe_empty=True)
        self.assertEqual(backend.status_calls, 1)
        self.assertEqual(backend.list_calls, 0)
        self.assertTrue(result["available"])
        self.assertEqual(result["state"], "owned")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["next_schedule"], "2026-10-20T12:15:00+00:00")

    def test_status_keeps_legacy_reader_for_injected_protocol_fakes(self):
        self._record_pilot_ownership()
        backend = cron_mod.InMemoryCronBackend([{
            "id": "job-42", "name": cron_mod.PILOT_JOB_NAME, "state": "active"}])
        result = status_mod._owned_jobs(backend, probe_empty=True)
        self.assertTrue(result["available"])
        self.assertEqual(result["count"], 1)
        self.assertFalse(hasattr(backend, "list_jobs_for_status"))


class OnboardingProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _service_for(self, payload, *, ctx=None, path=True):
        path_obj = _write_json(self.root / "user_config.json", payload) if path else None
        loaded = uc.load_user_config(path_obj)
        return onboarding.OnboardingService(
            ctx or Ctx(), config_resolver=lambda: loaded), loaded

    def test_missing_config_asks_for_timezone(self):
        svc = onboarding.OnboardingService(Ctx())
        info = svc.inspect()
        self.assertEqual(info["next_step"], "basics")
        self.assertFalse(any(key.startswith("setting:")
                             for key in info["state"]["decided"]))

    def test_factory_neutral_file_does_not_claim_user_choices(self):
        neutral = uc.default_config()
        neutral["advanced"] = {"onboarding_provenance": uc.neutral_init_marker(neutral)}
        svc, _loaded = self._service_for(neutral)
        info = svc.inspect()
        self.assertEqual(info["next_step"], "basics")
        self.assertEqual(info["state"]["settings"]["timezone"], "")
        self.assertNotIn("setting:daily_allowance", info["state"]["decided"])

    def test_legacy_loadedconfig_factory_shape_still_asks(self):
        loaded = uc.LoadedConfig(config=uc.validate_config(uc.default_config()),
                                 present=True, source="native")
        svc = onboarding.OnboardingService(
            Ctx(), config_resolver=lambda: loaded)
        info = svc.inspect()
        self.assertEqual(info["next_step"], "basics")
        self.assertNotIn("setting:daily_allowance", info["state"]["decided"])

    def test_partial_raw_file_seeds_only_present_fields(self):
        payload = {"schema": 1, "version": 1, "timezone": "Europe/Berlin"}
        svc, _loaded = self._service_for(payload)
        info = svc.inspect()
        self.assertEqual(info["state"]["settings"]["timezone"], "Europe/Berlin")
        self.assertIn("setting:timezone", info["state"]["decided"])
        self.assertEqual(info["next_step"], "destination")
        self.assertNotIn("setting:daily_allowance", info["state"]["decided"])

    def test_flat_native_only_changed_setting_is_known(self):
        payload = {"schema": 1, "version": 1}
        ctx = Ctx({"user_config": payload, "daily_normal_cap": 9})
        svc = onboarding.OnboardingService(ctx)
        info = svc.inspect()
        self.assertEqual(info["state"]["settings"]["daily_allowance"], 9)
        self.assertIn("setting:daily_allowance", info["state"]["decided"])
        self.assertEqual(info["next_step"], "basics")
        self.assertNotIn("setting:timezone", info["state"]["decided"])

    def test_legacy_explicit_utc_and_allowance_stay_known(self):
        payload = {"schema": 1, "version": 1, "timezone": "UTC",
                   "notification": {"daily_normal_cap": 5,
                                    "quiet_start_hour": 0, "quiet_end_hour": 8}}
        svc, _loaded = self._service_for(payload)
        info = svc.inspect()
        self.assertEqual(info["state"]["settings"]["timezone"], "UTC")
        self.assertIn("setting:timezone", info["state"]["decided"])
        self.assertIn("setting:daily_allowance", info["state"]["decided"])
        self.assertIn("setting:quiet_start_hour", info["state"]["decided"])
        self.assertEqual(info["next_step"], "destination")

    def test_accounts_only_config_still_asks_timezone_and_keeps_grant_false(self):
        payload = {"schema": 1, "version": 1, "mail": {"accounts": ["work"]}}
        svc, _loaded = self._service_for(payload)
        info = svc.inspect()
        self.assertEqual(info["state"]["source_scope"]["mail_accounts"], ["work"])
        self.assertIn("scope:mail_accounts", info["state"]["decided"])
        self.assertEqual(info["state"]["settings"]["timezone"], "")
        self.assertFalse(info["state"]["grants"]["mail"])
        self.assertEqual(info["next_step"], "basics")
        self.assertEqual(info["state"]["proposals"], [])

    def test_nondefault_choices_and_operator_routes_are_preserved(self):
        payload = {"schema": 1, "version": 1, "timezone": "Europe/Berlin",
                   "notification": {"destination": "local", "daily_normal_cap": 7,
                                    "quiet_start_hour": 22, "quiet_end_hour": 7},
                   "sources": {"mail": True},
                   "mail": {"accounts": ["work"]}}
        ctx = Ctx({**ROUTES, "user_config": payload})
        svc = onboarding.OnboardingService(ctx)
        info = svc.inspect()
        self.assertEqual(info["next_step"], "autonomy")
        self.assertEqual(info["state"]["settings"]["primary_model"], "model-a")
        self.assertTrue(info["state"]["grants"]["mail"])
        self.assertEqual(info["state"]["source_scope"]["mail_accounts"], ["work"])
        self.assertEqual(info["model_routes"]["primary"]["provider"], "provider-a")

    def test_onboarding_write_marks_only_chosen_fields(self):
        neutral = uc.validate_config(uc.default_config())
        neutral["advanced"] = {"onboarding_provenance": uc.neutral_init_marker(neutral)}
        ctx = Ctx({"user_config": neutral})
        svc = onboarding.OnboardingService(ctx)
        svc.update(settings={"timezone": "UTC", "daily_allowance": 5},
                   destination="local")
        result = svc.confirm(user_confirmed=True, apply_settings=True)
        self.assertEqual(result["status"], "pending_verification")
        saved = ctx.settings["user_config"]
        marker = saved["advanced"]["onboarding_provenance"]
        self.assertTrue(marker["neutral_init"])
        self.assertIn("timezone", marker["known_fields"])
        self.assertIn("notification.daily_normal_cap", marker["known_fields"])
        loaded = uc.load_user_config(native=ctx)
        self.assertIn("timezone", loaded.provenance["fields"])
        self.assertIn("notification.daily_normal_cap", loaded.provenance["fields"])
        self.assertNotIn("notification.quiet_start_hour",
                         loaded.provenance["fields"])


if __name__ == "__main__":
    unittest.main()
