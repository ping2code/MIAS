"""Phase 10E Trade Setup runner: CLI, assessment and invalidation replay, exit codes, atomic and no-clobber
writes, determinism, Phase 9 v1/v2 integration, and the runner/core I/O boundary."""
import ast
from contextlib import redirect_stdout
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import trade_setup
from trade_setup import runner
from trade_setup.canonical import canonical_json
from tests import trade_setup_cases as cases
from tests import trade_setup_replay_cases as replay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MI = str(replay.MARKET_INTELLIGENCE)
F = replay.path


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = runner.main([str(a) for a in argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(data) + "\n")
    return path


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.a, self.i = os.path.join(self.dir, "assessment.json"), os.path.join(self.dir, "invalidation.json")

    def tearDown(self):
        self.tmp.cleanup()

    def assess_args(self, oi="options_intelligence.v2.json", policy="policy.v2.json", mi=MI):
        return ["--market-intelligence", mi, "--options-intelligence", F(oi) if isinstance(oi, str) and "/" not in oi
                else oi, "--policy", F(policy) if "/" not in str(policy) else policy, "--assessment-output", self.a]

    def assertFailed(self, result, code, message=None):
        self.assertEqual(result[0], code, result[2])
        error = json.loads(result[2])
        self.assertEqual((error["result"], error["exit_code"]), ("FAILED", code))
        if message:
            self.assertEqual(error["error"], message)
        self.assertEqual(result[1], "")

    def invalidation_args(self, later="all_bullish"):
        later = F(f"later_market_intelligence.{later}.json") if "/" not in later else later
        return self.assess_args() + ["--later-market-intelligence", later, "--invalidation-output", self.i]

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith(".trade-setup-"))


class CliTests(RunnerTestCase):
    def test_help(self):                                                                                    # 1
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(runner.main(["--help"]), runner.OK)
        for flag in ("--market-intelligence", "--options-intelligence", "--policy", "--assessment-output",
                     "--later-market-intelligence", "--invalidation-output", "--overwrite"):
            self.assertIn(flag, buffer.getvalue())

    def test_required_arguments(self):                                                                      # 2
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(runner.main([]), runner.INVALID)
            self.assertEqual(runner.main(["--market-intelligence", MI]), runner.INVALID)
        self.assertFailed(run(*self.assess_args(), "--later-market-intelligence", F(
            "later_market_intelligence.all_bullish.json")), runner.INVALID,
            "--later-market-intelligence and --invalidation-output must be given together")
        self.assertFailed(run(*self.assess_args()[:-1], MI), runner.INVALID,
                          "output paths must be distinct from each other and from every input")
        self.assertFailed(run(*self.assess_args(), "--later-market-intelligence",
                              F("later_market_intelligence.all_bullish.json"), "--invalidation-output", self.a),
                          runner.INVALID, "output paths must be distinct from each other and from every input")
        self.assertFalse(os.path.exists(self.a))

    def test_valid_replay_and_stable_bytes(self):                                                           # 3, 12
        code, out, _ = run(*self.assess_args())
        self.assertEqual(code, runner.OK)
        self.assertEqual(read(self.a), read(F("assessment.v2.json")))
        summary = json.loads(out)
        self.assertEqual((summary["result"], summary["outcome"], summary["assessment_id"]),
                         ("WRITTEN", "setup_candidates", json.loads(read(self.a))["assessment_id"]))
        second = os.path.join(self.dir, "again.json")
        self.assertEqual(run(*self.assess_args()[:-1], second)[0], runner.OK)
        self.assertEqual(read(second), read(self.a))

    def test_invalid_inputs(self):                                                                          # 4-8
        mi = json.loads(read(MI))
        mi["timeframe_structure"]["pattern"] = "all_bearish"
        self.assertFailed(run(*self.assess_args(mi=write(self.dir, "mi.json", mi))), runner.INVALID,
                          "market intelligence id does not match its body (tampered or corrupt)")
        oi = json.loads(read(F("options_intelligence.v2.json")))
        oi["contracts"][0]["quote_state"] = "crossed"
        self.assertFailed(run(*self.assess_args(oi=write(self.dir, "oi.json", oi))), runner.INVALID,
                          "options intelligence id does not match its body (tampered or corrupt)")
        policy = json.loads(read(F("policy.v2.json")))
        policy["max_dte"] = 365
        self.assertFailed(run(*self.assess_args(policy=write(self.dir, "policy.json", policy))), runner.INVALID,
                          "policy is invalid: policy_id does not match the policy (tampered or corrupt)")
        nvda = write(self.dir, "nvda.json", cases.options_intelligence("nvda"))
        self.assertFailed(run(*self.assess_args(oi=nvda)), runner.INVALID,
                          "market intelligence and options intelligence symbols do not match")
        self.assertFailed(run(*self.assess_args(oi="options_intelligence.v1.json")), runner.INVALID,
                          "policy_requires_phase9_v2")
        with open(os.path.join(self.dir, "dup.json"), "w", encoding="utf-8") as handle:
            handle.write('{"a": 1, "a": 2}')
        self.assertFailed(run(*self.assess_args(policy=os.path.join(self.dir, "dup.json"))), runner.INVALID,
                          "policy file is unreadable, not JSON, or has duplicate keys")
        self.assertFailed(run(*self.assess_args(mi=os.path.join(self.dir, "missing.json"))), runner.INVALID,
                          "market intelligence file not found")
        self.assertFalse(os.path.exists(self.a))
        self.assertEqual(self.leftovers(), [])

    def test_integrity_failure_exit_3(self):
        with mock.patch.object(runner, "verify_assessment", side_effect=runner.TradeSetupInputError("forced")):
            self.assertFailed(run(*self.assess_args()), runner.INTEGRITY, "forced")
        self.assertFalse(os.path.exists(self.a))


