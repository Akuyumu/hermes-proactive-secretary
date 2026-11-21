"""V0.4.0 routed public-retrieval reliability tests (production collector path).

Every case runs through the ordinary production collector
(``collect_public_feeds`` -> ``_collect_one`` -> ``parse_source``), never a
router helper alone, so the wiring -- not just the classifier -- is exercised.
All clients are injected fakes; no socket, no network, no secret file, no model.
"""
from __future__ import annotations

import errno
import json
import multiprocessing
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import publicfeeds as pf  # noqa: E402
from proactive_pilot import routed_retrieval as rr  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.publicfeeds import FetchResponse, collect_public_feeds  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=SG)

PAGE_URL = "https://fixtures.example.org/page-1"
PRIVATE_URL = "https://10.0.0.5/hidden"

GOOD_HTML = (
    b"<html><head><title>Campus update</title>"
    b"<meta name=\"description\" content=\"A real public page\"></head>"
    b"<body><a href=\"https://www.university.example/news/x\">Latest news</a></body></html>"
)
JS_SHELL = b"<html><body>JavaScript is required to view this content.</body></html>"
BLOCKED_PAGE = b"<html><body>Enable JavaScript and cookies to continue</body></html>"


class _Fetcher:
    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        return FetchResponse(200, {"Content-Type": "text/html"}, self.body, url)


