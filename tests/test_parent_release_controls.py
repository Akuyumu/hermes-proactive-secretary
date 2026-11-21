"""Independent deployment negative controls; no live sources or sends."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from proactive_pilot.candidates import _cap_candidates


class ParentReleaseControls(unittest.TestCase):
    def setUp(self):
        # The runner now consults the native toggle first. Use a fake profile
        # that explicitly enables the plugin so the pipeline is exercised, and
        # never read a live profile config.
        self._home = tempfile.TemporaryDirectory(dir=ROOT)
        home = pathlib.Path(self._home.name)
        (home / "config.json").write_text(json.dumps(
            {"plugins": {"enabled": ["hermes-proactive-portal"]}}), encoding="utf-8")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self._home.cleanup()

    def test_default_unchanged_stdout_is_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            argv = [sys.executable, str(ROOT/'runners/proactive_portal_runner.py'), '--fixture', 'healthy_empty', '--state-dir', d, '--seeded-clock', '2026-10-03T10:00:00+08:00']
            for _ in range(2):
                p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout, '', 'Default scheduler output must never contain debug JSON')

    def test_default_pause_stdout_is_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            prefs = pathlib.Path(d)/'prefs.json'
            prefs.write_text(json.dumps({'schema':1,'paused':{'active':True,'provenance':'test'}}))
            p = subprocess.run([sys.executable, str(ROOT/'runners/proactive_portal_runner.py'), '--fixture', 'healthy_empty', '--state-dir', d, '--preferences', str(prefs), '--seeded-clock', '2026-10-03T10:00:00+08:00'], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, '', 'Global pause is silent, not a user-facing debug message')

    def test_overdue_inventory_does_not_starve_mail(self):
        rows = [{'kind':'tasks','ref':f't{i}','phase':'overdue'} for i in range(300)]
        rows += [{'kind':'mail','ref':'payment','phase':'far'}, {'kind':'calendar','ref':'meeting','phase':'within30min'}]
        selected, omitted = _cap_candidates(rows, 200)
        self.assertTrue(any(x['kind']=='mail' for x in selected), 'Overdue backlog cannot monopolise the evidence packet')
        self.assertTrue(any(x['ref']=='meeting' for x in selected), 'Imminent calendar cannot be starved')
        self.assertEqual(len(selected), 200)


if __name__ == '__main__':
    unittest.main()
