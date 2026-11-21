"""Doctor must import model diagnostics from its native directory package."""
import argparse
import contextlib
import types
import unittest
from unittest import mock

from test_051_projection_truth import sc
from proactive_pilot import native_runtime


class NativeDoctorImportTests(unittest.TestCase):
    def _check(self, strict):
        emitted = []
        caps = {"himalaya_mail": False, "productivity_secretary": False,
                "secretary_src_candidates": [], "monitoring": "public-only"}
        loaded = types.SimpleNamespace(present=False, config={
            "adapters": {}, "notification": {"destination": "local"}})
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sc, "_writable", return_value=True))
            stack.enter_context(mock.patch.object(sc, "_job_states", return_value={"available": True}))
            stack.enter_context(mock.patch.object(sc, "detect_capabilities", return_value=caps))
            stack.enter_context(mock.patch.object(sc, "_authoritative_config", return_value=loaded))
            stack.enter_context(mock.patch.object(sc, "get_native_context", return_value=object()))
            stack.enter_context(mock.patch.object(sc, "_emit", side_effect=lambda payload, **_: emitted.append(payload)))
            stack.enter_context(mock.patch.object(native_runtime, "bound_model_lane_status", return_value={"strict_active": strict}))
            # A global top-level import is unavailable, as under the native loader.
            stack.enter_context(mock.patch.dict("sys.modules", {"secretary_models": None}))
            self.assertEqual(sc.doctor_command(argparse.Namespace(json=True)), 0)
        check = next(row for row in emitted[0]["checks"] if row["check"] == "model_strict_capability")
        self.assertEqual(check["ok"], strict)
        self.assertNotIn("ModuleNotFoundError", check["detail"])

    def test_native_package_reports_bound_strict_capability(self):
        self._check(True)

    def test_missing_strict_capability_is_not_fabricated(self):
        self._check(False)