class _Renderer:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _MCPCtx:
    """Minimal stand-in for the host-built PluginContext ``ctx.call_mcp`` lane."""

    def __init__(self, response=None, *, raise_exc=None):
        self.responses = list(response) if isinstance(response, list) else [response]
        self.raise_exc = raise_exc
        self.calls = []

    def call_mcp(self, server, tool, arguments=None, timeout=30):
        self.calls.append((server, tool, dict(arguments or {}), timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _fc_markdown(markdown):
    return {"ok": True, "result": {"markdown": markdown}}


def _fc_content_json(markdown):
    return {"ok": True, "result": [
        {"type": "text", "text": json.dumps({"markdown": markdown})}]}


def _fc_error(message):
    return {"ok": False, "error": message}


def _seed():
    return {
        "kind": "web_page", "url": PAGE_URL,
        "provenance": "v0.4.0 reliability fixture", "interest": "campus",
        "cadence_minutes": 360,
    }


def _fc_policy(**over):
    p = _policy(
        firecrawl_enabled=True, firecrawl_consent=True,
        firecrawl_mcp_server="firecrawl", firecrawl_mcp_tool="firecrawl_scrape",
        firecrawl_max_attempts_per_cycle=1, firecrawl_max_attempts_per_day=10,
        firecrawl_concurrency=1, firecrawl_cooldown_minutes=60,
        firecrawl_cache_ttl_minutes=0,
    )
    p.update(over)
    return p


def _adapter(ctx, *, policy=None, tmpdir=None, now=NOW):
    store = StateStore(Path(tmpdir), SeededClock(now.isoformat())) if tmpdir else None
    return rr.FirecrawlMCPAdapter(policy if policy is not None else _fc_policy(),
                                  ctx=ctx, store=store, now=now)


def _policy(**over):
    p = {
        "feeds_enabled": True,
        "feed_max_sources_per_run": 1,
        "feed_max_items": 5,
        "feed_timeout_seconds": 5,
        "feed_max_bytes": 200_000,
        "generic_websites_opt_in": True,
        "feed_seed_sources": [_seed()],
    }
    p.update(over)
    return p


class _Pipeline:
    def __init__(self, body, *, renderer=None, policy=None, firecrawl=None):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.store = StateStore(Path(self.tmp.name), SeededClock(NOW.isoformat()))
        self.fetcher = _Fetcher(body)
        self.renderer = renderer
        self.firecrawl = firecrawl
        self.policy = policy if policy is not None else _policy()

    def run(self, when: datetime = NOW):
        kwargs = {}
        if self.renderer is not None:
            kwargs["renderer"] = self.renderer
        if self.firecrawl is not None:
            kwargs["firecrawl"] = self.firecrawl
        return collect_public_feeds(
            self.policy, SeededClock(when.isoformat()), store=self.store,
            fetcher=self.fetcher, **kwargs)

    def close(self):
        self.tmp.cleanup()


class JsShellFallbackTests(unittest.TestCase):
    def test_consented_renderer_recovers_public_js_shell(self):
        renderer = _Renderer(GOOD_HTML)
        pipe = _Pipeline(JS_SHELL, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok", results[0].failure)
        self.assertTrue(results[0].items)
        self.assertEqual(renderer.calls, [PAGE_URL])
        # Only the public URL is forwarded: no cookie/auth/context argument.
        self.assertIn(PAGE_URL, pipe.fetcher.calls)

    def test_js_shell_not_rendered_without_consent(self):
        renderer = _Renderer(GOOD_HTML)
        pipe = _Pipeline(JS_SHELL, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=False))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(renderer.calls, [])
        self.assertNotEqual(results[0].status, "ok")
        self.assertIn("web page dynamic", results[0].failure)
        self.assertEqual(results[0].items, [])

    def test_blocked_challenge_is_terminal_and_never_rendered(self):
        renderer = _Renderer(GOOD_HTML)
        pipe = _Pipeline(BLOCKED_PAGE, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(renderer.calls, [], "a challenge/interstitial must never be rendered")
        self.assertNotEqual(results[0].status, "ok")
        self.assertIn("web page blocked", results[0].failure)
        self.assertEqual(results[0].items, [])

    def test_renderer_returning_interstitial_is_not_evidence(self):
        renderer = _Renderer(BLOCKED_PAGE)
        pipe = _Pipeline(JS_SHELL, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(renderer.calls, [PAGE_URL])
        self.assertNotEqual(results[0].status, "ok")
        self.assertEqual(results[0].items, [])

    def test_failed_renderer_keeps_honest_failure(self):
        renderer = _Renderer(RuntimeError("browser crashed"))
        pipe = _Pipeline(JS_SHELL, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertNotEqual(results[0].status, "ok")
        self.assertIn("web page dynamic", results[0].failure)

    def test_unavailable_fallback_is_not_faked(self):
        # Flags on but NO approved renderer injected: candid failure, no success.
        pipe = _Pipeline(JS_SHELL, renderer=None, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertNotEqual(results[0].status, "ok")
        self.assertEqual(results[0].items, [])

    def test_direct_route_unchanged_without_renderer(self):
        pipe = _Pipeline(GOOD_HTML, renderer=None)
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(results[0].status, "ok")
        self.assertTrue(results[0].items)
        self.assertFalse(results[0].cached)

    def test_render_fallback_respects_cadence_cache(self):
        renderer = _Renderer(GOOD_HTML)
        pipe = _Pipeline(JS_SHELL, renderer=renderer, policy=_policy(
            native_render_fallback_enabled=True, native_render_consent=True))
        try:
            pipe.run(NOW)
            within = pipe.run(NOW.replace(minute=1))
        finally:
            pipe.close()
        self.assertEqual(renderer.calls, [PAGE_URL], "cached cadence must not re-render")
        self.assertTrue(within[0].cached)
        self.assertEqual(within[0].status, "ok")
        self.assertEqual(len(pipe.fetcher.calls), 1, "cadence must not refetch")


class SsrfAndPrivateUrlTests(unittest.TestCase):
    def test_render_helper_rejects_private_url(self):
        renderer = _Renderer(GOOD_HTML)
        policy = {"native_render_fallback_enabled": True, "native_render_consent": True}
        with self.assertRaises(rr.RenderUnavailable):
            rr.render_js_shell(PRIVATE_URL, renderer=renderer, policy=policy)
        self.assertEqual(renderer.calls, [])

    def test_private_seed_never_enters_registry_and_never_renders(self):
        renderer = _Renderer(GOOD_HTML)
        seed = {"kind": "web_page", "url": PRIVATE_URL,
                "provenance": "ssrf probe", "interest": "x", "cadence_minutes": 360}
        policy = _policy(feed_seed_sources=[seed],
                         native_render_fallback_enabled=True, native_render_consent=True)
        pipe = _Pipeline(BLOCKED_PAGE, renderer=renderer, policy=policy)
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(results, [])
        self.assertEqual(renderer.calls, [])
        self.assertEqual(pipe.fetcher.calls, [])


class CapabilityConsentTests(unittest.TestCase):
    def test_capabilities_are_candid_by_default(self):
        caps = rr.capabilities(_policy())
        self.assertTrue(caps["direct_pinned_https"]["available"])
        self.assertFalse(caps["native_render_fallback"]["available"])
        self.assertFalse(caps["firecrawl"]["available"])
        self.assertEqual(caps["firecrawl"]["reason"], "firecrawl disabled by policy")

    def test_native_render_requires_consent_and_a_renderer(self):
        flags = _policy(native_render_fallback_enabled=True)
        avail, reason = rr.native_render_route(flags)
        self.assertFalse(avail)
        self.assertIn("consent", reason)
        consent = _policy(native_render_fallback_enabled=True, native_render_consent=True)
        avail, reason = rr.native_render_route(consent)
        self.assertFalse(avail)
        self.assertIn("not reused", reason, "generic shared browser must not be substituted")
        avail, _ = rr.native_render_route(consent, renderer=_Renderer(GOOD_HTML))
        self.assertTrue(avail)

    def test_firecrawl_off_without_consent_and_config(self):
        avail, _ = rr.firecrawl_route(_policy(firecrawl_enabled=True))
        self.assertFalse(avail)
        # Consent alone is not enough: the server/tool are named by config, never
        # guessed, so the precise unknown is exposed instead of a fake contract.
        avail, reason = rr.firecrawl_route(
            _policy(firecrawl_enabled=True, firecrawl_consent=True), ctx=_MCPCtx())
        self.assertFalse(avail)
        self.assertIn("server/tool not configured", reason)
        avail, _ = rr.firecrawl_route(_fc_policy(), ctx=_MCPCtx())
        self.assertTrue(avail)

    def test_firecrawl_needs_native_call_mcp_not_a_key(self):
        # A named server/tool with NO host ctx.call_mcp is unavailable: the host
        # owns the OAuth connection and no API key is duplicated or required.
        avail, reason = rr.firecrawl_route(_fc_policy())
        self.assertFalse(avail)
        self.assertIn("no native ctx.call_mcp", reason)

    def test_firecrawl_capabilities_expose_schema_unknown(self):
        caps = rr.capabilities(_fc_policy(), ctx=_MCPCtx())
        self.assertTrue(caps["firecrawl"]["available"])
        self.assertFalse(caps["firecrawl"]["schema_verified"])
        self.assertTrue(caps["firecrawl"]["server_configured"])
        self.assertTrue(caps["firecrawl"]["tool_configured"])

    def test_secret_resolution_fails_closed_without_host_surface(self):
        # Staging has no `agent.secret_scope`; resolution must not read a file or
        # os.environ copy, and must not raise.
        self.assertEqual(rr.resolve_native_secret("FIRECRAWL_API_KEY"), "")


FC_MD = (
    "# Campus update\n\nA real public page with a scholarship application "
    "deadline 31 October 2026."
)


class FirecrawlNativeMCPTests(unittest.TestCase):
    def _tmp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_native_mcp_recovers_dynamic_public_page(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        pipe = _Pipeline(JS_SHELL, firecrawl=adapter, policy=_fc_policy())
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "ok", results[0].failure)
        self.assertTrue(results[0].items)
        # Exactly one narrow call: only the public URL and formats=markdown.
        self.assertEqual(len(ctx.calls), 1)
        server, tool, arguments, _timeout = ctx.calls[0]
        self.assertEqual((server, tool), ("firecrawl", "firecrawl_scrape"))
        self.assertEqual(arguments, {"url": PAGE_URL, "formats": ["markdown"]})

    def test_native_mcp_unwraps_text_content_json(self):
        ctx = _MCPCtx(_fc_content_json(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        body = adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn(b"Campus update", body)
        self.assertEqual(len(ctx.calls), 1)

    def test_no_max_age_argument_by_default(self):
        # The documented simple scrape shape is unchanged unless a verified
        # freshness bound is explicitly configured.
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        adapter.scrape(PAGE_URL, now=NOW)
        self.assertEqual(len(ctx.calls), 1)
        self.assertNotIn("maxAge", ctx.calls[0][2])

    def test_verified_schema_max_age_is_sent_when_configured(self):
        # ``maxAge: 0`` requests a live fetch; only ever sent when the owner set
        # a non-negative bound (never a premium/stealth/profile option).
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(
            ctx, tmpdir=self._tmp(),
            policy=_fc_policy(firecrawl_mcp_max_age=0, firecrawl_schema_verified=True))
        body = adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn(b"Campus update", body)
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(
            ctx.calls[0][2],
            {"url": PAGE_URL, "formats": ["markdown"], "maxAge": 0})

    def test_successful_direct_never_calls_firecrawl(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        pipe = _Pipeline(GOOD_HTML, firecrawl=adapter, policy=_fc_policy())
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(ctx.calls, [], "a valid direct response must never fall back")

    def test_blocked_challenge_never_calls_firecrawl(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        pipe = _Pipeline(BLOCKED_PAGE, firecrawl=adapter, policy=_fc_policy())
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(ctx.calls, [])
        self.assertNotEqual(results[0].status, "ok")

    def test_challenge_response_is_not_evidence(self):
        ctx = _MCPCtx(_fc_markdown("Enable JavaScript and cookies to continue"))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        pipe = _Pipeline(JS_SHELL, firecrawl=adapter, policy=_fc_policy())
        try:
            results = pipe.run()
        finally:
            pipe.close()
        self.assertEqual(len(ctx.calls), 1)
        self.assertNotEqual(results[0].status, "ok")
        self.assertEqual(results[0].items, [])

    def test_private_url_denied_before_budget_and_call(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PRIVATE_URL, now=NOW)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(adapter.ledger.describe(NOW)["day_used"], 0)

    def test_per_cycle_ceiling(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp(),
                           policy=_fc_policy(firecrawl_max_attempts_per_cycle=1))
        self.assertIn(b"Campus", adapter.scrape(PAGE_URL, now=NOW))
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("per-cycle", str(caught.exception))
        self.assertEqual(len(ctx.calls), 1)

    def test_daily_ceiling_persists_across_restart(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        policy = _fc_policy(firecrawl_max_attempts_per_day=1,
                            firecrawl_max_attempts_per_cycle=5)
        first = _adapter(ctx, policy=policy, tmpdir=tmp)
        self.assertIn(b"Campus", first.scrape(PAGE_URL, now=NOW))
        # A NEW adapter/process instance on the SAME store is still capped: the
        # reserved attempt was persisted before the call.
        second = _adapter(ctx, policy=policy, tmpdir=tmp)
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            second.scrape(PAGE_URL, now=NOW)
        self.assertIn("daily", str(caught.exception))
        self.assertEqual(len(ctx.calls), 1)

    def test_daily_ceiling_rolls_over_next_utc_day(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        policy = _fc_policy(firecrawl_max_attempts_per_day=1,
                            firecrawl_max_attempts_per_cycle=5)
        _adapter(ctx, policy=policy, tmpdir=tmp).scrape(PAGE_URL, now=NOW)
        later = NOW.replace(day=7)
        reopened = _adapter(ctx, policy=policy, tmpdir=tmp)
        self.assertIn(b"Campus", reopened.scrape(PAGE_URL, now=later))
        self.assertEqual(len(ctx.calls), 2)

    def test_429_sets_cooldown_and_blocks_then_expires(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_error("HTTP 429 Too Many Requests; Retry-After: 120"))
        adapter = _adapter(ctx, tmpdir=tmp,
                           policy=_fc_policy(firecrawl_max_attempts_per_cycle=5))
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("429", str(caught.exception))
        self.assertEqual(len(ctx.calls), 1)
        # A fresh adapter on the same store honours the persisted cooldown.
        later_ctx = _MCPCtx(_fc_markdown(FC_MD))
        blocked = _adapter(later_ctx, tmpdir=tmp,
                           policy=_fc_policy(firecrawl_max_attempts_per_cycle=5))
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            blocked.scrape(PAGE_URL, now=NOW.replace(minute=30))
        self.assertIn("cooldown", str(caught.exception))
        self.assertEqual(later_ctx.calls, [])
        # After the cooldown window the route is allowed again.
        after = NOW + timedelta(hours=2)
        self.assertIn(b"Campus", blocked.scrape(PAGE_URL, now=after))
        self.assertEqual(len(later_ctx.calls), 1)

    def test_concurrency_ceiling(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=self._tmp(),
                           policy=_fc_policy(firecrawl_max_attempts_per_cycle=5,
                                             firecrawl_concurrency=1))
        ok, _ = adapter.ledger.reserve(NOW)
        self.assertTrue(ok)
        held, reason = adapter.ledger.reserve(NOW)
        self.assertFalse(held)
        self.assertIn("concurrency", reason)
        adapter.ledger.release()

    def test_permission_error_is_unavailable_without_leaking(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD), raise_exc=PermissionError("denied: nope"))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PAGE_URL, now=NOW)

    def test_host_call_failure_still_consumes_reserved_attempt(self):
        ctx = _MCPCtx(_fc_markdown(FC_MD), raise_exc=RuntimeError("transient"))
        adapter = _adapter(ctx, tmpdir=self._tmp())
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertEqual(adapter.ledger.describe(NOW)["day_used"], 1)

    def test_build_adapter_is_none_by_neutral_default(self):
        self.assertIsNone(rr.build_firecrawl_adapter(_policy(), ctx=_MCPCtx()))

    def test_stale_ledger_config_fingerprint_not_reset(self):
        # An unrelated config change must not reset the persisted daily counter.
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_markdown(FC_MD), raise_exc=RuntimeError("x"))
        first = _adapter(ctx, tmpdir=tmp,
                         policy=_fc_policy(firecrawl_max_attempts_per_day=1,
                                           firecrawl_max_attempts_per_cycle=5))
        with self.assertRaises(rr.RetrievalUnavailable):
            first.scrape(PAGE_URL, now=NOW)
        # A different unrelated policy key, same metered caps.
        policy2 = _fc_policy(firecrawl_max_attempts_per_day=1,
                             firecrawl_max_attempts_per_cycle=5,
                             feed_max_items=3)
        second = _adapter(ctx, tmpdir=tmp, policy=policy2)
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            second.scrape(PAGE_URL, now=NOW)
        self.assertIn("daily", str(caught.exception))


# --------------------------------------------------------------------------- #
# Cross-process quota atomicity: real multiprocessing (fork context only).
# Every worker builds its own ledger over ONE shared state dir; no socket, no
# network, no model. Deterministic outcomes (retry only the provider-gate busy
# reason), so the shared-lock guarantees are asserted, not timing.
# --------------------------------------------------------------------------- #


def _mp_ledger(store_dir, now_iso, *, per_day, per_cycle, cooldown_minutes=60):
    from proactive_pilot.clock import SeededClock
    from proactive_pilot.routed_retrieval import MeteredAttemptLedger
    from proactive_pilot.state import StateStore

    now = datetime.fromisoformat(now_iso)
    store = StateStore(Path(store_dir), SeededClock(now_iso))
    return MeteredAttemptLedger(store, now=now, per_cycle=per_cycle, per_day=per_day,
                                cooldown_minutes=cooldown_minutes)


def _mp_reserve_worker(store_dir, now_iso, per_day, per_cycle, start_event, out_queue, attempts):
    ledger = _mp_ledger(store_dir, now_iso, per_day=per_day, per_cycle=per_cycle)
    now = datetime.fromisoformat(now_iso)
    start_event.wait(20)
    result = "retries-exhausted"
    for _ in range(int(attempts)):
        ok, reason = ledger.reserve(now)
        if ok:
            ledger.release()
            result = "ok"
            break
        if "concurrency" in reason:
            time.sleep(0.002)
            continue
        result = reason
        break
    out_queue.put(result)


def _mp_hold_worker(store_dir, now_iso, start_event, out_queue, hold_event):
    ledger = _mp_ledger(store_dir, now_iso, per_day=10, per_cycle=10)
    now = datetime.fromisoformat(now_iso)
    start_event.wait(20)
    ok, reason = ledger.reserve(now)
    out_queue.put((ok, reason))
    if ok:
        hold_event.wait(20)
        ledger.release()


def _mp_single_worker(store_dir, now_iso, per_day, per_cycle, out_queue):
    ledger = _mp_ledger(store_dir, now_iso, per_day=per_day, per_cycle=per_cycle)
    now = datetime.fromisoformat(now_iso)
    ok, reason = ledger.reserve(now)
    if ok:
        ledger.release()
    out_queue.put((ok, reason))


def _mp_crash_worker(store_dir, now_iso, out_queue):
    ledger = _mp_ledger(store_dir, now_iso, per_day=5, per_cycle=5)
    now = datetime.fromisoformat(now_iso)
    ok, reason = ledger.reserve(now)
    out_queue.put((ok, reason))
    if ok:
        time.sleep(60)


class CrossProcessQuotaTests(unittest.TestCase):
    def _tmp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(tmp.cleanup)
        return tmp.name

    @staticmethod
    def _ctx():
        return multiprocessing.get_context("fork")

    def test_simultaneous_reservations_never_exceed_daily_ceiling(self):
        tmp = self._tmp()
        now = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        per_day = 3
        workers = 8
        ctx = self._ctx()
        start = ctx.Event()
        queue = ctx.Queue()
        procs = [ctx.Process(target=_mp_reserve_worker,
                             args=(tmp, now.isoformat(), per_day, 100, start, queue, 500))
                 for _ in range(workers)]
        for proc in procs:
            proc.start()
        start.set()
        try:
            results = [queue.get(timeout=60) for _ in range(workers)]
        finally:
            for proc in procs:
                proc.join(60)
        self.assertEqual(results.count("ok"), per_day, results)
        store = StateStore(Path(tmp), SeededClock(now.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=now, per_day=per_day, per_cycle=100)
        self.assertEqual(ledger.describe(now)["day_used"], per_day)

    def test_cross_process_inflight_cap_is_conservative_one(self):
        tmp = self._tmp()
        now = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        ctx = self._ctx()
        start = ctx.Event()
        hold = ctx.Event()
        queue = ctx.Queue()
        workers = 4
        procs = [ctx.Process(target=_mp_hold_worker,
                             args=(tmp, now.isoformat(), start, queue, hold))
                 for _ in range(workers)]
        for proc in procs:
            proc.start()
        start.set()
        try:
            results = [queue.get(timeout=60) for _ in range(workers)]
            self.assertEqual(sum(1 for ok, _ in results if ok), 1, results)
            for ok, reason in results:
                if not ok:
                    self.assertIn("concurrency", reason)
            # The holder still holds the gate: a fresh process cannot enter.
            store = StateStore(Path(tmp), SeededClock(now.isoformat()))
            ledger = rr.MeteredAttemptLedger(store, now=now, per_day=10, per_cycle=10)
            ok, reason = ledger.reserve(now)
            self.assertFalse(ok)
            self.assertIn("concurrency", reason)
        finally:
            hold.set()
            for proc in procs:
                proc.join(60)
        # Gate released after the holder exits: a reservation is possible again.
        store = StateStore(Path(tmp), SeededClock(now.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=now, per_day=10, per_cycle=10)
        ok, reason = ledger.reserve(now)
        self.assertTrue(ok, reason)
        ledger.release()

    def test_cross_process_cooldown_is_honoured(self):
        tmp = self._tmp()
        now = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        store = StateStore(Path(tmp), SeededClock(now.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=now, per_day=10, per_cycle=10,
                                         cooldown_minutes=60)
        ledger.record_429(now, retry_after_seconds=120)
        ctx = self._ctx()
        blocked_at = now + timedelta(minutes=30)
        queue = ctx.Queue()
        proc = ctx.Process(target=_mp_single_worker,
                           args=(tmp, blocked_at.isoformat(), 10, 10, queue))
        proc.start()
        ok, reason = queue.get(timeout=60)
        proc.join(60)
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)
        allowed_at = now + timedelta(hours=2)
        queue2 = ctx.Queue()
        proc2 = ctx.Process(target=_mp_single_worker,
                            args=(tmp, allowed_at.isoformat(), 10, 10, queue2))
        proc2.start()
        ok2, reason2 = queue2.get(timeout=60)
        proc2.join(60)
        self.assertTrue(ok2, reason2)

    def test_cross_process_utc_rollover_resets_daily_ceiling(self):
        tmp = self._tmp()
        day1 = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)
        store = StateStore(Path(tmp), SeededClock(day1.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=day1, per_day=1, per_cycle=5)
        ok, reason = ledger.reserve(day1)
        self.assertTrue(ok, reason)
        ledger.release()
        ctx = self._ctx()
        same_day = day1 + timedelta(minutes=10)
        queue = ctx.Queue()
        proc = ctx.Process(target=_mp_single_worker,
                           args=(tmp, same_day.isoformat(), 1, 5, queue))
        proc.start()
        blocked, blocked_reason = queue.get(timeout=60)
        proc.join(60)
        self.assertFalse(blocked)
        self.assertIn("daily", blocked_reason)
        next_day = day1 + timedelta(hours=1)
        queue2 = ctx.Queue()
        proc2 = ctx.Process(target=_mp_single_worker,
                            args=(tmp, next_day.isoformat(), 1, 5, queue2))
        proc2.start()
        rolled, rolled_reason = queue2.get(timeout=60)
        proc2.join(60)
        self.assertTrue(rolled, rolled_reason)
        reopened = rr.MeteredAttemptLedger(store, now=next_day, per_day=1, per_cycle=5)
        self.assertEqual(reopened.describe(next_day)["day"], "2026-10-07")
        self.assertEqual(reopened.describe(next_day)["day_used"], 1)

    def test_crash_releases_provider_gate_but_keeps_attempt_counted(self):
        tmp = self._tmp()
        now = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        ctx = self._ctx()
        queue = ctx.Queue()
        proc = ctx.Process(target=_mp_crash_worker, args=(tmp, now.isoformat(), queue))
        proc.start()
        ok, reason = queue.get(timeout=60)
        self.assertTrue(ok, reason)
        proc.terminate()
        proc.join(60)
        self.assertFalse(proc.is_alive())
        store = StateStore(Path(tmp), SeededClock(now.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=now, per_day=5, per_cycle=5)
        ok, reason = ledger.reserve(now)
        self.assertTrue(ok, reason)
        ledger.release()
        self.assertEqual(ledger.describe(now)["day_used"], 2)

    def test_corrupt_ledger_fails_closed(self):
        tmp = self._tmp()
        (Path(tmp) / rr.METERED_STATE_FILE).write_text("{ not valid json", encoding="utf-8")
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        ledger = rr.MeteredAttemptLedger(store, now=NOW, per_day=10, per_cycle=10)
        ok, reason = ledger.reserve(NOW)
        self.assertFalse(ok)
        self.assertIn("corrupt", reason)
        self.assertTrue(ledger.describe(NOW)["corrupt"])
        self.assertEqual(ledger.describe(NOW)["day_used"], 0)
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=tmp)
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("corrupt", str(caught.exception))
        self.assertEqual(ctx.calls, [])

    def test_denied_preflight_consumes_no_budget_or_gate(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = _adapter(ctx, tmpdir=tmp)
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PRIVATE_URL, now=NOW)
        self.assertEqual(adapter.ledger.describe(NOW)["day_used"], 0)
        self.assertEqual(ctx.calls, [])
        # Neither the budget nor the provider gate was taken by the denial.
        ok, reason = adapter.ledger.reserve(NOW)
        self.assertTrue(ok, reason)
        adapter.ledger.release()


# --------------------------------------------------------------------------- #
# Fail-closed lock / corruption defects (second independent review).
# A ledger whose shared lock cannot be taken, whose store is corrupt, or whose
# strict load is unsupported must refuse every operation without writing or
# calling a provider -- never silently reset the persisted quota.
# --------------------------------------------------------------------------- #


class _RecordingStore:
    """Wrap a real StateStore and record every save (to assert "no write")."""

    def __init__(self, store):
        self._store = store
        self.root = store.root
        self.saves: list[dict] = []

    def load(self, name, default, strict=False):
        return self._store.load(name, default, strict=strict)

    def save(self, name, payload):
        self.saves.append(dict(payload))
        return self._store.save(name, payload)


class _LegacyStrictStore:
    """Mimics the unsafe old path: strict load raises TypeError and the
    non-strict fallback silently returns the blank default (a quota reset)."""

    def __init__(self, root):
        self.root = Path(root)
        self.saves: list[dict] = []

    def load(self, name, default, strict=False):
        if strict:
            raise TypeError("load() got an unexpected keyword argument 'strict'")
        return dict(default)

    def save(self, name, payload):
        self.saves.append(dict(payload))


class _FailNthSaveStore:
    """Delegate to a real store but fail every save from ``fail_at`` onwards."""

    def __init__(self, store, fail_at):
        self._store = store
        self.root = store.root
        self.fail_at = fail_at
        self.calls = 0

    def load(self, name, default, strict=False):
        return self._store.load(name, default, strict=strict)

    def save(self, name, payload):
        self.calls += 1
        if self.calls >= self.fail_at:
            raise OSError("injected persistence failure")
        return self._store.save(name, payload)


@contextmanager
def _ledger_lock_open_failure_at(fail_from):
    """Fail ``os.open`` of the ledger transaction lock from the Nth call on."""
    real_open = rr.os.open
    seen = {"count": 0}

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(rr.METERED_LOCK_FILE):
            seen["count"] += 1
            if seen["count"] >= fail_from:
                raise OSError(errno.EACCES, "injected ledger lock failure")
        return real_open(path, *args, **kwargs)

    with mock.patch.object(rr.os, "open", side_effect=failing_open):
        yield


@contextmanager
def _ledger_lock_open_failure():
    with _ledger_lock_open_failure_at(1):
        yield


class FailClosedLedgerTests(unittest.TestCase):
    def _tmp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def _store(self, tmp):
        return StateStore(Path(tmp), SeededClock(NOW.isoformat()))

    def _ledger(self, store, **over):
        kw = dict(now=NOW, per_cycle=5, per_day=10, cooldown_minutes=60,
                  cache_ttl_minutes=30)
        kw.update(over)
        return rr.MeteredAttemptLedger(store, **kw)

    def _mutations(self):
        return {
            "reserve": lambda ledger: ledger.reserve(NOW),
            "record_429": lambda ledger: ledger.record_429(NOW, retry_after_seconds=60),
            "record_success": lambda ledger: ledger.record_success(),
            "mark_scraped": lambda ledger: ledger.mark_scraped(PAGE_URL, NOW),
        }

    def test_lock_open_failure_denies_every_ledger_operation_without_write(self):
        operations = dict(self._mutations())
        operations["cache_fresh"] = lambda ledger: ledger.cache_fresh(PAGE_URL, NOW)
        operations["describe"] = lambda ledger: ledger.describe(NOW)
        for name, operation in operations.items():
            with self.subTest(operation=name):
                tmp = self._tmp()
                state_file = Path(tmp) / rr.METERED_STATE_FILE
                recording = _RecordingStore(self._store(tmp))
                ledger = self._ledger(recording)
                with _ledger_lock_open_failure():
                    if name in {"record_429", "record_success", "mark_scraped"}:
                        with self.assertRaises(rr.RetrievalUnavailable) as caught:
                            operation(ledger)
                        self.assertIn("lock unavailable", str(caught.exception))
                    elif name == "reserve":
                        ok, reason = operation(ledger)
                        self.assertFalse(ok)
                        self.assertIn("lock unavailable", reason)
                    elif name == "cache_fresh":
                        fresh, reason = operation(ledger)
                        self.assertFalse(fresh)
                        self.assertIn("lock unavailable", reason)
                    else:  # describe
                        report = operation(ledger)
                        self.assertFalse(report["available"])
                        self.assertIn("lock unavailable", report["unavailable_reason"])
                self.assertEqual(recording.saves, [], name)
                self.assertFalse(state_file.exists(), name)

    def test_corrupt_store_never_reset_by_any_mutation(self):
        payloads = {
            "invalid-json": "{ not valid json",
            "schema-mismatch": json.dumps({"schema": 99, "day_used": 0}),
        }
        for label, raw in payloads.items():
            for name, operation in self._mutations().items():
                with self.subTest(payload=label, operation=name):
                    tmp = self._tmp()
                    state_file = Path(tmp) / rr.METERED_STATE_FILE
                    state_file.write_text(raw, encoding="utf-8")
                    before = state_file.read_text(encoding="utf-8")
                    recording = _RecordingStore(self._store(tmp))
                    ledger = self._ledger(recording)
                    if name == "reserve":
                        ok, reason = operation(ledger)
                        self.assertFalse(ok)
                        self.assertIn("corrupt", reason)
                    else:
                        with self.assertRaises(rr.RetrievalUnavailable) as caught:
                            operation(ledger)
                        self.assertIn("corrupt", str(caught.exception))
                    self.assertEqual(recording.saves, [])
                    self.assertEqual(state_file.read_text(encoding="utf-8"), before)
                    self.assertTrue(ledger.describe(NOW)["corrupt"])

    def test_corrupt_store_cache_fresh_never_claims_fresh(self):
        tmp = self._tmp()
        (Path(tmp) / rr.METERED_STATE_FILE).write_text("{bad", encoding="utf-8")
        ledger = self._ledger(self._store(tmp))
        fresh, reason = ledger.cache_fresh(PAGE_URL, NOW)
        self.assertFalse(fresh)
        self.assertIn("corrupt", reason)

    def test_unsupported_strict_load_fails_closed_without_silent_reset(self):
        tmp = self._tmp()
        legacy = _LegacyStrictStore(Path(tmp))
        ledger = rr.MeteredAttemptLedger(legacy, now=NOW, per_cycle=5, per_day=10,
                                         cache_ttl_minutes=30)
        ok, reason = ledger.reserve(NOW)
        self.assertFalse(ok)
        self.assertIn("corrupt", reason)
        with self.assertRaises(rr.RetrievalUnavailable):
            ledger.record_429(NOW, retry_after_seconds=60)
        with self.assertRaises(rr.RetrievalUnavailable):
            ledger.mark_scraped(PAGE_URL, NOW)
        self.assertEqual(legacy.saves, [], "a strict-load TypeError must never reset")
        self.assertTrue(ledger.describe(NOW)["corrupt"])

    def test_adapter_lock_loss_during_cooldown_blocks_further_calls(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_error("HTTP 429 Too Many Requests; Retry-After: 120"))
        ledger = rr.MeteredAttemptLedger(
            self._store(tmp), now=NOW, per_cycle=5, per_day=10,
            cooldown_minutes=60, cache_ttl_minutes=0)
        adapter = rr.FirecrawlMCPAdapter(
            _fc_policy(firecrawl_max_attempts_per_cycle=5), ctx=ctx, ledger=ledger)
        # First ledger-lock acquisition (reserve) succeeds; the cooldown write's
        # lock acquisition fails.
        with _ledger_lock_open_failure_at(2):
            with self.assertRaises(rr.RetrievalUnavailable) as caught:
                adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("lock unavailable", str(caught.exception))
        self.assertEqual(len(ctx.calls), 1)
        # The cooldown could not be recorded: deny rather than continue.
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertEqual(len(ctx.calls), 1)

    def test_adapter_persistence_failure_during_cooldown_blocks_further_calls(self):
        tmp = self._tmp()
        ctx = _MCPCtx(_fc_error("HTTP 429 Too Many Requests; Retry-After: 120"))
        failing = _FailNthSaveStore(self._store(tmp), fail_at=2)
        ledger = rr.MeteredAttemptLedger(
            failing, now=NOW, per_cycle=5, per_day=10,
            cooldown_minutes=60, cache_ttl_minutes=0)
        adapter = rr.FirecrawlMCPAdapter(
            _fc_policy(firecrawl_max_attempts_per_cycle=5), ctx=ctx, ledger=ledger)
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("persistence failed", str(caught.exception))
        self.assertEqual(len(ctx.calls), 1)
        with self.assertRaises(rr.RetrievalUnavailable):
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertEqual(len(ctx.calls), 1)


# --------------------------------------------------------------------------- #
# Strict persisted-ledger validation (matching-schema malformed fields).
# A ledger that exists on disk, matches the schema, but carries any malformed
# admission-relevant field must FAIL CLOSED for every mutation -- never coerced
# to a benign 0 (which would silently re-open the daily quota), never rewritten,
# and never reaching a provider call. A cleanly absent file stays a clean
# initialization, and a known-valid previous 0.4 ledger keeps working.
# --------------------------------------------------------------------------- #


class StrictLedgerValidationTests(unittest.TestCase):
    def _tmp(self):
        tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def _store(self, tmp):
        return StateStore(Path(tmp), SeededClock(NOW.isoformat()))

    def _valid_payload(self, **over):
        payload = {
            "schema": rr.METERED_SCHEMA,
            "day": "2026-10-06",
            "day_used": 3,
            "cooldown_until": "",
            "scraped": {},
            "config_fingerprint": "0123456789abcdef",
        }
        payload.update(over)
        return payload

    def _write(self, tmp, payload):
        state_file = Path(tmp) / rr.METERED_STATE_FILE
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        state_file.write_text(raw, encoding="utf-8")
        return state_file, raw

    def _ledger(self, store, **over):
        kw = dict(now=NOW, per_cycle=5, per_day=10, cooldown_minutes=60,
                  cache_ttl_minutes=30)
        kw.update(over)
        return rr.MeteredAttemptLedger(store, **kw)

    def _mutations(self):
        return {
            "reserve": lambda ledger: ledger.reserve(NOW),
            "record_429": lambda ledger: ledger.record_429(NOW, retry_after_seconds=60),
            "record_success": lambda ledger: ledger.record_success(),
            "mark_scraped": lambda ledger: ledger.mark_scraped(PAGE_URL, NOW),
            "cache_fresh": lambda ledger: ledger.cache_fresh(PAGE_URL, NOW),
        }

    def _assert_malformed_fails_closed(self, payload, label):
        tmp = self._tmp()
        state_file, raw = self._write(tmp, payload)
        ledger = self._ledger(self._store(tmp))
        self.assertTrue(ledger.describe(NOW)["corrupt"], label)
        # Provider-anchored adapter over the corrupt ledger: the provider lane is
        # never entered (the denial happens before reserve/_invoke).
        ctx = _MCPCtx(_fc_markdown(FC_MD))
        adapter = rr.FirecrawlMCPAdapter(
            _fc_policy(firecrawl_max_attempts_per_cycle=5), ctx=ctx, ledger=ledger)
        with self.assertRaises(rr.RetrievalUnavailable) as caught:
            adapter.scrape(PAGE_URL, now=NOW)
        self.assertIn("corrupt", str(caught.exception), label)
        self.assertEqual(ctx.calls, [], label)
        # Every mutation denies and preserves the on-disk bytes byte-identically.
        for name, operation in self._mutations().items():
            with self.subTest(label=label, operation=name):
                if name in {"record_429", "record_success", "mark_scraped"}:
                    with self.assertRaises(rr.RetrievalUnavailable) as raised:
                        operation(ledger)
                    self.assertIn("corrupt", str(raised.exception))
                elif name == "reserve":
                    ok, reason = operation(ledger)
                    self.assertFalse(ok)
                    self.assertIn("corrupt", reason)
                else:  # cache_fresh
                    fresh, reason = operation(ledger)
                    self.assertFalse(fresh)
                    self.assertIn("corrupt", reason)
                self.assertEqual(state_file.read_text(encoding="utf-8"), raw)
        self.assertTrue(ledger.describe(NOW)["corrupt"], label)

    def test_malformed_day_used_counts_fail_closed(self):
        for label, value in (
            ("negative", -1), ("string", "3"), ("bool", True),
            ("null", None), ("floating", 1.5),
        ):
            with self.subTest(case=label):
                self._assert_malformed_fails_closed(
                    self._valid_payload(day_used=value), f"day_used:{label}")

    def test_invalid_day_fails_closed(self):
        for label, value in (
            ("text", "not-a-day"), ("bad-month", "2026-13-01"),
            ("bad-day", "2026-02-30"), ("int", 20261006), ("null", None),
            ("unpadded", "2026-1-6"), ("empty", ""),
        ):
            with self.subTest(case=label):
                self._assert_malformed_fails_closed(
                    self._valid_payload(day=value), f"day:{label}")

    def test_invalid_cooldown_fails_closed(self):
        for label, value in (
            ("naive", "2026-10-06T10:00:00"), ("text", "soon"),
            ("int", 123), ("null", None), ("list", []), ("blank", "   "),
        ):
            with self.subTest(case=label):
                self._assert_malformed_fails_closed(
                    self._valid_payload(cooldown_until=value), f"cooldown:{label}")

    def test_invalid_scraped_shape_fails_closed(self):
        good_key = rr._url_key(PAGE_URL)
        for label, value in (
            ("list", []), ("text", "not-a-map"),
            ("bad-value", {good_key: "not-a-timestamp"}),
            ("int-value", {good_key: 123}), ("null-value", {good_key: None}),
            ("oversized", {f"k{i}": "2026-10-06T10:00:00+00:00" for i in range(257)}),
        ):
            with self.subTest(case=label):
                self._assert_malformed_fails_closed(
                    self._valid_payload(scraped=value), f"scraped:{label}")

    def test_validator_rejects_non_string_scraped_key(self):
        payload = self._valid_payload(scraped={1: "2026-10-06T10:00:00+00:00"})
        self.assertIsNone(rr._validate_persisted_ledger(payload))

    def test_absent_required_field_fails_closed(self):
        for field in ("schema", "day", "day_used", "cooldown_until", "scraped"):
            with self.subTest(field=field):
                payload = self._valid_payload()
                payload.pop(field)
                self._assert_malformed_fails_closed(payload, f"absent:{field}")

    def test_non_exact_schema_fails_closed(self):
        for label, value in (
            ("bool", True), ("string", "1"), ("other", 2),
            ("float", 1.0), ("null", None),
        ):
            with self.subTest(case=label):
                self._assert_malformed_fails_closed(
                    self._valid_payload(schema=value), f"schema:{label}")

    def test_malformed_old_day_ledger_never_rolled_or_rewritten(self):
        # An old, malformed day must not be "rolled over" then persisted.
        payload = self._valid_payload(day="2026-10-05", day_used=-1)
        self._assert_malformed_fails_closed(payload, "old-day-malformed")

    def test_missing_file_is_clean_initialization_not_corrupt(self):
        tmp = self._tmp()
        state_file = Path(tmp) / rr.METERED_STATE_FILE
        self.assertFalse(state_file.exists())
        ledger = self._ledger(self._store(tmp))
        report = ledger.describe(NOW)
        self.assertFalse(report["corrupt"])
        self.assertTrue(report["available"])
        self.assertEqual(report["day_used"], 0)
        ok, reason = ledger.reserve(NOW)
        self.assertTrue(ok, reason)
        ledger.release()
        self.assertTrue(state_file.exists())
        stored = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema"], rr.METERED_SCHEMA)
        self.assertEqual(stored["day_used"], 1)

    def test_known_valid_previous_ledger_reloads(self):
        # A previous 0.4 ledger WITHOUT config_fingerprint and with a populated,
        # timezone-aware freshness map must still be accepted and continue.
        payload = {
            "schema": rr.METERED_SCHEMA,
            "day": "2026-10-06",
            "day_used": 4,
            "cooldown_until": "2026-10-05T00:00:00+00:00",
            "scraped": {rr._url_key(PAGE_URL): "2026-10-06T09:58:00+00:00"},
        }
        tmp = self._tmp()
        self._write(tmp, payload)
        ledger = self._ledger(self._store(tmp))
        report = ledger.describe(NOW)
        self.assertFalse(report["corrupt"])
        self.assertTrue(report["available"])
        self.assertEqual(report["day_used"], 4)
        ok, reason = ledger.reserve(NOW)
        self.assertTrue(ok, reason)
        ledger.release()
        reopened = self._ledger(self._store(tmp))
        self.assertEqual(reopened.describe(NOW)["day_used"], 5)
        self.assertFalse(reopened.describe(NOW)["corrupt"])

    def test_utc_rollover_of_valid_ledger_preserved(self):
        payload = self._valid_payload(day="2026-10-05", day_used=7)
        tmp = self._tmp()
        self._write(tmp, payload)
        ledger = self._ledger(self._store(tmp))
        ok, reason = ledger.reserve(NOW)
        self.assertTrue(ok, reason)
        ledger.release()
        report = ledger.describe(NOW)
        self.assertFalse(report["corrupt"])
        self.assertEqual(report["day"], "2026-10-06")
        self.assertEqual(report["day_used"], 1)


if __name__ == "__main__":
    unittest.main()
