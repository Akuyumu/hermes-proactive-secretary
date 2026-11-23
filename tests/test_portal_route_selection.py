"""Route-selection and activation-boundary tests for the portal lifecycle.

Scope: these tests use arbitrary synthetic provider/model names and an
in-memory backend. They assert only the route *shape* chosen by
``_resolve_controls`` and the pre-backend refusals/read-only behaviour of
``portal_command``. They do not model native host compatibility, real
credentials/network access, or guarded in-place updates/migrations; those
paths are intentionally left untested here.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package(name: str = "hermes_proactive_secretary_portal_route_qa"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PKG = _load_plugin_package()
cron_mod = importlib.import_module(f"{_PKG.__name__}.cron")
paths = importlib.import_module(f"{_PKG.__name__}.paths")
portal = importlib.import_module(f"{_PKG.__name__}.secretary.secretary_portal")

PORTAL = cron_mod.PORTAL_JOB_NAME


class _ConfigContext:
    """Synthetic native context whose config reads are observable."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = dict(config or {})
        self.reads: list[str] = []

    def get_config(self, key: str, default=None):
        self.reads.append(key)
        return self.config.get(key, default)


class _RecordingBackend(cron_mod.InMemoryCronBackend):
    """In-memory backend that records every backend operation attempt."""

    def __init__(self, jobs=None) -> None:
        super().__init__(jobs)
        self.calls: list[str] = []

    def list_jobs(self):
        self.calls.append("list")
        return super().list_jobs()

    def add_job(self, spec):
        self.calls.append("add")
        return super().add_job(spec)

    def remove_job(self, job_id):
        self.calls.append("remove")
        return super().remove_job(job_id)

    def pause_job(self, job_id):
        self.calls.append("pause")
        return super().pause_job(job_id)

    def resume_job(self, job_id):
        self.calls.append("resume")
        return super().resume_job(job_id)


class _RouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._keys = ("HERMES_HOME", paths.DATA_ENV)
        self._saved = {key: os.environ.get(key) for key in self._keys}
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)
        home = self.root / "home"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(home)
        os.environ[paths.DATA_ENV] = str(self.root / "data")

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def ledger(self) -> dict:
        return cron_mod.load_job_ownership(paths.job_ownership_path())

    def run_portal(self, backend, ctx, **args):
        payload_args = {"portal_action": "preview", "json": True}
        payload_args.update(args)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = portal.portal_command(
                payload_args, backend=backend, native_ctx=ctx)
        text = buf.getvalue().strip()
        return rc, (json.loads(text) if text else {})

    def resolve(self, ctx, **args):
        payload_args = {"portal_action": "preview"}
        payload_args.update(args)
        return portal._resolve_controls(payload_args, ctx)


class ResolverRouteShapeTests(_RouteTest):
    def test_explicit_full_pair_wins_over_saved_config(self):
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        })
        controls = self.resolve(
            ctx,
            provider="explicit-provider-9",
            model="explicit-model-9",
            schedule="*/7 * * * *",
        )
        self.assertEqual(controls["provider"], "explicit-provider-9")
        self.assertEqual(controls["model"], "explicit-model-9")
        # An explicit pair must not consult or mix with the saved primary pair.
        self.assertNotIn("primary_provider", ctx.reads)
        self.assertNotIn("primary_model", ctx.reads)

    def test_saved_full_pair_is_used_when_nothing_is_explicit(self):
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        })
        controls = self.resolve(ctx, schedule="*/5 * * * *")
        self.assertEqual(controls["provider"], "saved-provider-alpha")
        self.assertEqual(controls["model"], "saved-model-alpha")
        self.assertIn("primary_provider", ctx.reads)
        self.assertIn("primary_model", ctx.reads)

    def test_explicit_partial_pair_refuses_without_mixing_saved_config(self):
        saved = {
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        }
        for partial in ({"provider": "only-provider"},
                        {"model": "only-model"}):
            with self.subTest(partial=partial):
                ctx = _ConfigContext(saved)
                with self.assertRaises(ValueError) as raised:
                    self.resolve(ctx, schedule="*/5 * * * *", **partial)
                self.assertIn("together", str(raised.exception))
                # The refusal happens before the saved pair is even read.
                self.assertEqual(ctx.reads, [])


