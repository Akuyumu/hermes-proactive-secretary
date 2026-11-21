"""Fail-visible model capability in status/doctor, without calling a model.

Synthetic host contexts only: an old host without the explicit strict keyword, a
compatible strict host, and an unconfigured/disabled neutral install. These tests
assert that configured model work is non-healthy before dispatch on the old host,
that a compatible host stays healthy, and that neutral state is not falsely
broken. No network, provider, profile, scheduler or installed plugin is touched.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_package():
    name = "hermes_proactive_portal_model_capability_health"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
models = importlib.import_module(f"{PKG.__name__}.portal.portal_models")
sc = importlib.import_module(f"{PKG.__name__}.portal.portal_cli")
status_mod = importlib.import_module(f"{PKG.__name__}.portal.portal_status")

ROUTES = {"primary_provider": "provider-a", "primary_model": "model-a",
          "fallback_provider": "provider-b", "fallback_model": "model-b"}
ENV_KEYS = ("HOME", "HERMES_HOME", "HERMES_PROACTIVE_PORTAL_DATA", "TMPDIR")


class FakeLlm:
    def __init__(self):
        self.calls = []

    def complete_structured(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("no model call is allowed in these tests")


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class OldHostCtx:
    """Registers an auxiliary task but has no explicit fallback_policy keyword."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.llm = FakeLlm()
        self.state = MemoryState()
        self.plugin_id = "hermes-proactive-portal"
        self.aux_calls = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None):
        self.aux_calls.append({"key": key, "defaults": defaults})


class StrictHostCtx(OldHostCtx):
    """Compatible host: the explicit strict keyword is part of the signature."""

    def register_auxiliary_task(self, key, *, display_name, description,
                                defaults=None, inherit_from=None,
                                fallback_policy=None):
        self.aux_calls.append({"key": key, "defaults": defaults,
                               "fallback_policy": fallback_policy})


class _EnvCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self._old = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["HOME"] = str(self.root / "home")
        os.environ["HERMES_HOME"] = str(self.root / "home")
        os.environ["HERMES_PROACTIVE_PORTAL_DATA"] = str(self.root / "data")
        os.environ["TMPDIR"] = str(self.root / "tmp")
        (self.root / "tmp").mkdir()
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _doctor(self, ctx):
        emitted = []
        caps = {"himalaya_mail": False, "productivity_secretary": False,
                "secretary_src_candidates": [], "monitoring": "public-only",
                "truth": {}, "routed_retrieval": {}}
        loaded = types.SimpleNamespace(
            present=False,
            config={"adapters": {}, "notification": {"destination": "local"}})
        with mock.patch.object(sc, "get_native_context", return_value=ctx), \
                mock.patch.object(sc, "_writable", return_value=True), \
                mock.patch.object(sc, "_job_states",
                                  return_value={"available": True}), \
                mock.patch.object(sc, "detect_capabilities", return_value=caps), \
                mock.patch.object(sc, "_authoritative_config", return_value=loaded), \
                mock.patch.object(sc, "_is_paused", return_value=False), \
                mock.patch.object(sc, "_emit",
                                  side_effect=lambda payload, **_: emitted.append(payload)):
            rc = sc.doctor_command(argparse.Namespace(json=True, data_dir=None))
        return rc, emitted[0]


class _BrokenRouteService:
    """Route inspection fails: absence of route_configured must not mean neutral."""

    def inspect(self):
        raise RuntimeError("route read failed")


