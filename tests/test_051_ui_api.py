"""Supported 0.5.1 settings-page packaging, schema, API and synthetic JS tests."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig  # noqa: E402

PLUGIN_NAME = "hermes-proactive-secretary"

def _version_from(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


class PackagingAndSchemaTests(unittest.TestCase):
    def test_package_includes_desktop_dashboard_and_clean_scan(self):
        import release_export as exporter

        manifest_version = _version_from(ROOT / "plugin.yaml", r"(?m)^version:\s*(\S+)\s*$")
        module_version = _version_from(ROOT / "src" / "proactive_pilot" / "__init__.py",
                                       r'__version__\s*=\s*"([^"]+)"')
        self.assertEqual(manifest_version, module_version)
        self.assertEqual(manifest_version, "1.0.4")
        report = exporter.scan(ROOT)
        self.assertTrue(report["ok"], report["problems"])
        for name in ("desktop/plugin.js", "dashboard/plugin_api.py", "dashboard/manifest.json"):
            self.assertIn(name, report["files"])
        scratch = tempfile.TemporaryDirectory(
            prefix="secretary-051-package-", dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(scratch.cleanup)
        out = Path(scratch.name) / "package.tar.gz"
        result = exporter.build_package(out, root=ROOT)
        self.assertTrue(result["ok"])
        with tarfile.open(out, "r:gz") as archive:
            members = set(archive.getnames())
            archive.extractall(Path(scratch.name) / "extracted", filter="data")
        self.assertIn("desktop/plugin.js", members)
        self.assertIn("dashboard/plugin_api.py", members)
        self.assertIn("dashboard/manifest.json", members)
        self.assertIn("docs/operations.md", members)
        # Every relative README link must work in the shipped archive, not just Git.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)]+)\)", readme):
            if "://" not in link and not link.startswith("#"):
                self.assertIn(link.split("#", 1)[0], members)
        self.assertFalse(any(name.startswith((".build-context", "tests/")) for name in members))
        self.assertFalse(any("private" in Path(name).parts for name in members))

        # The extracted artifact, not the source checkout, is what a host sees.
        extracted = Path(scratch.name) / "extracted"
        self.assertTrue(exporter.scan(extracted)["ok"])
        imported = exporter._import_registration(extracted)
        self.assertTrue(imported["ok"], imported)
        dashboard = json.loads((extracted / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(dashboard["version"], "1.0.4")
        self.assertEqual(dashboard["api"], "plugin_api.py")

    def test_doctor_requires_the_real_custom_ui_and_api(self):
        import release_export as exporter
        scratch = tempfile.TemporaryDirectory(
            prefix="secretary-051-doctor-", dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(scratch.cleanup)
        extracted = Path(scratch.name) / "package"
        extracted.mkdir()
        for name in ("plugin.yaml", "__init__.py", "paths.py", "LICENSE"):
            shutil.copy(ROOT / name, extracted / name)
        (extracted / "secretary").mkdir()
        shutil.copy(ROOT / "secretary" / "secretary_cli.py",
                    extracted / "secretary" / "secretary_cli.py")
        src = extracted / "src" / "proactive_pilot"
        src.mkdir(parents=True)
        shutil.copy(ROOT / "src" / "proactive_pilot" / "__init__.py", src / "__init__.py")
        runners = extracted / "runners"
        runners.mkdir()
        for name in ("proactive_secretary_runner.py", "proactive_secretary_reminder_runner.py",
                     "proactive_secretary_prepare_runner.py"):
            (runners / name).write_text("", encoding="utf-8")
        result = exporter.doctor(extracted)
        self.assertFalse(result["ok"])
        self.assertIn("desktop/plugin.js", result["missing"])
        self.assertIn("dashboard/plugin_api.py", result["missing"])
        self.assertIn("dashboard/manifest.json", result["missing"])

    def test_manifest_and_desktop_id_match_native_plugin(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "hermes-proactive-secretary")
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue((ROOT / "dashboard" / manifest["api"]).is_file())
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("id: 'hermes-proactive-secretary'", js)
        self.assertIn("ctx.registerSettingsPage?.(", js)
        imports = re.findall(r"^import .*? from '([^']+)'", js, re.MULTILINE)
        self.assertEqual(set(imports), {"@hermes/plugin-sdk", "react/jsx-runtime", "react"})

    def test_native_schema_fields_have_typed_mapping(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^config_schema:\s*\n(.*?)(?=^\S|\Z)", text)
        self.assertIsNotNone(match)
        schema_keys = set(re.findall(r"(?m)^  ([a-z][a-z0-9_]*):\s*$", match.group(1)))
        self.assertTrue(schema_keys)
        allowed = set(userconfig.NATIVE_FLAT_FIELDS) | {
            "primary_provider", "primary_model", "fallback_provider", "fallback_model",
            "portal_reasoning_effort"}
        self.assertLessEqual(schema_keys, allowed)
        import yaml
        spec = yaml.safe_load(text)["config_schema"]["portal_reasoning_effort"]
        self.assertEqual((spec["type"], spec["default"]), ("str", ""))


class _ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"FastAPI test client unavailable: {type(exc).__name__}")
        spec = importlib.util.spec_from_file_location(
            "secretary_051_plugin_api", ROOT / "dashboard" / "plugin_api.py")
        cls.api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.api
        spec.loader.exec_module(cls.api)
        app = FastAPI()
        app.include_router(cls.api.router)
        cls.client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        from test_051_reset import Harness
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.api._service = lambda: self.h.actions


class ResetApiTests(_ApiBase):
    def _prepare(self):
        response = self.client.post("/setup/reset/prepare", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_status_and_trigger_are_read_only(self):
        before = self.h.snapshot()
        status = self.client.get("/setup/status")
        self.assertEqual(status.status_code, 200, status.text)
        payload = status.json()
        self.assertTrue(payload["ok"])
        self.assertNotIn("owner@example.test", status.text)
        trigger = self.client.post("/setup/trigger", json={})
        self.assertEqual(trigger.status_code, 200, trigger.text)
        self.assertFalse(trigger.json()["changed"])
        self.assertEqual(self.h.snapshot(), before)

    def test_cross_site_request_is_refused_without_writes(self):
        before = self.h.snapshot()
        response = self.client.post("/setup/reset/prepare", json={},
                                    headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.h.snapshot(), before)

    def test_wrong_phrase_and_wrong_challenge_write_nothing(self):
        plan = self._prepare()
        before = self.h.snapshot()
        wrong_phrase = self.client.post("/setup/reset/commit", json={
            "challenge": plan["challenge"], "phrase": "reset", "explicit": True})
        self.assertEqual(wrong_phrase.status_code, 400)
        self.assertEqual(wrong_phrase.json()["error"], "phrase_mismatch")
        self.assertFalse((self.h.backup_root / "backups").exists())

        wrong_challenge = self.client.post("/setup/reset/commit", json={
            "challenge": "wrong", "phrase": "RESET", "explicit": True})
        self.assertEqual(wrong_challenge.status_code, 409)
        self.assertFalse((self.h.backup_root / "backups").exists())
        after = self.h.snapshot()
        self.assertEqual(after["settings"], before["settings"])
        self.assertEqual(after["prefs"], before["prefs"])

    def test_confirmed_reset_returns_exact_scope_and_backup(self):
        plan = self._prepare()
        response = self.client.post("/setup/reset/commit", json={
            "challenge": plan["challenge"], "phrase": "RESET", "explicit": True})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["cleared"], plan["cleared"])
        self.assertEqual(payload["preserved"], plan["preserved"])
        self.assertTrue((self.h.backup_root / "backups" / payload["backup_id"]).is_dir())
        self.assertFalse(payload["permissions"]["reading_enabled"])


class SettingsApiTests(_ApiBase):
    def test_settings_round_trip_is_explicit_typed_and_read_back(self):
        before = self.h.snapshot()
        read = self.client.get("/settings")
        self.assertEqual(read.status_code, 200, read.text)
        payload = read.json()
        self.assertEqual(payload["values"]["timezone"], "Europe/London")
        self.assertEqual(payload["values"]["public_interests"], ["local transit"])
        revision = payload["revision"]

        update = self.client.post("/settings", json={
            "revision": revision,
            "values": {"timezone": "Asia/Tokyo", "destination": ""}
        })
        self.assertEqual(update.status_code, 200, update.text)
        result = update.json()
        self.assertTrue(result["changed"])
        self.assertEqual(result["values"]["timezone"], "Asia/Tokyo")
        self.assertEqual(result["values"]["destination"], "")
        self.assertIn(("backup_id"), result)
        readback = self.client.get("/settings").json()
        self.assertEqual(readback["values"]["timezone"], "Asia/Tokyo")
        self.assertEqual(readback["values"]["destination"], "")
        self.assertNotEqual(readback["revision"], revision)
        # Parent connection/source/model values were not replaced by the form edit.
        self.assertEqual(self.h.ctx.settings["user_config"]["mail"],
                         userconfig.load_user_config(self.h.config_path, native=self.h.ctx).config["mail"])
        self.assertTrue((self.h.backup_root / "backups" / result["backup_id"]).is_dir())
        self.assertNotEqual(before["settings"], self.h.ctx.settings)

    def test_unknown_or_untyped_keys_and_stale_revision_write_nothing(self):
        before = self.h.snapshot()
        unknown = self.client.post("/settings", json={
            "revision": self.client.get("/settings").json()["revision"],
            "values": {"api_key": "not-allowed"}
        })
        self.assertEqual(unknown.status_code, 400)
        invalid = self.client.post("/settings", json={
            "revision": self.client.get("/settings").json()["revision"],
            "values": {"daily_normal_cap": "2.5"}
        })
        self.assertEqual(invalid.status_code, 400)
        stale = self.client.post("/settings", json={
            "revision": "old-revision", "values": {"timezone": "Asia/Tokyo"}})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.h.snapshot(), before)

        # Canonical digit text is accepted by the optional string cap control
        # with the same revision/consent semantics as any ordinary edit.
        revision = self.client.get("/settings").json()["revision"]
        accepted = self.client.post("/settings", json={
            "revision": revision, "values": {"daily_normal_cap": "12"}})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["values"]["daily_normal_cap"], 12)
        self.assertEqual(
            self.client.get("/settings").json()["values"]["daily_normal_cap"], 12)

    def test_permission_expansion_is_previewed_and_requires_explicit_ack(self):
        config = self.h.ctx.settings["user_config"]
        config["sources"]["mail"] = False
        config["public_interests"] = ["keep"]
        self.h.ctx.settings["user_config"] = config
        revision = self.client.get("/settings").json()["revision"]
        before = self.h.snapshot()

        preview = self.client.post("/settings/preview", json={
            "revision": revision,
            "values": {"mail_enabled": True, "public_interests": []}
        })
        self.assertEqual(preview.status_code, 200, preview.text)
        plan = preview.json()
        self.assertTrue(plan["requires_confirmation"])
        self.assertIn("mail_enabled", plan["expanding"])
        self.assertTrue(plan["explanation"])
        self.assertTrue(plan["expansion_details"])
        self.assertEqual(self.h.snapshot(), before)

        refused = self.client.post("/settings", json={
            "revision": revision,
            "values": {"mail_enabled": True, "public_interests": []}
        })
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"], "permission_confirmation_required")
        self.assertEqual(self.h.snapshot(), before)

        result = self.client.post("/settings", json={
            "revision": revision,
            "values": {"mail_enabled": True, "public_interests": []},
            "acknowledged_expanding": plan["expanding"]
        })
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertIn("mail_enabled", payload["expanding"])
        self.assertTrue(payload["explanation"])
        self.assertEqual(payload["values"]["public_interests"], [])

    def test_production_backup_adapter_uses_shared_manifest_schema(self):
        from proactive_pilot import config_backup
        writer = self.api._foundation_backup_writer(self.h.ctx, data_root=self.h.backup_root)
        before = self.h.snapshot()
        result = writer(loaded=None, onboarding=None, preferences=None,
                        reason="owner_confirmed_reset_and_onboard")
        self.assertTrue(result["id"])
        backup = self.h.backup_root / "backups" / result["id"]
        verified = config_backup.verify_backup(backup)
        self.assertTrue(verified["ok"], verified)
        manifest = verified["manifest"]
        self.assertEqual(manifest["schema"], 1)
        names = {entry["name"] for entry in manifest["files"]}
        self.assertIn("native_settings.json", names)
        self.assertIn("onboarding_state.json", names)
        self.assertTrue(names <= config_backup._ALLOWED_FILES)
        self.assertFalse(any(name.startswith(".env") for name in names))
        self.assertEqual(self.h.snapshot(), before)


class PluginContextBindingTests(unittest.TestCase):
    def _api(self):
        spec = importlib.util.spec_from_file_location(
            "secretary_051_plugin_api_context", ROOT / "dashboard" / "plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        return api

    def test_fallback_parses_the_real_manifest_with_the_host_signature(self):
        api = self._api()
        try:
            import hermes_cli.plugins as host_plugins
        except Exception as exc:  # noqa: BLE001 - provisioned host runtime only
            self.skipTest(f"host plugin runtime unavailable: {type(exc).__name__}")
        manager = SimpleNamespace(_plugins={})
        with mock.patch.object(host_plugins, "get_plugin_manager", return_value=manager):
            ctx, package = api._plugin_context()
        self.assertTrue(package)
        self.assertEqual(ctx.manifest.name, "hermes-proactive-secretary")
        self.assertEqual(Path(ctx.manifest.path).resolve(), ROOT.resolve())
        self.assertEqual(ctx.plugin_id, "hermes-proactive-secretary")
        loaded_package = importlib.import_module(package)
        self.assertEqual(Path(loaded_package.__file__).resolve().parent, ROOT.resolve())

    def test_loaded_plugin_branch_uses_real_module_and_manager_storage(self):
        api = self._api()
        try:
            import hermes_cli.plugins as host_plugins
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"host plugin runtime unavailable: {type(exc).__name__}")
        manifest = SimpleNamespace(name=PLUGIN_NAME, skill_namespace=PLUGIN_NAME)
        loaded = SimpleNamespace(manifest=manifest, module=SimpleNamespace(__name__="installed_plugin_pkg"))
        manager = SimpleNamespace(_plugins={f"user/{PLUGIN_NAME}": loaded})
        with mock.patch.object(host_plugins, "get_plugin_manager", return_value=manager):
            ctx, package = api._plugin_context()
        self.assertEqual(package, "installed_plugin_pkg")
        self.assertIs(ctx.manifest, manifest)

    def test_imports_match_the_host_desktop_import_allowlist(self):
        js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        imports = re.findall(r"^import \{([^}]+)\} from '([^']+)'", js, re.MULTILINE)
        allowed = {"@hermes/plugin-sdk", "react/jsx-runtime", "react"}
        self.assertEqual({spec for _, spec in imports}, allowed)
        host_sdk = Path("/home/hermes/.hermes/hermes-agent/apps/desktop/src/sdk/index.ts")
        if not host_sdk.is_file():
            self.skipTest("installed Desktop SDK source unavailable")
        sdk_source = host_sdk.read_text(encoding="utf-8")
        exported = set(re.findall(r"(?m)^export (?:const|function|class) ([A-Za-z_$][\w$]*)", sdk_source))
        exported.update(re.findall(r"(?m)^export \{([^}]+)\}", sdk_source))
        for _names, spec in imports:
            if spec != "@hermes/plugin-sdk":
                continue
            names = [name.strip().split(" as ")[-1] for name in
                     js.split("from '@hermes/plugin-sdk'")[0].split("{", 1)[-1].split("}")[0].split(",")
                     if name.strip()]
            for name in names:
                self.assertTrue(
                    re.search(rf"\b{re.escape(name)}\b", sdk_source),
                    f"{name} is not exported by the installed Desktop SDK")


class SyntheticJsRenderTests(unittest.TestCase):
    def test_settings_page_registers_and_renders_bounded_source_statuses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        scratch = tempfile.TemporaryDirectory(
            prefix="secretary-051-js-", dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        shutil.copy(ROOT / "desktop" / "plugin.js", root / "plugin.mjs")
        modules = root / "node_modules"
        sdk = modules / "@hermes" / "plugin-sdk"
        sdk.mkdir(parents=True)
        (sdk / "package.json").write_text(json.dumps(
            {"name": "@hermes/plugin-sdk", "type": "module", "exports": "./index.js"}))
        (sdk / "index.js").write_text("""
