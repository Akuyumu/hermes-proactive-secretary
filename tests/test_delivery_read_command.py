"""Offline acceptance for ``hermes portal deliveries``."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_package():
    name = "hermes_proactive_portal_delivery_read_qa"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = _load_package()
cli = importlib.import_module(f"{PKG.__name__}.portal.portal_cli")

from proactive_pilot import deliveries  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class DeliveryReadCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "var"
        self.clock = SeededClock(T0.isoformat(), timezone_name="UTC")
        self.store = StateStore(self.state, self.clock)
        self._patch = mock.patch.object(cli.paths, "state_dir",
                                        return_value=self.state)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _run(self, **kwargs):
        args = argparse.Namespace(json=kwargs.pop("json", True),
                                  since=kwargs.pop("since", None),
                                  limit=kwargs.pop("limit", 10))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.deliveries_command(args)
        return code, out.getvalue(), err.getvalue()

    def _record(self, index=0, *, now=None, message=None):
        deliveries.record_emission(
            self.store,
            message=message or f"notice {index}",
            kind="portal-notice",
            event_key=f"evt-{index}",
            notice_id=f"n-{index}",
            job_id="job-1",
            destination="telegram:owner",
            now=now or T0 + timedelta(minutes=index),
        )

    def test_empty_store_is_an_honest_empty_success(self):
        code, out, _err = self._run(json=False)
        self.assertEqual(code, 0)
        self.assertIn("no deliveries", out)
        code, out, _err = self._run(json=True)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["deliveries"], [])

    def test_limit_default_and_hard_max(self):
        for index in range(60):
            self._record(index)
        code, out, _err = self._run(json=True)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 10)

        code, out, _err = self._run(json=True, limit=999)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["count"], 50)

        code, out, _err = self._run(json=True, limit=0)
        self.assertEqual(code, 1)
        self.assertIn("at least 1", json.loads(out)["error"])

    def test_since_filters_and_is_echoed(self):
        self._record(0, now=T0)
        self._record(1, now=T0 + timedelta(hours=1))
        since = (T0 + timedelta(minutes=30)).isoformat()
        code, out, _err = self._run(json=True, since=since)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["deliveries"][0]["message"], "notice 1")
        self.assertEqual(payload["since"], since)

    def test_malformed_since_is_a_bounded_error(self):
        self._record()
        code, out, err = self._run(json=True, since="not-a-date")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("malformed --since", payload["error"])
        self.assertNotIn(str(self.state), out)
        self.assertEqual(err, "")

    def test_corrupt_store_is_not_a_fabricated_empty_success(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / deliveries.STORE).write_text("{broken", encoding="utf-8")
        code, out, err = self._run(json=True)
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("corrupt", payload["error"].lower())
        self.assertNotIn("deliveries\": []", out)
        self.assertNotIn(str(self.root), out)
        self.assertEqual(err, "")

    def test_json_shape_has_no_secret_fields_or_host_path(self):
        self._record(message="A delivered cheat sheet",
                     now=datetime.now(timezone.utc))
        code, out, _err = self._run(json=True, limit=5)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        row = payload["deliveries"][0]
        self.assertEqual(set(row), {"at", "kind", "destination", "message"})
        self.assertEqual(row["message"], "A delivered cheat sheet")
        self.assertNotIn(str(self.root), out)
        self.assertNotIn(str(self.state), out)
        self.assertNotIn("job_id", out)
        self.assertNotIn("evidence_refs", out)
        self.assertNotIn("execution_id", out)

    def test_read_path_does_not_use_the_network_or_host_db(self):
        self._record()
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("socket.create_connection",
                           side_effect=AssertionError("network")):
            code, out, _err = self._run(json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 1)


if __name__ == "__main__":
    unittest.main()