class ActivationBoundaryTests(_RouteTest):
    def test_missing_route_refuses_enable_before_backend(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({})
        rc, payload = self.run_portal(
            backend, ctx, portal_action="enable", schedule="*/5 * * * *")
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["status"], "error")
        self.assertIn("enable requires a complete provider and model route",
                      payload["detail"])
        self.assertEqual(backend.calls, [])
        self.assertFalse(paths.job_ownership_path().exists())

    def test_explicit_partial_pair_refuses_enable_before_backend(self):
        for partial in ({"provider": "only-provider"},
                        {"model": "only-model"}):
            with self.subTest(partial=partial):
                backend = _RecordingBackend()
                ctx = _ConfigContext({
                    "primary_provider": "saved-provider-alpha",
                    "primary_model": "saved-model-alpha",
                })
                rc, payload = self.run_portal(
                    backend, ctx, portal_action="enable",
                    schedule="*/5 * * * *", **partial)
                self.assertEqual(rc, 1, payload)
                self.assertEqual(payload["status"], "error")
                self.assertIn("together", payload["detail"])
                self.assertEqual(backend.calls, [])
                self.assertFalse(paths.job_ownership_path().exists())

    def test_read_only_preview_without_route_does_not_mutate(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({})
        rc, payload = self.run_portal(
            backend, ctx, portal_action="preview", schedule="*/5 * * * *")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "would-create")
        self.assertEqual(backend.calls, ["list"])
        for mutation in ("add", "remove", "pause", "resume"):
            self.assertNotIn(mutation, backend.calls)
        self.assertFalse(paths.job_ownership_path().exists())

    def test_enable_uses_explicit_pair_not_saved_pair(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        })
        rc, payload = self.run_portal(
            backend, ctx, portal_action="enable", schedule="*/5 * * * *",
            provider="explicit-provider-9", model="explicit-model-9")
        self.assertEqual(rc, 0, payload)
        job = backend.list_jobs()[0]
        self.assertEqual(job["provider"], "explicit-provider-9")
        self.assertEqual(job["model"], "explicit-model-9")
        record = self.ledger()["jobs"][PORTAL]
        self.assertEqual(record["provider"], "explicit-provider-9")
        self.assertEqual(record["model"], "explicit-model-9")

    def test_enable_uses_saved_full_pair_when_nothing_is_explicit(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        })
        rc, payload = self.run_portal(
            backend, ctx, portal_action="enable", schedule="*/5 * * * *")
        self.assertEqual(rc, 0, payload)
        job = backend.list_jobs()[0]
        self.assertEqual(job["provider"], "saved-provider-alpha")
        self.assertEqual(job["model"], "saved-model-alpha")
        record = self.ledger()["jobs"][PORTAL]
        self.assertEqual(record["provider"], "saved-provider-alpha")
        self.assertEqual(record["model"], "saved-model-alpha")

    def test_preview_requested_pins_reflect_explicit_route_only(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
        })
        rc, payload = self.run_portal(
            backend, ctx, portal_action="preview", schedule="*/5 * * * *",
            provider="explicit-provider-9", model="explicit-model-9")
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "would-create")
        self.assertEqual(payload["requested_pins"]["provider"],
                         "explicit-provider-9")
        self.assertEqual(payload["requested_pins"]["model"],
                         "explicit-model-9")
        self.assertEqual(backend.calls, ["list"])


class CadenceSelectionTests(_RouteTest):
    def test_configured_3h_and_6h_cadence_remain_canonical(self):
        for label, expected in (("3h", "0 */3 * * *"),
                                ("6h", "0 */6 * * *")):
            with self.subTest(cadence=label):
                ctx = _ConfigContext({
                    "primary_provider": "saved-provider-alpha",
                    "primary_model": "saved-model-alpha",
                    "portal_cadence": label,
                })
                controls = self.resolve(ctx)
                self.assertEqual(controls["schedule"], expected)
                self.assertEqual(controls["provider"], "saved-provider-alpha")
                self.assertEqual(controls["model"], "saved-model-alpha")

    def test_enable_with_configured_6h_cadence_keeps_route_pair(self):
        backend = _RecordingBackend()
        ctx = _ConfigContext({
            "primary_provider": "saved-provider-alpha",
            "primary_model": "saved-model-alpha",
            "portal_cadence": "6h",
        })
        rc, payload = self.run_portal(
            backend, ctx, portal_action="enable")
        self.assertEqual(rc, 0, payload)
        job = backend.list_jobs()[0]
        self.assertEqual(job["schedule"], "0 */6 * * *")
        self.assertEqual(job["provider"], "saved-provider-alpha")
        self.assertEqual(job["model"], "saved-model-alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
