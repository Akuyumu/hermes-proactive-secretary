"""Installed-package entrypoint qualification on a synthetic Hermes profile.

This test extracts the real code-only package into a fresh synthetic profile and
lets the ACTUAL Hermes PluginManager discover/register it. It never reads the
real profile, installs dependencies, enables a live plugin, sends a message or
calls a provider/model. The model-wire path remains a parent live gate.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import release_export  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.datetime.fromisoformat("2026-10-20T12:00:00+00:00")


def _runtime_argv(code: str) -> list[str]:
    """Build a child command from the already-provisioned native runtime.

    This process already runs inside the actual Hermes runtime (the parent
    verified ``sys.executable``/``sys.path`` before state was created), so the
    child reuses that exact interpreter and a filtered snapshot of this path.
    It never shells out to the managed ``hermes`` launcher or imports
    ``hermes_bootstrap`` again, which would re-provision a synthetic profile.
    Candidate source, tests, CWD and empty entries are removed so the child sees
    only the host SDK/dependencies plus the extracted in-profile plugin.
    """
    candidate = ROOT.resolve()
    blocked = {candidate, (candidate / "src"), (candidate / "tests"),
               Path.cwd().resolve()}
    paths: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved in blocked or candidate in resolved.parents:
            continue
        text = str(resolved)
        if text not in paths:
            paths.append(text)
    bootstrap = (
        "import sys\n"
        f"sys.path[:0] = {paths!r}\n"
        "sys._hermes_pin_default_home = True\n"
    )
    return [sys.executable, "-I", "-c", bootstrap + code]


def _reap_child_group(pid: int) -> None:
    """Terminate only this test child's own new process group, then reap it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)


