"""Synthetic request-level deployment scope regressions; no real connectors."""
import argparse
import contextlib
import importlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from proactive_pilot import collectors as C, userconfig as U
from proactive_pilot.clock import SeededClock
from proactive_pilot.reminders import SecretaryEventGetter

ROOT = Path(__file__).resolve().parents[1]
CLOCK = SeededClock("2026-10-05T12:00:00+00:00")


class CalendarClient:
    def __init__(self, allowed=None):
        self.allowed = allowed
        self.requests = []

    def calendars(self):
        return {"items": [
            {"id": "primary", "primary": True},
            {"id": "team", "selected": True},
            {"id": "holiday", "holiday": True},
            {"id": "unselected"},
        ]}

    def _is_holiday_calendar(self, cal):
        return cal.get("holiday", False)

    def _events(self, cid):
        return "https://fake.invalid/calendars/" + cid + "/events"

    def _request(self, method, url):
        self.requests.append((method, url))
        assert method == "GET"
        cid = urlsplit(url).path.split("/")[2]
        assert self.allowed is None or cid in self.allowed, "excluded calendar read"
        query = parse_qs(urlsplit(url).query)
        if query.get("eventTypes") == ["birthday"]:
            return {"items": []}
        token = query.get("pageToken", [""])[0]
        assert token in {"", "next-" + cid}, "cursor belongs to another calendar"
        event = {"id": cid + ("-2" if token else "-1"), "summary": "synthetic",
                 "start": {"dateTime": "2026-10-05T13:00:00+00:00"}}
        result = {"items": [event]}
        if not token:
            result["nextPageToken"] = "next-" + cid
        return result


class TasksClient:
    base = "https://fake.invalid/tasks"

    def __init__(self, allowed=None):
        self.allowed = allowed
        self.requests = []

    def _tasks(self, lid):
        return self.base + "/lists/" + lid + "/tasks"

    def _request(self, method, url):
        self.requests.append((method, url))
        assert method == "GET"
        parsed = urlsplit(url)
        token = parse_qs(parsed.query).get("pageToken", [""])[0]
        if parsed.path.endswith("/users/@me/lists"):
            if not token:
                return {"items": [{"id": "excluded", "title": "Other"}],
                        "nextPageToken": "lists-next"}
            assert token == "lists-next"
            return {"items": [{"id": "wanted", "title": "My Tasks"},
                              {"id": "collision", "title": "wanted"}]}
        lid = parsed.path.split("/")[3]
        assert self.allowed is None or lid in self.allowed, "excluded task list read"
        assert token in {"", "next-" + lid}, "cursor belongs to another task list"
        result = {"items": [{"id": lid + ("-2" if token else "-1"),
                             "status": "needsAction", "title": "synthetic"}]}
        if not token:
            result["nextPageToken"] = "next-" + lid
        return result


