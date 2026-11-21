"""Conservative opportunity deadline/eligibility evidence through the ordinary
acquisition pipeline.

All clients are injected (fake fetcher, seeded clock, isolated workspace temp
state). No network, no private source, no live evaluator. Supplied official
fixtures are local; synthetic pages exercise the positive/ambiguity branches.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _sample(name: str) -> bytes:
    """Read a reviewed public sample from the canonical ``public_samples`` directory."""
    path = ROOT / "public_samples" / name
    if path.is_file():
        return path.read_bytes()
    raise FileNotFoundError(f"sample fixture not found: {name}")


from proactive_pilot import main as M  # noqa: E402
from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FeedExpired,
    FeedFailed,
    FetchResponse,
    UNIVERSITY_SCHOLARSHIP_PROCESS_URL,
    URLRejected,
    WebPageUnavailable,
    collect_public_feeds,
    parse_university_opportunity,
    validate_public_url,
)
from proactive_pilot.state import StateStore  # noqa: E402

SG = "+08:00"
NOW = datetime.fromisoformat("2026-10-05T12:00:00" + SG)
PROCESS_BODY = _sample("scholarship_process_sample.html")
REDIRECT_BODY = _sample("scholarship_redirect_sample.html")
# Parent-fetched immutable public fixture of the real official opportunity page.
TRAVEL_AWARD_BODY = _sample("scholarship_award_sample.html")
TRAVEL_AWARD_URL = (
    "https://www.university.example/admissions/undergraduate/fees-and-funding/"
    "financial-aid/overseas-student-programme-loan-travel-award"
)


def _table_page(heading, rows):
    """A bounded synthetic table page for structural context negatives."""
    body = ("<html><head><title>Scholarship</title></head><body>"
            "<h1>Scholarship</h1>")
    body += f"<h3>{heading}</h3><table><tbody>"
    body += "<tr><th>Category</th><th>Application Details</th></tr>"
    for category, detail in rows:
        body += f"<tr><td>{category}</td><td>{detail}</td></tr>"
    body += "</tbody></table></body></html>"
    return body.encode()

FUTURE_PAGE = (
    b"<html><head><title>Future Scholarship</title></head><body>"
    b"<h1>Future Scholarship 2027</h1>"
    b"<p>Audience: current university undergraduates who are Citizens or permanent residents.</p>"
    b"<p>Applications open from 1 March 2027 to 30 April 2027.</p>"
    b"</body></html>"
)
PAST_AWARD_PAGE = (
    b"<html><head><title>Legacy Award</title></head><body>"
    b"<h1>Legacy Award 2026</h1>"
    b"<p>Eligibility: Citizen or permanent resident; outstanding "
    b"diploma from a polytechnic; full-time first degree.</p>"
    b"<p>Applications from 11 May 2026 to 2 June 2026.</p>"
    b"</body></html>"
)
EXACT_DEADLINE_PAGE = (
    b"<html><head><title>Exact Scholarship</title></head><body>"
    b"<h1>Exact Scholarship</h1>"
    b"<p>Application deadline: 5 October 2026.</p></body></html>"
)
MULTI_DATE_PAGE = (
    b"<html><head><title>Multi Scholarship</title></head><body>"
    b"<h1>Multi Scholarship</h1>"
    b"<p>Applications from 1 March 2027 to 30 April 2027.</p>"
    b"<p>Also from 1 June 2027 to 30 June 2027.</p></body></html>"
)
NO_YEAR_PAGE = (
    b"<html><head><title>Recurring Scholarship</title></head><body>"
    b"<h1>Recurring Scholarship</h1>"
    b"<p>Applications are open from 1 to 31 July every year.</p></body></html>"
)
NATIONALITY_PAGE = (
    b"<html><head><title>National Scholarship</title></head><body>"
    b"<h1>National Scholarship</h1>"
    b"<p>Open to Citizens and permanent residents only.</p>"
    b"<p>Applications from 1 March 2027 to 30 April 2027.</p></body></html>"
)
NO_AUDIENCE_PAGE = (
    b"<html><head><title>Plain Scholarship</title></head><body>"
    b"<h1>Plain Scholarship</h1>"
    b"<p>Applications from 1 March 2027 to 30 April 2027.</p></body></html>"
)
INTERVIEW_PAGE = (
    b"<html><head><title>Scholarship interview</title></head><body>"
    b"<h1>Scholarship interview</h1>"
    b"<p>Interviews from 1 March 2027 to 30 April 2027.</p></body></html>"
)
PROGRAMME_PAGE = (
    b"<html><head><title>Scholarship programme</title></head><body>"
    b"<h1>Scholarship programme</h1>"
    b"<p>Programme runs from 1 March 2027 to 30 April 2027.</p></body></html>"
)
AWARD_EVENT_PAGE = (
    b"<html><head><title>Scholarship award</title></head><body>"
    b"<h1>Scholarship award</h1>"
    b"<p>The award ceremony is an event from 1 March 2027 to 30 April 2027.</p>"
    b"</body></html>"
)
APP_PLUS_INTERVIEW_PAGE = (
    b"<html><head><title>Scholarship</title></head><body>"
    b"<h1>Scholarship</h1>"
    b"<p>Applications open from 1 March 2027 to 30 April 2027.</p>"
    b"<p>Interviews from 1 May 2027 to 30 May 2027.</p></body></html>"
)
TIME_DEADLINE_PAGE = (
    b"<html><head><title>Timed Scholarship</title></head><body>"
    b"<h1>Timed Scholarship</h1>"
    b"<p>Application deadline: 5 October 2026 at 23:59 SGT.</p></body></html>"
)
NAIVE_TIME_PAGE = (
    b"<html><head><title>Naive Scholarship</title></head><body>"
    b"<h1>Naive Scholarship</h1>"
    b"<p>Application deadline: 5 October 2026 at 23:59.</p></body></html>"
)
LOOSE_TIME_PAGE = (
    b"<html><head><title>Loose Scholarship</title></head><body>"
    b"<h1>Loose Scholarship</h1>"
    b"<p>Application deadline: 5 October 2026 at noon.</p></body></html>"
)


class OpportunityParserTests(unittest.TestCase):
    def test_future_synthetic_explicit_deadline_positive(self):
        row = parse_university_opportunity(FUTURE_PAGE, now=NOW)[0]
        self.assertEqual(row["category"], "opportunity")
        self.assertEqual(row["valid_from"], "2027-03-01")
        self.assertEqual(row["valid_until"], "2027-04-30")
        self.assertEqual(row["deadline"], "2027-04-30")
        self.assertTrue(row["opportunity_only"])
        self.assertFalse(row["personal_qualification"])
        self.assertTrue(row["audience_declared"])
        self.assertIn("Citizen", row["audience"])

    def test_exact_deadline_and_expired_are_negative(self):
        with self.assertRaises(FeedExpired):
            parse_university_opportunity(EXACT_DEADLINE_PAGE, now=NOW)
        with self.assertRaises(FeedExpired):
            parse_university_opportunity(PAST_AWARD_PAGE, now=NOW)
        # One day before the deadline is still open (not expired).
        row = parse_university_opportunity(
            EXACT_DEADLINE_PAGE,
            now=datetime.fromisoformat("2026-10-04T12:00:00" + SG),
        )[0]
        self.assertEqual(row["valid_until"], "2026-10-05")

    def test_missing_year_and_missing_date_fail_closed(self):
        with self.assertRaises(FeedFailed):
            parse_university_opportunity(NO_YEAR_PAGE, now=NOW)
        with self.assertRaises(FeedFailed):
            parse_university_opportunity(
                b"<html><body><h1>Scholarship</h1><p>Apply now.</p></body></html>",
                now=NOW,
            )

    def test_multiple_date_ambiguity_fails_closed(self):
        with self.assertRaises(FeedFailed):
            parse_university_opportunity(MULTI_DATE_PAGE, now=NOW)

    def test_annual_recurrence_is_not_a_fabricated_opening(self):
        # The supplied process page states a recurring 1-31 July window with no
        # explicit year; it must never become a currently open opportunity.
        self.assertIn(b"1 to 31 July every year", PROCESS_BODY)
        with self.assertRaises(FeedFailed):
            parse_university_opportunity(PROCESS_BODY, now=NOW)

    def test_nationality_audience_uncertainty_is_evidence_not_personal_fit(self):
        row = parse_university_opportunity(NATIONALITY_PAGE, now=NOW)[0]
        self.assertTrue(row["audience_declared"])
        self.assertIn("Citizen", row["audience"])
        self.assertFalse(row["personal_qualification"])
        self.assertNotIn("Alex", row["audience"])
        plain = parse_university_opportunity(NO_AUDIENCE_PAGE, now=NOW)[0]
        self.assertFalse(plain["audience_declared"])
        self.assertEqual(plain["audience"], "")

    def test_redirect_fixture_fails_closed_and_past_award_expired(self):
        with self.assertRaises(WebPageUnavailable):
            parse_university_opportunity(REDIRECT_BODY, now=NOW)
        with self.assertRaises(FeedExpired):
            parse_university_opportunity(PAST_AWARD_PAGE, now=NOW)

    def test_non_application_windows_are_not_fabricated_deadlines(self):
        # Defect 1: interview/programme/event/award dates must never become an
        # application window merely because "Scholarship" appears on the page.
        for page in (INTERVIEW_PAGE, PROGRAMME_PAGE, AWARD_EVENT_PAGE):
            with self.assertRaises(FeedFailed):
                parse_university_opportunity(page, now=NOW)
        # A real application window alongside an interview window keeps only the
        # application window.
        row = parse_university_opportunity(APP_PLUS_INTERVIEW_PAGE, now=NOW)[0]
        self.assertEqual(row["valid_from"], "2027-03-01")
        self.assertEqual(row["deadline"], "2027-04-30")

    def test_explicit_local_closing_time_is_not_falsely_expired(self):
        # Defect 2: noon on the close day is still open at 23:59 SGT.
        row = parse_university_opportunity(TIME_DEADLINE_PAGE, now=NOW)[0]
        self.assertEqual(row["deadline_precision"], "time")
        self.assertTrue(row["closing_time_known"])
        self.assertEqual(row["valid_from"], "",
                         "a single deadline must never invent a start date")
        self.assertIn("2026-10-05T23:59:00+08:00", row["deadline"])
        # At the exact closing minute it is closed.
        with self.assertRaises(FeedExpired):
            parse_university_opportunity(
                TIME_DEADLINE_PAGE,
                now=datetime.fromisoformat("2026-10-05T23:59:00" + SG),
            )

    def test_unsupported_time_precision_fails_closed_not_expired(self):
        # A clock time without an unambiguous timezone, or a vague close time,
        # is unsupported precision; it must not be silently declared expired.
        for page in (NAIVE_TIME_PAGE, LOOSE_TIME_PAGE):
            try:
                parse_university_opportunity(page, now=NOW)
            except FeedExpired as exc:  # pragma: no cover - guard against regression
                self.fail(f"unsupported time precision raised FeedExpired: {exc}")
            except FeedFailed:
                pass
            else:  # pragma: no cover - guard against regression
                self.fail("unsupported time precision was accepted")

    def test_date_only_single_deadline_discloses_unknown_closing_time(self):
        row = parse_university_opportunity(
            EXACT_DEADLINE_PAGE,
            now=datetime.fromisoformat("2026-10-04T12:00:00" + SG),
        )[0]
        self.assertEqual(row["valid_from"], "")
        self.assertEqual(row["deadline"], "2026-10-05")
        self.assertFalse(row["closing_time_known"])
        self.assertIn("closing time not stated", row["summary"])

    def test_university_static_allowlist_is_exact_reviewed_pages_only(self):
        validate_public_url(UNIVERSITY_SCHOLARSHIP_PROCESS_URL)
        validate_public_url(
            "https://www.university.example/admissions/undergraduate/"
            "scholarships-and-awards/scholarship-opportunities"
        )
        # The prior broad scholarships-and-awards prefix is no longer allowlisted.
        with self.assertRaises(URLRejected):
            validate_public_url(
                "https://www.university.example/admissions/undergraduate/"
                "scholarships-and-awards/faqs-about-scholarships"
            )

    def test_real_travel_award_application_period_table_is_parsed(self):
        # The actual saved official page: the date sits in a table row next to a
        # "Travel awards" category cell. Structural row/cell boundaries keep the
        # date tied to its own "Application Details" cell, so the real Application
        # Period window is accepted; no owner-specific qualification is asserted.
        self.assertIn(b"Application Period", TRAVEL_AWARD_BODY)
        row = parse_university_opportunity(TRAVEL_AWARD_BODY, now=NOW)[0]
        self.assertEqual(row["category"], "opportunity")
        self.assertEqual(row["valid_from"], "2026-09-07")
        self.assertEqual(row["valid_until"], "2026-10-12")
        self.assertEqual(row["deadline"], "2026-10-12")
        self.assertEqual(row["deadline_precision"], "date")
        self.assertFalse(row["closing_time_known"])
        self.assertTrue(row["opportunity_only"])
        self.assertTrue(row["audience_declared"])
        self.assertIn("undergraduate", row["audience"].lower())
        self.assertFalse(row["personal_qualification"])
        self.assertNotIn("Alex", row["audience"])
        self.assertIn("2026-09-07", row["summary"])
        self.assertIn("2026-10-12", row["summary"])
        self.assertIn("Source row evidence", row["summary"])

    def test_real_travel_award_expires_on_deadline_day(self):
        # Date-only deadline: day before is still open, the deadline day is closed
        # with the unknown closing time disclosed.
        open_row = parse_university_opportunity(
            TRAVEL_AWARD_BODY,
            now=datetime.fromisoformat("2026-10-11T23:00:00" + SG),
        )[0]
        self.assertEqual(open_row["deadline"], "2026-10-12")
        self.assertFalse(open_row["closing_time_known"])
        with self.assertRaises(FeedExpired):
            parse_university_opportunity(
                TRAVEL_AWARD_BODY,
                now=datetime.fromisoformat("2026-10-12T09:00:00" + SG),
            )

    def test_travel_award_exact_url_allowlisted_suffix_rejected(self):
        validate_public_url(TRAVEL_AWARD_URL)
        for suffix in ("-unreviewed", "/unreviewed", "?token=x"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(URLRejected):
                    validate_public_url(TRAVEL_AWARD_URL + suffix)

    def test_table_context_negatives_fail_closed(self):
        # An interview/programme table row is never an application deadline even
        # when the page says "Scholarship".
        interview = _table_page("Interview Period", [
            ("Shortlisted candidates", "Interviews from 1 March 2027 to 30 April 2027."),
        ])
        programme = _table_page("Programme Period", [
            ("GEM Explorer", "Programme runs from 1 March 2027 to 30 April 2027."),
        ])
        # A date in an unrelated row of an Application Period table: the row's own
        # programme context wins, so it is not surfaced.
        unrelated_row = _table_page("Application Period", [
            ("HESL OSP Loan", "Programme briefing from 1 March 2027 to 30 April 2027."),
        ])
        for page in (interview, programme, unrelated_row):
            with self.subTest(page=page[:40]):
                with self.assertRaises(FeedFailed):
                    parse_university_opportunity(page, now=NOW)

    def test_multiple_plausible_application_rows_are_ambiguous(self):
        page = _table_page("Application Period", [
            ("Travel awards for GEM Discoverer", "Applications from 1 March 2027 to 30 April 2027."),
            ("GEM Explorer Award", "Applications from 1 June 2027 to 30 June 2027."),
        ])
        with self.assertRaises(FeedFailed):
            parse_university_opportunity(page, now=NOW)


class _FakeFetcher:
    def __init__(self, pages: dict[str, bytes]):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected URL {url}")
        return FetchResponse(200, {"Content-Type": "text/html"}, self.pages[url], url)


def _policy(seeds, *, per_run=1, enabled=True):
    return {
        "feeds_enabled": enabled,
        "feed_max_sources_per_run": per_run,
        "feed_max_items": 5,
        "feed_timeout_seconds": 5,
        "feed_max_bytes": 200_000,
        "feed_seed_sources": list(seeds),
    }


def _seed(url, cadence=360):
    return {"kind": "university_opportunity", "url": url, "provenance": "fixture source",
            "interest": "opportunity", "cadence_minutes": cadence}


class OrdinaryOpportunityPipelineTests(unittest.TestCase):
    def test_collector_to_packet_carries_deadline_and_audience_evidence(self):
        url = "https://www.university.example/admissions/undergraduate/scholarships-and-awards/scholarship-application-process"
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher({url: FUTURE_PAGE})
            results = collect_public_feeds(_policy([_seed(url)]), SeededClock(NOW.isoformat()),
                                           store=store, fetcher=fetcher, now=NOW)
            self.assertEqual(results[0].status, "ok")
            packet = build_packet(results, clock=SeededClock(NOW.isoformat()),
                                  policy=_policy([_seed(url)]), baseline={})
            opp = [c for c in packet["candidates"] if c.get("category") == "opportunity"]
            self.assertEqual(len(opp), 1)
            self.assertEqual(opp[0]["valid_until"], "2027-04-30")
            self.assertEqual(opp[0]["deadline"], "2027-04-30")
            self.assertTrue(opp[0]["opportunity_only"])
            self.assertFalse(opp[0]["personal_qualification"])
            self.assertTrue(opp[0]["audience_declared"])

    def test_cached_opportunity_expires_between_fetches(self):
        url = "https://www.university.example/admissions/undergraduate/scholarships-and-awards/scholarship-application-process"
        page = (b"<html><head><title>Window Scholarship</title></head><body>"
                b"<h1>Window Scholarship</h1>"
                b"<p>Applications from 1 October 2026 to 30 November 2026.</p>"
                b"</body></html>")
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher({url: page})
            policy = _policy([_seed(url, cadence=10_000_000)])
            fresh = collect_public_feeds(policy, SeededClock(NOW.isoformat()),
                                         store=store, fetcher=fetcher, now=NOW)
            self.assertEqual(fresh[0].status, "ok")
            after = collect_public_feeds(
                policy, SeededClock("2026-12-01T12:00:00" + SG), store=store,
                fetcher=fetcher, now=datetime.fromisoformat("2026-12-01T12:00:00" + SG))
            self.assertEqual(after[0].status, "stale")
            self.assertEqual(after[0].items, [])
            self.assertEqual(len(fetcher.calls), 1, "expired cached opportunity must not refetch")
            packet = build_packet(after, clock=SeededClock("2026-12-01T12:00:00" + SG),
                                  policy=policy, baseline={})
            self.assertEqual(packet["candidates"], [])

    def test_source_budget_cadence_and_pause_are_unchanged(self):
        u1 = "https://www.university.example/admissions/undergraduate/scholarships-and-awards/scholarship-application-process"
        u2 = "https://www.university.example/admissions/undergraduate/scholarships-and-awards/scholarship-opportunities"
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher({u1: FUTURE_PAGE, u2: NATIONALITY_PAGE})
            # Pause: no acquisition at all.
            self.assertEqual(
                collect_public_feeds(_policy([_seed(u1)], enabled=False),
                                     SeededClock(NOW.isoformat()), store=store,
                                     fetcher=fetcher, now=NOW), [])
            self.assertEqual(fetcher.calls, [])
            # Per-run source cap limits attempts to one per cycle.
            budgeted = collect_public_feeds(
                _policy([_seed(u1, cadence=360), _seed(u2, cadence=360)], per_run=1),
                SeededClock(NOW.isoformat()), store=store, fetcher=fetcher, now=NOW)
            self.assertEqual(len(budgeted), 2)
            self.assertEqual(len(fetcher.calls), 1)
            # Cadence: a second run inside the window does not refetch.
            carried = collect_public_feeds(
                _policy([_seed(u1, cadence=360), _seed(u2, cadence=360)], per_run=1),
                SeededClock("2026-10-05T12:30:00" + SG), store=store, fetcher=fetcher,
                now=datetime.fromisoformat("2026-10-05T12:30:00" + SG))
            self.assertEqual(fetcher.calls.count(u1), 1,
                             "a source inside cadence must not be refetched")

    def test_real_travel_award_complete_page_through_ordinary_pipeline(self):
        # The exact saved complete page through the ordinary pinned-collector seam
        # (fake fetch standing in for the pinned HTTPS transport), then packet.
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher({TRAVEL_AWARD_URL: TRAVEL_AWARD_BODY})
            results = collect_public_feeds(
                _policy([_seed(TRAVEL_AWARD_URL)]), SeededClock(NOW.isoformat()),
                store=store, fetcher=fetcher, now=NOW)
            self.assertEqual(results[0].status, "ok")
            self.assertEqual(fetcher.calls, [TRAVEL_AWARD_URL])
            packet = build_packet(results, clock=SeededClock(NOW.isoformat()),
                                  policy=_policy([_seed(TRAVEL_AWARD_URL)]), baseline={})
            opp = [c for c in packet["candidates"] if c.get("category") == "opportunity"]
            self.assertEqual(len(opp), 1)
            self.assertEqual(opp[0]["valid_from"], "2026-09-07")
            self.assertEqual(opp[0]["valid_until"], "2026-10-12")
            self.assertEqual(opp[0]["deadline"], "2026-10-12")
            self.assertTrue(opp[0]["audience_declared"])
            self.assertFalse(opp[0]["personal_qualification"])


class OrdinaryMainOpportunityTests(unittest.TestCase):
    def test_main_run_once_flows_opportunity_evidence_to_evaluator(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            root = Path(temp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "timezone": "Asia/Singapore",
                "location_context_enabled": False,
                "feeds_enabled": True,
                "obligations_enabled": False,
                "feed_max_sources_per_run": 1,
                "public_budget_seconds": 10,
                "feed_timeout_seconds": 5,
                "feed_max_bytes": 200_000,
                "feed_max_items": 5,
                "feed_cadence_minutes": 360,
                "feed_seed_sources": [
                    {"kind": "university_opportunity", "url": UNIVERSITY_SCHOLARSHIP_PROCESS_URL,
                     "provenance": "fixture source", "interest": "opportunity",
                     "cadence_minutes": 360},
                ],
            }), encoding="utf-8")
            preferences = root / "preferences.json"
            preferences.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False},
            }), encoding="utf-8")
            fetcher = _FakeFetcher({UNIVERSITY_SCHOLARSHIP_PROCESS_URL: FUTURE_PAGE})
            evaluator = FakeEvaluator(EvaluatorResult("silence", reason="synthetic evaluator"))
            args = M.build_parser().parse_args([
                "--dry-run", "--seeded-clock", NOW.isoformat(),
                "--policy", str(policy_path), "--preferences", str(preferences),
                "--state-dir", str(root / "state"),
            ])
            args.public_fetcher = fetcher
            old_sources = M.default_sources
            M.default_sources = lambda *a, **k: [
                lambda: SourceResult("calendar:synthetic", "ok", checked_at=NOW.isoformat())
            ]
            try:
                code = M._run_once(args, evaluator=evaluator)
            finally:
                M.default_sources = old_sources

        self.assertEqual(code, 0)
        self.assertEqual(fetcher.calls, [UNIVERSITY_SCHOLARSHIP_PROCESS_URL])
        opp = [c for c in evaluator.calls[-1]["candidates"] if c.get("category") == "opportunity"]
        self.assertEqual(len(opp), 1)
        self.assertFalse(opp[0]["personal_qualification"])
        self.assertEqual(opp[0]["valid_until"], "2027-04-30")

    def test_main_run_once_real_travel_award_reaches_evaluator(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            root = Path(temp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "timezone": "Asia/Singapore",
                "location_context_enabled": False,
                "feeds_enabled": True,
                "obligations_enabled": False,
                "feed_max_sources_per_run": 1,
                "public_budget_seconds": 10,
                "feed_timeout_seconds": 5,
                "feed_max_bytes": 200_000,
                "feed_max_items": 5,
                "feed_cadence_minutes": 360,
                "feed_seed_sources": [
                    {"kind": "university_opportunity", "url": TRAVEL_AWARD_URL,
                     "provenance": "fixture source", "interest": "opportunity",
                     "cadence_minutes": 360},
                ],
            }), encoding="utf-8")
            preferences = root / "preferences.json"
            preferences.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False},
            }), encoding="utf-8")
            fetcher = _FakeFetcher({TRAVEL_AWARD_URL: TRAVEL_AWARD_BODY})
            evaluator = FakeEvaluator(EvaluatorResult("silence", reason="synthetic evaluator"))
            args = M.build_parser().parse_args([
                "--dry-run", "--seeded-clock", NOW.isoformat(),
                "--policy", str(policy_path), "--preferences", str(preferences),
                "--state-dir", str(root / "state"),
            ])
            args.public_fetcher = fetcher
            old_sources = M.default_sources
            M.default_sources = lambda *a, **k: [
                lambda: SourceResult("calendar:synthetic", "ok", checked_at=NOW.isoformat())
            ]
            try:
                code = M._run_once(args, evaluator=evaluator)
            finally:
                M.default_sources = old_sources

        self.assertEqual(code, 0)
        self.assertEqual(fetcher.calls, [TRAVEL_AWARD_URL])
        opp = [c for c in evaluator.calls[-1]["candidates"] if c.get("category") == "opportunity"]
        self.assertEqual(len(opp), 1)
        self.assertEqual(opp[0]["valid_from"], "2026-09-07")
        self.assertEqual(opp[0]["valid_until"], "2026-10-12")
        self.assertEqual(opp[0]["deadline"], "2026-10-12")
        self.assertTrue(opp[0]["audience_declared"])
        self.assertFalse(opp[0]["personal_qualification"])


if __name__ == "__main__":
    unittest.main()