class WriteTests(RunnerTestCase):
    def test_no_clobber_and_overwrite(self):                                                                # 9, 10, 30
        with open(self.a, "w", encoding="utf-8") as handle:
            handle.write("original\n")
        self.assertFailed(run(*self.assess_args()), runner.OUTPUT,
                          "output file already exists (use --overwrite to replace it)")
        self.assertEqual(read(self.a), b"original\n")
        self.assertEqual(run(*self.assess_args(), "--overwrite")[0], runner.OK)
        self.assertEqual(read(self.a), read(F("assessment.v2.json")))
        self.assertEqual(self.leftovers(), [])

    def test_collision_on_second_output_writes_nothing(self):                                              # 30
        with open(self.i, "w", encoding="utf-8") as handle:
            handle.write("original\n")
        self.assertFailed(run(*self.invalidation_args()), runner.OUTPUT)
        self.assertFalse(os.path.exists(self.a))
        self.assertEqual(read(self.i), b"original\n")

    def test_commit_failure_rolls_back(self):                                                               # 11
        real_link, calls = os.link, []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError("simulated")
            return real_link(src, dst)
        with mock.patch.object(runner.os, "link", side_effect=flaky):
            self.assertFailed(run(*self.invalidation_args()), runner.OUTPUT, "cannot write the output file")
        self.assertEqual(len(calls), 2)
        self.assertFalse(os.path.exists(self.a))
        self.assertFalse(os.path.exists(self.i))
        self.assertEqual(self.leftovers(), [])

    def test_build_failures_write_nothing(self):                                                            # 28, 29
        self.assertFailed(run(*self.invalidation_args(MI)), runner.INVALID,
                          "market intelligence is not newer than the market intelligence that established the setup")
        self.assertFalse(os.path.exists(self.a) or os.path.exists(self.i))
        self.assertEqual(self.leftovers(), [])


