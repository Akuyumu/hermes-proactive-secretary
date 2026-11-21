"""Repair pass 2, Defect 1: connector-library path resolution + capability status.

Offline only: no network, no real sources, no installs. Exercises the resolution
order, the fail-closed message when the library is absent, that public-source
monitoring still works without the library, and that ``doctor``/``status`` report
the capability instead of crashing.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import collectors  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FetchResponse,
    collect_public_feeds,
)
from proactive_pilot.state import StateStore  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

ENV_KEYS = ("HOME", "HERMES_HOME", "SECRETARY_SRC", "PRODUCTIVITY_SECRETARY_SRC",
            "HERMES_PROACTIVE_PORTAL_DATA")


def _load_plugin_package():
    name = "hermes_proactive_portal_repair2_connectors"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
sc = importlib.import_module(f"{_PKG.__name__}.portal.portal_cli")
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")


class _EnvMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.hermes = self.root / "hermes-home"
        self.data = self.root / "data"
        self.home.mkdir()
        self.hermes.mkdir()
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)
        os.environ["HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.hermes)
        os.environ["HERMES_PROACTIVE_PORTAL_DATA"] = str(self.data)
        os.environ.pop("SECRETARY_SRC", None)
        os.environ.pop("PRODUCTIVITY_SECRETARY_SRC", None)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _touch(path: Path) -> str:
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class ResolutionOrderTests(_EnvMixin):
    def test_candidate_order_is_env_then_hermes_then_home(self):
        os.environ["SECRETARY_SRC"] = "/tmp/explicit-src"
        candidates = collectors.secretary_src_candidates()
        self.assertEqual(candidates[0], "/tmp/explicit-src")
        self.assertEqual(candidates[1], str(self.hermes / "productivity-secretary" / "src"))
        self.assertEqual(candidates[2], str(self.home / "productivity-secretary" / "src"))

    def test_explicit_env_override_wins(self):
        self._touch(self.hermes / "productivity-secretary" / "src")
        override = self._touch(self.root / "override-src")
        os.environ["SECRETARY_SRC"] = override
        self.assertEqual(collectors.resolve_secretary_src(), override)

    def test_alt_env_override_used_when_primary_absent(self):
        alt = self._touch(self.root / "alt-src")
        os.environ["PRODUCTIVITY_SECRETARY_SRC"] = alt
        self.assertEqual(collectors.resolve_secretary_src(), alt)

    def test_hermes_home_candidate_preferred_over_plain_home(self):
        hermes_lib = self._touch(self.hermes / "productivity-secretary" / "src")
        self._touch(self.home / "productivity-secretary" / "src")
        self.assertEqual(collectors.resolve_secretary_src(), hermes_lib)

    def test_plain_home_fallback_when_hermes_candidate_absent(self):
        plain = self._touch(self.home / "productivity-secretary" / "src")
        self.assertEqual(collectors.resolve_secretary_src(), plain)

    def test_no_candidate_falls_back_to_first_candidate_name(self):
        # Nothing exists: resolution names candidate #1 for diagnostics only.
        self.assertEqual(collectors.resolve_secretary_src(),
                         collectors.secretary_src_candidates()[0])


class MissingLibraryFailClosedTests(_EnvMixin):
    def test_message_names_library_and_env_override(self):
        message = collectors.secretary_library_error()
        self.assertTrue(message)
        self.assertIn("productivity-secretary", message)
        self.assertIn("SECRETARY_SRC", message)
        self.assertIn("Public-source monitoring", message)

    def test_insert_raises_typed_error_not_bare_importerror(self):
        with self.assertRaises(collectors.PortalLibraryUnavailable) as ctx:
            collectors._insert_secretary_src()
        self.assertIn("SECRETARY_SRC", str(ctx.exception))

    def test_calendar_fails_closed_with_actionable_error(self):
        result = collectors.collect_calendar("primary", clock=SeededClock(NOW.isoformat()))
        self.assertEqual(result.status, "error")
        self.assertFalse(result.healthy)
        self.assertEqual(result.items, [])
        self.assertIn("productivity-secretary", result.error)
        self.assertIn("SECRETARY_SRC", result.error)

    def test_tasks_fails_closed_with_actionable_error(self):
        result = collectors.collect_tasks("primary", clock=SeededClock(NOW.isoformat()))
        self.assertEqual(result.status, "error")
        self.assertFalse(result.healthy)
        self.assertIn("SECRETARY_SRC", result.error)


class _RssFetcher:
    def __init__(self, url: str):
        self.url = url
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        return FetchResponse(200, {}, RSS, url)


RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
       b'<item><title>Public item</title><link>https://example.com/x</link>'
       b'<pubDate>Mon, 05 Oct 2026 11:00:00 +0000</pubDate><guid>x1</guid>'
       b'</item></channel></rss>')


class PublicOnlyRunTests(_EnvMixin):
    def _policy(self):
        policy = load_policy(None)
        policy["feed_seed_sources"] = [{
            "kind": "reddit_rss", "url": "https://www.reddit.com/r/hermes/.rss",
            "provenance": "test", "interest": "test", "cadence_minutes": 60,
        }]
        return policy

    def test_public_feeds_still_work_with_library_absent(self):
        # Library absent (no candidate created): calendar/tasks fail closed...
        self.assertTrue(collectors.secretary_library_error())
        self.assertEqual(
            collectors.collect_calendar("primary", clock=SeededClock(NOW.isoformat())).status,
            "error",
        )
        # ...while public-source monitoring keeps working.
        store = StateStore(self.root / "state", SeededClock(NOW.isoformat()))
        fetcher = _RssFetcher("https://www.reddit.com/r/hermes/.rss")
        results = collect_public_feeds(self._policy(), SeededClock(NOW.isoformat()),
                                       store=store, fetcher=fetcher)
        feeds = [r for r in results if r.name.startswith("feed:")]
        self.assertTrue(feeds, "public feed must still be collected without the library")
        self.assertTrue(feeds[0].healthy)
        self.assertEqual(feeds[0].count, 1)

    def test_expanded_sources_builds_without_library(self):
        factories = collectors.expanded_sources(self._policy(), SeededClock(NOW.isoformat()))
        # Core mail + feed + obligation factories still build; nothing raises.
        self.assertGreaterEqual(len(factories), 3)


class CapabilityReportingTests(_EnvMixin):
    def setUp(self):
        super().setUp()
        paths.ensure_data_dirs()
        self._orig_backend = sc._backend
        self._orig_which = sc.shutil.which
        # Force every optional connector absent so the public-only path is exercised.
        sc.shutil.which = lambda _name: None
        self.addCleanup(lambda: setattr(sc.shutil, "which", self._orig_which))
        self.addCleanup(lambda: setattr(sc, "_backend", self._orig_backend))

    def tearDown(self):
        sc._backend = self._orig_backend
        sc.shutil.which = self._orig_which

    def _ns(self):
        return argparse.Namespace(json=True, data_dir=None)

    def _capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*args, **kwargs)
        return rc, json.loads(buf.getvalue())

    def test_detect_capabilities_public_only(self):
        caps = sc.detect_capabilities()
        self.assertFalse(caps["productivity_secretary"])
        self.assertFalse(caps["calendar_tasks"])
        self.assertTrue(caps["public_sources"])
        self.assertEqual(caps["monitoring"], "public-only")
        self.assertTrue(any("productivity-secretary" in c
                            for c in caps["secretary_src_candidates"]))

    def test_doctor_reports_capability_not_crash(self):
        sc._backend = lambda: cron_mod.InMemoryCronBackend()
        rc, payload = self._capture(sc.doctor_command, self._ns())
        self.assertEqual(rc, 0, "a missing optional connector must not fail doctor")
        checks = {c["check"]: c for c in payload["checks"]}
        self.assertFalse(checks["productivity_secretary"]["ok"])
        self.assertFalse(checks["productivity_secretary"]["critical"])
        self.assertIn("SECRETARY_SRC", checks["productivity_secretary"]["detail"])
        self.assertEqual(payload["capabilities"]["monitoring"], "public-only")
        self.assertTrue(checks["public_sources"]["ok"])

    def test_status_says_public_only_with_env_hint(self):
        sc._backend = lambda: cron_mod.InMemoryCronBackend()
        rc, payload = self._capture(sc.status_command, self._ns())
        self.assertEqual(rc, 0)
        self.assertEqual(payload["monitoring"], "public-only")
        self.assertFalse(payload["capabilities"]["productivity_secretary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
