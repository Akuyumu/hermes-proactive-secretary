"""Focused production-seam coverage for the final runtime repair (offline fakes).

Proves the four seams through the ACTUAL production surfaces rather than detached
helpers:

1. deterministic registered ``hermes portal reminders`` handler, native ctx
   threading and exact calendar-only event scope;
2. neutral no-config production entry (no implicit legacy private sources) and
   invalid-config fail-closed before any collector/model/state;
3. setup/doctor/status share one authoritative native-first config + delivery
   destination, with invalid config a critical doctor failure and zero
   jobs/runners;
4. fail-closed subprocess bridges: exactly one invocation, no duplicate fallback,
   stderr forwarded separately and only the alert line on stdout.

No network, model, live source, install, or scheduler is touched.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import collectors  # noqa: E402
from proactive_pilot import main as main_mod  # noqa: E402
from proactive_pilot import reminders as R  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402


def _load_plugin_package(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package("hermes_proactive_portal_seams_test")
sc = importlib.import_module(f"{_PKG.__name__}.portal.portal_cli")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
runner_install = importlib.import_module(f"{_PKG.__name__}.runner_install")


class FakeCtx:
    """Duck-typed native context; only the supported get_config accessor."""

    def __init__(self, settings=None):
        self._settings = settings or {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def _load_runner(filename: str):
    module_name = "seam_runner_" + filename.replace(".py", "")
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "runners" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


PILOT_RUNNER = _load_runner("proactive_portal_runner.py")
REMINDER_RUNNER = _load_runner("proactive_portal_reminder_runner.py")


def _ok_checks(_fixture):
    return {"self_test_rc": 0, "self_test": {"total": 1, "passed": 1, "failed": 0},
             "dry_run_rc": 0}


class NativeScheduledBootstrapSeam(unittest.TestCase):
    def _run_pipeline(self, *, enabled=True, paused=False, block=True):
        from proactive_pilot.native_bootstrap import NativeBootstrapError
        config = {"schema": 1, "version": 1, "retrieval": {
            "firecrawl_enabled": enabled, "firecrawl_consent": True,
            "server": "firecrawl", "tool": "firecrawl_scrape"}}
        ctx = FakeCtx({"user_config": config})
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            policy = root / "policy.json"
            policy.write_text("{}", encoding="utf-8")
            prefs = root / "preferences.json"
            prefs.write_text(json.dumps({"schema": 1, "version": 1, "paused": {"active": paused}}), encoding="utf-8")
            with mock.patch.object(paths, "policy_path", return_value=policy), \
                 mock.patch.object(paths, "preferences_path", return_value=prefs), \
                 mock.patch.object(paths, "state_dir", return_value=root / "state"), \
                 mock.patch.object(sc, "get_native_context", return_value=ctx), \
                 mock.patch("proactive_pilot.native_bootstrap.require_native",
                            side_effect=NativeBootstrapError("effective_config_empty") if block else None) as boot, \
                 mock.patch.object(main_mod, "load_context_snapshot", side_effect=RuntimeError("collection-boundary")) as collect, \
                 contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    rc = sc.pipeline_command(argparse.Namespace(check=False, fixture=None))
                except RuntimeError as exc:
                    self.assertEqual(str(exc), "collection-boundary")
                    rc = "collection-boundary"
            return rc, boot.call_args_list, collect.called, out.getvalue(), err.getvalue(), ctx

    def test_plain_scheduled_pipeline_bootstraps_native_before_collection(self):
        rc, calls, collected, stdout, stderr, ctx = self._run_pipeline()
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [mock.call(ctx, "firecrawl")])
        self.assertFalse(collected)
        self.assertEqual(stdout.strip(), "")
        self.assertIn("effective_config_empty", stderr)

    def test_successful_bootstrap_precedes_collection_on_same_context(self):
        rc, calls, collected, _, _, ctx = self._run_pipeline(block=False)
        self.assertEqual(rc, "collection-boundary")
        self.assertTrue(collected)
        self.assertEqual(calls, [mock.call(ctx, "firecrawl")])

    def test_pause_and_disabled_optin_do_not_bootstrap(self):
        rc, calls, collected, _, _, _ = self._run_pipeline(paused=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertFalse(collected)
        rc, calls, collected, _, _, _ = self._run_pipeline(enabled=False)
        self.assertEqual(rc, "collection-boundary")
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# 1. deterministic reminders: native config, calendar-only scope, handler
# --------------------------------------------------------------------------- #


class ReminderHandlerSeam(unittest.TestCase):
    def _policy_file(self, tmp, config=None):
        policy_path = Path(tmp) / "policy.json"
        policy_path.write_text(json.dumps({"timezone": "Asia/Singapore"}), encoding="utf-8")
        if config is not None:
            (Path(tmp) / "user_config.json").write_text(json.dumps(config), encoding="utf-8")
        return policy_path

    def test_registered_reminders_handler_exists_and_parses(self):
        parser = argparse.ArgumentParser(prog="hermes portal")
        sc.register_cli(parser)
        ns = parser.parse_args(["reminders", "dispatch"])
        self.assertEqual(ns.portal_cli_command, "reminders")
        self.assertIn("reminders", sc._DISPATCH)
        parser2 = argparse.ArgumentParser(prog="hermes portal")
        sc.register_cli(parser2)
        ns = parser2.parse_args(["reminders", "list"])
        self.assertEqual(ns.reminder_args, ["list"])

    def test_native_setting_is_authoritative_for_reminders(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            policy_path = self._policy_file(tmp, {"schema": 1, "version": 1,
                                                  "timezone": "America/New_York"})
            ctx = FakeCtx({"user_config": {"schema": 1, "version": 1,
                                           "timezone": "Europe/London"}})
            policy, aliases, error = R.load_effective_policy(policy_path, native=ctx)
            self.assertEqual(error, "")
            self.assertEqual(policy["timezone"], "Europe/London")
            self.assertEqual(aliases, [])

    def test_calendar_scope_never_unions_mail_accounts(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            config = {"schema": 1, "version": 1, "timezone": "UTC",
                      "sources": {"calendar": True},
                      "calendar": {"accounts": ["cal-work"], "calendars": ["primary"]},
                      "mail": {"accounts": ["mail-only"]}}
            policy_path = self._policy_file(tmp, config)
            policy, aliases, error = R.load_effective_policy(policy_path)
            self.assertEqual(error, "")
            self.assertEqual(aliases, ["cal-work"])
            self.assertNotIn("mail-only", aliases)
            getter = R.PortalEventGetter(aliases=aliases, policy=policy)
            self.assertTrue(getter.event_reads_allowed)

    def test_disabled_calendar_makes_no_event_get_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            config = {"schema": 1, "version": 1, "timezone": "UTC",
                      "sources": {"calendar": False},
                      "calendar": {"accounts": ["work"], "calendars": ["primary"]}}
            policy_path = self._policy_file(tmp, config)
            policy, aliases, _ = R.load_effective_policy(policy_path)
            getter = R.PortalEventGetter(aliases=aliases, policy=policy)
            self.assertFalse(getter.event_reads_allowed)
            now = R.parse_aware("2026-10-05T09:00:00+00:00")
            reg = R.empty_registry()
            reg["reminders"]["r1"] = {
                "id": "r1", "kind": "event", "status": "pending", "text": "synthetic",
                "provenance": "explicit user request", "alias": "work",
                "calendar_id": "primary", "event_id": "e1", "lead_minutes": 10,
                "late_grace_minutes": 10, "event_due": "2026-10-05T10:00:00+00:00",
                "trigger_at": "2026-10-05T09:50:00+00:00", "last_check_at": "",
                "event_version": "v1", "attempts": 0,
            }
            with mock.patch.object(getter, "get",
                                   side_effect=AssertionError("no event get may run")):
                plan = R.plan_dispatch(reg, now=now,
                                       prefs={"paused": {"active": False}}, getter=getter)
            self.assertEqual(plan.checked, 0)
            self.assertEqual(reg["reminders"]["r1"]["status"], "pending")

    def test_empty_explicit_accounts_make_no_event_get_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            config = {"schema": 1, "version": 1, "timezone": "UTC",
                      "calendar": {"accounts": [], "calendars": []}}
            policy_path = self._policy_file(tmp, config)
            policy, aliases, _ = R.load_effective_policy(policy_path)
            self.assertEqual(aliases, [])
            getter = R.PortalEventGetter(aliases=aliases, policy=policy)
            self.assertFalse(getter.event_reads_allowed)
            self.assertFalse(getter.get("any", "any", "any")["ok"])

    def test_native_or_file_config_failure_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            policy_path = self._policy_file(tmp, {"schema": 1, "version": 1,
                                                  "calendar": {"token": "x"}})
            policy, aliases, error = R.load_effective_policy(policy_path)
            self.assertTrue(error)
            self.assertIsNone(aliases)
            # A malformed NATIVE setting also fails closed, never falls back.
            policy_path2 = self._policy_file(tmp, {"schema": 1, "version": 1})
            _p, a2, e2 = R.load_effective_policy(policy_path2, native=FakeCtx(
                {"user_config": "{not json"}))
            self.assertTrue(e2)
            self.assertIsNone(a2)

    def test_exact_time_reminder_works_with_require_config_neutral(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            policy_path = self._policy_file(tmp)
            policy, aliases, error = R.load_effective_policy(policy_path, require_config=True)
            self.assertEqual(error, "")
            self.assertEqual(aliases, [])
            self.assertFalse(policy["calendar_enabled"])
            self.assertFalse(R.PortalEventGetter(
                aliases=aliases, policy=policy).event_reads_allowed)
            now = R.parse_aware("2026-10-05T09:00:00+00:00")
            reg = R.empty_registry()
            R.add_time(reg, text="Take meds", due="2026-10-05T09:30:00+00:00",
                       provenance="explicit user request", now=now)
            plan = R.plan_dispatch(reg, now=R.parse_aware("2026-10-05T09:30:00+00:00"),
                                   prefs={"paused": {"active": False}},
                                   getter=R.PortalEventGetter(aliases=[], policy=policy))
            self.assertEqual(plan.messages, ["Take meds"])


# --------------------------------------------------------------------------- #
# 2. neutral no-config production entry + invalid-config fail-closed
# --------------------------------------------------------------------------- #


class ProductionEntrySeam(unittest.TestCase):
    def _write_env(self, root):
        (root / "prefs.json").write_text(json.dumps({
            "schema": 1, "version": 0, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": False}}), encoding="utf-8")

    def _args(self, root, policy_path, prefs_path):
        args = main_mod.build_parser().parse_args([
            "--state-dir", str(root / "state"),
            "--policy", str(policy_path),
            "--preferences", str(prefs_path),
            "--run",
        ])
        args.require_config = True
        args.native_ctx = None
        return args

    def test_missing_config_production_applies_neutral_no_private_sources(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(dict(DEFAULT_POLICY)), encoding="utf-8")
            prefs_path = root / "prefs.json"
            self._write_env(root)
            captured = {}

            def _fake_default_sources(policy, clock, budget=None):
                captured["policy"] = dict(policy)
                return []

            with mock.patch.object(main_mod, "default_sources", _fake_default_sources):
                rc = main_mod.run_once(
                    self._args(root, policy_path, prefs_path),
                    evaluator=FakeEvaluator(EvaluatorResult("silence")))
            self.assertEqual(rc, 0)
            policy = captured["policy"]
            self.assertFalse(policy["mail_enabled"])
            self.assertFalse(policy["calendar_enabled"])
            self.assertFalse(policy["tasks_enabled"])
            self.assertFalse(policy["feeds_enabled"])
            self.assertFalse(policy["obligations_enabled"])
            self.assertFalse(policy["gws_calendar_enabled"])

    def test_neutral_default_config_reports_no_explicit_scope(self):
        neutral = uc.default_config()
        merged = uc.apply_user_config(json.loads(json.dumps(DEFAULT_POLICY)), neutral)
        self.assertEqual(collectors.configured_calendar_accounts(merged), [])
        self.assertEqual(collectors.configured_mail_accounts(merged), ())
        self.assertFalse(collectors.source_enabled(merged, "calendar_enabled"))
        self.assertFalse(collectors.source_enabled(merged, "mail_enabled"))

    def test_invalid_config_fails_closed_before_collectors(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(dict(DEFAULT_POLICY)), encoding="utf-8")
            prefs_path = root / "prefs.json"
            self._write_env(root)
            (root / "user_config.json").write_text(json.dumps(
                {"schema": 1, "version": 1, "mail": {"token": "x"}}), encoding="utf-8")
            called = {"sources": 0}

            def _spy_default_sources(policy, clock, budget=None):
                called["sources"] += 1
                return []

            with mock.patch.object(main_mod, "default_sources", _spy_default_sources):
                rc = main_mod.run_once(self._args(root, policy_path, prefs_path),
                                       evaluator=FakeEvaluator(EvaluatorResult("silence")))
            self.assertEqual(rc, 1)
            self.assertEqual(called["sources"], 0)


# --------------------------------------------------------------------------- #
# 3. setup / doctor / status: one authoritative config + destination
# --------------------------------------------------------------------------- #


class _DataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        self._saved = {k: os.environ.get(k) for k in (paths.DATA_ENV, "HERMES_HOME")}
        os.environ[paths.DATA_ENV] = str(self.root / "data")
        os.environ["HERMES_HOME"] = str(self.root / "home")
        self._orig_checks = sc._run_offline_checks
        sc._run_offline_checks = _ok_checks
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        sc._run_offline_checks = self._orig_checks
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _setup_ns(self, **overrides):
        base = dict(force=False, no_cron=False, json=True, fixture="healthy_empty",
                    deliver=None, data_dir=None, import_from=None, import_dry_run=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _write_invalid_config(self):
        paths.data_root().mkdir(parents=True, exist_ok=True)
        paths.user_config_path().write_text(json.dumps(
            {"schema": 1, "version": 1, "notification": {"token": "x"}}), encoding="utf-8")


class SetupConfigConsistency(_DataDirMixin):
    def test_invalid_config_creates_zero_jobs_runners_and_skips_checks(self):
        self._write_invalid_config()
        observed = {"checks": 0}

        def _spy_checks(_fixture):
            observed["checks"] += 1
            return _ok_checks(_fixture)

        sc._run_offline_checks = _spy_checks
        backend = cron_mod.InMemoryCronBackend()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(self._setup_ns(), backend=backend)
        result = json.loads(buf.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(observed["checks"], 0, "invalid config must fail before any runner/cron")
        self.assertEqual(backend.list_jobs(), [])
        self.assertEqual(runner_install.owned_files(paths.runner_ownership_path()), [])

    def test_configured_destination_used_when_deliver_absent(self):
        paths.data_root().mkdir(parents=True, exist_ok=True)
        paths.user_config_path().write_text(json.dumps({
            "schema": 1, "version": 1, "timezone": "UTC",
            "notification": {"destination": "origin"}}), encoding="utf-8")
        backend = cron_mod.InMemoryCronBackend()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.setup_command(self._setup_ns(), backend=backend)
        result = json.loads(buf.getvalue())
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["deliver"], "origin")
        self.assertFalse(any("not delivered" in w for w in result["warnings"]))
        self.assertTrue(all(j["deliver"] == "origin" for j in result["jobs"]))

    def test_doctor_invalid_config_is_critical_failure(self):
        self._write_invalid_config()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.doctor_command(argparse.Namespace(json=True, data_dir=None))
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 1)
        self.assertIn("user_config_valid", payload["failed"])
        check = next(c for c in payload["checks"] if c["check"] == "user_config_valid")
        self.assertTrue(check["critical"])

    def test_status_reports_effective_config_and_destination(self):
        paths.data_root().mkdir(parents=True, exist_ok=True)
        paths.user_config_path().write_text(json.dumps({
            "schema": 1, "version": 1, "timezone": "UTC",
            "notification": {"destination": "platform:chat-9"}}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.status_command(argparse.Namespace(json=True, data_dir=None))
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["config_error"], "")
        self.assertEqual(payload["deliver"], "platform:chat-9")
        self.assertEqual(payload["config"]["timezone"], "UTC")

    def test_status_invalid_config_reports_error_without_crashing(self):
        self._write_invalid_config()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sc.status_command(argparse.Namespace(json=True, data_dir=None))
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(payload["config_error"])


# --------------------------------------------------------------------------- #
# 4. fail-closed bridges: exactly one call, no duplicate fallback
# --------------------------------------------------------------------------- #


class BridgeSeam(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       (paths.DATA_ENV, "HERMES_BIN")}
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        os.environ[paths.DATA_ENV] = str(Path(self._tmp.name) / "data")
        os.environ.pop("HERMES_BIN", None)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _call(self, module):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = module._native_cli_bridge()
        return rc, out.getvalue(), err.getvalue()

    def _assert_single_success(self, module, expect_args):
        completed = types.SimpleNamespace(returncode=0, stdout="Alert line\n",
                                          stderr="diagnostic\n")
        with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
                mock.patch("subprocess.run", return_value=completed) as run:
            rc, out, err = self._call(module)
        self.assertEqual(rc, 0)
        run.assert_called_once()
        argv = run.call_args.args[0]
        for token in expect_args:
            self.assertIn(token, argv)
        self.assertEqual(out, "Alert line\n", "only the alert line goes to stdout")
        self.assertIn("diagnostic", err, "stderr is forwarded separately")

    def test_pilot_bridge_single_invocation_and_alert_only(self):
        self._assert_single_success(PILOT_RUNNER, ["portal", "pipeline", "--data-dir"])

    def test_reminder_bridge_single_invocation_and_alert_only(self):
        self._assert_single_success(
            REMINDER_RUNNER, ["portal", "reminders", "dispatch", "--data-dir"])

    def test_timeout_after_start_fails_closed_without_fallback(self):
        for module in (PILOT_RUNNER, REMINDER_RUNNER):
            with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
                    mock.patch("subprocess.run",
                               side_effect=subprocess.TimeoutExpired(["hermes"], 300)) as run:
                rc, _out, err = self._call(module)
            self.assertEqual(rc, 1, module.__name__)
            run.assert_called_once()
            self.assertIn("timed out", err.lower())

    def test_oserror_after_start_fails_closed_without_fallback(self):
        for module in (PILOT_RUNNER, REMINDER_RUNNER):
            with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
                    mock.patch("subprocess.run", side_effect=OSError("spawn failed")) as run:
                rc, _out, err = self._call(module)
            self.assertEqual(rc, 1, module.__name__)
            run.assert_called_once()
            self.assertIn("failed", err.lower())

    def test_nonzero_native_exit_is_propagated_fail_closed(self):
        completed = types.SimpleNamespace(returncode=3, stdout="", stderr="config error\n")
        for module in (PILOT_RUNNER, REMINDER_RUNNER):
            with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
                    mock.patch("subprocess.run", return_value=completed):
                rc, out, err = self._call(module)
            self.assertEqual(rc, 3, module.__name__)
            self.assertEqual(out, "")
            self.assertIn("config error", err)

    def test_missing_cli_is_the_only_reported_fallback(self):
        for module in (PILOT_RUNNER, REMINDER_RUNNER):
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch("subprocess.run") as run:
                rc, _out, err = self._call(module)
            self.assertIsNone(rc, module.__name__)
            run.assert_not_called()
            self.assertIn("fallback", err.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