class InvalidationReplayTests(RunnerTestCase):
    def test_results(self):                                                                                 # 13, 17-19
        for later, result in (("all_bullish", "holds"), ("all_bearish", "invalidated"), ("missing_1d", "not_evaluable")):
            with self.subTest(later=later):
                code, out, _ = run(*self.invalidation_args(later), "--overwrite")
                self.assertEqual(code, runner.OK)
                self.assertEqual(read(self.i), read(F(f"invalidation.{later}.json")))
                self.assertEqual(read(self.a), read(F("assessment.v2.json")))
                self.assertEqual(json.loads(out)["invalidation_result"], result)

    def test_time_and_symbol_errors(self):                                                                  # 14-16
        older = write(self.dir, "older.json", cases.later_mi("all_bullish", seconds=-60))
        nvda = write(self.dir, "nvda_mi.json", cases.later_mi("all_bullish", symbol="NVDA"))
        message = "market intelligence is not newer than the market intelligence that established the setup"
        self.assertFailed(run(*self.invalidation_args(MI)), runner.INVALID, message)
        self.assertFailed(run(*self.invalidation_args(older)), runner.INVALID, message)
        self.assertFailed(run(*self.invalidation_args(nvda)), runner.INVALID,
                          "market intelligence symbol does not match the setup")

    def test_no_setup_cannot_be_invalidated(self):                                                          # 20, 29
        policy = write(self.dir, "narrow.json", cases.make_policy(**dict(replay.POLICY_V2, max_dte=5)).to_dict())
        args = self.assess_args(policy=policy) + ["--later-market-intelligence",
                                                  F("later_market_intelligence.all_bullish.json"),
                                                  "--invalidation-output", self.i]
        self.assertFailed(run(*args), runner.INVALID, "invalidation requires a setup_candidates assessment")
        self.assertFalse(os.path.exists(self.a) or os.path.exists(self.i))
        self.assertEqual(run(*self.assess_args(policy=policy))[0], runner.OK)   # the no_setup assessment alone is fine
        self.assertEqual(json.loads(read(self.a))["outcome"]["status"], "no_setup")


class Phase9IntegrationTests(RunnerTestCase):
    def test_v1_and_v2_replay(self):                                                                        # 31, 32
        self.assertEqual(run(*self.assess_args(oi="options_intelligence.v1.json", policy="policy.v1.json"))[0],
                         runner.OK)
        self.assertEqual(read(self.a), read(F("assessment.v1.json")))
        v1 = json.loads(read(self.a))
        self.assertEqual(v1["execution_readiness"]["options_intelligence_format_version"], "phase9-v1")
        self.assertTrue(all(c["source"]["shares_per_contract"] is None for c in v1["candidates"]))

    def test_v2_facts(self):                                                                                # 33-38
        data = json.loads(read(F("assessment.v2.json")))
        states = {c["key"]: c["count"] for c in data["execution_readiness"]["quote_state_counts"]}
        self.assertEqual(states, {"bid_missing": 1, "complete": 4, "crossed": 1, "locked": 1})
        self.assertEqual(sorted({c["source"]["quote_state"] for c in data["candidates"]}), ["complete", "locked"])
        for c in data["candidates"]:
            self.assertEqual(c["source"]["current_session_volume"], "1200")
            self.assertEqual((c["source"]["open_interest_value"], c["source"]["shares_per_contract"]), ("15000", "100"))
            self.assertEqual(c["derived"]["premium_risk_status"], "computed")
        self.assertEqual({x["reason_code"]: x["count"] for x in data["rejections"]},
                         {"multiplier_unavailable": 1, "quote_crossed": 1, "quote_one_sided": 1, "side_mismatch": 1})

    def test_unavailable_quotes(self):                                                                      # 35
        live_like = os.path.join(ROOT, "tests", "fixtures", "options_intelligence", "live_like_meta.intelligence.json")
        code, out, _ = run(*self.assess_args(oi=live_like, policy="policy.v1.json"))
        self.assertEqual(code, runner.OK)
        summary = json.loads(out)
        # The live-like chain is also truncated and the policy requires a complete chain: both gates report.
        self.assertEqual((summary["outcome"], summary["no_setup_reasons"], summary["quote_state_counts"]),
                         ("no_setup", ["execution_data_unavailable", "options_chain_truncated"], {"unavailable": 12}))

    def test_fixtures_are_current(self):
        for build in (replay.build_inputs, replay.build_outputs):
            for name, data in build().items():
                with self.subTest(fixture=name):
                    self.assertEqual(read(F(name)), (canonical_json(data) + "\n").encode())


