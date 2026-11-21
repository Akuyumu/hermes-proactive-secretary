"""Parent controls for the stage-only diverse public rollout.

Covers the vetted UNIVERSITY newsroom JSON adapter + the single vetted CNA category RSS
with deterministic relevance/date/link safety controls. EVERY test injects a
synthetic fake fetcher: no live HTTP, no network, no SubprocessRunner, no
personal account, no /tmp. Standalone:

  PYTHONPATH=src python3 tests/test_parent_public_diversity.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.policy import active_snooze, in_quiet_hours, load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    ARTICLE_HOSTS,
    CNA_BUSINESS_RSS,
    UNIVERSITY_BASE,
    UNIVERSITY_STORY_ENDPOINT,
    PUBLIC_HOST_PATHS,
    VETTED_NEWS_HOSTS,
    FeedFailed,
    FetchFailed,
    FetchResponse,
    URLRejected,
    _SafeRedirectHandler,
    auto_added_ok,
    cna_title_relevant,
    collect_public_feeds,
    default_seed_sources,
    university_title_relevant,
    parse_university_date,
    parse_university_stories,
    parse_rss_atom,
    public_item_relevant,
    safe_article_link,
    validate_public_url,
)
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)


class FakeFetcher:
    """Explicit synthetic fetcher. Never opens a socket."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        resp = self.responses.get(url)
        if resp is None:
            for key, value in self.responses.items():
                if url.startswith(key):
                    resp = value
                    break
        if resp is None:
            raise FetchFailed(f"no fake response for {url}")
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(url, **kwargs)
        return resp


def _policy(seeds=None):
    policy = load_policy(None)
    policy["timezone"] = "Asia/Singapore"
    policy["feed_seed_sources"] = [] if seeds is None else seeds
    return policy


def _university_seed():
    return [{"kind": "university_json", "url": UNIVERSITY_STORY_ENDPOINT,
             "provenance": "test", "interest": "university campus news", "cadence_minutes": 60}]


def _cna_seed():
    return [{"kind": "cna_rss", "url": CNA_BUSINESS_RSS,
             "provenance": "test", "interest": "cna technology news", "cadence_minutes": 60}]


def _university_body(stories):
    return json.dumps({"totalPages": 1, "totalItems": len(stories), "items": stories}).encode("utf-8")


def _university_response(stories):
    return FetchResponse(200, {"ETag": "university1"}, _university_body(stories), UNIVERSITY_STORY_ENDPOINT)


def _cna_body(items):
    rows = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        f"<description>{desc}</description><pubDate>Fri, 02 Oct 2026 08:00:00 +0800</pubDate>"
        f"<category>Business</category><guid>{link}</guid></item>"
        for (t, link, desc) in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>CNA Business</title>'
        f"<description>test</description>{rows}</channel></rss>"
    ).encode("utf-8")


def _cna_response(items):
    return FetchResponse(200, {"ETag": "cna1"}, _cna_body(items), CNA_BUSINESS_RSS)


# -- UNIVERSITY JSON adapter ----------------------------------------------------------


def test_university_json_exact_shape_parses_and_normalizes():
    stories = [{
        "title": "UNIVERSITY launches new AI scholarship for undergraduates",
        "url": "/news/detail/ai-scholarship",
        "description": "A <b>new</b> scholarship for students.",
        "date": "02 Oct 2026",
        "contentType": "News",
        "imageUrl": None,
    }]
    items = parse_university_stories(_university_body(stories), now=NOW)
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "UNIVERSITY launches new AI scholarship for undergraduates"
    assert it["link"] == "https://www.university.example/news/detail/ai-scholarship"
    assert it["published"] == "2026-10-02"
    assert it["date_precision"] == "date"
    assert it["content_type"] == "News"
    assert "<b>" not in it["summary"]


def test_university_json_null_and_malformed_tolerated():
    nulls = [{"title": None, "url": None, "description": None, "date": None, "contentType": None}]
    items = parse_university_stories(_university_body(nulls), now=NOW)
    assert items and items[0]["title"] == "" and items[0]["link"] == ""
    assert items[0]["published"] == "" and items[0]["date_precision"] == "unknown"
    for bad in (b"{not-json", b"[]", b'{"items": {}}', b'{"items": null}'):
        try:
            parse_university_stories(bad, now=NOW)
        except FeedFailed:
            continue
        raise AssertionError(f"expected FeedFailed for {bad!r}")


