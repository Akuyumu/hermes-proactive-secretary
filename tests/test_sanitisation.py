"""Sanitisation invariants: no personal identifiers ship with the plugin.

Scans the shipped runtime surface (source, policy, skills, manifests, docs,
launchers and the plugin wiring) for owner names, honourifics, absolute personal
home paths and owner-specific seed topics. Worker/reference artifacts
(``references/``, the build scope/report, this session's event log) are input,
not shipped plugin content, and are excluded.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {"references", "__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
EXCLUDED_FILES = {
    "plugin-events.jsonl",
    "PLUGIN_BUILD_SCOPE.md",
    "PLUGIN_CHECKPOINT.md",
    "PLUGIN_REPORT.md",
    "test_sanitisation.py",
    "release_export.py",
}

# Release operators may provide forbidden terms without shipping them.
FORBIDDEN_TERMS = tuple(filter(
    None, os.environ.get("SECRETARY_FORBIDDEN_IDENTIFIERS", "").split(",")))
ABSOLUTE_HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")

SHIPPED_DIRS = ("src", "policy", "skills", "runners", "public_samples")
SHIPPED_SUFFIXES = (".py", ".md", ".json", ".yaml", ".yml", ".html", ".txt")


def _shipped_files():
    for directory in SHIPPED_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
                continue
            if path.name in EXCLUDED_FILES:
                continue
            yield path
    for path in sorted(ROOT.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SHIPPED_SUFFIXES:
            continue
        if path.name in EXCLUDED_FILES or path.name.startswith("."):
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class IdentifierSanitisationTests(unittest.TestCase):
    def test_no_owner_name_or_honourific_in_shipped_files(self):
        offenders = []
        for path in _shipped_files():
            text = _read(path)
            for term in FORBIDDEN_TERMS:
                if term in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {term!r}")
        self.assertEqual(offenders, [], f"personal identifiers found: {offenders}")

    def test_no_absolute_personal_home_path_in_shipped_files(self):
        offenders = []
        for path in _shipped_files():
            for match in ABSOLUTE_HOME_RE.finditer(_read(path)):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(offenders, [], f"absolute personal paths found: {offenders}")

    def test_evaluator_prompt_is_policy_driven(self):
        text = _read(ROOT / "src" / "proactive_pilot" / "evaluator.py")
        self.assertIn("{persona}", text)
        self.assertIn("{notify_address}", text)
        self.assertIn("DEFAULT_PERSONA", text)
        self.assertIn("DEFAULT_NOTIFY_ADDRESS", text)

    def test_hardcoded_source_paths_are_profile_safe(self):
        collectors = _read(ROOT / "src" / "proactive_pilot" / "collectors.py")
        self.assertIn("_default_secretary_src", collectors)
        self.assertIn("HERMES_HOME", collectors)
        evaluator = _read(ROOT / "src" / "proactive_pilot" / "evaluator.py")
        self.assertIn("_default_agent_src", evaluator)
        self.assertIsNone(ABSOLUTE_HOME_RE.search(evaluator))


class PolicyDefaultTests(unittest.TestCase):
    def test_generic_default_has_no_seed_topics(self):
        policy = json.loads(_read(ROOT / "policy" / "policy.default.json"))
        self.assertEqual(policy.get("feed_seed_sources"), [])
        blob = json.dumps(policy).lower()
        for forbidden in ("university.example", "channelnewsasia", "smrt.com.sg", "scholarship"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(policy.get("persona"), "Hermes")
        self.assertEqual(policy.get("notify_address"), "the user")

    def test_only_generic_policy_files_ship(self):
        policy_dir = ROOT / "policy"
        populated = [p.name for p in policy_dir.glob("preferences*.json")]
        self.assertEqual(populated, [], "a populated preferences file must not ship")
        for name in ("policy.default.json", "policy.sample.json"):
            data = json.loads(_read(policy_dir / name))
            self.assertEqual(data.get("feed_seed_sources"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
