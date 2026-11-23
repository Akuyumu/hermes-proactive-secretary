"""Native-contract test for the portal route update.

This is deliberately not another fake-backend test. It imports the real host
``cron.jobs`` module from the parent runtime's ``PYTHONPATH`` and exercises the
plugin's real ``HermesCronBackend.update_route`` / ``update_portal_route`` path
against that module's actual create/update/readback storage.

Safety boundaries:

* every scratch path is created under the task ``TMPDIR``;
* ``HOME``, ``HERMES_HOME``, the plugin data override and the XDG roots point
  into that fresh root *before* the host jobs module is imported;
* the test fails closed unless the host's cron/jobs storage resolves under that
  root (so a live profile or production ``jobs.json`` can never be touched);
* no shell, CLI, network, credentials, live scheduler, production settings,
  Library or publication surface is used. A coordinator is expected to run this
  file network-isolated; the test itself makes no network call.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package():
    name = "hermes_proactive_secretary_portal_route_native"
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

PORTAL = cron_mod.PORTAL_JOB_NAME

SCHEDULE = "0 */5 * * *"
OLD_PROVIDER = "Synthetic-Provider-Native-Original"
OLD_MODEL = "Synthetic-Model-Native-Original"
OLD_EFFORT = "low"
NEW_PROVIDER = "Synthetic-Provider-Native-Routed"
NEW_MODEL = "Synthetic-Model-Native-Routed"
NEW_EFFORT = "high"


def _within(path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _find_host_root() -> Path | None:
    """Locate the real host ``cron.jobs`` package on the parent PYTHONPATH."""
    candidates = []
    raw_paths = os.environ.get("PYTHONPATH", "")
    candidates.extend(Path(part) for part in raw_paths.split(os.pathsep) if part)
    candidates.extend(Path(part) for part in sys.path if part)
    for candidate in candidates:
        try:
            base = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if base == ROOT:
            continue
        if ((base / "cron" / "jobs.py").is_file()
                or (base / "cron" / "jobs" / "__init__.py").is_file()):
            return base
    return None


def _cron_module_names() -> list[str]:
    return [name for name in sys.modules if name == "cron" or name.startswith("cron.")]


def _import_host_jobs(host_root: Path):
    """Import a host-profile-bound ``cron.jobs``, never a stale prior profile.

    A module imported by an earlier test is already bound to that test's
    synthetic roots, so it is removed and re-imported under the current roots.
    The previous modules are returned so the caller can restore them afterwards.
    """
    saved_modules = {name: sys.modules[name] for name in _cron_module_names()}
    for name in _cron_module_names():
        del sys.modules[name]

    saved_path = list(sys.path)
    try:
        sys.path[:] = [
            str(host_root),
            *(part for part in sys.path if not _same_path(part, ROOT)),
        ]
        host_jobs = importlib.import_module("cron.jobs")
        origin = Path(getattr(host_jobs, "__file__", "") or "").resolve()
        if not _within(origin, host_root):
            raise AssertionError(
                "imported cron.jobs is not the host package from PYTHONPATH; "
                "failing closed"
            )
    except BaseException:
        sys.path[:] = saved_path
        for name in _cron_module_names():
            del sys.modules[name]
        sys.modules.update(saved_modules)
        raise
    return host_jobs, saved_path, saved_modules


def _same_path(left, right: Path) -> bool:
    try:
        return Path(left).resolve() == right.resolve()
    except (OSError, RuntimeError):
        return False


def _host_rows(host_jobs) -> list[dict]:
    reader = getattr(host_jobs, "list_jobs", None)
    if not callable(reader):
        raise AssertionError("host cron.jobs.list_jobs is unavailable; failing closed")
    try:
        rows = reader(include_disabled=True)
    except TypeError:
        rows = reader()
    if not isinstance(rows, (list, tuple)):
        raise AssertionError("host cron.jobs.list_jobs returned an unexpected shape")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _host_job(host_jobs, job_id: str) -> dict:
    getter = getattr(host_jobs, "get_job", None)
    if not callable(getter):
        raise AssertionError("host cron.jobs.get_job is unavailable; failing closed")
    row = getter(str(job_id))
    if not isinstance(row, dict):
        raise AssertionError(f"host cron.jobs has no job {job_id!r}")
    return dict(row)


def _create_synthetic_job(host_jobs, desired: dict) -> dict:
    """Call the host's own create definition, passing only fields it exposes."""
    creator = getattr(host_jobs, "create_job", None)
    if not callable(creator):
        raise AssertionError("host cron.jobs.create_job is unavailable; failing closed")
    parameters = inspect.signature(creator).parameters
    accepts_extra = any(
        parameter.kind is parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs = {key: value for key, value in desired.items() if key in parameters}
    remaining = {key: value for key, value in desired.items() if key not in kwargs}

    aliases = {
        "reasoning": ("reasoning_effort", "effort"),
        "skills": ("skill",),
        "context_from": ("continuity",),
        "name": ("job_name",),
        "deliver": ("delivery", "deliver_to"),
    }
    for key, value in list(remaining.items()):
        for alternate in aliases.get(key, ()):
            if alternate not in parameters:
                continue
            if key == "context_from":
                kwargs[alternate] = bool(_context_list(value))
            elif key == "skills" and isinstance(value, (list, tuple)):
                if value:
                    kwargs[alternate] = str(value[0])
            else:
                kwargs[alternate] = value
            remaining.pop(key, None)
            break
    if accepts_extra:
        kwargs.update(remaining)
    if "context_from" not in kwargs and not kwargs.get("continuity"):
        raise AssertionError(
            "host create_job cannot express the required self context; failing closed"
        )
    for required in ("prompt", "schedule"):
        if required not in kwargs:
            raise AssertionError(
                f"host create_job does not accept required field {required!r}; "
                "failing closed"
            )
    return creator(**kwargs)


def _context_list(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


class _HostReadBackend(cron_mod.HermesCronBackend):
    """Real plugin backend whose listing comes from the host's in-process store."""

    def __init__(self, host_jobs) -> None:
        super().__init__(runner=self._no_shell)
        self.host_jobs = host_jobs

    @staticmethod
    def _no_shell(_argv):
        raise AssertionError("this contract test must never spawn the cron CLI")

    def is_available(self) -> bool:
        return True

    def list_jobs(self) -> list[dict]:
        rows = []
        for row in _host_rows(self.host_jobs):
            job_id = str(row.get("id") or row.get("job_id") or "")
            rows.append(_host_job(self.host_jobs, job_id) if job_id else row)
        return rows


class PortalRouteNativeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        task_tmpdir = os.environ.get("TMPDIR")
        if not task_tmpdir or not Path(task_tmpdir).is_dir():
            self.fail("task TMPDIR is required for isolated native-route scratch")
        self._tmp = tempfile.TemporaryDirectory(
            prefix="portal-route-native-", dir=task_tmpdir)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

        self.home = self.root / "home"
        self.hermes_home = self.root / "hermes-home"
        self.data = self.root / "data"
        self.scratch = self.root / "tmp"
        for directory in (self.home, self.hermes_home, self.data, self.scratch):
            directory.mkdir(parents=True, exist_ok=True)

        managed = (
            "HOME", "HERMES_HOME", "TMPDIR", paths.DATA_ENV,
            "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
        )
        self._saved_env = {key: os.environ.get(key) for key in managed}
        self.addCleanup(self._restore_env)

        os.environ["HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        os.environ["TMPDIR"] = str(self.scratch)
        os.environ[paths.DATA_ENV] = str(self.data)
        os.environ["XDG_CACHE_HOME"] = str(self.root / "xdg-cache")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg-config")
        os.environ["XDG_DATA_HOME"] = str(self.root / "xdg-data")
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg-state")

        host_root = _find_host_root()
        if host_root is None:
            self.fail("real host cron.jobs is not available on the parent PYTHONPATH")
        (self.host_jobs, self._saved_sys_path,
         self._saved_cron_modules) = _import_host_jobs(host_root)
        self.addCleanup(self._restore_cron_modules)
        self.addCleanup(self._restore_sys_path)

        self._assert_synthetic_storage()

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _restore_sys_path(self) -> None:
        sys.path[:] = self._saved_sys_path

    def _restore_cron_modules(self) -> None:
        for name in _cron_module_names():
            del sys.modules[name]
        sys.modules.update(self._saved_cron_modules)

    def _assert_synthetic_storage(self) -> None:
        """Fail closed before any mutation if the host store is not the fresh root."""
        if Path(os.environ["HOME"]).resolve() != self.home:
            self.fail("HOME is not the synthetic fresh root")
        if Path(os.environ["HERMES_HOME"]).resolve() != self.hermes_home:
            self.fail("HERMES_HOME is not the synthetic fresh root")

        hermes_dir = Path(getattr(self.host_jobs, "HERMES_DIR", "")).resolve()
        if hermes_dir != self.hermes_home:
            self.fail(f"host HERMES_DIR {hermes_dir} is not the synthetic root")

        jobs_file = Path(getattr(self.host_jobs, "JOBS_FILE", "")).resolve()
        expected_jobs = (self.hermes_home / "cron" / "jobs.json").resolve()
        if jobs_file != expected_jobs:
            self.fail(f"host JOBS_FILE {jobs_file} is not under the synthetic root")

        for label, value in (
            ("CRON_DIR", getattr(self.host_jobs, "CRON_DIR", "")),
            ("OUTPUT_DIR", getattr(self.host_jobs, "OUTPUT_DIR", "")),
        ):
            path = Path(value).resolve()
            if not _within(path, self.hermes_home):
                self.fail(f"host {label} {path} is outside the synthetic root")

        output_getter = getattr(self.host_jobs, "get_cron_output_dir", None)
        if callable(output_getter):
            output_dir = Path(output_getter()).resolve()
            if not _within(output_dir, self.hermes_home):
                self.fail(
                    f"active cron output dir {output_dir} is outside the synthetic root"
                )

    def test_real_host_route_update_preserves_identity_cadence_and_context(self):
        created = _create_synthetic_job(self.host_jobs, {
            "prompt": "Synthetic native contract portal prompt; performs no work.",
            "schedule": SCHEDULE,
            "name": PORTAL,
            "deliver": "local",
            "model": OLD_MODEL,
            "provider": OLD_PROVIDER,
            "reasoning_effort": OLD_EFFORT,
            "skills": ["synthetic-native-contract-skill"],
            "context_from": ["self"],
            "enabled": False,
        })
        self.assertIsNotNone(created)

        matches = [row for row in _host_rows(self.host_jobs)
                   if str(row.get("name") or "") == PORTAL]
        self.assertEqual(len(matches), 1, matches)
        job_id = str(matches[0].get("id") or "")
        self.assertTrue(job_id, "the host created job must expose an exact id")

        before = _host_job(self.host_jobs, job_id)
        self.assertEqual(str(before.get("id") or ""), job_id)
        before_schedule = before.get("schedule")
        self.assertIsInstance(before_schedule, dict)
        self.assertEqual(before_schedule.get("kind"), "cron")
        self.assertEqual(before_schedule.get("expr"), SCHEDULE)
        before_schedule = dict(before_schedule)
        before["schedule"] = before_schedule
        self.assertEqual(_context_list(before.get("context_from")), ["self"])
        self.assertEqual(str(before.get("provider") or ""), OLD_PROVIDER)
        self.assertEqual(str(before.get("model") or ""), OLD_MODEL)
        self.assertEqual(str(before.get("reasoning_effort") or ""), OLD_EFFORT)

        ownership = {
            "schema": cron_mod.JOB_OWNERSHIP_SCHEMA,
            "plugin": cron_mod.OWNED_PLUGIN,
            "jobs": {
                PORTAL: {
                    "id": job_id,
                    "name": PORTAL,
                    "created_at": "2026-11-23T00:00:00+00:00",
                }
            },
        }
        backend = _HostReadBackend(self.host_jobs)

        result = cron_mod.update_portal_route(
            backend, NEW_PROVIDER, NEW_MODEL, NEW_EFFORT, ownership=ownership)

        self.assertEqual(result.action, "updated", result.detail)
        self.assertIn(f"id={job_id}", result.detail)

        persisted = _host_job(self.host_jobs, job_id)

        self.assertEqual(str(persisted.get("id") or ""), job_id)
        self.assertEqual(persisted.get("schedule"), before_schedule)
        self.assertEqual(_context_list(persisted.get("context_from")), ["self"])
        self.assertEqual(str(persisted.get("provider") or ""), NEW_PROVIDER)
        self.assertEqual(str(persisted.get("model") or ""), NEW_MODEL)
        self.assertEqual(str(persisted.get("reasoning_effort") or ""), NEW_EFFORT)

        route_fields = {"provider", "model", "reasoning_effort"}
        for key, value in before.items():
            if key in route_fields:
                continue
            self.assertEqual(persisted.get(key), value, key)

        self.assertEqual(cron_mod._job_id(persisted), job_id)
        self.assertEqual(str(persisted.get("name") or ""), PORTAL)
        self.assertEqual(str(persisted.get("deliver") or ""), "local")
        self.assertEqual(ownership["jobs"][PORTAL]["id"], job_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