def test_university_relative_and_unsafe_links():
    # Relative/absolute safe UNIVERSITY links normalize to the official https base.
    assert safe_article_link(UNIVERSITY_BASE, "/news/x") == "https://www.university.example/news/x"
    assert safe_article_link(UNIVERSITY_BASE, "news/x") == "https://www.university.example/news/x"
    assert safe_article_link(UNIVERSITY_BASE, "//www.university.example/news/x") == "https://www.university.example/news/x"
    assert safe_article_link(UNIVERSITY_BASE, "https://www.university.example/news/x") == "https://www.university.example/news/x"
    # Private / metadata / credentials / http / off-host all rejected.
    for bad in (
        "http://169.254.169.254/latest/meta-data",
        "http://www.university.example/news/x",
        "https://127.0.0.1/news/x",
        "https://10.0.0.5/news/x",
        "https://user:pass@www.university.example/news/x",
        "javascript:alert(1)",
        "//evil.example/news/x",
        "https://evil.example/news/x",
        "https://www.university.example.evil.example/news/x",
        "https://www.university.example/news/x?access_token=leak",
    ):
        assert safe_article_link(UNIVERSITY_BASE, bad) == "", bad


def test_university_date_only_normalization_and_future_rejection():
    assert parse_university_date("02 Oct 2026") == "2026-10-02"
    assert parse_university_date("2 October 2026") == "2026-10-02"
    assert parse_university_date("2026-10-02T09:00:00Z") == "2026-10-02"
    for bad in ("", None, "not a date", "32 Feb 2026", "2026-13-01"):
        assert parse_university_date(bad) == "", bad
    # A malformed/fabricated future date is blanked (never fresh).
    future = [{"title": "UNIVERSITY career fair", "url": "/news/x", "date": "01 Jan 2027",
               "description": "", "contentType": "News"}]
    items = parse_university_stories(_university_body(future), now=NOW)
    assert items[0]["published"] == "" and items[0]["date_precision"] == "unknown"
    # An absent date can never masquerade as fresh.
    absent = [{"title": "UNIVERSITY campus update", "url": "/news/y", "date": "",
               "description": "", "contentType": "News"}]
    assert parse_university_stories(_university_body(absent), now=NOW)[0]["published"] == ""


def test_university_date_only_stable_across_polls():
    stories = [{"title": "UNIVERSITY library extends hours", "url": "/news/lib",
                "description": "", "date": "02 Oct 2026", "contentType": "News"}]
    a = parse_university_stories(_university_body(stories), now=NOW)[0]
    b = parse_university_stories(_university_body(stories), now=NOW + timedelta(days=3))[0]
    assert a["published"] == b["published"] == "2026-10-02"
    assert a["date_precision"] == b["date_precision"] == "date"


# -- relevance gates -----------------------------------------------------------


def test_cna_relevance_rejects_generic_business_and_keeps_tech():
    for title in (
        "Singapore tech firm launches new AI chip",
        "Two universities to open quantum computing laboratory",
        "New cybersecurity rules for banks take effect",
        "Startup raises funds to expand electric vehicle battery output",
    ):
        assert cna_title_relevant(title), title
    for title in (
        "Singapore shares fall as earnings disappoint",
        "Bank posts lower quarterly profit",
        "Man charged with homicide in Toa Payoh",
        "Oil prices rise on supply concerns",
        "Property market cools after new measures",
    ):
        assert not cna_title_relevant(title), title
    # Word-boundary safety: "ai" must not match inside other words.
    assert not cna_title_relevant("Man said to have chaired the meeting")


def test_university_campus_kept_researcher_achievement_excluded():
    for title in (
        "UNIVERSITY student wins national scholarship",
        "UNIVERSITY to hold annual career fair for students",
        "UNIVERSITY launches new AI laboratory for undergraduates",
        "UNIVERSITY researchers develop new battery technology",
        "UNIVERSITY students win national sports championship",
    ):
        assert university_title_relevant(title), title
    for title in (
        "UNIVERSITY professor named fellow of national academy",
        "UNIVERSITY researchers publish paper in top journal",
        "UNIVERSITY scientist receives prestigious award",
        "UNIVERSITY climbs in global university ranking",
    ):
        assert not university_title_relevant(title), title
    # Any-campus-news blanket drop is forbidden: at least one plain campus item
    # survives, and a non-news source class is never gated.
    assert university_title_relevant("UNIVERSITY orientation week schedule announced")
    assert public_item_relevant("www.reddit.com", "anything at all") is True


# -- vetted allowlist / SSRF / no global loosening -----------------------------