class DeterminismTests(unittest.TestCase):
    def test_fresh_processes(self):                                                                         # 21-27
        outputs = []
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "5", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "321", "LANG": "C", "LC_ALL": "C"},
                      {"PYTHONHASHSEED": "77", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1", "TZ": "UTC"}):
            with tempfile.TemporaryDirectory() as cwd:
                a, i = os.path.join(cwd, "a.json"), os.path.join(cwd, "i.json")
                result = subprocess.run(
                    [sys.executable, "-m", "trade_setup.runner", "--market-intelligence", MI,
                     "--options-intelligence", str(F("options_intelligence.v2.json")), "--policy",
                     str(F("policy.v2.json")), "--assessment-output", a, "--later-market-intelligence",
                     str(F("later_market_intelligence.all_bearish.json")), "--invalidation-output", i],
                    cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra), capture_output=True, text=True, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr[-300:])
                outputs.append((read(a), read(i)))
        self.assertEqual(set(outputs), {(read(F("assessment.v2.json")), read(F("invalidation.all_bearish.json")))})


class BoundaryTests(unittest.TestCase):
    IO_MODULES = {"os", "io", "tempfile", "pathlib", "shutil", "socket", "subprocess", "sqlite3", "urllib", "http",
                  "argparse", "sys", "time"}

    def modules(self):
        import importlib
        import pkgutil
        return [importlib.import_module(f"trade_setup.{m.name}") for m in pkgutil.iter_modules(trade_setup.__path__)]

    def imports(self, module):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module.split(".")[0])
        return names

    def test_only_the_runner_performs_io(self):                                                             # 43, 44
        for module in self.modules():
            if module is runner:
                continue
            with self.subTest(module=module.__name__):
                self.assertFalse(self.imports(module) & self.IO_MODULES)
                for node in ast.walk(ast.parse(inspect.getsource(module))):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, ("open", "print", "input"))

    def test_runner_imports(self):                                                                          # 45-48
        self.assertEqual(self.imports(runner) - {"trade_setup"},
                         {"argparse", "collections", "json", "os", "sys", "tempfile", "time"})
        code = ("import sys, trade_setup.runner\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'market_intelligence', 'options_intelligence',"
                " 'options_data', 'market_data', 'evidence_packet', 'evidence_synthesis', 'evidence', 'evaluation',"
                " 'persistence', 'sqlalchemy', 'psycopg2', 'requests', 'urllib3', 'socket', 'ssl', 'openai', 'redis',"
                " 'live_validation'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])
        attributes = {n.attr for n in ast.walk(ast.parse(inspect.getsource(runner))) if isinstance(n, ast.Attribute)}
        self.assertFalse(attributes & {"environ", "getenv", "now", "utcnow", "today", "time_ns"})

    def test_no_ranking_sizing_target_stop_reward(self):                                                    # 49-53
        banned = {"rank", "ranking", "best", "score", "confidence", "sizing", "size", "position", "quantity",
                  "account", "target", "stop", "reward", "recommend", "recommendation", "pick"}
        for node in ast.walk(ast.parse(inspect.getsource(runner))):
            name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
            if isinstance(name, str):
                self.assertFalse(set(name.lower().split("_")) & banned, name)
        code, out, _ = run("--market-intelligence", MI, "--options-intelligence", F("options_intelligence.v2.json"),
                           "--policy", F("policy.v2.json"), "--assessment-output",
                           os.path.join(tempfile.mkdtemp(), "a.json"))
        keys = set(json.loads(out))
        self.assertFalse({w for k in keys for w in k.split("_")} & banned)


if __name__ == "__main__":
    unittest.main()
