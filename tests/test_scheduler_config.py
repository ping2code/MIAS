"""Phase 3 scheduler configuration: safe defaults, validation, safe printing, CLI config behavior."""
from io import StringIO
import json
import unittest
import unittest.mock

from orchestrator import cli
from orchestrator.config import DEFAULTS, SchedulerConfigError, load_settings

SECRET = "sk-scheduler-canary-secret-000000000000"


class SchedulerConfigTests(unittest.TestCase):
    def test_safe_defaults(self):
        settings = load_settings({})
        self.assertEqual((settings.enabled, settings.dry_run, settings.history_size, settings.child_output),
                         (False, False, 50, "inherit"))
        for family in settings.families:
            interval, timeout, offset = DEFAULTS[family.name]
            self.assertEqual((family.enabled, family.interval_seconds, family.timeout_seconds, family.start_offset_seconds,
                              family.send_alerts), (family.name != "technical", interval, timeout, offset, False))
        offsets = [f.start_offset_seconds for f in settings.families]
        self.assertEqual(len(set(offsets)), 7)  # Deterministic, distinct start offsets: no start-up stampede.

    def test_overrides(self):
        settings = load_settings({"MIAS_SCHEDULER_ENABLED": "TRUE", "MIAS_SCHEDULER_DRY_RUN": "true", "NEWS_INTERVAL_SECONDS": "120",
                                  "NEWS_TIMEOUT_SECONDS": "60", "SEC_SCHEDULE_ENABLED": "false", "FED_START_OFFSET_SECONDS": "0",
                                  "MIAS_SCHEDULER_HISTORY_SIZE": "5", "MIAS_SCHEDULER_CHILD_OUTPUT": "discard"})
        families = {f.name: f for f in settings.families}
        self.assertEqual((settings.enabled, settings.dry_run, settings.history_size, settings.child_output), (True, True, 5, "discard"))
        self.assertEqual((families["news"].interval_seconds, families["news"].timeout_seconds), (120, 60))
        self.assertEqual((families["sec"].enabled, families["fed"].start_offset_seconds), (False, 0))
        short = load_settings({"NEWS_INTERVAL_SECONDS": "60"})  # Default timeout clamps below a short interval.
        self.assertEqual({f.name: f.timeout_seconds for f in short.families}["news"], 59)

    def test_invalid_values_are_rejected_naming_only_the_setting(self):
        bad = [{"NEWS_INTERVAL_SECONDS": "59"}, {"NEWS_INTERVAL_SECONDS": "86401"}, {"NEWS_INTERVAL_SECONDS": "abc"},
               {"NEWS_TIMEOUT_SECONDS": "0"}, {"NEWS_INTERVAL_SECONDS": "300", "NEWS_TIMEOUT_SECONDS": "300"},
               {"FED_START_OFFSET_SECONDS": "3601"}, {"MIAS_SCHEDULER_ENABLED": "yes"}, {"SEC_SCHEDULE_ENABLED": "1"},
               {"MIAS_SCHEDULER_HISTORY_SIZE": "0"}, {"MIAS_SCHEDULER_CHILD_OUTPUT": "file"},
               {"SEC_SCHEDULE_SEND_ALERTS": "true"}, {"NEWS_SCHEDULE_SEND_ALERTS": "false"},
               {"MIAS_SCHEDULER_KILL_GRACE_SECONDS": "0"}]
        for env in bad:
            with self.subTest(env=env), self.assertRaises(SchedulerConfigError) as raised:
                load_settings(dict(env, OPENAI_API_KEY=SECRET))
            self.assertNotIn(SECRET, str(raised.exception))

    def test_status_config_prints_only_safe_settings(self):
        out, err = StringIO(), StringIO()
        env = {"OPENAI_API_KEY": SECRET, "TELEGRAM_BOT_TOKEN": SECRET, "DATABASE_URL": f"postgresql://u:{SECRET}@h/d"}
        self.assertEqual(cli.main(["status-config"], environ=env, out=out, err=err), 0)
        view = json.loads(out.getvalue())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertEqual(sorted(view["registry"]), ["fed", "geopolitical", "macro", "news", "sec", "technical", "treasury"])
        self.assertEqual(view["registry"]["technical"], dict(module="technical.runner", args=[]))
        self.assertEqual((view["families"]["technical"]["enabled"], view["families"]["technical"]["send_alerts"]),
                         (False, "never (no alert path)"))
        self.assertEqual(view["registry"]["geopolitical"], dict(module="orchestrator.entrypoints", args=["geopolitical"]))
        self.assertFalse(any("python" in json.dumps(v).lower() for v in view["registry"].values()))  # No interpreter paths.

    def test_cli_exit_codes(self):
        err = StringIO()
        self.assertEqual(cli.main(["status-config"], environ={"NEWS_INTERVAL_SECONDS": "1"}, out=StringIO(), err=err), 2)
        self.assertIn("NEWS_INTERVAL_SECONDS", err.getvalue())
        for command in ("run", "run-once"):
            err = StringIO()
            self.assertEqual(cli.main([command], environ={}, out=StringIO(), err=err), 3)  # Disabled by default.
            self.assertIn("MIAS_SCHEDULER_ENABLED", err.getvalue())
        with unittest.mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["bogus"], environ={}, out=StringIO(), err=StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
