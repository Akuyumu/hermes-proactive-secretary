"""Minimal, honest extension points for public acquisition coverage.

This module does NOT invent endpoints or claim research is complete. It records,
as data, which acquisition categories are supported, which are only supported
through the existing GENERIC vetted route, and which remain UNSUPPORTED until the
parent wires a real validated source.

Integration seam (existing production code):
  * ``publicfeeds.parse_source(body, kind, ...)`` is the per-kind parser dispatch.
  * ``publicfeeds.validate_public_url`` + ``publictransport.PinnedHTTPSTransport``
    are the mandatory vetted/pinned transport for any new https source.
  * ``discovery.classify_hit`` already maps a discovered page to one of the
    generic kinds ``rss`` / ``web_json`` / ``web_page``.

Adding a real source later therefore means: a vetted URL seed (or a discovered
route), a bounded parser bound to a ``kind`` in ``parse_source``, and no change
to budgets/cadence/quiet/caps. Until then a category marked ``unsupported``
stays unsupported and produces no fabricated evidence.
"""
from __future__ import annotations

from typing import Any

# The generic routes that already exist and can carry new validated sources
# without bespoke code.
GENERIC_ROUTES: tuple[str, ...] = ("rss", "web_json", "web_page")

# The kinds already wired in ``publicfeeds.parse_source``.
WIRED_KINDS: tuple[str, ...] = (
    "github_releases", "github_search", "university_json", "web_json", "web_page",
    "rss_atom", "nea_forecast", "smrt_notice", "university_opportunity",
    "lta_train_alerts",
)

ACQUISITION_COVERAGE: dict[str, dict[str, Any]] = {
    "opportunity": {
        # A conservative, explicitly-bounded ``university_opportunity`` parser is wired
        # over fixed reviewed public pages, in addition to the generic vetted
        # page/feed route; mail-side opportunity follow-through is already handled
        # by obligations.py. It emits deadline/audience evidence only when a
        # deadline carries an explicit year AND its own clause is
        # application/submission context, and fails closed on recurrence,
        # ambiguity, missing dates, interview/programme/event/award windows,
        # unsupported time precision, past/closed windows and challenge pages.
        "status": "supported_generic",
        # The parser is ready and wired, but NO real seed is enabled and no
        # official page has been shown to establish a currently open dated
        # individual opportunity, so this is parser-ready, NOT active verified
        # acquisition. Keeping the existing ``status`` value preserves existing
        # field consumers; these labels add the distinction.
        "acquisition": "parser_ready",
        "verified_active": False,
        "route": list(GENERIC_ROUTES),
        "wired_kinds": ["university_opportunity"],
        "integration_point": "fixed EXACT reviewed example-university page -> parse_university_opportunity -> packet/evaluator; discovery.classify_hit -> publicfeeds.parse_source",
        "note": "not an opportunity-specific live feed and not active verified acquisition; explicit-year application-context deadline required; single deadline never invents a start; audience is source-declared and never a personal qualification",
    },
    "weather": {
        "status": "supported_fixed_official",
        "route": ["nea_forecast", "PinnedHTTPSTransport", "ordinary public feed budget/cap/cadence"],
        "integration_point": "publicfeeds.default_seed_sources -> collect_public_feeds -> parse_nea_forecast -> packet/evaluator",
        "note": "keyless official 24-hour forecast only; not an active warning or all-clear; 6-hour cadence and explicit update/validity expiry",
    },
    "planned_transport": {
        "status": "supported_planned_only",
        "route": ["smrt_notice", "PinnedHTTPSTransport", "ordinary public feed budget/cap/cadence"],
        "integration_point": "reviewed fixed SMRT notice -> bounded parser -> packet/evaluator",
        "note": "dated planned notice only; not live incident status, an all-clear, or route-specific advice",
    },
    "live_transport": {
        # Official credentialed LTA DataMall TrainServiceAlerts, wired over a
        # fixed EXACT host+path with an env-only request-header key. Status 1 is
        # an explicit all-clear marker; Status 2 describes each affected segment/
        # message. It is bounded and fails closed on a missing key, redirects,
        # host/path/scheme/port mismatch, non-JSON bodies or malformed shapes.
        "status": "supported_fixed_official",
        "acquisition": "wired_credentialed",
        "verified_active": False,
        "route": ["lta_train_alerts", "fetch_lta_train_alerts (credentialed, header-only key)", "PinnedHTTPSTransport (ordinary feeds unchanged)"],
        "wired_kinds": ["lta_train_alerts"],
        "integration_point": "publicfeeds.default_seed_sources -> collect_public_feeds -> fetch_lta_train_alerts -> parse_lta_train_alerts -> packet/evaluator",
        "note": "requires env var LTA_DATAMALL_ACCOUNT_KEY; fails closed without a network call when absent; accepts both the object and the documented list-of-clusters value shapes; not polled on a fixed cadence (agentic candidate pre-filter + auxiliary model verdict + persisted interval/daily-cap floors, fail closed); not a forecast or planned notice",
    },
}


def coverage_status(category: str) -> dict[str, Any]:
    """Return the recorded coverage for a category (unsupported if unknown)."""
    entry = ACQUISITION_COVERAGE.get(str(category))
    if entry is None:
        return {"status": "unsupported", "route": [],
                "integration_point": "", "note": "unknown category"}
    return dict(entry)


def is_supported_kind(kind: str) -> bool:
    """True only for a kind that already has a wired bounded parser."""
    return str(kind) in WIRED_KINDS


def unsupported_categories() -> list[str]:
    """Categories that must remain explicitly unsupported (no fabricated data)."""
    return sorted(name for name, entry in ACQUISITION_COVERAGE.items()
                  if entry["status"] == "unsupported")