def _run_native_child(code: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a child in the actual provisioned runtime, isolated from this test tree."""
    argv = _runtime_argv(code)
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        _reap_child_group(proc.pid)
        proc.communicate()
        raise
    finally:
        _reap_child_group(proc.pid)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


class InstalledLastMileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.profile = self.root / "profile"
        self.data = self.root / "data"
        self.home.mkdir()
        self.profile.mkdir()
        self.data.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ, HOME=str(self.home), HERMES_HOME=str(self.profile),
                   TMPDIR=str(self.root), PYTHONDONTWRITEBYTECODE="1",
                   HERMES_PROACTIVE_SECRETARY_DATA=str(self.data),
                   UV_OFFLINE="1", TZ="UTC")
        # The bootstrap is already isolated; never pass candidate source/test/CWD
        # paths or a managed-venv facade through to the native child.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("VIRTUAL_ENV", None)
        env.update(extra)
        return env

    def _assert_no_runtime_install(self):
        installs = self.profile / "installs"
        listing = ([p.relative_to(self.profile).as_posix()
                    for p in self.profile.rglob("*")] if installs.exists() else [])
        self.assertFalse(installs.exists(),
                         f"native test child created profile/installs "
                         f"(dependency installation): {listing!r}")

    def test_code_only_package_loads_through_actual_plugin_manager(self):
        package = self.root / "secretary.tar.gz"
        release_export.build_package(package, root=ROOT)
        plugin_dir = self.profile / "plugins" / "hermes-proactive-secretary"
        plugin_dir.mkdir(parents=True)
        with tarfile.open(package) as archive:
            archive.extractall(plugin_dir)
        self.assertTrue((plugin_dir / "runners" / "proactive_secretary_prepare_runner.py").is_file())
        (self.profile / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - hermes-proactive-secretary\n", encoding="utf-8")
        code = r'''
import argparse, contextlib, io, json, os, sys
from pathlib import Path
import hermes_constants
import hermes_cli
import hermes_cli.plugins
assert hermes_constants.get_hermes_home().resolve() == Path(os.environ["HERMES_HOME"]).resolve()
plugin_dir = Path(os.environ["PLUGIN_DIR"]).resolve()
for native in (hermes_constants, hermes_cli, hermes_cli.plugins):
    source = Path(native.__file__).resolve()
    assert source != plugin_dir and plugin_dir not in source.parents, source
network = []
def _audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo"):
        network.append(event)
sys.addaudithook(_audit)
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins(force=True)
mgr = get_plugin_manager()
loaded = [p for p in mgr._plugins.values()
          if p.manifest and p.manifest.name == "hermes-proactive-secretary"]
assert len(loaded) == 1, loaded
assert not loaded[0].error, loaded[0].error
plugin_paths = []
for module in list(sys.modules.values()):
    source = getattr(module, "__file__", None)
    if not source:
        continue
    try:
        path = Path(source).resolve()
    except Exception:
        continue
    if path == plugin_dir or plugin_dir in path.parents:
        plugin_paths.append(str(path))
for obj in (loaded[0], loaded[0].manifest):
    for value in vars(obj).values():
        if isinstance(value, (str, os.PathLike)):
            try:
                path = Path(value).resolve()
            except Exception:
                continue
            if path == plugin_dir:
                plugin_paths.append(str(path))
assert plugin_paths, ("plugin not loaded from extracted install", str(plugin_dir))
assert any(plugin_dir == Path(path) or plugin_dir in Path(path).parents
           for path in plugin_paths), plugin_paths
assert "secretary" in mgr._cli_commands
assert "secretary_responsibility" in loaded[0].tools_registered
assert "secretary_onboarding" in loaded[0].tools_registered
assert "pre_llm_call" in loaded[0].hooks_registered
cmd = mgr._cli_commands["secretary"]
parser = argparse.ArgumentParser()
cmd["setup_fn"](parser)
ns = parser.parse_args(["status", "--json"])
status_out = io.StringIO()
with contextlib.redirect_stdout(status_out):
    rc = cmd["handler_fn"](ns)
status_payload = json.loads(status_out.getvalue())
assert status_payload["jobs"]["available"] is True, status_payload["jobs"]
assert status_payload["jobs"]["state"] == "none", status_payload["jobs"]
assert not network, network
print("\nRESULT", json.dumps({"rc": rc, "tools": sorted(loaded[0].tools_registered),
                              "hooks": sorted(loaded[0].hooks_registered),
                              "commands": sorted(mgr._cli_commands),
                              "jobs": status_payload["jobs"],
                              "plugin_paths": sorted(set(plugin_paths))[:8],
                              "network": network}))
'''
        proc = _run_native_child(code, self._env(PLUGIN_DIR=str(plugin_dir)))
        self._assert_no_runtime_install()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        marker = proc.stdout.rsplit("RESULT ", 1)[-1].strip()
        payload = json.loads(marker)
        self.assertEqual(payload["rc"], 0)
        self.assertEqual(payload["network"], [])
        self.assertTrue(payload["plugin_paths"])
        self.assertTrue(any(str(plugin_dir.resolve()) in path
                            for path in payload["plugin_paths"]))
        self.assertIn("secretary_responsibility", payload["tools"])
        self.assertIn("pre_llm_call", payload["hooks"])
        self.assertIn("secretary", payload["commands"])
        self.assertTrue(payload["jobs"]["available"])
        self.assertEqual(payload["jobs"]["state"], "none")
        self.assertNotIn("Traceback", proc.stderr)

    def test_installed_cli_prepare_dispatches_with_fake_only_llm_wire(self):
        package = self.root / "secretary.tar.gz"
        release_export.build_package(package, root=ROOT)
        plugin_dir = self.profile / "plugins" / "hermes-proactive-secretary"
        plugin_dir.mkdir(parents=True)
        with tarfile.open(package) as archive:
            archive.extractall(plugin_dir)
        data = self.data
        config = uc.default_config()
        config["timezone"] = "UTC"
        config["preparation"] = {"enabled": True, "scopes": ["conversation"],
                                 "source_consent": False}
        (data / "user_config.json").write_text(
            json.dumps(uc.validate_config(config)), encoding="utf-8")
        (data / "preferences.json").write_text(json.dumps({
            "explicit": [], "inferred": [], "snoozes": [], "paused": {}}),
            encoding="utf-8")
        store = StateStore(data / "var", SeededClock(T0.isoformat()))
        R.create(
            store, desired_outcome="Draft a short note",
            acceptance_criterion="One short note",
            provenance={"kind": "user_commitment", "source_ref": "conversation:c1",
                        "conversation_ref": "conversation:c1", "at": T0.isoformat()},
            due_at=(T0 - datetime.timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:c1"],
            facts=[{"ref": "conversation:c1", "text": "Please draft a short note.",
                    "provenance": "conversation", "observed_at": T0.isoformat()}],
            now=T0)
        (self.profile / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - hermes-proactive-secretary\n", encoding="utf-8")
        code = r'''
import argparse, json, os, sys
from pathlib import Path
import hermes_constants
import hermes_cli
import hermes_cli.plugins
assert hermes_constants.get_hermes_home().resolve() == Path(os.environ["HERMES_HOME"]).resolve()
plugin_dir = Path(os.environ["PLUGIN_DIR"]).resolve()
for native in (hermes_constants, hermes_cli, hermes_cli.plugins):
    source = Path(native.__file__).resolve()
    assert source != plugin_dir and plugin_dir not in source.parents, source
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins(force=True)
mgr = get_plugin_manager()
loaded = [p for p in mgr._plugins.values()
          if p.manifest and p.manifest.name == "hermes-proactive-secretary"]
assert len(loaded) == 1 and not loaded[0].error, loaded
secretary_cli = next(m for n, m in sys.modules.items()
                     if n.endswith(".secretary_cli")
                     and hasattr(m, "set_native_context"))
assert plugin_dir in Path(secretary_cli.__file__).resolve().parents
from proactive_pilot import responsibilities as R
from proactive_pilot.clock import SeededClock
from proactive_pilot.native_runtime import bind_model_lane

class Llm:
    def __init__(self):
        self.calls = []
    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        class Result:
            pass
        result = Result()
        result.parsed = {"title": "Note", "format": "markdown",
                         "content": "Short note [[ref: conversation:c1]]",
                         "evidence_refs": ["conversation:c1"]}
        result.text = json.dumps(result.parsed)
        return result

class Ctx:
    def __init__(self):
        self.llm = Llm()
        self.settings = {}
    def get_config(self, key, default=None):
        return self.settings.get(key, default)

ctx = Ctx()
secretary_cli.set_native_context(ctx)
bind_model_lane(ctx, allowed=True, status={
    "strict_active": True, "configured_model_calls_allowed": True})
cmd = mgr._cli_commands["secretary"]
parser = argparse.ArgumentParser()
cmd["setup_fn"](parser)
ns = parser.parse_args(["prepare", "--now", os.environ["SEEDED_NOW"]])
rc = cmd["handler_fn"](ns)
from proactive_pilot.state import StateStore
store = StateStore(os.path.join(os.environ["HERMES_PROACTIVE_SECRETARY_DATA"], "var"),
                   SeededClock(os.environ["SEEDED_NOW"]))
records = R.list_records(store)
print("\nRESULT", json.dumps({
    "rc": rc, "llm_calls": len(ctx.llm.calls),
    "ready": [r["ready"]["status"] for r in records],
    "artifacts": [r["ready"]["artifact_ref"] for r in records],
}))
'''
        proc = _run_native_child(code, self._env(
            PLUGIN_DIR=str(plugin_dir), SEEDED_NOW=T0.isoformat()))
        self._assert_no_runtime_install()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        marker = proc.stdout.rsplit("RESULT ", 1)[-1].strip()
        payload = json.loads(marker)
        self.assertEqual(payload["rc"], 0)
        self.assertEqual(payload["llm_calls"], 1)
        self.assertEqual(payload["ready"], ["queued"])
        self.assertTrue(payload["artifacts"][0].startswith("artifact:"))
        self.assertNotIn("Traceback", proc.stderr)
        # The native prepare runner contract is silent stdout.
        self.assertNotIn("claim_token", proc.stdout)


if __name__ == "__main__":
    unittest.main()
