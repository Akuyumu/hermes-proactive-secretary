"""Validated per-user configuration: defaults, fail-closed validation, migration.

All synthetic, no network, no live profile, no credentials. Two synthetic users
are exercised with different timezones, account names and source subsets.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig as uc  # noqa: E402


class _FakeCtx:
    def __init__(self, settings):
        self._settings = settings

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def _user_a():
    return {
        "schema": 1, "version": 1, "timezone": "Europe/London",
        "notification": {"destination": "local", "quiet_start_hour": 23, "quiet_end_hour": 6},
        "sources": {"public_feeds": True, "obligations": True, "mail": True,
                    "calendar": True, "tasks": False},
        "mail": {"accounts": ["work", "personal"]},
        "calendar": {"accounts": ["work"], "calendars": ["primary", "team@group.calendar.google.com"]},
        "tasks": {"lists": ["My Tasks"], "adapter": "tasks"},
        "public_interests": ["distributed systems"],
        "adaptive": {"mode": "shadow", "privacy_review": True, "inferred_goals": False},
        "bounds": {"collector_budget_seconds": 45},
        "adapters": {"productivity_secretary": True, "gws_calendar": False},
    }


def _user_b():
    return {
        "schema": 1, "version": 1, "timezone": "America/New_York",
        "notification": {"destination": "platform:chat-9", "daily_normal_cap": 1},
        "sources": {"public_feeds": False, "obligations": False, "mail": False,
                    "calendar": True, "tasks": True},
        "mail": {"accounts": []},
        "calendar": {"accounts": ["home"], "calendars": ["primary"]},
        "tasks": {"lists": ["Home"]},
        "public_interests": ["local events"],
        "adaptive": {"mode": "disabled"},
        "bounds": {"max_candidates_per_kind": 20},
        "adapters": {"productivity_secretary": False, "gws_calendar": True},
    }


class DefaultAndSchemaTests(unittest.TestCase):
    def test_defaults_are_generic_and_neutral(self):
        cfg = uc.default_config()
        self.assertEqual(cfg["timezone"], "UTC")
        self.assertEqual(cfg["mail"]["accounts"], [])
        self.assertEqual(cfg["calendar"]["calendars"], [])
        self.assertEqual(cfg["adaptive"]["mode"], "disabled")
        self.assertFalse(cfg["sources"]["mail"])
        blob = json.dumps(cfg).lower()
        for forbidden in ("singapore", "/home/", "api_key", "password", "client_secret"):
            self.assertNotIn(forbidden, blob)

    def test_example_config_validates(self):
        example = json.loads((ROOT / "policy" / "user-config.example.json").read_text(encoding="utf-8"))
        cfg = uc.validate_config(example)
        self.assertEqual(cfg["timezone"], "UTC")

    def test_schema_summary_names_native_settings(self):
        summary = uc.schema_summary()
        self.assertEqual(summary["version"], uc.CONFIG_VERSION)
        self.assertIn("settings", summary["native_settings"])


class FailClosedValidationTests(unittest.TestCase):
    def test_unknown_schema_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 99, "version": 1})

    def test_corrupt_version_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": "one"})

    def test_newer_version_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": uc.CONFIG_VERSION + 1})

    def test_credential_value_rejected(self):
        for key in ("token", "api_key", "client_secret", "password"):
            with self.assertRaises(uc.ConfigError, msg=key):
                uc.validate_config({"schema": 1, "version": 1, "mail": {key: "abc123"}})

    def test_bad_timezone_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": 1, "timezone": "not a zone!"})

    def test_unsafe_destination_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": 1,
                                "notification": {"destination": "local; rm -rf /"}})

    def test_unsafe_account_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": 1,
                                "mail": {"accounts": ["formal; touch /tmp/x"]}})

    def test_bounds_rejected(self):
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": 1,
                                "bounds": {"collector_budget_seconds": 99999}})


class StaleVersionTests(unittest.TestCase):
    def test_stale_version_fails_closed_without_migrate(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "user_config.json"
            path.write_text(json.dumps({"schema": 1, "version": 0}), encoding="utf-8")
            with self.assertRaises(uc.ConfigError) as ctx:
                uc.load_user_config(path)
            self.assertIn("migrate", str(ctx.exception))

    def test_stale_version_migrates_when_explicit(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "user_config.json"
            path.write_text(json.dumps({"schema": 1, "version": 0, "timezone": "UTC"}),
                            encoding="utf-8")
            loaded = uc.load_user_config(path, migrate=True)
            self.assertTrue(loaded.migrated)
            self.assertEqual(loaded.version, uc.CONFIG_VERSION)


class NativePreferenceTests(unittest.TestCase):
    def test_native_settings_preferred_over_file(self):
        ctx = _FakeCtx({"user_config": _user_a()})
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "user_config.json"
            path.write_text(json.dumps(_user_b()), encoding="utf-8")
            loaded = uc.load_user_config(path, native=ctx)
        self.assertEqual(loaded.source, "native")
        self.assertEqual(loaded.config["timezone"], "Europe/London")

    def test_native_json_string_setting_is_accepted(self):
        ctx = _FakeCtx({"user_config": json.dumps(_user_b())})
        loaded = uc.load_user_config(None, native=ctx)
        self.assertEqual(loaded.source, "native")
        self.assertEqual(loaded.config["timezone"], "America/New_York")

    def test_absent_file_is_defaults_not_present(self):
        loaded = uc.load_user_config(None)
        self.assertFalse(loaded.present)
        self.assertEqual(loaded.source, "defaults")


class TwoUserTests(unittest.TestCase):
    def test_two_users_have_independent_overrides(self):
        a = uc.validate_config(_user_a())
        b = uc.validate_config(_user_b())
        oa, ob = uc.policy_overrides(a), uc.policy_overrides(b)
        self.assertEqual(oa["timezone"], "Europe/London")
        self.assertEqual(ob["timezone"], "America/New_York")
        self.assertEqual(oa["mail_accounts"], ["work", "personal"])
        self.assertEqual(ob["mail_accounts"], [])
        self.assertNotEqual(oa["calendar_ids"], ob["calendar_ids"])
        self.assertTrue(ob["gws_calendar_enabled"])
        self.assertFalse(oa["gws_calendar_enabled"])

    def test_apply_user_config_does_not_mutate_base(self):
        base = {"timezone": "Asia/Singapore", "feeds_enabled": True}
        merged = uc.apply_user_config(base, uc.validate_config(_user_a()))
        self.assertEqual(base["timezone"], "Asia/Singapore")
        self.assertEqual(merged["timezone"], "Europe/London")


class MigrationParityTests(unittest.TestCase):
    def _legacy(self):
        return {
            "timezone": "Asia/Singapore",
            "feeds_enabled": True,
            "obligations_enabled": False,
            "adaptive_mode": "active",
            "adaptive_privacy_review_enabled": True,
            "adaptive_inferred_goals_enabled": True,
            "collector_budget_seconds": 42,
            "daily_normal_cap": 4,
            "feed_seed_sources": [{"kind": "github_releases", "url": "https://api.github.com/x"}],
        }

    def test_migration_preserves_behaviour_explicitly(self):
        config, report = uc.migrate_legacy(self._legacy())
        overrides = uc.policy_overrides(uc.validate_config(config))
        self.assertEqual(overrides["timezone"], "Asia/Singapore")
        self.assertEqual(overrides["mail_accounts"], ["formal", "informal"])
        self.assertTrue(overrides["feeds_enabled"])
        self.assertFalse(overrides["obligations_enabled"])
        self.assertEqual(overrides["adaptive_mode"], "active")
        self.assertTrue(overrides["adaptive_inferred_goals_enabled"])
        self.assertEqual(overrides["collector_budget_seconds"], 42)
        self.assertEqual(overrides["feed_seed_sources"][0]["kind"], "github_releases")
        self.assertIn("bounds", report["mapped"])

    def test_migration_never_moves_pause(self):
        config, report = uc.migrate_legacy(
            self._legacy(), legacy_preferences={"paused": {"active": True}})
        blob = json.dumps(config).lower()
        self.assertNotIn("paused", blob)
        self.assertTrue(any("pause" in note for note in report["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