def test_vetted_hosts_and_ssrf_unchanged():
    validate_public_url(UNIVERSITY_STORY_ENDPOINT)
    validate_public_url(CNA_BUSINESS_RSS)
    for bad in (
        "http://www.university.example/news/GetStories/?page=1",
        "https://user:pass@www.university.example/news/GetStories/",
        "https://www.university.example:8443/news/GetStories/",
        "https://127.0.0.1/news/GetStories/",
        "https://10.0.0.5/news/GetStories/",
        "https://evil.example/news/GetStories/",
        "https://www.university.example/news/GetStories/?token=leak",
        "https://www.channelnewsasia.com/secret/feed",
    ):
        try:
            validate_public_url(bad)
        except URLRejected:
            continue
        raise AssertionError(f"expected rejection for {bad}")
    # Vetted hosts are allowlisted; arbitrary hosts still need the opt-in.
    assert auto_added_ok("www.university.example") and auto_added_ok("www.channelnewsasia.com")
    assert not auto_added_ok("evil.example")
    assert "data.gov.sg" not in PUBLIC_HOST_PATHS
    assert VETTED_NEWS_HOSTS == ARTICLE_HOSTS
    # Redirect hops remain re-validated.
    handler = _SafeRedirectHandler(dict(PUBLIC_HOST_PATHS))
    import urllib.request
    req = urllib.request.Request(UNIVERSITY_STORY_ENDPOINT)
    try:
        handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/x")
    except URLRejected:
        return
    raise AssertionError("unsafe redirect was not rejected")


def test_only_reviewed_weather_planned_and_live_transport_sources_claimed():
    seeds = default_seed_sources()
    kinds = {s["kind"] for s in seeds}
    assert kinds == {"github_releases", "reddit_rss", "university_json", "cna_rss",
                     "nea_forecast", "smrt_notice", "lta_train_alerts"}, kinds
    blob = " ".join(f"{s['kind']} {s['url']} {s['interest']}" for s in seeds).lower()
    for forbidden in ("pm25", "psi", "opportunity feed", "internship feed"):
        assert forbidden not in blob, forbidden
    by_kind = {s["kind"]: s for s in seeds}
    assert by_kind["nea_forecast"]["url"] == "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
    assert "not an active warning" in by_kind["nea_forecast"]["provenance"]
    assert "not live incident coverage" in by_kind["smrt_notice"]["provenance"]
    # The one live transport source is the official credentialed LTA endpoint
    # (env-only header key), never a keyless SMRT page.
    assert by_kind["lta_train_alerts"]["url"] == "https://datamall2.mytransport.sg/ltaodataservice/TrainServiceAlerts"
    assert "LTA_DATAMALL_ACCOUNT_KEY" in by_kind["lta_train_alerts"]["provenance"] or "credentialed" in by_kind["lta_train_alerts"]["provenance"]
    assert "scholarship" not in blob


# -- end-to-end collection with fake fetcher only ------------------------------


def test_collect_university_and_cna_only_relevant_and_vetted():
    university_stories = [
        {"title": "UNIVERSITY launches new AI scholarship for undergraduates",
         "url": "/news/detail/ai", "description": "", "date": "02 Oct 2026", "contentType": "News"},
        {"title": "UNIVERSITY professor named fellow of national academy",
         "url": "https://www.university.example/news/detail/fellow", "description": "",
         "date": "01 Oct 2026", "contentType": "News"},
        {"title": "UNIVERSITY students win national sports championship",
         "url": "http://169.254.169.254/latest/meta-data", "description": "",
         "date": "30 Sep 2026", "contentType": "News"},
    ]
    cna_items = [
        ("Singapore tech firm launches new AI chip", "https://www.channelnewsasia.com/business/chip", "d"),
        ("Singapore shares fall as earnings disappoint",
         "https://www.channelnewsasia.com/business/shares", "d"),
        ("Man charged with homicide in Toa Payoh",
         "https://www.channelnewsasia.com/singapore/crime", "d"),
    ]
    seeds = _university_seed() + _cna_seed()
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(seeds)
        fetcher = FakeFetcher({
            UNIVERSITY_STORY_ENDPOINT: _university_response(university_stories),
            CNA_BUSINESS_RSS: _cna_response(cna_items),
        })
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        # Only the two fixed official endpoints were fetched; no article bodies.
        assert sorted(fetcher.calls) == sorted([UNIVERSITY_STORY_ENDPOINT, CNA_BUSINESS_RSS]), fetcher.calls
        feeds = [r for r in results if r.name.startswith("feed:")]
        assert len(feeds) == 2
        all_items = [it for r in feeds for it in r.items]
        titles = [it["title"] for it in all_items]
        assert "UNIVERSITY launches new AI scholarship for undergraduates" in titles
        assert "UNIVERSITY students win national sports championship" in titles
        assert "Singapore tech firm launches new AI chip" in titles
        assert not any("professor named fellow" in t for t in titles)
        assert not any("shares fall" in t or "homicide" in t for t in titles)
        for it in all_items:
            link = it["link"]
            if link:
                host = link.split("//", 1)[-1].split("/", 1)[0]
                assert host in ARTICLE_HOSTS, link
        # The unsafe metadata link on a relevant campus item is blanked, never fetched.
        sports = next(it for it in all_items if "sports championship" in it["title"])
        assert sports["link"] == ""