class RequestScopeTests(unittest.TestCase):
    def setUp(self):
        self.library = patch.object(C, "secretary_library_error", return_value="")
        self.library.start()
        self.addCleanup(self.library.stop)

    def test_calendar_subset_constrains_pages_and_excludes_primary_birthday(self):
        client = CalendarClient({"team"})
        with patch.object(C, "_calendar_client", return_value=client):
            result = C.collect_calendar("cal", clock=CLOCK, calendar_ids=["team"], page_size=1)
        self.assertEqual(result.status, "ok")
        self.assertEqual({item["calendar"] for item in result.items}, {"team"})
        self.assertEqual(len(client.requests), 2)
        self.assertIn("pageToken=next-team", client.requests[1][1])

    def test_calendar_empty_ids_preserves_selected_and_birthday_reads(self):
        client = CalendarClient({"primary", "team", "holiday"})
        with patch.object(C, "_calendar_client", return_value=client):
            result = C.collect_calendar("cal", clock=CLOCK, calendar_ids=[])
        self.assertEqual(result.count, 6)
        self.assertEqual(len(client.requests), 7)
        self.assertTrue(any("eventTypes=birthday" in url for _, url in client.requests))

    def test_calendar_no_matching_ids_reads_no_events(self):
        client = CalendarClient(set())
        with patch.object(C, "_calendar_client", return_value=client):
            result = C.collect_calendar("cal", clock=CLOCK, calendar_ids=["absent"])
        self.assertEqual(result.status, "ok")
        self.assertEqual(client.requests, [])

    def test_task_list_scope_constrains_item_pages_after_list_pagination(self):
        for scope in (["id:wanted"], ["name:My Tasks"], ["My Tasks"]):
            with self.subTest(scope=scope):
                client = TasksClient({"wanted"})
                with patch.object(C, "_tasks_client", return_value=client):
                    result = C.collect_tasks("task", clock=CLOCK, task_lists=scope, page_size=1)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.count, 2)
                self.assertEqual({item["tasklist"] for item in result.items}, {"wanted"})
                self.assertEqual(len(client.requests), 4)
                self.assertIn("pageToken=next-wanted", client.requests[-1][1])

    def test_task_bare_id_or_title_matches_exactly(self):
        client = TasksClient({"wanted", "collision"})
        with patch.object(C, "_tasks_client", return_value=client):
            result = C.collect_tasks("task", clock=CLOCK, task_lists=["wanted"])
        self.assertEqual(result.status, "ok")
        self.assertEqual({item["tasklist"] for item in result.items}, {"wanted", "collision"})

    def test_task_scope_no_match_reads_no_task_items(self):
        for scope in (["missing"], ["my tasks"]):
            client = TasksClient(set())
            with patch.object(C, "_tasks_client", return_value=client):
                result = C.collect_tasks("task", clock=CLOCK, task_lists=scope)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.count, 0)
            self.assertEqual(len(client.requests), 2)  # list metadata only

    def test_task_empty_lists_preserves_all_visible_lists(self):
        client = TasksClient()
        with patch.object(C, "_tasks_client", return_value=client):
            result = C.collect_tasks("task", clock=CLOCK, task_lists=[])
        self.assertEqual(result.count, 6)
        self.assertEqual({item["tasklist"] for item in result.items},
                         {"excluded", "wanted", "collision"})

    def test_unsupported_adapter_never_constructs_client(self):
        with patch.object(C, "_tasks_client") as factory:
            result = C.collect_tasks("task", clock=CLOCK, task_adapter="unsupported")
        self.assertEqual(result.status, "error")
        factory.assert_not_called()

    def test_task_only_configuration_uses_exact_task_alias(self):
        config = U.default_config()
        config["sources"]["tasks"] = True
        config["tasks"] = {"accounts": ["task-only"], "lists": ["id:wanted"], "adapter": "tasks"}
        config["mail"]["accounts"] = ["unrelated-mail"]
        config["adapters"]["productivity_secretary"] = True
        policy = U.policy_overrides(U.validate_config(config))
        client = TasksClient({"wanted"})
        with patch.object(C, "_tasks_client", return_value=client) as task_factory, \
                patch.object(C, "_calendar_client") as cal_factory, \
                patch.object(C, "_google_aliases", side_effect=AssertionError("calendar discovery")):
            results = [fn() for fn in C.default_sources(policy, CLOCK)]
        task_factory.assert_called_once_with("task-only")
        cal_factory.assert_not_called()
        self.assertEqual([r.name for r in results], ["tasks:task-only"])
        self.assertEqual(results[0].count, 2)

    def test_distinct_calendar_and_task_accounts_and_subsets_reach_real_clients(self):
        policy = {"mail_enabled": False, "calendar_accounts": ["cal"],
                  "task_accounts": ["task"], "calendar_ids": ["team"],
                  "task_lists": ["id:wanted"]}
        with patch.object(C, "_calendar_client", return_value=CalendarClient({"team"})) as cf, \
                patch.object(C, "_tasks_client", return_value=TasksClient({"wanted"})) as tf:
            results = [fn() for fn in C.default_sources(policy, CLOCK)]
        cf.assert_called_once_with("cal")
        tf.assert_called_once_with("task")
        self.assertEqual([r.count for r in results], [2, 2])

    def test_disabled_empty_accounts_and_disabled_adapter_make_zero_connectors(self):
        policies = [
            {"calendar_enabled": False, "tasks_enabled": False},
            {"calendar_accounts": [], "task_accounts": []},
            {"productivity_secretary_enabled": False, "calendar_accounts": ["cal"],
             "task_accounts": ["task"]},
            U.policy_overrides(U.default_config()),
        ]
        for policy in policies:
            with self.subTest(policy=policy), patch.object(C, "_calendar_client") as cf, \
                    patch.object(C, "_tasks_client") as tf:
                self.assertEqual(C.default_sources({**policy, "mail_enabled": False}, CLOCK), [])
                cf.assert_not_called()
                tf.assert_not_called()

    def test_legacy_omitted_scope_preserves_selected_calendars_and_all_task_lists(self):
        migrated, report = U.migrate_legacy({"mail_enabled": False})
        policy = U.policy_overrides(U.validate_config(json.loads(json.dumps(migrated))))
        self.assertNotIn("calendar_accounts", policy)
        self.assertNotIn("task_accounts", policy)
        self.assertEqual(report["source_mappings"]["legacy_account_discovery"], ["calendar", "tasks"])
        with patch.object(C, "_google_aliases", return_value=["legacy"]), \
                patch.object(C, "_calendar_client", return_value=CalendarClient()) as cf, \
                patch.object(C, "_tasks_client", return_value=TasksClient()) as tf:
            results = [fn() for fn in C.default_sources(policy, CLOCK)]
        cf.assert_called_once_with("legacy")
        tf.assert_called_once_with("legacy")
        self.assertEqual([r.count for r in results], [6, 6])

    def test_reminder_empty_ids_and_subset_match_monitoring_boundary(self):
        client = CalendarClient({"team"})
        getter = SecretaryEventGetter(aliases=["cal"], client_factory=lambda alias: client,
                                      policy={"calendar_ids": [], "calendar_accounts": ["cal"]})
        # Exact GET uses the same selected-calendar check; fake response remains synthetic.
        getter.get("cal", "team", "event")
        self.assertEqual(len(client.requests), 1)
        getter = SecretaryEventGetter(aliases=["cal"], client_factory=lambda alias: client,
                                      policy={"calendar_ids": ["team"]})
        result = getter.get("cal", "primary", "event")
        self.assertFalse(result["ok"])
        self.assertEqual(len(client.requests), 1)