const el = (type, props = {}) => ({ type, props })
export const host = {
  notify() {}, notifyError() {},
  composer: { insertText: async () => { globalThis.__composerCalls = (globalThis.__composerCalls || 0) + 1; return true }, focus() {} }
}
export const Badge = ({ children }) => el('badge', { children })
export const Button = ({ children, onClick, variant, loading }) => el('button', { children, onClick, variant, loading })
export const ConfirmDialog = ({ open, title, description, children }) => open ? el('dialog', { children: [title, description, children] }) : null
export const Input = props => el('input', props)
export const ListRow = ({ title, description, action }) => el('listrow', { children: [title, description, action] })
export const StatusDot = ({ tone }) => el('statusdot', { tone })
export const Switch = ({ checked, onCheckedChange, ...props }) => el('switch', { ...props, checked, onCheckedChange })
export const useQuery = () => globalThis.__status
export const useQueryClient = () => ({ invalidateQueries() {} })
""")
        react = modules / "react"
        react.mkdir()
        (react / "package.json").write_text(json.dumps(
            {"name": "react", "type": "module", "exports": "./index.js"}))
        (react / "index.js").write_text(
            "export function useState(value) { return [value, () => {}] }\n")
        jsx = modules / "react" / "jsx-runtime.js"
        jsx.write_text(
            "export const jsx = (type, props) => ({ type, props: props || {} })\n"
            "export const jsxs = jsx\n")
        (react / "package.json").write_text(json.dumps(
            {"name": "react", "type": "module", "exports": {".": "./index.js", "./jsx-runtime": "./jsx-runtime.js"}}))
        harness = root / "harness.mjs"
        harness.write_text("""
