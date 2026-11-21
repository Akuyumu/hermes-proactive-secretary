"""Strict registration + bounded behavior of plugin-owned preparation tools."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import secretary.secretary_work as W  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")
PLUGIN = "hermes-proactive-secretary"


class FakeCtx:
    """Mirrors the verified host signature exactly; no permissive **kwargs."""
    def __init__(self):
        self.calls = []

    def register_tool(self, name, toolset, schema, handler,
                      check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        self.calls.append({
            "name": name, "toolset": toolset, "schema": schema, "handler": handler,
            "check_fn": check_fn, "requires_env": requires_env, "is_async": is_async,
            "description": description, "emoji": emoji, "override": override,
        })
        return object()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.root / "home"),
            "HERMES_PROACTIVE_SECRETARY_DATA": str(self.data),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(self.data / "var", SeededClock(T0.isoformat()))
        # Handlers reopen the same state through _store(); keep their clock
        # aligned with the claim fixture instead of letting wall time expire it.
        clock_patch = mock.patch(
            "proactive_pilot.clock.Clock", return_value=SeededClock(T0.isoformat()))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def make_claim(self):
        rec = R.create(
            self.store, desired_outcome="Prepare checklist",
            acceptance_criterion="Covers requirements", provenance={
                "kind": "user_commitment", "source_ref": "conversation:1",
                "conversation_ref": "chat", "at": T0.isoformat()},
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["mail:msg-1"],
            facts=[{"ref": "mail:msg-1", "text": "Requirement A.",
                    "provenance": "authorized mail", "observed_at": T0.isoformat()}])
        claim = R.claim_due(self.store, now=T0)
        return rec, claim

    def test_registration_uses_real_signature_and_no_override(self):
        ctx = FakeCtx()
        W.register_tools(ctx)
        self.assertEqual([c["name"] for c in ctx.calls], [W.READ_TOOL, W.WRITE_TOOL])
        for call in ctx.calls:
            self.assertEqual(call["toolset"], W.TOOLSET)
            self.assertFalse(call["override"])
            self.assertTrue(callable(call["handler"]))
            self.assertFalse(call["schema"]["parameters"]["additionalProperties"])
            self.assertNotIn("path", call["schema"]["parameters"]["properties"])
            self.assertNotIn("command", call["schema"]["parameters"]["properties"])

    def test_read_brief_is_claim_scoped(self):
        rec, claim = self.make_claim()
        ok = json.loads(W.read_brief({"responsibility_id": rec["id"],
                                      "claim_token": claim["claim_token"]},
                                     gate_fn=lambda: True))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["brief"]["responsibility_id"], rec["id"])
        self.assertEqual(ok["brief"]["facts"][0]["trust"], "untrusted_source_data")
        bad = json.loads(W.read_brief({"responsibility_id": rec["id"],
                                       "claim_token": "wrong"}, gate_fn=lambda: True))
        self.assertFalse(bad["ok"])

    def test_write_artifact_only_beneath_owned_root_and_never_completes(self):
        rec, claim = self.make_claim()
        out = json.loads(W.write_artifact({
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"],
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "Checklist", "content": "# Draft\n\n- [ ] Requirement A (mail:msg-1)\n",
            "evidence_refs": ["mail:msg-1"], "format": "markdown",
        }, gate_fn=lambda: True))
        self.assertTrue(out["ok"], out)
        artifact = out["artifact"]
        self.assertTrue(artifact["queued"])
        self.assertFalse(artifact["completed"])
        path = self.data / "preparation" / "artifacts" / artifact["artifact_ref"].rsplit(":", 1)[-1]
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, (self.data / "preparation" / "artifacts").resolve())
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        read = R.inspect(self.store, rec["id"])
        self.assertEqual(read["status"], R.ACCEPTED)
        self.assertEqual(read["ready"]["status"], "queued")
        from proactive_pilot.preparation import foreground_handoff, list_ready
        self.assertEqual(len(list_ready(self.store)), 1)
        handoff = foreground_handoff(self.store, rec["id"], now=T0)
        self.assertEqual(handoff["execution"], "not_performed")
        self.assertEqual(handoff["artifact_ref"], artifact["artifact_ref"])

    def test_write_rejects_unclaimed_or_unsafe_content(self):
        rec, claim = self.make_claim()
        bad = json.loads(W.write_artifact({
            "responsibility_id": rec["id"], "claim_token": "wrong",
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "x", "evidence_refs": ["mail:msg-1"],
        }, gate_fn=lambda: True))
        self.assertFalse(bad["ok"])
        html = json.loads(W.write_artifact({
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"],
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "<script>x</script>",
            "evidence_refs": ["mail:msg-1"],
        }, gate_fn=lambda: True))
        self.assertFalse(html["ok"])

    def test_missing_or_revoked_gate_fails_closed(self):
        rec, claim = self.make_claim()
        args = {
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"],
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "safe", "evidence_refs": ["mail:msg-1"],
        }
        no_gate = json.loads(W.write_artifact(args))
        self.assertFalse(no_gate["ok"])
        read_no_gate = json.loads(W.read_brief({
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"]}))
        self.assertFalse(read_no_gate["ok"])
        revoked = json.loads(W.write_artifact(args, gate_fn=lambda: (False, "paused")))
        self.assertFalse(revoked["ok"])
        self.assertIn("not currently authorized", revoked["error"])
        self.assertFalse(any((self.data / "preparation" / "artifacts").glob("*"))
                         if (self.data / "preparation" / "artifacts").exists() else [])

    def test_gate_rechecked_between_model_draft_and_commit(self):
        rec, claim = self.make_claim()
        verdicts = [True, False]

        def gate():
            return verdicts.pop(0)

        out = json.loads(W.write_artifact({
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"],
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "safe", "evidence_refs": ["mail:msg-1"],
        }, gate_fn=gate))
        self.assertFalse(out["ok"])
        self.assertIn("not currently authorized", out["error"])
        read = R.inspect(self.store, rec["id"])
        self.assertEqual(read["ready"]["status"], "")

    def test_registered_handlers_use_supplied_gate_and_never_leak_token(self):
        ctx = FakeCtx()
        W.register_tools(ctx, gate_provider=lambda: True)
        rec, claim = self.make_claim()
        reader = ctx.calls[0]["handler"]
        ok = json.loads(reader({"responsibility_id": rec["id"],
                                "claim_token": claim["claim_token"]}))
        self.assertTrue(ok["ok"])
        self.assertNotIn(claim["claim_token"], json.dumps(ok))
        writer = ctx.calls[1]["handler"]
        bad = json.loads(writer({
            "responsibility_id": rec["id"], "claim_token": "wrong-token",
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "x", "evidence_refs": ["mail:msg-1"]}))
        self.assertFalse(bad["ok"])
        self.assertNotIn("wrong-token", bad["error"])

        closed = FakeCtx()
        W.register_tools(closed)
        failed = json.loads(closed.calls[1]["handler"]({
            "responsibility_id": rec["id"], "claim_token": claim["claim_token"],
            "generation": claim["generation"], "source_version": claim["source_version"],
            "title": "x", "content": "x", "evidence_refs": ["mail:msg-1"]}))
        self.assertFalse(failed["ok"])

    def test_module_loads_with_only_src_on_sys_path(self):
        script = (
            "import importlib.util,json,sys\n"
            f"sys.path[:] = [p for p in sys.path if p not in ({str(ROOT)!r}, '' )]\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            f"spec=importlib.util.spec_from_file_location('secretary_work_probe',"
            f"{str(ROOT / 'secretary' / 'secretary_work.py')!r})\n"
            "mod=importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "store=mod._store()\n"
            "print(json.dumps({'root': str(store.root)}))\n"
        )
        env = dict(os.environ)
        env.update({"HERMES_PROACTIVE_SECRETARY_DATA": str(self.data),
                    "HERMES_HOME": str(self.root / "home")})
        proc = __import__("subprocess").run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(payload["root"], str(self.data / "var"))


if __name__ == "__main__":
    unittest.main()
