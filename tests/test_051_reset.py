"""Source-only acceptance tests for the 0.5.1 setup status and confirmed reset.

All synthetic artifacts live under TMPDIR. No live configuration, profile,
plugin data, scheduler, network or model call is touched.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from proactive_pilot import userconfig  # noqa: E402
from secretary.secretary_setup_actions import (  # noqa: E402
    CLEARED_FIELDS,
    PRESERVED_FIELDS,
    SetupActionError,
    SetupActions,
)

class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value):
        self.data[key] = copy.deepcopy(value)


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = copy.deepcopy(settings or {})
        self.state = MemoryState()
        self.plugin_id = "hermes-proactive-secretary"
        self.writes = []
        self.fail_keys = set()

    def get_config(self, key, default=None):
        if key not in self.settings:
            return default
        return copy.deepcopy(self.settings[key])

    def set_config(self, key, value):
        if key in self.fail_keys:
            self.fail_keys.discard(key)
            raise RuntimeError("synthetic write failure")
        self.settings[key] = copy.deepcopy(value)
        self.writes.append(key)


def owner_config():
    """A synthetic personal-shaped config; no value belongs to the owner."""
    config = userconfig.default_config()
    config.update({
        "timezone": "Europe/London",
        "notification": {
            "persona": "Ada",
            "notify_address": "owner",
            "destination": "telegram:12345",
            "quiet_start_hour": 21,
            "quiet_end_hour": 9,
            "daily_normal_cap": 12,
            "cooldown_minutes": 15,
        },
        "sources": {"public_feeds": True, "obligations": True, "mail": True,
                    "calendar": True, "tasks": True},
        "mail": {"accounts": ["owner@example.test"], "aliases": {"work": "owner@example.test"}},
        "calendar": {"accounts": ["owner@example.test"], "calendars": ["primary"]},
        "tasks": {"accounts": ["owner@example.test"], "lists": ["Inbox"], "adapter": "tasks"},
        "public_interests": ["local transit"],
        "public_sources": [{"kind": "rss", "url": "https://example.test/feed.xml"}],
        "adaptive": {"mode": "active", "privacy_review": True, "inferred_goals": True},
        "health": {"critical_sources": ["mail:formal"]},
        "preparation": {"enabled": True, "scopes": ["mail"], "source_consent": True},
        "bounds": {"collector_budget_seconds": 90, "max_candidates_per_kind": 70,
                   "feed_max_sources_per_run": 3},
        "adapters": {"productivity_secretary": True, "gws_calendar": False},
        "advanced": {"legacy_policy": {"feed_seed_sources": ["https://legacy.example/feed"]},
                     "onboarding_provenance": {"schema": 1, "neutral_init": False,
                                               "known_fields": ["timezone"]}},
    })
    return userconfig.validate_config(config)


def onboarding_state():
    return {
        "schema": 1,
        "version": 1,
        "status": "in_progress",
        "current_step": "permissions",
        "facts": {"callname": "synthetic"},
        "proposals": [{"key": "city", "value": "synthetic"}],
        "settings": {"timezone": "Europe/London", "daily_allowance": 12},
        "grants": {"mail": True, "calendar": True, "tasks": True,
                   "public_sources": True, "memory": True, "discretionary": True},
        "source_scope": {"mail_accounts": ["owner@example.test"]},
        "history": [{"changed": ["grants"]}],
    }


class Harness:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(
            prefix="secretary-051-case-", dir=os.environ.get("TMPDIR") or None))
        self._finalizer = weakref.finalize(self, shutil.rmtree, self.root, True)
        self.config_path = self.root / "user_config.json"
        self.prefs_path = self.root / "preferences.json"
        self.backup_root = self.root / "data"
        self.reminders_path = self.root / "var" / "reminders.json"
        self.reminders_path.parent.mkdir(parents=True)
        self.reminders_path.write_text(json.dumps({"reminders": {"r1": {"status": "pending"}}}),
                                       encoding="utf-8")
        self.preferences = {
            "schema": 1,
            "version": 4,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "explicit": [{"kind": "mail", "match": "synthetic"}],
            "inferred": [{"kind": "feed", "match": "synthetic"}],
            "snoozes": [{"kind": "any", "until": "2099-01-01T00:00:00+00:00"}],
            "paused": {"active": True, "reason": "synthetic"},
        }
        self.prefs_path.write_text(json.dumps(self.preferences), encoding="utf-8")
        self.ctx = FakeCtx({
            "user_config": owner_config(),
            "primary_provider": "configured-provider",
            "primary_model": "configured-model",
            "fallback_provider": "fallback-provider",
            "fallback_model": "fallback-model",
        })
        self.ctx.state.set("onboarding_state", onboarding_state())
        self.ctx.state.set("unrelated_runtime_key", {"keep": "me"})
        self.actions = SetupActions(
            self.ctx,
            config_path=self.config_path,
            preferences_path=self.prefs_path,
            backup_root=self.backup_root,
            ttl_seconds=600,
        )

    def close(self):
        self._finalizer()

    def snapshot(self):
        return {
            "settings": copy.deepcopy(self.ctx.settings),
            "state": copy.deepcopy(self.ctx.state.data),
            "prefs": self.prefs_path.read_text(encoding="utf-8"),
            "reminders": self.reminders_path.read_text(encoding="utf-8"),
        }


class StatusTests(unittest.TestCase):
    def test_status_reads_effective_config_and_never_calls_off_connected(self):
        h = Harness()
        status = h.actions.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["config"]["source"], "native")
        self.assertEqual(status["config"]["timezone"], "Europe/London")
        self.assertTrue(status["config"]["delivery_configured"])
        self.assertFalse(status["permissions"]["needs_reconfirmation"])
        self.assertEqual({row["status"] for row in status["sources"].values()},
                         {"unverified"})
        self.assertNotIn("owner@example.test", json.dumps(status))
        self.assertNotIn("telegram:12345", json.dumps(status))
        self.assertNotIn("local transit", json.dumps(status))

    def test_status_degrades_without_claiming_a_connection(self):
        h = Harness()
        h.ctx.settings["user_config"] = {"schema": 1, "version": 1, "timezone": "Not/AZone"}
        status = h.actions.status()
        self.assertEqual(status["config"]["error"], "ConfigError")
        self.assertTrue(all(row["verified"] is False for row in status["sources"].values()))


class TriggerTests(unittest.TestCase):
    def test_trigger_is_non_destructive(self):
        h = Harness()
        before = h.snapshot()
        result = h.actions.trigger()
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["command"], "/secretary setup")
        self.assertEqual(h.snapshot(), before)


class ResetGuardTests(unittest.TestCase):
    def test_missing_wrong_or_unauthorized_confirmation_writes_nothing(self):
        h = Harness()
        before = h.snapshot()
        for kwargs in (
            {"challenge": "", "phrase": "RESET", "explicit": True},
            {"challenge": "not-issued", "phrase": "RESET", "explicit": True},
            {"challenge": "not-issued", "phrase": "reset", "explicit": True},
            {"challenge": "not-issued", "phrase": "RESET", "explicit": False},
        ):
            with self.assertRaises(SetupActionError):
                h.actions.commit_reset(**kwargs)
            self.assertEqual(h.snapshot(), before)
        self.assertFalse((h.backup_root / "backups").exists())

    def test_wrong_challenge_with_issued_plan_writes_nothing(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        before = h.snapshot()
        with self.assertRaises(SetupActionError) as caught:
            h.actions.commit_reset(challenge="wrong-token", phrase="RESET", explicit=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(h.snapshot(), before)
        self.assertFalse((h.backup_root / "backups").exists())

    def test_concurrent_edit_makes_confirmation_stale(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        edited = copy.deepcopy(h.ctx.settings["user_config"])
        edited["timezone"] = "Asia/Tokyo"
        h.ctx.settings["user_config"] = edited
        before = h.snapshot()
        with self.assertRaises(SetupActionError) as caught:
            h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        self.assertEqual(caught.exception.code, "stale_confirmation")
        after = h.snapshot()
        self.assertEqual(after["settings"], before["settings"])
        self.assertEqual(after["prefs"], before["prefs"])
        self.assertEqual(after["reminders"], before["reminders"])
        self.assertFalse((h.backup_root / "backups").exists())
        self.assertEqual(h.ctx.state.get("setup_reset_challenge"), None)

    def test_challenge_is_one_time(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        before = h.snapshot()
        with self.assertRaises(SetupActionError) as caught:
            h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(h.snapshot(), before)


class ResetOutcomeTests(unittest.TestCase):
    def test_reset_clears_preferences_and_preserves_exact_scope(self):
        h = Harness()
        original = owner_config()
        plan = h.actions.prepare_reset()
        self.assertEqual(plan["cleared"], list(CLEARED_FIELDS))
        self.assertEqual(plan["preserved"], list(PRESERVED_FIELDS))
        result = h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        self.assertTrue(result["changed"])

        saved = userconfig.validate_config(h.ctx.settings["user_config"])
        for key in ("mail", "calendar", "tasks", "public_sources", "health",
                    "bounds", "adapters", "retrieval"):
            self.assertEqual(saved[key], original[key], key)
        self.assertEqual(saved["advanced"]["legacy_policy"],
                         original["advanced"]["legacy_policy"])
        provenance = saved["advanced"]["onboarding_provenance"]
        self.assertTrue(provenance["neutral_init"])
        self.assertEqual(provenance["known_fields"], [])
        self.assertEqual(provenance["baseline"]["timezone"], "UTC")
        self.assertEqual(saved["timezone"], "UTC")
        self.assertEqual(saved["notification"], userconfig.default_config()["notification"])
        self.assertFalse(any(saved["sources"].values()))
        self.assertEqual(saved["public_interests"], [])
        self.assertEqual(saved["adaptive"]["mode"], "disabled")
        self.assertFalse(saved["preparation"]["enabled"])

        # Model route flat controls and reminders are not reset.
        self.assertEqual(h.ctx.settings["primary_provider"], "configured-provider")
        self.assertEqual(h.ctx.settings["primary_model"], "configured-model")
        self.assertEqual(h.ctx.settings["fallback_provider"], "fallback-provider")
        self.assertEqual(h.ctx.settings["fallback_model"], "fallback-model")
        self.assertEqual(json.loads(h.reminders_path.read_text(encoding="utf-8")),
                         {"reminders": {"r1": {"status": "pending"}}})
        self.assertEqual(h.ctx.state.get("unrelated_runtime_key"), {"keep": "me"})
        self.assertIsNone(h.ctx.state.get("onboarding_state"))

        prefs = json.loads(h.prefs_path.read_text(encoding="utf-8"))
        self.assertEqual(prefs["explicit"], [])
        self.assertEqual(prefs["inferred"], [])
        self.assertEqual(prefs["snoozes"], [])
        self.assertEqual(prefs["paused"], {"active": True, "reason": "synthetic"})

        backup = h.backup_root / "backups" / result["backup_id"]
        self.assertTrue(backup.is_dir())
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o700")
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        for name, entry in manifest["files"].items():
            data = (backup / name).read_bytes()
            self.assertEqual(len(data), entry["bytes"])
            import hashlib
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            self.assertEqual(oct((backup / name).stat().st_mode & 0o777), "0o600")
        backup_config = json.loads((backup / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(backup_config["mail"], original["mail"])
        self.assertNotIn("primary_provider", json.dumps(backup_config))
        self.assertFalse((backup / "reminders.json").exists())

    def test_backup_failure_prevents_every_write(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        before = h.snapshot()
        h.backup_root.mkdir(parents=True, exist_ok=True)
        (h.backup_root / "backups").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(Exception):
            h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        self.assertEqual(h.snapshot(), before)

    def test_write_failure_rolls_back(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        before = h.snapshot()
        # Fail the first projection write after user_config has been replaced.
        h.ctx.fail_keys.add("timezone")
        with self.assertRaises(SetupActionError) as caught:
            h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        self.assertEqual(caught.exception.code, "reset_failed")
        h.ctx.fail_keys.clear()
        # Rollback itself re-writes timezone, so compare effective scope/preferences
        # and state rather than the raw write log.
        restored = userconfig.load_user_config(h.config_path, native=h.ctx)
        self.assertEqual(restored.config["mail"], owner_config()["mail"])
        self.assertEqual(restored.config["public_sources"], owner_config()["public_sources"])
        self.assertEqual(restored.config["timezone"], "Europe/London")
        restored_prefs = json.loads(h.prefs_path.read_text(encoding="utf-8"))
        for key in ("explicit", "inferred", "snoozes", "paused"):
            self.assertEqual(restored_prefs[key], h.preferences[key], key)
        self.assertEqual(h.ctx.state.get("onboarding_state"), onboarding_state())
        self.assertEqual(json.loads(h.reminders_path.read_text(encoding="utf-8")),
                         {"reminders": {"r1": {"status": "pending"}}})
        self.assertTrue((h.backup_root / "backups").is_dir())

    def test_reset_projection_matches_native_schema_fields(self):
        h = Harness()
        plan = h.actions.prepare_reset()
        result = h.actions.commit_reset(challenge=plan["challenge"], phrase="RESET", explicit=True)
        flat = userconfig.native_flat_values(userconfig.validate_config(h.ctx.settings["user_config"]))
        self.assertEqual(set(result["projected_fields"]), set(flat))
        for key, value in flat.items():
            self.assertEqual(h.ctx.settings[key], value, key)


if __name__ == "__main__":
    unittest.main()