class MigrationAndParserTests(unittest.TestCase):
    def test_all_preexisting_source_fields_survive_roundtrip(self):
        legacy = {"mail_accounts": ["mail"], "mail_account_aliases": {"work": "mail"},
                  "calendar_accounts": ["calendar"], "calendar_ids": ["team"],
                  "task_accounts": ["task"], "task_lists": ["My Tasks"], "task_adapter": "tasks",
                  "mail_enabled": False, "calendar_enabled": False, "tasks_enabled": True,
                  "public_interests": ["science"], "productivity_secretary_enabled": False,
                  "gws_calendar_enabled": True}
        config, report = U.migrate_legacy(legacy)
        overrides = U.policy_overrides(U.validate_config(json.loads(json.dumps(config))))
        for key, value in legacy.items():
            self.assertEqual(overrides[key], value, key)
        self.assertEqual(report["source_mappings"]["mail"]["aliases"], {"work": "mail"})

    def test_explicit_empty_migration_inputs_win(self):
        config, _ = U.migrate_legacy(
            {"mail_accounts": ["old"], "calendar_accounts": ["old"], "calendar_ids": ["old"],
             "task_accounts": ["old"], "task_lists": ["old"], "task_adapter": "tasks"},
            mail_accounts=[], calendar_accounts=[], calendar_ids=[], task_accounts=[],
            task_lists=[], task_adapter="")
        policy = U.policy_overrides(U.validate_config(config))
        for key in ("mail_accounts", "calendar_accounts", "calendar_ids", "task_accounts", "task_lists"):
            self.assertEqual(policy[key], [], key)
        self.assertEqual(policy["task_adapter"], "")

    def test_preexisting_empty_scopes_do_not_become_discovery(self):
        config, _ = U.migrate_legacy({"mail_accounts": [], "calendar_accounts": [],
                                     "task_accounts": [], "calendar_ids": [], "task_lists": []})
        policy = U.policy_overrides(U.validate_config(config))
        self.assertEqual(policy["calendar_accounts"], [])
        self.assertEqual(policy["task_accounts"], [])
        self.assertNotIn("legacy_account_discovery", config["advanced"])

    def test_old_config_shared_aliases_are_reported_but_explicit_empty_wins(self):
        raw = {"calendar": {"accounts": ["old"]}, "tasks": {"lists": []}}
        config = U.validate_config(raw)
        self.assertEqual(config["tasks"]["accounts"], ["old"])
        self.assertTrue(any("tasks.accounts omitted" in w for w in config["_warnings"]))
        raw["tasks"]["accounts"] = []
        self.assertEqual(U.validate_config(raw)["tasks"]["accounts"], [])
        self.assertEqual(U.default_config()["tasks"]["accounts"], [])

    def test_legacy_shared_calendar_scope_copied_without_mail_union(self):
        config, _ = U.migrate_legacy({"calendar_accounts": ["cal"], "mail_accounts": ["mail"]})
        self.assertEqual(config["tasks"]["accounts"], ["cal"])

    def test_invalid_tasks_accounts_and_adapter_fail_closed_native_and_file(self):
        class Native:
            def __init__(self, config):
                self.config = config

            def get_config(self, key, default=None):
                return self.config if key == "user_config" else default

        for bad in ({"accounts": "bad"}, {"accounts": [17]}, {"adapter": "unsupported"}):
            config = U.default_config()
            config["tasks"] = bad
            with self.subTest(bad=bad), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(U.ConfigError):
                    U.load_user_config(path)
                with self.assertRaises(U.ConfigError):
                    U.load_user_config(path, native=Native(config))

    def test_real_cli_parser_and_handler_pass_independent_task_accounts(self):
        name = "deployment_scope_cli_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        sc = importlib.import_module(name + ".secretary.secretary_cli")
        parser = argparse.ArgumentParser()
        sc.register_cli(parser)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            legacy = Path(tmp) / "policy.json"
            legacy.write_text(json.dumps({"calendar_accounts": ["prior"]}), encoding="utf-8")
            args = parser.parse_args([
                "configure", "migrate", "--from-policy", str(legacy), "--file", str(Path(tmp) / "user.json"),
                "--dry-run", "--json", "--task-account", "task-a", "--task-account", "task-b",
                "--calendar-account", "cal", "--calendar-id", "team", "--mail-account", "mail",
                "--task-list", "id:wanted", "--adapter", "tasks"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                rc = sc.configure_command(args)
            payload = json.loads(output.getvalue())
        self.assertEqual(rc, 0)
        policy = payload["policy_overrides"]
        self.assertEqual(policy["task_accounts"], ["task-a", "task-b"])
        self.assertEqual(policy["calendar_accounts"], ["cal"])
        self.assertEqual(policy["calendar_ids"], ["team"])
        self.assertEqual(policy["task_lists"], ["id:wanted"])
        self.assertEqual(policy["task_adapter"], "tasks")
        self.assertIsNone(parser.parse_args(["configure", "migrate"]).task_account)
