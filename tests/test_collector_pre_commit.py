"""Pre-commit callback contract for the real collectors.

Both collectors invoke ``pre_commit`` with the results they are about to
persist. This suite proves the callback receives an authorized fixture item,
and that a callback failure leaves the durable obligations page cursor and the
public-feed registry exactly as they were -- so the same item is still
available when the collection is retried.

Transport attempt telemetry (source observation / shared attempt ledger) is
tracked separately: it is intentionally NOT rolled back, and these tests never
require such a rollback.

Standalone (pytest not required):
  TMPDIR=<scratch> python3 tests/test_collector_pre_commit.py
All mail/feed fixtures are synthetic; no production account or network is read.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.adaptive import PublicAttemptLedger  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.obligations import collect_obligations, load_obligations  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    collect_public_feeds,
    load_registry,
    new_registry,
    validate_public_url,
)
from proactive_pilot.state import StateStore  # noqa: E402

from test_obligations import (  # noqa: E402
    NOW,
    _NativeRunner,
    _native_env,
    _policy as _obligation_policy,
)
from test_public_feeds import (  # noqa: E402
    FakeFetcher,
    _policy as _feed_policy,
    _rss_response,
    _seed,
)


# -- obligations --------------------------------------------------------------

_OBLIGATION_ITEM = "Action required: confirm your order INV-48213 by 2026-10-10"


def _obligation_runner(subject: str = _OBLIGATION_ITEM) -> _NativeRunner:
    return _NativeRunner({1: [_native_env("m1", subject)]})


def test_obligations_pre_commit_observes_authorized_item():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _obligation_policy()
        policy["mail_accounts"] = ["formal"]
        policy["obligation_metadata_items_per_run"] = 1

        seen: list = []
        results = collect_obligations(
            policy, SeededClock(NOW.isoformat()), store=store,
            runner=_obligation_runner(), pre_commit=lambda rs: seen.extend(rs),
        )

        assert seen, "pre_commit must receive the results before persistence"
        assert [r.name for r in seen] == [r.name for r in results]
        formal = next(r for r in seen if r.name == "obligation:formal")
        assert formal.items, "the authorized fixture message must yield an item"
        item = formal.items[0]
        # The item came from the explicitly authorized mail account.
        assert item["source"] == "formal"
        assert item["ref"].startswith("obligation:formal:")


def test_obligations_pre_commit_failure_preserves_cursor_and_retry_has_item():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _obligation_policy()
        policy["mail_accounts"] = ["formal"]
        policy["obligation_metadata_items_per_run"] = 1

        # Establish a persisted baseline cursor with a bounded empty scan.
        collect_obligations(
            policy, SeededClock(NOW.isoformat()), store=store,
            runner=_NativeRunner({1: []}),
        )
        baseline = load_obligations(store)
        assert baseline["cursors"]["formal"]["bootstrap_done"] is True

        received: list = []

        def _reject(results):
            received.extend(results)
            raise RuntimeError("pre-commit rejected persistence")

        # Attempt telemetry is deliberately separate from durable state.
        observed: list = []
        policy["_source_observe"] = lambda res: observed.append(res)

        raised = False
        try:
            collect_obligations(
                policy, SeededClock(NOW.isoformat()), store=store,
                runner=_obligation_runner(), pre_commit=_reject,
            )
        except RuntimeError:
            raised = True
        assert raised, "a pre_commit failure must abort the write"

        after = load_obligations(store)
        # The persisted page cursor/records/window are unchanged: nothing was
        # committed after the callback raised.
        assert after["cursors"] == baseline["cursors"]
        assert after["records"] == baseline["records"]
        assert after["window_cutoff"] == baseline["window_cutoff"]

        # Transport attempt telemetry happened, and is NOT rolled back.
        assert observed and any(r.name == "obligation:formal" for r in observed)

        formal_before = next(r for r in received if r.name == "obligation:formal")
        expected_refs = {it["ref"] for it in formal_before.items}
        assert expected_refs

        # Retry with the SAME fixture message: the identical item is offered
        # again because the rejected run advanced no durable cursor.
        policy.pop("_source_observe", None)
        seen: list = []
        later = SeededClock((NOW + timedelta(minutes=5)).isoformat())
        collect_obligations(
            policy, later, store=store, runner=_obligation_runner(),
            pre_commit=lambda rs: seen.extend(rs),
        )
        retry = next(r for r in seen if r.name == "obligation:formal")
        retry_refs = {it["ref"] for it in retry.items}
        assert expected_refs & retry_refs, "the rejected item was not available on retry"
        # And only now is it actually persisted.
        records = load_obligations(store)["records"]
        assert any(f"obligation:formal:{key}" in expected_refs for key in records)


# -- public feeds -------------------------------------------------------------

_FEED_URL = "https://www.reddit.com/r/hermesagent/.rss"


def test_public_feeds_pre_commit_observes_authorized_item():
    validate_public_url(_FEED_URL)  # the fixture source is on the allowlist
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _feed_policy(_seed(url=_FEED_URL))
        fetcher = FakeFetcher({_FEED_URL: _rss_response()})

        seen: list = []
        results = collect_public_feeds(
            policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher,
            pre_commit=lambda rs: seen.extend(rs),
        )

        assert seen, "pre_commit must receive the results before persistence"
        assert [r.name for r in seen] == [r.name for r in results]
        feed = next(r for r in seen if r.name.startswith("feed:"))
        assert feed.items, "the authorized fixture feed must yield an item"
        assert feed.items[0]["ref"].startswith("feed:")


def test_public_feeds_pre_commit_failure_preserves_registry_and_retry_has_item():
    validate_public_url(_FEED_URL)
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _feed_policy(_seed(url=_FEED_URL, cadence=60))
        before = load_registry(store)
        assert before == new_registry(), "no registry has been persisted yet"

        received: list = []

        def _reject(results):
            received.extend(results)
            raise RuntimeError("pre-commit rejected registry write")

        ledger = PublicAttemptLedger(4, ordinary_active=True, adaptive_active=False)
        raised = False
        try:
            collect_public_feeds(
                policy, SeededClock(NOW.isoformat()), store=store,
                fetcher=FakeFetcher({_FEED_URL: _rss_response()}),
                attempt_ledger=ledger, pre_commit=_reject,
            )
        except RuntimeError:
            raised = True
        assert raised, "a pre_commit failure must abort the registry write"

        # The persisted registry (source cache/cursor) is exactly as before.
        assert load_registry(store) == before == new_registry()

        # Shared transport attempt telemetry is separate and not rolled back.
        assert ledger.used == 1

        feed_before = next(r for r in received if r.name.startswith("feed:"))
        expected_refs = {it["ref"] for it in feed_before.items}
        assert expected_refs

        # Retry: the source is still due (nothing was cached), so the SAME item
        # is fetched and offered again.
        seen: list = []
        later = SeededClock((NOW + timedelta(minutes=5)).isoformat())
        collect_public_feeds(
            policy, later, store=store,
            fetcher=FakeFetcher({_FEED_URL: _rss_response()}),
            attempt_ledger=ledger, pre_commit=lambda rs: seen.extend(rs),
        )
        retry = next(r for r in seen if r.name.startswith("feed:"))
        retry_refs = {it["ref"] for it in retry.items}
        assert expected_refs & retry_refs, "the rejected item was not available on retry"
        # Only now is the registry actually persisted.
        assert load_registry(store)["sources"]


# -- standalone runner --------------------------------------------------------

import unittest as _unittest  # noqa: E402


class AwarenessControls(_unittest.TestCase):
    pass


def _attach_tests(cls):
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            setattr(cls, _name, (lambda self, _fn=_fn: _fn()))


_attach_tests(AwarenessControls)


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
    print(f"\n{passed}/{len(tests)} pre-commit collector tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