def test_cna_relevance_gate_applies_to_packet_candidates():
    cna_items = [
        ("Singapore tech firm launches new AI chip", "https://www.channelnewsasia.com/business/chip", "d"),
        ("Bank posts lower quarterly profit", "https://www.channelnewsasia.com/business/bank", "d"),
    ]
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_cna_seed())
        fetcher = FakeFetcher({CNA_BUSINESS_RSS: _cna_response(cna_items)})
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()), policy=policy, baseline={})
        subjects = " | ".join(c["subject"] for c in packet["candidates"])
        assert "AI chip" in subjects
        assert "quarterly profit" not in subjects


def test_university_absent_date_candidate_is_not_fresh():
    stories = [{"title": "UNIVERSITY campus update", "url": "/news/x", "description": "",
                "date": "", "contentType": "News"}]
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_university_seed())
        fetcher = FakeFetcher({UNIVERSITY_STORY_ENDPOINT: _university_response(stories)})
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()), policy=policy, baseline={})
        cand = packet["candidates"][0]
        assert cand["phase"] == "far" and cand["date"] == ""


# -- unchanged envelope: cadence / retention / quiet / pause / caps -------------


def test_cadence_retention_quiet_pause_caps_unchanged():
    policy = load_policy(None)
    policy["timezone"] = "Asia/Singapore"
    assert policy["feed_max_sources_per_run"] == 2
    assert policy["feed_cadence_minutes"] == 60
    assert policy["max_public_registry"] == 12
    assert policy["public_findings_retention_days"] == 30
    assert policy["daily_normal_cap"] is None
    assert policy["cooldown_minutes"] == 30
    assert policy["quiet_end_hour"] == 8
    assert policy["generic_websites_opt_in"] is False
    assert in_quiet_hours(policy, NOW.replace(hour=3)) is True
    assert in_quiet_hours(policy, NOW.replace(hour=12)) is False
    assert active_snooze({"paused": {"active": True}}, NOW) is True


def test_university_cadence_caches_and_does_not_refetch():
    stories = [{"title": "UNIVERSITY AI lab opens", "url": "/news/x", "description": "",
                "date": "02 Oct 2026", "contentType": "News"}]
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy([{"kind": "university_json", "url": UNIVERSITY_STORY_ENDPOINT, "provenance": "t",
                           "interest": "university campus news", "cadence_minutes": 60}])
        fetcher = FakeFetcher({UNIVERSITY_STORY_ENDPOINT: _university_response(stories)})
        collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1
        later = SeededClock((NOW + timedelta(minutes=10)).isoformat())
        results = collect_public_feeds(policy, later, store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1, "within cadence must not refetch"
        feed = next(r for r in results if r.name.startswith("feed:"))
        assert feed.cached and feed.cache_age_minutes >= 10


def test_initial_bootstrap_defers_and_never_fakes_health():
    seeds = [
        {"kind": "github_releases", "url": "https://api.github.com/repos/a/b/releases",
         "provenance": "t", "interest": "r", "cadence_minutes": 60},
        {"kind": "reddit_rss", "url": "https://www.reddit.com/r/a/.rss",
         "provenance": "t", "interest": "c", "cadence_minutes": 60},
    ] + _university_seed() + _cna_seed()
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(seeds)
        fetcher = FakeFetcher({
            "https://api.github.com/repos/a/b/releases": FetchResponse(200, {}, b"[]", ""),
            "https://www.reddit.com/r/a/.rss": FetchResponse(200, {}, b"<rss></rss>", ""),
            UNIVERSITY_STORY_ENDPOINT: _university_response([{"title": "UNIVERSITY AI lab opens", "url": "/news/x",
                                                "description": "", "date": "02 Oct 2026",
                                                "contentType": "News"}]),
            CNA_BUSINESS_RSS: _cna_response([("Singapore tech firm launches AI chip",
                                              "https://www.channelnewsasia.com/business/chip", "d")]),
        })
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        # Per-run cap unchanged: at most 2 sources attempted on the cold run.
        assert len(fetcher.calls) <= 2
        deferred = [r for r in results if "deferred" in (r.failure or "")]
        assert deferred, "cold bootstrap must defer rather than burst"
        for res in deferred:
            assert res.cached is False and res.checked_at == "", "no fabricated fresh health"


class ParentPublicDiversity(unittest.TestCase):
    pass


def _attach(cls):
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            setattr(cls, _name, (lambda self, _fn=_fn: _fn()))


_attach(ParentPublicDiversity)


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{passed}/{len(tests)} parent public diversity tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
