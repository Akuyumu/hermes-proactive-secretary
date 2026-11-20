"""Regression: exercise the real argparse seam, not an argv-only fake."""
import argparse
import pathlib
import runpy
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]

class ReminderBridgeParserContract(unittest.TestCase):
    def test_rendered_bridge_parent_options_not_forwarded_to_reminders(self):
        with patch.dict('os.environ', {'HERMES_BIN': '/fake/hermes'}), patch('sys.path', [str(ROOT), *sys.path]):
            import importlib
            from test_final_runtime_seams import _load_plugin_package
            _load_plugin_package('parent_parser_probe_plugin')
            secretary_cli = importlib.import_module('parent_parser_probe_plugin.secretary.secretary_cli')
            runner = runpy.run_path(str(ROOT / 'runners/proactive_secretary_reminder_runner.py'), run_name='bridge_test')
            captured = []
            def parse_actual_cli(argv, **kwargs):
                parser = argparse.ArgumentParser()
                secretary_cli.register_cli(parser)
                args = parser.parse_args(argv[2:])
                self.assertEqual(args.reminder_args, ['dispatch'])
                self.assertEqual(args.data_dir, str(secretary_cli.paths.data_root()))
                captured.append(argv)
                return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            with patch('subprocess.run', side_effect=parse_actual_cli):
                self.assertEqual(runner['_native_cli_bridge'](), 0)
            self.assertEqual(len(captured), 1)
