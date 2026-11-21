"""End-to-end acceptance (fake): novel topic -> broad search -> vetted recurring
source -> real candidate packet. No network, no model: planner/bridge/fetcher are
fakes and DNS is injected.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.adaptive import collect_adaptive  # noqa: E402
from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget, SourceResult  # noqa: E402
from proactive_pilot.planner import FakePlanner, validate_plan  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.publicfeeds import FetchResponse  # noqa: E402
from proactive_pilot.searchbridge import FakeSearchBridge  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action  # noqa: E402

NOW = "2026-10-03T12:00:00"
DISCOVERED = "https://www.university.example/news/GetStories/?page=1"
PAGE = (
    "<html><head><title>Research update</title>"
    "<meta name=\"description\" content=\"quantum networking\"></head>"
    "<body><a href=\"/news/detail/quantum\">Quantum networking lab opens</a></body></html>"
).encode()


def _policy():
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["adaptive_mode"] = "active"
    policy["adaptive_broad_discovery_enabled"] = True
    policy["adaptive_fetch_max_sources_per_run"] = 4
    policy["feed_max_sources_per_run"] = 4
    policy["native_search_max_discovered_sources"] = 2
    return policy


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        for key, value in self.responses.items():
            if url.startswith(key):
                return value
        raise AssertionError(f"unexpected fetch: {url}")


def _resolver(host):
    return ["93.184.216.34"]


class FinalAdaptiveBroadTests(unittest.TestCase):
    def test_novel_topic_reaches_vetted_source_and_packet_candidate(self):
        clock = SeededClock(NOW)
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), clock)
            apply_action(store, {"action": "add", "public_topic": "quantum networking",
                                 "scope": "public", "provenance": "explicit request"},
                         clock.now())
            planner = FakePlanner({"schema": 1, "queries": ["quantum networking"],
                                   "scope": "public", "semantic": True, "provenance": "model"})
            bridge = FakeSearchBridge({
                "quantum networking": {"success": True, "data": {"web": [
                    {"title": "UNIVERSITY research", "url": DISCOVERED, "description": "d"}]}}})
            fetcher = FakeFetcher({
                "https://www.reddit.com/search.rss": FetchResponse(200, {}, b"<rss></rss>", ""),
                "https://api.github.com/search/repositories": FetchResponse(200, {}, b'{"items": []}', ""),
                DISCOVERED: FetchResponse(200, {"ETag": "e1"}, PAGE, DISCOVERED),
            })
            policy = _policy()
            summary = collect_adaptive(
                policy, clock, store=store, planner=planner, fetcher=fetcher,
                budget=Budget(30), search_bridge=bridge, resolver=_resolver)
            self.assertEqual(summary["status"], "ok")
            # The discovered vetted page is a live recurring source.
            results = summary["results"]
            self.assertTrue(any(DISCOVERED in f for f in fetcher.calls), fetcher.calls)
            packet = build_packet([r for r in results if isinstance(r, SourceResult)],
                                  clock=clock, policy=policy, baseline={})
            subjects = " | ".join(c["subject"] for c in packet["candidates"])
            self.assertIn("Quantum networking lab opens", subjects)

    def test_backend_down_registers_no_source_and_is_not_ok(self):
        clock = SeededClock(NOW)
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), clock)
            apply_action(store, {"action": "add", "public_topic": "quantum networking",
                                 "scope": "public", "provenance": "explicit request"},
                         clock.now())
            planner = FakePlanner({"schema": 1, "queries": ["quantum networking"],
                                   "scope": "public", "semantic": True, "provenance": "model"})
            bridge = FakeSearchBridge({
                "quantum networking": {"success": False, "error": "provider down"}})
            fetcher = FakeFetcher({
                "https://www.reddit.com/search.rss": FetchResponse(200, {}, b"<rss></rss>", ""),
                "https://api.github.com/search/repositories": FetchResponse(200, {}, b'{"items": []}', ""),
            })
            policy = _policy()
            summary = collect_adaptive(
                policy, clock, store=store, planner=planner, fetcher=fetcher,
                budget=Budget(30), search_bridge=bridge, resolver=_resolver)
            self.assertNotEqual(summary["status"], "failed")
            discovery = summary["planned"][0]["discovery"]
            self.assertEqual(discovery["status"], "failed")
            self.assertEqual(discovery["discovered"], 0)
            # No discovered URL was ever fetched.
            self.assertFalse(any("university.example" in c for c in fetcher.calls))


if __name__ == "__main__":
    unittest.main()
