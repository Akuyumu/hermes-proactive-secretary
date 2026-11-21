"""A persisted user configuration must contain only real schema sections.

Regression: the migrate/init handlers used to write the caller-facing
``_warnings``/``_explicit_version`` diagnostics to disk, so every later load
logged 'ignoring unknown config key'. Verified through the registered CLI
module, not a detached copy of the filter.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from test_final_runtime_seams import _load_plugin_package  # noqa: E402

INTERNAL_PREFIX = "_"


class ConfigPersistenceIsClean(unittest.TestCase):
    def test_internal_keys_are_not_persisted(self):
        _load_plugin_package("persist_probe_plugin")
        cli = importlib.import_module("persist_probe_plugin.portal.portal_cli")
        from proactive_pilot import userconfig

        payload = userconfig.validate_config(userconfig.default_config())
        self.assertTrue(
            any(str(k).startswith(INTERNAL_PREFIX) for k in payload),
            "precondition: validation still returns internal diagnostics",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "user_config.json"
            status = cli._write_user_config(target, payload, force=True)
            self.assertEqual(status, "written")
            import json
            on_disk = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual([k for k in on_disk if str(k).startswith(INTERNAL_PREFIX)], [])
            self.assertIn("schema", on_disk)
            self.assertIn("notification", on_disk)
            # And the cleaned file reloads without complaint.
            loaded = userconfig.load_user_config(target)
            self.assertTrue(loaded.present)


if __name__ == "__main__":
    unittest.main()
