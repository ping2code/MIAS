"""Phase 7G isolation, no-I/O and runner behavior.

Checks:
- no forbidden imports;
- no sockets, database, file writes or clock during synthesis;
- the read-only CLI.
"""
import inspect
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from evidence_packet.serialization import canonical_json
from evidence_synthesis import builder, canonical, model, rules, runner, validation
from evidence_synthesis.builder import synthesize
from tests import evidence_synthesis_cases as cases

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def packet_text(name="meta_real_shaped"):
    return cases.packet_path(name).read_text(encoding="utf-8")


class IsolationTests(unittest.TestCase):
    def test_no_forbidden_imports(self):
        code = ("import sys, evidence_synthesis.runner, evidence_synthesis.builder\n"
                "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('technical', 'evidence', 'evaluation', "
                "'collector', 'analyzer', 'alert_engine', 'orchestrator', 'openai', 'redis', 'telegram', 'dotenv', "
                "'requests', 'feedparser', 'sqlalchemy', 'persistence') or n == 'shared.config')\nprint(bad)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_evidence_packet_never_imports_synthesis(self):
        code = ("import sys, evidence_packet.models, evidence_packet.assembler, evidence_packet.runner\n"
                "print(sorted(n for n in sys.modules if n.startswith('evidence_synthesis')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_synthesis_needs_no_network_database_files_or_clock(self):
        data = json.loads(packet_text())
        expected = cases.synthesis_path("meta_real_shaped").read_text(encoding="utf-8")
        blocked = AssertionError("blocked side effect")
        with patch.object(socket, "socket", side_effect=blocked), \
                patch.object(socket, "create_connection", side_effect=blocked), \
                patch("builtins.open", side_effect=blocked), \
                patch.object(time, "time", side_effect=blocked), patch.object(time, "time_ns", side_effect=blocked), \
                patch.object(time, "monotonic", side_effect=blocked):
            result = canonical_json(synthesize(data).to_dict()) + "\n"
        self.assertEqual(result, expected)

    def test_source_has_no_clock_env_or_ai_usage(self):
        for module in (builder, canonical, model, rules, validation, runner):
            source = inspect.getsource(module)
            for token in ("datetime.now", "date.today", "time.time", "os.environ", "getenv", "load_dotenv", "openai",
                          "requests", "socket", "sqlalchemy", "technical_evidence_ledger", "import technical",
                          "from technical", "from evidence.", "import evidence.", "evaluation"):
                self.assertNotIn(token, source, (module.__name__, token))


class RunnerTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        return runner.main(argv, out=out, err=err), out.getvalue(), err.getvalue()

    def test_stdout_output_file_and_pretty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "packet.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(packet_text())
            code, text, err = self.run_cli(["--packet", path])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(text, cases.synthesis_path("meta_real_shaped").read_text(encoding="utf-8"))
            output = os.path.join(directory, "synthesis.json")
            self.assertEqual(self.run_cli(["--packet", path, "--output", output])[:2], (0, ""))
            with open(output, "rb") as handle:
                self.assertEqual(handle.read(), text.encode("utf-8"))
            pretty = self.run_cli(["--packet", path, "--pretty"])[1]
            self.assertNotEqual(pretty, text)
            self.assertEqual(canonical_json(json.loads(pretty)) + "\n", text)

    def test_invalid_unreadable_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = json.loads(packet_text())
            bad["symbol"] = "NVDA"  # Body changed, old packet_id kept: tampered.
            path = os.path.join(directory, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(bad, handle)
            code, text, err = self.run_cli(["--packet", path])
            self.assertEqual((code, text, json.loads(err)["error"]), (2, "", "invalid_packet"))
            garbage = os.path.join(directory, "garbage.json")
            with open(garbage, "w") as handle:
                handle.write("{not json")
            self.assertEqual(json.loads(self.run_cli(["--packet", garbage])[2])["error"], "unreadable_packet")
            self.assertEqual(self.run_cli(["--packet", os.path.join(directory, "missing.json")])[0], 2)
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_cli([])[0], 2)

    def test_runner_is_environment_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "packet.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(packet_text())
            baseline = self.run_cli(["--packet", path])[1]
            with patch.dict(os.environ, {"MARKET_DATA_PROVIDER": "massive_stocks", "DATABASE_URL": "sqlite://",
                                         "TECHNICAL_EVIDENCE_LEDGER_ENABLED": "true"}):
                self.assertEqual(self.run_cli(["--packet", path])[1], baseline)


if __name__ == "__main__":
    unittest.main()
