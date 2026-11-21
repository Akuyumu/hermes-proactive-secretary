"""Tentative artifact privacy, validation, notice and concurrency regressions."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_agentic_onboarding as agentic  # noqa: E402  (helpers only)

from proactive_pilot import native_runtime  # noqa: E402
from proactive_pilot import preparation  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot import tentative  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

main_mod = importlib.import_module("proactive_pilot.main")
autonomy = importlib.import_module(f"{agentic._PKG.__name__}.secretary.secretary_autonomy")

T0 = datetime.fromisoformat("2026-10-20T12:00:00+00:00")
TENTATIVE_RE = re.compile(r"tentative:[0-9a-f]{24}")


def _tentative_root() -> Path:
    return preparation.default_artifact_root() / "tentative"


def _clean_tentative() -> None:
    shutil.rmtree(_tentative_root(), ignore_errors=True)


def _config(*, enabled=True, scopes=("conversation",), quiet=(0, 0),
            cap=5, cooldown=0, timezone_name="UTC"):
    config = agentic._config(preparation={
        "enabled": bool(enabled), "scopes": list(scopes),
        "source_consent": False})
    config["timezone"] = timezone_name
    config["notification"]["quiet_start_hour"] = int(quiet[0])
    config["notification"]["quiet_end_hour"] = int(quiet[1])
    config["notification"]["daily_normal_cap"] = int(cap)
    config["notification"]["cooldown_minutes"] = int(cooldown)
    return config


def _ctx(config=None):
    return agentic._registered(config or _config())[0]


def _human(ctx, text, session="s-tentative"):
    return ctx.hooks["pre_llm_call"](
        platform="telegram", user_message=text, session_id=session,
        chat_type="dm")


def _prepare(ctx, text, body, *, title="Tentative draft", refs=None,
             session="s-tentative"):
    _human(ctx, text, session=session)
    lane = agentic._FakeLane({
        "title": title, "content": body, "format": "markdown",
        "evidence_refs": list(refs or [])})
    with mock.patch.object(native_runtime, "build_model_lane", return_value=lane):
        return autonomy.prepare_tentative(
            ctx, desired_outcome="Consider a bounded inferred idea",
            evidence_quote=text, reasons="from the current conversation",
            session_id=session)


def _main_args(ctx, at=T0):
    scratch = Path(tempfile.mkdtemp(
        dir=os.environ.get("TMPDIR") or tempfile.gettempdir(),
        prefix="tentative-main-"))
    policy = dict(DEFAULT_POLICY)
    (scratch / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (scratch / "preferences.json").write_text(json.dumps({
        "explicit": [], "inferred": [], "snoozes": [], "paused": {}}),
        encoding="utf-8")
    state_dir = Path(os.environ["HERMES_PROACTIVE_SECRETARY_DATA"]) / "var"
    args = main_mod.build_parser().parse_args([
        "--state-dir", str(state_dir),
        "--policy", str(scratch / "policy.json"),
        "--preferences", str(scratch / "preferences.json"), "--run"])
    args.require_config = True
    args.native_ctx = ctx
    args.clock = at
    args._scratch = scratch
    return args


def _run_main(ctx, at=T0):
    args = _main_args(ctx, at=at)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(main_mod, "default_sources", lambda *a, **k: []), \
            mock.patch.object(main_mod, "build_clock",
                              lambda *a, **k: SeededClock(at.isoformat())), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main_mod.run_once(
            args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
    shutil.rmtree(args._scratch, ignore_errors=True)
    return rc, out.getvalue(), err.getvalue()


class _IsolatedTentativeCase(unittest.TestCase):
    def setUp(self):
        # Each test owns its entire profile. Cleaning only tentative artifacts
        # leaves accepted drafts from earlier modules eligible for notice first.
        profile = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir(),
            prefix="tentative-profile-")
        self.addCleanup(profile.cleanup)
        root = Path(profile.name)
        env = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(root / "home"),
            "HERMES_PROACTIVE_SECRETARY_DATA": str(root / "data"),
        })
        env.start()
        self.addCleanup(env.stop)


class TentativeArtifactSafetyTests(_IsolatedTentativeCase):
    def setUp(self):
        super().setUp()
        _clean_tentative()

    def tearDown(self):
        _clean_tentative()

    def test_symlink_ancestor_and_unowned_root_are_denied(self):
        with tempfile.TemporaryDirectory(
                dir=os.environ.get("TMPDIR") or tempfile.gettempdir()) as name:
            base = Path(name) / "artifacts"
            outside = Path(name) / "outside"
            base.mkdir()
            outside.mkdir()
            (base / "tentative").symlink_to(outside, target_is_directory=True)
            with mock.patch.object(preparation, "default_artifact_root",
                                   return_value=base):
                with self.assertRaises((tentative.TentativeError,
                                        R.ResponsibilityError)):
                    tentative.paths_for_content("private draft")
            (base / "tentative").unlink()
            (base / "tentative").mkdir(mode=0o700)
            with mock.patch.object(preparation, "default_artifact_root",
                                   return_value=base), \
                    mock.patch.object(tentative.os, "geteuid",
                                      return_value=os.geteuid() + 1):
                with self.assertRaises(tentative.TentativeError):
                    tentative.root()

    def test_missing_hash_missing_path_and_foreign_profile_are_denied(self):
        state = agentic.MemoryState()
        state.set(tentative.TENTATIVE_KEY, [{
            "at": T0.isoformat(), "artifact_ref": "tentative:" + "a" * 24,
            "status": "tentative", "not_accepted": True,
            "profile": tentative.profile_scope(), "scope": "conversation",
            "expires_at": (T0 + timedelta(days=1)).isoformat()}])
        with self.assertRaises(tentative.TentativeError):
            tentative.read_index(state)
        with self.assertRaises(tentative.TentativeError):
            tentative.path_for_ref("tentative:" + "b" * 24)

        ctx = _ctx()
        record = _prepare(ctx, "The conference note is in this chat.",
                          "Bounded draft body.", session="s-profile")
        row = dict(ctx.state.data[tentative.TENTATIVE_KEY][0])
        row["profile"] = "different-profile"
        ctx.state.data[tentative.TENTATIVE_KEY] = [row]
        self.assertIsNone(tentative.claim_notice(
            ctx.state, policy={"preparation_scopes": ["conversation"]},
            now=T0, allowed=True))

    def test_unsupplied_citation_url_secret_and_html_are_rejected(self):
        bad_bodies = [
            "See [[ref: conversation:invented]] for the private fact.",
            "See https://unsupplied.example/private for details.",
            "Set password = supersecretvalue before continuing.",
            "<script>alert(1)</script>",
        ]
        for index, body in enumerate(bad_bodies):
            ctx = _ctx()
            with self.subTest(body=body):
                with self.assertRaises(autonomy.AutonomyError):
                    _prepare(ctx, "The conference deadline is in this chat.",
                             body, session=f"s-bad-{index}")
                self.assertNotIn(tentative.TENTATIVE_KEY, ctx.state.data)
                self.assertEqual(
                    list(_tentative_root().glob("*.md"))
                    if _tentative_root().exists() else [], [])

    def test_unsafe_title_and_oversize_drafts_are_rejected(self):
        ctx = _ctx()
        with self.assertRaises(autonomy.AutonomyError):
            _prepare(ctx, "The conference deadline is in this chat.",
                     "A bounded body.", title="<script>bad</script>",
                     session="s-bad-title")
        ctx = _ctx()
        with self.assertRaises(autonomy.AutonomyError):
            _prepare(ctx, "The conference deadline is in this chat.",
                     "x" * (tentative.MAX_TENTATIVE_BYTES + 100),
                     session="s-oversize")
        self.assertNotIn(tentative.TENTATIVE_KEY, ctx.state.data)


class TentativeMainNoticeTests(_IsolatedTentativeCase):
    def setUp(self):
        super().setUp()
        _clean_tentative()

    def tearDown(self):
        _clean_tentative()

    def test_valid_draft_reaches_main_notice_once(self):
        ctx = _ctx()
        prepared = _prepare(ctx, "The conference deadline is mentioned in this chat.",
                            "Useful bounded draft body.", session="s-notice")
        self.assertTrue(prepared["ok"], prepared)
        ref = prepared["tentative"]["artifact_ref"]
        self.assertRegex(ref, r"^tentative:[0-9a-f]{24}$")

        rc, out, err = _run_main(ctx, at=T0)
        self.assertEqual(rc, 0, err)
        self.assertIn("private tentative portal draft is ready", out.lower())
        self.assertIn(ref, out)
        row = ctx.state.data[tentative.TENTATIVE_KEY][0]
        self.assertEqual(row["notification"]["status"], "emitted-unconfirmed")
        delivered = StateStore(
            Path(os.environ["HERMES_PROACTIVE_SECRETARY_DATA"]) / "var",
            SeededClock(T0.isoformat())).load_notify_state(strict=True)
        self.assertEqual(delivered["delivered"][-1]["delivery"],
                         "emitted-unconfirmed")

        later = T0 + timedelta(minutes=1)
        rc2, out2, err2 = _run_main(ctx, at=later)
        self.assertEqual(rc2, 0, err2)
        self.assertNotIn("private tentative portal draft", out2.lower())

    def test_quiet_cap_disable_and_revoked_scope_suppress_notice(self):
        cases = [
            ("quiet", _config(quiet=(22, 7)), datetime.fromisoformat(
                "2026-10-20T23:00:00+00:00"), None),
            ("cap", _config(cap=1, cooldown=0), T0, 1),
            ("disabled", _config(enabled=False), T0, None),
            ("revoked-scope", _config(scopes=()), T0, None),
        ]
        for name, config, at, seed_cap in cases:
            with self.subTest(case=name):
                _clean_tentative()
                ctx = _ctx()
                prepared = _prepare(
                    ctx, f"The {name} case mentions a conference deadline.",
                    f"Bounded draft body for {name}.", session=f"s-{name}")
                self.assertTrue(prepared["ok"], prepared)
                ctx.settings["user_config"] = config
                args = _main_args(ctx, at=at)
                if seed_cap:
                    store = StateStore(
                        Path(os.environ["HERMES_PROACTIVE_SECRETARY_DATA"]) / "var",
                        SeededClock(at.isoformat()))
                    store.save("notify_state.json", {
                        "schema": 1, "items": {}, "health": {},
                        "delivered": [{"at": at.isoformat(),
                                       "signature": f"seed-{i}",
                                       "urgent": False}
                                      for i in range(seed_cap)]})
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(main_mod, "default_sources",
                                       lambda *a, **k: []), \
                        mock.patch.object(main_mod, "build_clock",
                                          lambda *a, **k: SeededClock(at.isoformat())), \
                        contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = main_mod.run_once(
                        args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
                shutil.rmtree(args._scratch, ignore_errors=True)
                self.assertEqual(rc, 0, err.getvalue())
                self.assertNotIn("private tentative portal draft",
                                 out.getvalue().lower())
                row = ctx.state.data[tentative.TENTATIVE_KEY][0]
                self.assertEqual(row["notification"]["status"], "pending")


class TentativeConcurrencyAndEmitTests(_IsolatedTentativeCase):
    def setUp(self):
        super().setUp()
        _clean_tentative()

    def tearDown(self):
        _clean_tentative()

    def test_actual_main_consumers_emit_exactly_once(self):
        ctx = _ctx()
        prepared = _prepare(ctx, "The conference deadline is in this chat.",
                            "Bounded concurrent draft.", session="s-concurrent")
        self.assertTrue(prepared["ok"], prepared)
        args = _main_args(ctx, at=T0)
        emitted = []
        lock = threading.Lock()

        def record(text):
            if text:
                with lock:
                    emitted.append(text)

        barrier = threading.Barrier(4)
        errors = []

        def consume():
            try:
                barrier.wait(timeout=10)
                main_mod.run_once(
                    args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        with mock.patch.object(main_mod, "default_sources", lambda *a, **k: []), \
                mock.patch.object(main_mod, "build_clock",
                                  lambda *a, **k: SeededClock(T0.isoformat())), \
                mock.patch.object(main_mod, "_emit", side_effect=record), \
                mock.patch.object(main_mod, "_diag", lambda *a, **k: None):
            threads = [threading.Thread(target=consume) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        shutil.rmtree(args._scratch, ignore_errors=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(emitted), 1, emitted)
        self.assertIn(prepared["tentative"]["artifact_ref"], emitted[0])

    def test_index_writer_and_main_consumer_do_not_lose_rows(self):
        ctx = _ctx(_config(cap=10, cooldown=0))
        args = _main_args(ctx, at=T0)
        emitted = []
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        writer_done = threading.Event()

        def record(text):
            if text:
                with lock:
                    emitted.append(text)

        def write_rows():
            try:
                barrier.wait(timeout=10)
                for index in range(3):
                    text = f"Concurrent conference notice {index}."
                    lane = agentic._FakeLane({
                        "title": f"Draft {index}", "content": f"Bounded body {index}.",
                        "format": "markdown", "evidence_refs": []})
                    _human(ctx, text, session="s-writer")
                    with mock.patch.object(native_runtime, "build_model_lane",
                                           return_value=lane):
                        result = autonomy.prepare_tentative(
                            ctx, desired_outcome="Consider a bounded idea",
                            evidence_quote=text, session_id="s-writer")
                    if not result.get("ok"):
                        errors.append(AssertionError(result))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                writer_done.set()

        def consume():
            try:
                barrier.wait(timeout=10)
                while not writer_done.is_set():
                    main_mod.run_once(
                        args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
                for _ in range(5):
                    main_mod.run_once(
                        args, evaluator=FakeEvaluator(EvaluatorResult("silence")))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        with mock.patch.object(main_mod, "default_sources", lambda *a, **k: []), \
                mock.patch.object(main_mod, "build_clock",
                                  lambda *a, **k: SeededClock(T0.isoformat())), \
                mock.patch.object(main_mod, "_emit", side_effect=record), \
                mock.patch.object(main_mod, "_diag", lambda *a, **k: None):
            writer = threading.Thread(target=write_rows)
            consumer = threading.Thread(target=consume)
            writer.start()
            consumer.start()
            writer.join(timeout=30)
            consumer.join(timeout=30)
        shutil.rmtree(args._scratch, ignore_errors=True)
        self.assertEqual(errors, [])
        rows = ctx.state.data[tentative.TENTATIVE_KEY]
        refs = [row["artifact_ref"] for row in rows]
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(set(refs)), 3)
        emitted_refs = [match.group(0) for text in emitted
                        for match in TENTATIVE_RE.finditer(text)]
        self.assertEqual(sorted(emitted_refs), sorted(refs))
        self.assertEqual(len(emitted_refs), len(set(emitted_refs)))

    def test_emit_exception_is_terminal_unknown_and_never_retried(self):
        ctx = _ctx()
        prepared = _prepare(ctx, "The conference deadline is in this chat.",
                            "Bounded failure draft.", session="s-emit")
        self.assertTrue(prepared["ok"], prepared)
        args = _main_args(ctx, at=T0)
        store = StateStore(Path(args.state_dir), SeededClock(T0.isoformat()))
        state = store.load_notify_state()
        policy = dict(DEFAULT_POLICY, preparation_scopes=["conversation"],
                      preparation_enabled=True, _config_present=True)
        with mock.patch.object(main_mod, "_emit", side_effect=OSError("stdout gone")), \
                mock.patch.object(main_mod, "_diag", lambda *a, **k: None):
            rc = main_mod._maybe_emit_tentative_notice(
                args, store, state, policy, SeededClock(T0.isoformat()), True, "ok")
        self.assertEqual(rc, 1)
        row = ctx.state.data[tentative.TENTATIVE_KEY][0]
        self.assertEqual(row["notification"]["status"], "unknown")
        self.assertTrue(row["notified_at"])

        emitted = []
        with mock.patch.object(main_mod, "_emit", side_effect=emitted.append), \
                mock.patch.object(main_mod, "_diag", lambda *a, **k: None):
            again = main_mod._maybe_emit_tentative_notice(
                args, store, state, policy,
                SeededClock((T0 + timedelta(minutes=1)).isoformat()), True, "ok")
        self.assertIsNone(again)
        self.assertEqual([text for text in emitted if text], [])
        self.assertEqual(
            ctx.state.data[tentative.TENTATIVE_KEY][0]["notification"]["status"],
            "unknown")


if __name__ == "__main__":
    unittest.main()