class StatusCapabilityTests(_EnvCase):
    def test_missing_or_failed_route_is_nonhealthy_unknown(self):
        summary = status_mod._route_summary(None, _BrokenRouteService())
        self.assertIsNone(summary["route_configured"])
        self.assertEqual(summary["route_lookup_error"], "RuntimeError")

        capability = status_mod.model_capability_health(summary["status"])
        self.assertFalse(capability["known"])
        self.assertFalse(capability["healthy"])
        self.assertFalse(capability["requested"])
        self.assertEqual(capability["status"], "blocked")
        self.assertIn("could not be determined", capability["reason"])
        self.assertIn("RuntimeError", capability["reason"])
        self.assertIn("not treated as intentionally unconfigured",
                      capability["remedy"])
        self.assertIn("no model call", capability["detail"])

        absent = status_mod.model_capability_health({"strict_active": False})
        self.assertFalse(absent["known"])
        self.assertEqual(absent["status"], "blocked")
        self.assertFalse(absent["healthy"])

    def test_enabled_old_host_is_blocked_before_any_model_call(self):
        ctx = OldHostCtx(ROUTES)
        result = models.register_configured_models(ctx)
        self.assertFalse(result["strict_active"])
        self.assertEqual(ctx.llm.calls, [])

        status = status_mod.collect(ctx)
        capability = status["model_capability"]
        self.assertEqual(capability["status"], "blocked")
        self.assertFalse(capability["healthy"])
        self.assertTrue(capability["requested"])
        self.assertFalse(capability["strict_active"])
        self.assertIn("register_auxiliary_task", capability["reason"])
        self.assertIn("configured_only", capability["reason"])
        self.assertIn("no model call", capability["detail"])
        self.assertIn("host patch", capability["remedy"])
        self.assertEqual(ctx.llm.calls, [])

        text = status_mod.render_chat(status)
        self.assertIn("capability=blocked", text)
        self.assertIn("register_auxiliary_task", text)

    def test_compatible_strict_host_is_healthy(self):
        ctx = StrictHostCtx(ROUTES)
        result = models.register_configured_models(ctx)
        self.assertTrue(result["strict_active"])
        self.assertEqual(ctx.aux_calls[0]["fallback_policy"], "configured_only")

        status = status_mod.collect(ctx)
        capability = status["model_capability"]
        self.assertEqual(capability["status"], "ok")
        self.assertTrue(capability["healthy"])
        self.assertTrue(capability["requested"])
        self.assertTrue(capability["configured_model_calls_allowed"])
        self.assertEqual(ctx.llm.calls, [])

    def test_unconfigured_install_is_neutral_not_falsely_broken(self):
        ctx = OldHostCtx()
        models.register_configured_models(ctx)
        capability = status_mod.collect(ctx)["model_capability"]
        self.assertEqual(capability["status"], "neutral")
        self.assertTrue(capability["healthy"])
        self.assertFalse(capability["requested"])
        self.assertIn("no configured model route", capability["detail"])
        self.assertEqual(ctx.llm.calls, [])

    def test_intentional_pause_is_neutral(self):
        ctx = OldHostCtx(ROUTES)
        models.register_configured_models(ctx)
        with mock.patch.object(status_mod, "_read_prefs",
                               return_value={"paused": {"active": True}}):
            capability = status_mod.collect(ctx)["model_capability"]
        self.assertEqual(capability["status"], "neutral")
        self.assertTrue(capability["healthy"])
        self.assertFalse(capability["requested"])
        self.assertIn("global pause", capability["detail"])
        self.assertEqual(ctx.llm.calls, [])


class DoctorCapabilityTests(_EnvCase):
    def test_doctor_old_host_with_configured_route_is_nonhealthy(self):
        ctx = OldHostCtx(ROUTES)
        models.register_configured_models(ctx)
        rc, payload = self._doctor(ctx)
        self.assertEqual(rc, 1, payload)
        check = next(row for row in payload["checks"]
                     if row["check"] == "model_capability")
        self.assertFalse(check["ok"])
        self.assertTrue(check["critical"])
        self.assertIn("register_auxiliary_task", check["detail"])
        self.assertIn("configured_only", check["detail"])
        self.assertIn("model_capability", payload["failed"])
        self.assertEqual(ctx.llm.calls, [])

    def test_doctor_compatible_strict_host_is_healthy(self):
        ctx = StrictHostCtx(ROUTES)
        models.register_configured_models(ctx)
        rc, payload = self._doctor(ctx)
        self.assertEqual(rc, 0, payload)
        check = next(row for row in payload["checks"]
                     if row["check"] == "model_capability")
        self.assertTrue(check["ok"])
        self.assertIn("strict host contract", check["detail"])
        self.assertEqual(ctx.llm.calls, [])

    def test_doctor_unconfigured_install_is_neutral(self):
        ctx = OldHostCtx()
        models.register_configured_models(ctx)
        rc, payload = self._doctor(ctx)
        self.assertEqual(rc, 0, payload)
        check = next(row for row in payload["checks"]
                     if row["check"] == "model_capability")
        self.assertTrue(check["ok"])
        self.assertFalse(check["critical"])
        self.assertIn("no configured model route", check["detail"])
        self.assertEqual(ctx.llm.calls, [])

    def test_doctor_capability_exception_is_critical(self):
        ctx = OldHostCtx(ROUTES)
        models.register_configured_models(ctx)
        with mock.patch.object(models, "effective_route_status",
                               side_effect=RuntimeError("route read failed")):
            rc, payload = self._doctor(ctx)
        self.assertEqual(rc, 1, payload)
        check = next(row for row in payload["checks"]
                     if row["check"] == "model_capability")
        self.assertFalse(check["ok"])
        self.assertTrue(check["critical"])
        self.assertIn("unavailable", check["detail"])
        self.assertIn("RuntimeError", check["detail"])
        self.assertIn("model_capability", payload["failed"])
        self.assertEqual(ctx.llm.calls, [])


if __name__ == "__main__":
    unittest.main()
