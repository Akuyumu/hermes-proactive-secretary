"""Foundation A+B regressions: configured timezone, neutral sources/allowance,
context capacity/tombstones, native settings and truthful capability status.

Offline only: no connectors, network, models, delivery or live profile.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import contextsnapshot as cs  # noqa: E402
from proactive_pilot import feedback  # noqa: E402
from proactive_pilot import main as main_mod  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.clock import Clock, SeededClock, build_clock  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.gates import _over_cap  # noqa: E402
from proactive_pilot.policy import (  # noqa: E402
    DEFAULT_POLICY,
    in_quiet_hours,
    load_policy,
)
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.userconfig import apply_user_config  # noqa: E402


def _load_plugin_package():
    name = "hermes_proactive_secretary_foundation_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_cli")


class _FlatCtx:
    def __init__(self, settings):
        self._settings = settings

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


class TimezoneContractTests(unittest.TestCase):
    def test_clock_uses_configured_zone(self):
        self.assertEqual(Clock("UTC").now().utcoffset(), timedelta(0))
        seeded = SeededClock("2026-10-20T00:15:00", timezone_name="UTC")
        self.assertEqual(build_clock("UTC", "2026-10-20T00:15:00").now().hour, 0)
        self.assertEqual(seeded.now().hour, 0)
        # An aware instant is converted to the configured zone.
        self.assertEqual(
            SeededClock("2026-10-20T00:15:00+00:00",
                        timezone_name="Asia/Singapore").now().hour, 8)

    def test_quiet_hours_follow_configured_zone_not_representation(self):
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        policy["timezone"] = "UTC"
        instant = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(in_quiet_hours(policy, instant))
        # Same instant represented in Singapore is still local midnight UTC.
        self.assertTrue(in_quiet_hours(policy, instant.astimezone(ZoneInfo("Asia/Singapore"))))

    def test_dst_local_hours(self):
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        policy.update(timezone="America/New_York", quiet_start_hour=0, quiet_end_hour=8)
        # 05:30 UTC is 00:30 EST before the spring transition (quiet).
        self.assertTrue(in_quiet_hours(policy, datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc)))
        # 07:30 UTC is 03:30 EDT after the transition (still quiet).
        self.assertTrue(in_quiet_hours(policy, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)))
        # 12:30 UTC is 08:30 EDT (outside quiet hours).
        self.assertFalse(in_quiet_hours(policy, datetime(2026, 3, 8, 12, 30, tzinfo=timezone.utc)))

    def test_daily_cap_counts_configured_local_day(self):
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        policy.update(timezone="UTC", daily_normal_cap=1)
        now = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)
        prior = {"delivered": [{"at": "2026-10-19T23:30:00+00:00"}]}
        self.assertFalse(_over_cap(prior, policy, now, "normal"))
        same_day = {"delivered": [{"at": "2026-10-20T00:10:00+00:00"}]}
        self.assertTrue(_over_cap(same_day, policy, now, "normal"))


class NeutralDefaultsTests(unittest.TestCase):
    def test_explicit_empty_public_sources_clears_inherited_seeds(self):
        base = load_policy(None)
        self.assertTrue(base["feed_seed_sources"], "fixture precondition: inherited seeds exist")
        config = uc.validate_config({
            "schema": 1, "version": 1,
            "sources": {"public_feeds": True},
            "public_sources": [],
        })
        policy = apply_user_config(base, config)
        self.assertEqual(policy["feed_seed_sources"], [])

    def test_neutral_default_config_has_no_seed_sources(self):
        base = load_policy(None)
        policy = apply_user_config(base, uc.default_config())
        self.assertEqual(policy["feed_seed_sources"], [])

    def test_explicit_nonempty_sources_are_preserved(self):
        source = {"kind": "github_releases", "url": "https://api.github.com/repos/x/y/releases"}
        config = uc.validate_config({"schema": 1, "version": 1, "public_sources": [source]})
        policy = apply_user_config(load_policy(None), config)
        self.assertEqual(policy["feed_seed_sources"], [source])

    def test_default_allowance_is_none_and_explicit_nonnegative_integer(self):
        self.assertIsNone(uc.default_config()["notification"]["daily_normal_cap"])
        self.assertEqual(uc.validate_config({"schema": 1, "version": 1,
                                             "notification": {"daily_normal_cap": 250}})
                         ["notification"]["daily_normal_cap"], 250)
        zero = uc.validate_config({"schema": 1, "version": 1,
                                   "notification": {"daily_normal_cap": 0}})
        self.assertEqual(zero["notification"]["daily_normal_cap"], 0)
        with self.assertRaises(uc.ConfigError):
            uc.validate_config({"schema": 1, "version": 1,
                                "notification": {"daily_normal_cap": -1}})
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        policy["daily_normal_cap"] = 0
        self.assertTrue(_over_cap({}, policy, datetime.now(timezone.utc), "normal"))

    def test_zero_remains_a_ceiling_not_a_target(self):
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        policy["daily_normal_cap"] = 0
        # No delivered rows still reports the cap as reached (nothing ordinary
        # may be emitted), and the value itself is not treated as a quota.
        self.assertTrue(_over_cap({"delivered": []}, policy,
                                  datetime.now(timezone.utc), "normal"))
        self.assertFalse(_over_cap({}, policy, datetime.now(timezone.utc), "urgent"))


class MigrationTests(unittest.TestCase):
    def test_existing_explicit_cap_is_not_reset_or_widened(self):
        legacy = {"timezone": "UTC", "daily_normal_cap": 3,
                  "feeds_enabled": False, "obligations_enabled": False}
        first, _ = uc.migrate_legacy(legacy)
        second, _ = uc.migrate_legacy(legacy)
        self.assertEqual(first["notification"]["daily_normal_cap"], 3)
        self.assertEqual(uc.policy_overrides(first)["daily_normal_cap"], 3)
        self.assertEqual(uc.policy_overrides(second)["daily_normal_cap"], 3)
        self.assertEqual(uc.policy_overrides(first)["feed_seed_sources"], [])

    def test_omitted_legacy_cap_has_no_hidden_default_five(self):
        config, _ = uc.migrate_legacy({"timezone": "UTC"})
        self.assertIsNone(config["notification"]["daily_normal_cap"])

    def test_explicit_zero_and_large_legacy_caps_are_preserved(self):
        for cap in (0, 7, 250):
            config, _ = uc.migrate_legacy({"timezone": "UTC", "daily_normal_cap": cap})
            self.assertEqual(config["notification"]["daily_normal_cap"], cap)
            self.assertEqual(uc.policy_overrides(config)["daily_normal_cap"], cap)


class ContextSnapshotRepairTests(unittest.TestCase):
    def _store(self, tmp):
        return StateStore(Path(tmp), SeededClock("2026-10-20T12:00:00+00:00"))

    @staticmethod
    def _fields(i, expires):
        return {"id": f"cs_{i:012x}", "text": f"note {i}",
                "source": "assistant:knowledge", "kind": "note",
                "expires_at": expires}

    def test_expired_entries_reclaim_capacity(self):
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for i in range(cs.MAX_ENTRIES):
                cs.update_context_entry(
                    store, self._fields(i, (now + timedelta(hours=1)).isoformat()), now)
            later = now + timedelta(days=2)
            entry = cs.update_context_entry(
                store, self._fields(99, (later + timedelta(days=1)).isoformat()), later)
            self.assertTrue(entry["id"].startswith("cs_"))
            view = cs.load_context_snapshot(store, later)
            self.assertEqual(view.status, cs.STATUS_AVAILABLE)
            self.assertEqual([e["id"] for e in view.entries], [entry["id"]])

    def test_tombstone_writer_never_exceeds_reader_bound_or_corrupts(self):
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cs, "MAX_REVOKED", 3):
            store = self._store(tmp)
            for i in range(3):
                entry = cs.update_context_entry(
                    store, self._fields(i, (now + timedelta(days=1)).isoformat()), now)
                cs.revoke_context_entry(store, entry["id"], now)
            live = cs.update_context_entry(
                store, self._fields(50, (now + timedelta(days=1)).isoformat()), now)
            with self.assertRaises(cs.ContextSnapshotError):
                cs.revoke_context_entry(store, live["id"], now)
            # The failed over-bound revoke wrote nothing: snapshot stays valid
            # and the withdrawn ids remain withdrawn.
            view = cs.load_context_snapshot(store, now)
            self.assertEqual(view.status, cs.STATUS_AVAILABLE)
            self.assertEqual([e["id"] for e in view.entries], [live["id"]])
            with self.assertRaises(cs.ContextSnapshotError):
                cs.update_context_entry(store, self._fields(0, (now + timedelta(days=1)).isoformat()), now)


class NativeSettingsTests(unittest.TestCase):
    def test_flat_native_controls_use_the_same_validated_owner(self):
        ctx = _FlatCtx({
            "timezone": "America/New_York",
            "daily_normal_cap": 0,
            "public_feeds": True,
            "public_sources": [],
        })
        loaded = uc.load_user_config(None, native=ctx)
        self.assertEqual(loaded.source, "native")
        self.assertEqual(loaded.config["timezone"], "America/New_York")
        self.assertEqual(loaded.config["notification"]["daily_normal_cap"], 0)
        overrides = uc.policy_overrides(loaded.config)
        self.assertEqual(overrides["daily_normal_cap"], 0)
        self.assertEqual(overrides["feed_seed_sources"], [])

    def test_flat_native_invalid_range_fails_closed(self):
        ctx = _FlatCtx({"daily_normal_cap": -1})
        with self.assertRaises(uc.ConfigError):
            uc.load_user_config(None, native=ctx)

    def test_manifest_schema_defaults_match_validated_owner(self):
        import yaml
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        # Hermes reads config_schema as key -> field spec (not a JSON-Schema
        # root with a "properties" wrapper).
        props = manifest["config_schema"]
        self.assertNotIn("properties", props)
        self.assertEqual(props["daily_normal_cap"]["default"], "")
        self.assertNotIn("minimum", props["daily_normal_cap"])
        self.assertIn("timezone", props)
        self.assertIn("public_sources", props)
        self.assertNotIn("api_key", props)
        # Every declared flat control maps onto the one validated config owner,
        # and the manifest declares no unmapped duplicate preference.
        route_fields = {"primary_provider", "primary_model",
                        "fallback_provider", "fallback_model"}
        self.assertEqual(set(props), set(uc.NATIVE_FLAT_FIELDS) | route_fields
                         | {"portal_reasoning_effort", "portal_cadence"})
        self.assertEqual(
            (props["portal_reasoning_effort"]["type"],
             props["portal_reasoning_effort"]["default"]), ("str", ""))
        self.assertEqual(props["portal_cadence"]["default"], "3h")
        self.assertEqual(props["portal_cadence"]["choices"], ["3h", "6h", "9h", "12h", "24h"])
        self.assertEqual(props["collector_budget_seconds"]["minimum"], 5)
        self.assertEqual(props["max_candidates_per_kind"]["maximum"], 500)

    def test_flat_native_bounded_cost_fields_validate(self):
        ctx = _FlatCtx({
            "collector_budget_seconds": 15,
            "max_candidates_per_kind": 7,
            "feed_max_sources_per_run": 0,
        })
        loaded = uc.load_user_config(None, native=ctx)
        overrides = uc.policy_overrides(loaded.config)
        self.assertEqual(overrides["collector_budget_seconds"], 15)
        self.assertEqual(overrides["max_candidates_per_kind"], 7)
        self.assertEqual(overrides["feed_max_sources_per_run"], 0)
        for key, value in (("collector_budget_seconds", 601),
                           ("max_candidates_per_kind", 0),
                           ("feed_max_sources_per_run", 21)):
            with self.assertRaises(uc.ConfigError, msg=key):
                uc.load_user_config(None, native=_FlatCtx({key: value}))


class TruthfulCapabilityTests(unittest.TestCase):
    def test_installed_is_not_reported_as_coverage(self):
        config = uc.default_config()
        truth = sc._capability_truth(config, {})
        public = truth["public"]
        self.assertTrue(public["available"])
        self.assertFalse(public["configured"])
        self.assertFalse(public["verified"])
        self.assertEqual(public["status"], "disabled")
        self.assertTrue(public["remedy"])

    def test_enabled_but_unread_public_source_is_not_fresh(self):
        config = uc.default_config()
        config["sources"]["public_feeds"] = True
        config["public_sources"] = [{"kind": "github_releases", "url": "https://api.github.com/x"}]
        truth = sc._capability_truth(config, {})
        self.assertEqual(truth["public"]["status"], "configured-unverified")
        self.assertFalse(truth["public"]["fresh"])

    def test_public_feed_health_key_counts_as_verified_and_fresh(self):
        # Production feed results are recorded under `feed:<id>`, not `public`.
        now = datetime.now(timezone.utc)
        config = uc.default_config()
        config["sources"]["public_feeds"] = True
        config["public_sources"] = [{"kind": "github_releases", "url": "https://api.github.com/x"}]
        state = {"source_health": {"stale_after_minutes": 45, "sources": {
            "feed:gh": {"last_success_at": now.isoformat()}}}}
        truth = sc._capability_truth(config, state)
        self.assertEqual(truth["public"]["status"], "verified-fresh")
        self.assertTrue(truth["public"]["fresh"])

    def test_public_feed_health_older_than_bound_is_stale(self):
        now = datetime.now(timezone.utc)
        config = uc.default_config()
        config["sources"]["public_feeds"] = True
        config["public_sources"] = [{"kind": "github_releases", "url": "https://api.github.com/x"}]
        state = {"source_health": {"stale_after_minutes": 45, "sources": {
            "feed:gh": {"last_success_at": (now - timedelta(hours=2)).isoformat()}}}}
        truth = sc._capability_truth(config, state)
        self.assertEqual(truth["public"]["status"], "configured-stale")
        self.assertFalse(truth["public"]["fresh"])


class ProductionRuntimeTests(unittest.TestCase):
    def test_run_once_feeds_the_configured_zone_to_collectors_and_gates(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(dict(DEFAULT_POLICY)), encoding="utf-8")
            prefs_path = root / "preferences.json"
            prefs_path.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False}}), encoding="utf-8")
            (root / "user_config.json").write_text(json.dumps({
                "schema": 1, "version": 1, "timezone": "America/New_York",
                "notification": {"daily_normal_cap": 0},
                "sources": {"public_feeds": False, "obligations": False,
                            "mail": False, "calendar": False, "tasks": False},
                "public_sources": [],
            }), encoding="utf-8")
            args = main_mod.build_parser().parse_args([
                "--state-dir", str(root / "state"),
                "--policy", str(policy_path),
                "--preferences", str(prefs_path),
                "--seeded-clock", "2026-10-20T12:00:00",
                "--run",
            ])
            args.require_config = True
            args.native_ctx = None
            captured = {}

            def _fake_sources(policy, clock, budget=None):
                captured["policy"] = dict(policy)
                captured["clock"] = clock
                return []

            with mock.patch.object(main_mod, "default_sources", _fake_sources):
                rc = main_mod.run_once(
                    args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
            self.assertEqual(rc, 0)
            self.assertEqual(captured["policy"]["timezone"], "America/New_York")
            self.assertEqual(captured["policy"]["daily_normal_cap"], 0)
            self.assertEqual(captured["policy"]["feed_seed_sources"], [])
            # 2026-10-20 is EDT (UTC-4): the production run clock must be the
            # configured zone, not the historical Singapore default.
            self.assertEqual(captured["clock"].now().utcoffset(), timedelta(hours=-4))


class ForgetContractTests(unittest.TestCase):
    def test_skill_does_not_promise_forget_creates_tombstone(self):
        text = (ROOT / "skills" / "proactive-secretary" / "SKILL.md").read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertIn("does not create a topic tombstone", lowered)
        self.assertIn("inferred-revoke", lowered)
        self.assertNotIn("explicit `forget` records a durable revocation", lowered)
        self.assertNotIn("feedback forget --id <exact existing id>  # revoke", lowered)

    def test_forget_erases_only_the_record_and_creates_no_revocation_state(self):
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        prefs = feedback.empty_preferences()
        added = feedback.apply_action(prefs, {
            "action": "add-explicit", "kind": "mail", "match": "career",
            "scope": "generic career broadcasts", "provenance": "user correction",
        }, now)
        self.assertEqual(len(added["explicit"]), 1)
        record_id = added["explicit"][0]["id"]
        forgotten = feedback.apply_action(added, {
            "action": "forget", "id": record_id}, now)
        self.assertEqual(forgotten["explicit"], [])
        self.assertNotIn("revoked", forgotten)
        self.assertEqual(feedback.find_records(forgotten, record_id), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
