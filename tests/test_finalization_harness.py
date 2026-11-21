"""Config-neutral checks for the parent-run finalization harness.

The harness itself is never shipped (``finalization/`` is excluded from the
release/export), so this test SKIPS cleanly in an installed/release tree. In the
candidate workspace it compiles every script, verifies the export rules and
checks the benchmark's schema matcher against the real host contract. No
network, secret, live path or scheduler is touched.
"""
from __future__ import annotations

import importlib.util
import py_compile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "finalization"
SCRIPTS = (
    "common.py", "build_release.py", "native_mcp_benchmark.py",
    "apply_cutover.py", "publish_private.py",
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(HARNESS.is_dir(), "parent-run harness is not shipped")
class FinalizationHarnessTests(unittest.TestCase):
    def test_scripts_compile_and_export_rules_and_schema_match(self):
        for name in SCRIPTS:
            py_compile.compile(str(HARNESS / name), doraise=True)

        sys.path.insert(0, str(HARNESS))
        common = _load("finalization_common_test", HARNESS / "common.py")

        # Nothing private/reference/state ever enters the export.
        for rel in ("host-references/plugins.py", "references/native/agent/plugin_llm.py",
                    "V040_SCOPE.md", "OWNER_DEPLOY_PUBLISH_ACCEPTANCE.md",
                    "policy/policy.json", "var/notify_state.json",
                    "finalization/build_release.py", "tests/__pycache__/x.pyc"):
            forbidden, _reason = common.is_forbidden(rel)
            self.assertTrue(forbidden, rel)
        for rel in ("src/proactive_pilot/main.py", "tests/test_proactive_pilot.py",
                    "policy/policy.default.json", "README.md", "docs/AGENT_SETUP.md",
                    "plugin.yaml", "runners/proactive_secretary_runner.py"):
            forbidden, reason = common.is_forbidden(rel)
            self.assertFalse(forbidden, f"{rel}: {reason}")

        # The benchmark matches the ACTUAL supplied Firecrawl contract.
        bench = _load("finalization_benchmark_test", HARNESS / "native_mcp_benchmark.py")
        contract = bench.load_contract(ROOT / "host-references" / "firecrawl-contract.json")
        schema = bench.verify_scrape_schema(contract, "firecrawl_scrape")
        self.assertTrue(schema["markdown_supported"])
        self.assertTrue(schema["max_age_supported"])
        self.assertEqual(schema["url_arg"], "url")
        self.assertEqual(schema["formats_arg"], "formats")


if __name__ == "__main__":
    unittest.main()