import plugin from './plugin.mjs'

globalThis.__status = {
  data: {
    ok: true,
    onboarding: { status: 'in_progress', label: 'Setup in progress' },
    config: { present: true, source: 'native', error: '' },
    permissions: { needs_reconfirmation: true, paused: false, reading_enabled: false },
    sources: {
      mail: { status: 'fresh', scope_count: 1, remedy: '' },
      calendar: { status: 'stale', scope_count: 1, remedy: '' },
      tasks: { status: 'off', scope_count: 0, remedy: '' },
      public: { status: 'unconfigured', scope_count: 0, remedy: '' },
      obligations: { status: 'unavailable', scope_count: 1, remedy: 'Connector unavailable.' }
    },
    model_routes: { strict_active: false, primary: {}, blockers: [] },
    jobs: { active_reminders: 2, owned_jobs: 1 }
  },
  isLoading: false,
  error: null,
  refetch: async () => {}
}

const texts = []
const buttons = []
const visited = new Set()
function walk(node) {
  if (node == null || node === false) return
  if (Array.isArray(node)) { node.forEach(walk); return }
  if (typeof node === 'string' || typeof node === 'number') { texts.push(String(node)); return }
  if (typeof node !== 'object') return
  if (typeof node.type === 'function') {
    if (visited.has(node.type) && node.type.name !== 'SetupPage') { /* still render once per element */ }
    const props = node.props || {}
    if (typeof props.children === 'string') texts.push(props.children)
    walk(node.type(props))
    return
  }
  const props = node.props || {}
  if (node.type === 'button' && typeof props.children === 'string') buttons.push(props.children)
  walk(props.children)
}

