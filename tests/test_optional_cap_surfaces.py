"""Optional daily normal cap across setup, onboarding and the widget seam.

Offline only. These tests pin the same canonical semantics as
``proactive_pilot.userconfig``: unset stays unset (never a hidden 5 or 0), an
explicit saved integer is preserved, a blank clears it, and invalid values fail
validation instead of being coerced.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig as uc  # noqa: E402

_OMIT = object()


def _load_plugin_package():
    name = "hermes_proactive_portal_optional_cap_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sa = importlib.import_module(f"{_PKG.__name__}.portal.portal_setup_actions")
onb = importlib.import_module(f"{_PKG.__name__}.portal.portal_onboarding")


class _State:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _Ctx:
    def __init__(self):
        self.state = _State()


def _loaded(cap=_OMIT):
    payload = {"schema": 1, "version": 1}
    if cap is not _OMIT:
        payload["notification"] = {"daily_normal_cap": cap}
    return uc.LoadedConfig(config=uc.validate_config(payload), present=True,
                           source="test")


def _actions(loaded):
    return sa.SetupActions(
        _Ctx(),
        config_path=ROOT / "unused-optional-cap.json",
        preferences_path=ROOT / "unused-optional-preferences.json",
        backup_root=ROOT / "unused-optional-backup",
        config_loader=lambda: loaded,
    )


class SetupFlatPatchTests(unittest.TestCase):
    def test_blank_and_none_mean_unset(self):
        for value in ("", "   ", None):
            clean = sa.SetupActions._validated_flat_patch(
                {"daily_normal_cap": value})
            self.assertIsNone(clean["daily_normal_cap"], value)

    def test_numeric_text_and_explicit_numbers_are_preserved(self):
        for value, expected in (("9", 9), (" 0 ", 0), (9, 9), (0, 0)):
            clean = sa.SetupActions._validated_flat_patch(
                {"daily_normal_cap": value})
            self.assertEqual(clean["daily_normal_cap"], expected)
            self.assertIs(type(clean["daily_normal_cap"]), int)

    def test_invalid_cap_values_fail_validation(self):
        for value in (True, False, -1, 1.5, "x", "1.5", "-1", [], {}):
            with self.assertRaises(sa.SetupActionError, msg=repr(value)):
                sa.SetupActions._validated_flat_patch(
                    {"daily_normal_cap": value})

    def test_other_integer_fields_still_reject_blank(self):
        with self.assertRaises(sa.SetupActionError):
            sa.SetupActions._validated_flat_patch({"quiet_start_hour": ""})


class SetupStatusAndPreservationTests(unittest.TestCase):
    def _status_cap(self, loaded):
        actions = _actions(loaded)
        actions._preferences = lambda: {}
        actions._truth = lambda: ({}, "")
        actions._jobs = lambda: {"available": False, "active_reminders": 0,
                                 "owned_jobs": 0, "next_schedule": "", "error": ""}
        return actions.status()["config"]["daily_normal_cap"]

    def test_unset_status_is_honest_null_not_zero(self):
        self.assertIsNone(self._status_cap(_loaded()))

    def test_explicit_zero_and_saved_integer_status_are_preserved(self):
        self.assertEqual(self._status_cap(_loaded(0)), 0)
        self.assertEqual(self._status_cap(_loaded(9)), 9)

    def test_unrelated_patch_preserves_saved_and_unset_caps(self):
        for loaded, expected_raw, expected_after in (
                (_loaded(9), 9, 9), (_loaded(), None, "")):
            actions = _actions(loaded)
            revision = actions._settings_revision(loaded)
            _loaded_after, _before, after, validated, changed, _expanding = \
                actions._prepared_settings({"timezone": "UTC"}, revision)
            self.assertEqual(validated["notification"]["daily_normal_cap"],
                             expected_raw)
            self.assertEqual(after["daily_normal_cap"], expected_after)
            self.assertNotIn("daily_normal_cap", changed)

    def test_blank_clears_saved_cap_and_numeric_text_sets_one(self):
        loaded = _loaded(9)
        actions = _actions(loaded)
        revision = actions._settings_revision(loaded)
        _l, _before, _after, validated, changed, _e = actions._prepared_settings(
            {"daily_normal_cap": ""}, revision)
        self.assertIsNone(validated["notification"]["daily_normal_cap"])
        self.assertIn("daily_normal_cap", changed)

        loaded = _loaded()
        actions = _actions(loaded)
        revision = actions._settings_revision(loaded)
        _l, _before, after, validated, changed, _e = actions._prepared_settings(
            {"daily_normal_cap": "9"}, revision)
        self.assertEqual(validated["notification"]["daily_normal_cap"], 9)
        self.assertEqual(after["daily_normal_cap"], 9)
        self.assertIn("daily_normal_cap", changed)

    def test_blank_against_already_unset_cap_is_a_true_noop(self):
        loaded = _loaded()
        actions = _actions(loaded)
        revision = actions._settings_revision(loaded)
        _l, _before, _after, validated, changed, _e = actions._prepared_settings(
            {"daily_normal_cap": ""}, revision)
        self.assertIsNone(validated["notification"]["daily_normal_cap"])
        self.assertNotIn("daily_normal_cap", changed)


def _onboarding_loaded(cap=_OMIT):
    payload = {"schema": 1, "version": 1}
    fields = []
    if cap is not _OMIT:
        payload["notification"] = {"daily_normal_cap": cap}
        fields = ["notification.daily_normal_cap"]
    return uc.LoadedConfig(
        config=uc.validate_config(payload), present=True, source="test",
        provenance={"mode": "explicit", "fields": fields, "present": []})


class OnboardingOptionalCapTests(unittest.TestCase):
    def test_new_default_is_unset_not_five(self):
        self.assertIsNone(onb.DEFAULT_ALLOWANCE)
        state = onb.default_state()
        self.assertIsNone(state["settings"]["daily_allowance"])

    def test_validation_accepts_blank_numbers_and_rejects_bad_values(self):
        self.assertIsNone(onb.validate_settings({"daily_allowance": ""})["daily_allowance"])
        self.assertIsNone(onb.validate_settings({"daily_allowance": None})["daily_allowance"])
        self.assertEqual(onb.validate_settings({"daily_allowance": "9"})["daily_allowance"], 9)
        self.assertEqual(onb.validate_settings({"daily_allowance": 0})["daily_allowance"], 0)
        for value in (True, False, -1, 1.5, "x", "1.5", []):
            with self.assertRaises(onb.OnboardingError, msg=repr(value)):
                onb.validate_settings({"daily_allowance": value})

    def test_seed_preserves_existing_value_and_clear_only_when_explicit(self):
        service = onb.OnboardingService(
            state=_State(), config_resolver=lambda: _onboarding_loaded(9))
        state = onb.default_state()
        service._seed_from_effective_config(state)
        self.assertEqual(state["settings"]["daily_allowance"], 9)

        service = onb.OnboardingService(
            state=_State(), config_resolver=lambda: _onboarding_loaded(0))
        state = onb.default_state()
        service._seed_from_effective_config(state)
        self.assertEqual(state["settings"]["daily_allowance"], 0)

        # Explicit present blank clears a completed setup's stale value.
        service = onb.OnboardingService(
            state=_State(), config_resolver=lambda: _onboarding_loaded(None))
        state = onb.default_state()
        state["status"] = "pending_verification"
        state["decided"] = ["setting:daily_allowance"]
        state["settings"]["daily_allowance"] = 9
        service._seed_from_effective_config(state)
        self.assertIsNone(state["settings"]["daily_allowance"])

        # An omitted cap is not a decision: the existing value survives.
        service = onb.OnboardingService(
            state=_State(), config_resolver=lambda: _onboarding_loaded())
        state = onb.default_state()
        state["status"] = "pending_verification"
        state["decided"] = ["setting:daily_allowance"]
        state["settings"]["daily_allowance"] = 9
        service._seed_from_effective_config(state)
        self.assertEqual(state["settings"]["daily_allowance"], 9)

    def test_update_clears_blank_but_unrelated_edit_preserves_value(self):
        raw = onb.default_state()
        facade = _State({onb.STATE_KEY: raw})
        service = onb.OnboardingService(
            state=facade, config_resolver=lambda: _onboarding_loaded(9))
        result = service.update(settings={"quiet_start_hour": 22})
        self.assertEqual(result["readback"]["settings"]["daily_allowance"], 9)

        result = service.update(settings={"daily_allowance": ""})
        self.assertIsNone(result["readback"]["settings"]["daily_allowance"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
