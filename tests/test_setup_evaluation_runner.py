"""Phase 11 replay runner: evaluate, build-schedule and test-protocol; exit codes; no-clobber; byte replay;
metadata-only summaries; and the runner I/O boundary."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from setup_evaluation import runner
from setup_evaluation.builder import evaluate
from setup_evaluation.canonical import canonical_json
from tests import setup_evaluation_cases as c

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    return runner.main([str(a) for a in argv], out=out, err=err), out.getvalue(), err.getvalue()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.files = {}
        for name, data in (("assessment", c.setup()), ("snapshot", c.snapshot()), ("schedule", c.schedule()),
                           ("protocol", c.protocol()), ("check", c.check())):
            self.files[name] = self.write(f"{name}.json", data)
        self.output = os.path.join(self.dir, "evaluation.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(data) + "\n")
        return path

    def args(self, horizon="session_1", *extra):
        f = self.files
        return ["evaluate", "--assessment", f["assessment"], "--snapshot", f["snapshot"], "--schedule", f["schedule"],
                "--protocol", f["protocol"], "--horizon", horizon, "--output", self.output, *extra]

    def test_evaluate_replay(self):
        code, out, err = run(*self.args("session_1", "--invalidation-check", self.files["check"]))
        self.assertEqual(code, runner.OK, err)
        expected = evaluate(c.setup(), c.snapshot(), c.schedule(), c.protocol(), "session_1", [c.check()]).to_dict()
        with open(self.output, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), canonical_json(expected) + "\n")
        summary = json.loads(out)
        self.assertEqual((summary["evaluation_id"], summary["protocol_purpose"], summary["invalidation_relation"]),
                         (expected["evaluation_id"], "test", "invalidated_by_target"))
        for banned in ("premium", "bid", "ask", "return", "dollar", "contract_id"):
            self.assertNotIn(banned, out)

    def test_errors_and_no_clobber(self):
        self.assertEqual(run(*self.args("session_5"))[0], runner.INVALID)        # snapshot outside the session_5 window
        self.assertFalse(os.path.exists(self.output))
        with mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(runner.main(["evaluate"]), runner.INVALID)
            self.assertEqual(runner.main(["evaluate", *self.args()[1:-2], "--horizon", "session_10"]), runner.INVALID)
        self.assertEqual(run(*self.args())[0], runner.OK)
        code, _, err = run(*self.args())
        self.assertEqual((code, json.loads(err)["error"]),
                         (runner.OUTPUT, "output file already exists (use --overwrite to replace it)"))
        self.assertEqual(run(*self.args(), "--overwrite")[0], runner.OK)
        same = self.args()
        same[-1] = self.files["snapshot"]
        self.assertEqual(run(*same)[0], runner.INVALID)
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n.startswith(".setup-evaluation-")), [])

    def test_integrity_exit(self):
        with mock.patch.object(runner, "verify_evaluation", side_effect=runner.SetupEvaluationInputError("forced")):
            self.assertEqual(run(*self.args())[0], runner.INTEGRITY)
        self.assertFalse(os.path.exists(self.output))

    def test_build_schedule_and_test_protocol(self):
        path = os.path.join(self.dir, "s.json")
        code, out, _ = run("build-schedule", "--from", "2026-09-28", "--through", "2026-12-31", "--output", path)
        self.assertEqual(code, runner.OK)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.read()), c.schedule())
        self.assertEqual(run("build-schedule", "--from", "2026-12-31", "--through", "2026-09-28", "--output",
                             os.path.join(self.dir, "x.json"))[0], runner.INVALID)
        code, out, _ = run("test-protocol", "--output", os.path.join(self.dir, "p.json"))
        self.assertEqual((code, json.loads(out)["purpose"]), (runner.OK, "test"))

    def test_fresh_process_replay(self):
        outputs = set()
        for extra in ({"PYTHONHASHSEED": "1", "TZ": "Asia/Tokyo"}, {"PYTHONHASHSEED": "2", "LANG": "C", "HOSTNAME": "x"}):
            with tempfile.TemporaryDirectory() as cwd:
                target = os.path.join(cwd, "e.json")
                args = self.args()
                args[-1] = target
                result = subprocess.run([sys.executable, "-m", "setup_evaluation.runner", *args], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra), capture_output=True, text=True,
                                        timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr[-300:])
                with open(target, "rb") as handle:
                    outputs.add(handle.read())
        self.assertEqual(len(outputs), 1)

    def test_runner_boundary(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(runner))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attributes & {"environ", "getenv", "now", "utcnow", "today"})
        code = ("import sys, setup_evaluation.runner\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'requests', 'urllib3', 'socket', 'sqlalchemy',"
                " 'psycopg2', 'openai', 'redis', 'persistence', 'evidence', 'evaluation', 'options_data', 'trade_setup',"
                " 'market_data', 'exchange_calendars'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])   # the calendar loads only in build-schedule


if __name__ == "__main__":
    unittest.main()