let page = null
const restCalls = []
const ctx = {
  rest: async (path, opts) => { restCalls.push([path, opts && opts.method || 'GET']); return page ? { command: '/secretary setup' } : {} },
  registerSettingsPage: value => { page = value }
}
plugin.register(ctx)
if (!page) throw new Error('settings page not registered')
walk(page.render())
walk(page.children[0].render())
walk(page.children[1].render())

const allTriggers = []
function find(node) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) return node.forEach(find)
  if (typeof node.type === 'function') return find(node.type(node.props || {}))
  if (node.type === 'button' && node.props && node.props.children === 'Trigger onboarding') allTriggers.push(node.props)
  find(node.props && node.props.children)
}
find(page.render())
const trigger = allTriggers[0]

const output = {
  title: page.title,
  children: page.children.map(child => child.title),
  buttons,
  hasFresh: texts.includes('Fresh'),
  hasStale: texts.includes('Stale'),
  hasOff: texts.includes('Off'),
  hasUnconfigured: texts.includes('Not configured'),
  hasUnavailable: texts.includes('Unavailable'),
  claimsConnected: texts.some(value => /^connected$/i.test(value.trim())),
  triggerPresent: buttons.includes('Trigger onboarding'),
  resetPresent: buttons.includes('Reset and onboard'),
  settingsEditable: texts.includes('Edit settings') && buttons.includes('Save settings'),
  rawJsonPrimary: texts.some(value => /raw json/i.test(value)),
  triggerIsHandler: Boolean(trigger && typeof trigger.onClick === 'function')
}
console.log(JSON.stringify(output))
""")
        proc = subprocess.run([node, str(harness)], cwd=str(root), capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["title"], 'Proactive Secretary')
        self.assertEqual(result["children"], ['Sources', 'Advanced'])
        self.assertTrue(result["hasFresh"])
        self.assertTrue(result["hasStale"])
        self.assertTrue(result["hasOff"])
        self.assertTrue(result["hasUnconfigured"])
        self.assertTrue(result["hasUnavailable"])
        self.assertFalse(result["claimsConnected"])
        self.assertTrue(result["triggerPresent"])
        self.assertTrue(result["resetPresent"])
        self.assertTrue(result["settingsEditable"])
        self.assertFalse(result["rawJsonPrimary"])
        self.assertTrue(result["triggerIsHandler"])


if __name__ == "__main__":
    unittest.main()
