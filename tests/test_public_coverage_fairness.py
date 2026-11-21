"""Focused coverage-fairness tests (fake only, no network)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import publicfeeds as pf  # noqa: E402
from proactive_pilot.adaptive import (PublicAttemptLedger,  # noqa: E402
                                      adaptive_feed_cursor,
                                      advance_adaptive_feed_cursor,
                                      new_adaptive_registry)
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget  # noqa: E402
from proactive_pilot.main import collect_public_and_adaptive  # noqa: E402
from proactive_pilot.planner import FakePlanner  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class _CountingStore:
    def __init__(self, inner):
        self.inner = inner
        self.saves = 0

    def load(self, *args, **kwargs):
        return self.inner.load(*args, **kwargs)

    def save(self, name, data):
        self.saves += 1
        return self.inner.save(name, data)

    def lock(self, *args, **kwargs):
        return self.inner.lock(*args, **kwargs)


def _rec(kind, host, url, *, cache_at="", items=None):
    rid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return {"id": f"{kind}:{rid}", "kind": kind, "url": url, "host": host,
            "cadence_minutes": 1, "last_fetch_at": "", "cache_at": cache_at,
            "cache_items": list(items or []), "failures": 0}


def _store(tmp):
    return StateStore(Path(tmp), SeededClock(NOW.isoformat()))


class LedgerShares(unittest.TestCase):
    def test_total_two_both_due_one_each(self):
        ledger = PublicAttemptLedger(2, adaptive_first=True)
        self.assertEqual(ledger.remaining_for("adaptive"), 1)
        self.assertEqual(ledger.remaining_for("ordinary"), 1)
        self.assertEqual(ledger.take(1, path="adaptive"), 1)
        self.assertEqual(ledger.remaining_for("ordinary"), 1)
        self.assertEqual(ledger.take(2, path="ordinary"), 1)
        self.assertEqual(ledger.used, 2)

    def test_total_one_alternates(self):
        self.assertEqual(PublicAttemptLedger(1, adaptive_first=True)
                         .remaining_for("adaptive"), 1)
        self.assertEqual(PublicAttemptLedger(1, adaptive_first=True)
                         .remaining_for("ordinary"), 0)
        self.assertEqual(PublicAttemptLedger(1, adaptive_first=False)
                         .remaining_for("ordinary"), 1)

    def test_single_path_gets_full_budget(self):
        ledger = PublicAttemptLedger(2, ordinary_active=False)
        self.assertEqual(ledger.remaining_for("adaptive"), 2)
        ledger = PublicAttemptLedger(3, adaptive_active=False)
        self.assertEqual(ledger.remaining_for("ordinary"), 3)

    def test_unused_share_can_be_borrowed_after_path_done(self):
        ledger = PublicAttemptLedger(2, adaptive_first=True)
        self.assertEqual(ledger.take(1, path="adaptive"), 1)
        ledger.mark_done("adaptive")
        self.assertEqual(ledger.remaining_for("ordinary"), 1)
        ledger2 = PublicAttemptLedger(2, adaptive_first=True)
        ledger2.mark_done("adaptive")
        self.assertEqual(ledger2.remaining_for("ordinary"), 2)


class CursorIsolation(unittest.TestCase):
    def test_per_watch_cursor_is_isolated_and_migrates(self):
        registry = new_adaptive_registry()
        self.assertEqual(adaptive_feed_cursor(registry, "w1", 4), 0)
        advance_adaptive_feed_cursor(registry, "w1", 4, 2)
        self.assertEqual(adaptive_feed_cursor(registry, "w1", 4), 2)
        self.assertEqual(adaptive_feed_cursor(registry, "w2", 4), 0)
        legacy = {"feed_cursor": 3, "sources": {}}
        self.assertEqual(adaptive_feed_cursor(legacy, "w1", 4), 3)
        self.assertEqual(legacy["feed_cursors"]["w1"], 3)


class OrdinaryAttemptCharging(unittest.TestCase):
    def _run(self, registry, *, budget, ledger=None, ledger_limit=0):
        calls = []
        clock = SeededClock(NOW.isoformat())

        def fetch(url, **kwargs):
            calls.append(url)
            if "api.github.com" in url:
                return pf.FetchResponse(200, {"Content-Type": "application/json"},
                                        b'{"items": []}', url)
            raise AssertionError(f"unexpected fetch: {url}")

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = _store(tmp)
            pf.save_registry(store, registry)
            results = pf.collect_public_feeds(
                {"feeds_enabled": True, "feed_max_sources_per_run": 1},
                clock, store=store, budget=budget, fetcher=fetch,
                attempt_ledger=ledger, reddit_ledger=pf.RedditLedger(ledger_limit))
            saved = pf.load_registry(store)
        return results, saved, calls

    def test_skipped_row_does_not_charge_and_rotation_advances(self):
        registry = {"schema": 1, "sources": {
            "a_reddit": _rec("reddit_search", "www.reddit.com",
                             "https://www.reddit.com/r/a/.rss"),
            "b_github": _rec("github_search", "api.github.com",
                             "https://api.github.com/search/repositories?q=g2"),
            "c_github": _rec("github_search", "api.github.com",
                             "https://api.github.com/search/repositories?q=g3"),
        }}
        ledger = PublicAttemptLedger(2, adaptive_active=False)
        results, saved, calls = self._run(registry, budget=Budget(30),
                                          ledger=ledger, ledger_limit=0)
        self.assertEqual(calls, ["https://api.github.com/search/repositories?q=g2"])
        self.assertEqual(ledger.used, 1)
        self.assertIn("reddit", results[0].failure)
        self.assertIs(results[0].attempted, False)
        self.assertEqual(saved["feed_cursor"], 2)
        self.assertIs(results[-1].attempted, False)
        self.assertIn("per-run", results[-1].failure)

    def test_tiny_positive_budget_defers_without_http_or_metadata_churn(self):
        rec = _rec("github_search", "api.github.com",
                   "https://api.github.com/search/repositories?q=g")
        registry = {"schema": 1, "sources": {"g": rec}}

        def fetch(url, **kwargs):
            raise AssertionError("tiny budget must not fetch")

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = _store(tmp)
            pf.save_registry(store, registry)
            results = pf.collect_public_feeds(
                {"feeds_enabled": True, "feed_max_sources_per_run": 2},
                SeededClock(NOW.isoformat()), store=store, budget=Budget(0.25),
                fetcher=fetch)
            saved = pf.load_registry(store)
        self.assertIs(results[0].attempted, False)
        self.assertIn("budget deferred", results[0].failure)
        self.assertEqual(saved["sources"]["g"]["last_fetch_at"], "")
        self.assertEqual(saved["sources"]["g"]["failures"], 0)
        self.assertEqual(saved["sources"]["g"].get("stale_reason", ""), "")

    def test_changing_due_membership_still_advances_by_source_identity(self):
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            if "reddit.com" in url:
                raise pf.FetchFailed("http 429")
            return pf.FetchResponse(200, {"Content-Type": "application/json"},
                                    b'{"items": []}', url)

        registry = {"schema": 1, "updated_at": "", "feed_cursor": 0, "sources": {
            "a_reddit": _rec("reddit_rss", "www.reddit.com",
                             "https://www.reddit.com/r/a/.rss"),
            "b_reddit": _rec("reddit_rss", "www.reddit.com",
                             "https://www.reddit.com/r/b/.rss"),
            "c_github_aa": _rec("github_search", "api.github.com",
                                "https://api.github.com/search/repositories?q=aa"),
            "d_github_bb": _rec("github_search", "api.github.com",
                                "https://api.github.com/search/repositories?q=bb"),
        }}
        policy = {"feeds_enabled": True, "feed_max_sources_per_run": 2,
                  "discovery_topics_cap": 0}
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            inner = _store(tmp)
            pf.save_registry(inner, registry)
            clock = SeededClock(NOW.isoformat())
            pf.collect_public_feeds(policy, clock, store=inner, fetcher=fetch)
            first = list(calls)
            self.assertEqual(len(first), 2)
            self.assertIn("q=aa", first[1])
            # The fetched aa source is no longer due on the next cycle, so the
            # previous numeric index would alias back to the head.
            saved = pf.load_registry(inner)
            for rec in saved["sources"].values():
                if "q=aa" in str(rec.get("url") or ""):
                    rec["cadence_minutes"] = 60
            pf.save_registry(inner, saved)
            later = SeededClock((NOW + timedelta(minutes=5)).isoformat())
            pf.collect_public_feeds(policy, later, store=inner, fetcher=fetch)
            second = calls[len(first):]
        self.assertTrue(second)
        self.assertIn("q=bb", second[0])
        self.assertNotIn("reddit.com", second[0])

    def test_expired_structured_cache_stays_stale_when_deferred(self):
        rec = _rec("university_json", "www.university.example", "https://www.university.example/news/",
                   cache_at=(NOW - timedelta(minutes=10)).isoformat(),
                   items=[{"category": "weather_forecast",
                           "valid_from": (NOW - timedelta(hours=2)).isoformat(),
                           "valid_until": (NOW - timedelta(hours=1)).isoformat()}])
        registry = {"schema": 1, "sources": {"s": rec}}
        results, _saved, _calls = self._run(registry, budget=Budget(0.25),
                                            ledger=None, ledger_limit=1)
        self.assertEqual(results[0].status, "stale")
        self.assertIn("structured cache expired", results[0].failure)

    def test_pure_budget_deferral_leaves_registry_and_save_count_unchanged(self):
        import copy
        rec = _rec("github_releases", "api.github.com",
                   "https://api.github.com/repos/o/r/releases")
        registry = {"schema": 1, "updated_at": "", "feed_cursor": 0,
                    "sources": {"only": rec}}

        def fetch(url, **kwargs):
            raise AssertionError("deferred budget must not fetch")

        for remaining in (0.0, 0.75):
            with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
                inner = _store(tmp)
                pf.save_registry(inner, registry)
                before = copy.deepcopy(pf.load_registry(inner))
                store = _CountingStore(inner)
                results = pf.collect_public_feeds(
                    {"feeds_enabled": True, "feed_max_sources_per_run": 2,
                     "discovery_topics_cap": 0},
                    SeededClock(NOW.isoformat()), store=store,
                    budget=Budget(remaining), fetcher=fetch)
                after = pf.load_registry(inner)
            self.assertEqual(results[0].attempted, False)
            self.assertEqual(after, before)
            self.assertEqual(store.saves, 0)


class MainIntegratedQuotaSplit(unittest.TestCase):
    def _run(self, total, adaptive_first):
        clock = SeededClock(NOW.isoformat())
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            if "reddit.com" in url:
                return pf.FetchResponse(200, {}, b"<rss></rss>", url)
            return pf.FetchResponse(200, {"Content-Type": "application/json"},
                                    b'{"items": []}', url)

        planner = FakePlanner({"schema": 1, "queries": ["quantum"], "scope": "public",
                               "semantic": True, "provenance": "model"})
        policy = {"feeds_enabled": True, "feed_max_sources_per_run": total,
                  "adaptive_mode": "active", "adaptive_fetch_max_sources_per_run": 2,
                  "adaptive_cadence_minutes": 360, "public_budget_seconds": 30,
                  "obligation_budget_seconds": 15, "feed_timeout_seconds": 5}
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = _store(tmp)
            pf.save_registry(store, {"schema": 1, "updated_at": "", "sources": {
                "ord": _rec("github_releases", "api.github.com",
                            "https://api.github.com/repos/o/r/releases")}})
            apply_action(store, {"action": "add", "public_topic": "quantum",
                                 "scope": "public", "provenance": "explicit"},
                         clock.now())
            collect_public_and_adaptive(
                policy, clock, store=store, context=[], public_budget=Budget(30),
                planner=planner, fetcher=fetch, public_fetcher=fetch,
                overall_budget=Budget(60), adaptive_first=adaptive_first)
        return calls

    def test_total_one_rotates_to_one_path(self):
        adaptive = self._run(1, True)
        ordinary = self._run(1, False)
        self.assertEqual(len(adaptive), 1)
        self.assertEqual(len(ordinary), 1)
        self.assertIn("reddit.com", adaptive[0])
        self.assertIn("api.github.com", ordinary[0])

    def test_total_two_gives_both_paths_one_and_keeps_combined_cap(self):
        adaptive = self._run(2, True)
        ordinary = self._run(2, False)
        self.assertEqual(len(adaptive), 2)
        self.assertEqual(len(ordinary), 2)
        self.assertEqual(sum("reddit.com" in url for url in adaptive), 1)
        self.assertEqual(sum("reddit.com" in url for url in ordinary), 1)


if __name__ == "__main__":
    unittest.main()
