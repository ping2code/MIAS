"""Phase 3 collector registry: six collectors, existing entry points, argument lists only, no collector imports."""
import subprocess
import sys
import unittest
import unittest.mock

from orchestrator import registry
from orchestrator.config import FAMILIES, load_settings
from orchestrator.job_runner import ROOT
from orchestrator.models import JobDefinition, OverlapPolicy


class RegistryTests(unittest.TestCase):
    def test_registry_contains_all_six_collectors_in_deterministic_order(self):
        definitions = registry.build_definitions(load_settings({}))
        self.assertEqual([d.name for d in definitions], ["news", "fed", "sec", "macro", "treasury", "geopolitical"])
        self.assertEqual(set(FAMILIES), {"macro", "treasury", "geopolitical", "fed", "sec", "news"})
        self.assertEqual(definitions, registry.build_definitions(load_settings({})))

    def test_commands_are_existing_entry_points_as_argument_lists(self):
        definitions = {d.name: d for d in registry.build_definitions(load_settings({}), python="/py")}
        expected = dict(news=("/py", "-m", "collector.multi_source_collector"), fed=("/py", "-m", "collector.fed_collector"),
                        sec=("/py", "-m", "collector.sec_collector"), macro=("/py", "-m", "collector.macro_collector"),
                        treasury=("/py", "-m", "collector.treasury_collector"),
                        geopolitical=("/py", "-m", "orchestrator.entrypoints", "geopolitical"))
        self.assertEqual({n: d.argv for n, d in definitions.items()}, expected)
        for definition in definitions.values():
            self.assertIsInstance(definition.argv, tuple)
            self.assertTrue(all(isinstance(a, str) and " " not in a for a in definition.argv))  # No shell strings.
            self.assertIs(definition.overlap_policy, OverlapPolicy.SKIP)
            self.assertLess(definition.timeout_seconds, definition.interval_seconds)

    def test_send_alerts_maps_only_to_the_existing_flag(self):
        env = {f"{p}_SCHEDULE_SEND_ALERTS": "true" for p in ("FED", "MACRO", "TREASURY", "GEOPOLITICAL")}
        definitions = {d.name: d for d in registry.build_definitions(load_settings(env), python="/py")}
        for name in ("fed", "macro", "treasury", "geopolitical"):
            self.assertEqual(definitions[name].argv[-1], "--send-alerts")
        for name in ("news", "sec"):
            self.assertNotIn("--send-alerts", definitions[name].argv)
        default = {d.name: d for d in registry.build_definitions(load_settings({}), python="/py")}
        self.assertFalse(any("--send-alerts" in d.argv for d in default.values()))  # Default: unchanged policy.

    def test_registry_and_cli_import_no_collector_or_dotenv_config(self):
        code = ("import sys, orchestrator.cli, orchestrator.registry, orchestrator.scheduler\n"
                "print(sorted(m for m in sys.modules if m.startswith(('collector', 'shared.config', 'persistence', 'dotenv'))))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_geopolitical_wrapper_usage_and_flags(self):
        from orchestrator import entrypoints
        with unittest.mock.patch("sys.stderr"):
            self.assertEqual(entrypoints.main([]), 2)
            self.assertEqual(entrypoints.main(["unknown"]), 2)
        calls = []
        fake = unittest.mock.MagicMock(collect_geopolitical_events=lambda **kw: (calls.append(kw) or ([], {"fetched": 0})),
                                       format_geopolitical_alert=str)
        with unittest.mock.patch.dict(sys.modules, {"collector.geopolitical_collector": fake}), \
             unittest.mock.patch("sys.stdout"):
            self.assertEqual(entrypoints.main(["geopolitical"]), 0)
            self.assertEqual(entrypoints.main(["geopolitical", "--no-ai", "--send-alerts"]), 0)
        self.assertEqual(calls, [dict(enable_ai=True, send_alerts=False), dict(enable_ai=False, send_alerts=True)])

    def test_job_definition_validation(self):
        for bad in (dict(name="", argv=("x",)), dict(name="a", argv=()), dict(name="a", argv=("x",), interval_seconds=0),
                    dict(name="a", argv=("x",), timeout_seconds=-1), dict(name="a", argv=("x",), start_offset_seconds=-1),
                    dict(name="a", argv=("x", 1))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                JobDefinition(**bad)


if __name__ == "__main__":
    unittest.main()
