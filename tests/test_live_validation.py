"""Phase 10E live-validation tooling: phase9-v2 OptionsIntelligence and MarketIntelligence builders, the
metadata-only snapshot summary, secret safety, and the end-to-end live sequence run on synthetic files."""
import ast
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from live_validation import phase10
from options_intelligence.canonical import canonical_json
from tests import evidence_synthesis_corpus as corpus
from tests import options_intelligence_cases as oc
from tests import trade_setup_cases as cases
from tests import trade_setup_replay_cases as replay
from trade_setup import runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "sk-live-validation-canary-0123456789"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = phase10.main([str(a) for a in argv], out=out, err=err, calendar=oc.CALENDAR)
    return code, out.getvalue(), err.getvalue()


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(data) + "\n")
        return path


class BuildTests(ToolTestCase):
    def test_options_intelligence_v2(self):
        snapshot = oc.snapshot("quoted_with_price")
        out = os.path.join(self.dir, "oi.json")
        code, report, _ = run("options-intelligence", "--snapshot", self.file("s.json", snapshot.to_dict()),
                              "--output", out)
        self.assertEqual(code, phase10.OK)
        from options_intelligence.builder import build
        expected = build(snapshot, calendar=oc.CALENDAR, format="phase9-v2").to_dict()
        with open(out, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), canonical_json(expected) + "\n")
        summary = json.loads(report)
        self.assertEqual(summary["options_intelligence_format_version"], "phase9-v2")
        self.assertEqual(summary["shares_per_contract_present_count"], 9)
        self.assertEqual(run("options-intelligence", "--snapshot", self.file("s2.json", snapshot.to_dict()),
                             "--output", out)[0], phase10.OUTPUT)

    def test_market_intelligence(self):
        synthesis = os.path.join(ROOT, corpus.synthesis_path("all_bullish"))
        out = os.path.join(self.dir, "mi.json")
        code, report, _ = run("market-intelligence", "--synthesis", synthesis, "--output", out)
        self.assertEqual(code, phase10.OK)
        with open(out, encoding="utf-8") as handle:
            data = json.loads(handle.read())
        self.assertEqual(data, cases.market_intelligence("all_bullish"))
        self.assertEqual(json.loads(report)["pattern"], "all_bullish")

    def test_errors(self):
        self.assertEqual(run("options-intelligence", "--snapshot", os.path.join(self.dir, "none.json"), "--output",
                             os.path.join(self.dir, "x.json"))[0], phase10.INVALID)
        bad = oc.snapshot("quoted_with_price").to_dict()
        bad["snapshot_id"] = "sha256:" + "0" * 64
        code, _, err = run("options-intelligence", "--snapshot", self.file("bad.json", bad), "--output",
                           os.path.join(self.dir, "x.json"))
        self.assertEqual(code, phase10.INVALID)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.json")))
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(phase10.main(["bogus"]), phase10.USAGE)


class SummaryTests(ToolTestCase):
    def test_quote_freshness_metadata(self):                                                                # 39
        snapshot = oc.snapshot("quoted_with_price").to_dict()
        code, report, _ = run("snapshot-summary", "--snapshot", self.file("s.json", snapshot))
        self.assertEqual(code, phase10.OK)
        summary = json.loads(report)
        self.assertEqual(summary["contract_count"], 9)
        self.assertEqual(summary["quote_age_seconds"], dict(min=60, p50=60, p90=60, max=60))  # observed 1 min early
        self.assertEqual(summary["quote_age_buckets"], {"<= 60s": summary["timestamped_quote_count"]})
        self.assertIn("excluded_after_as_of", summary["quote_status_counts"])

    def test_live_like_snapshot(self):
        snapshot = oc.snapshot("live_like_meta").to_dict()
        summary = json.loads(run("snapshot-summary", "--snapshot", self.file("s.json", snapshot))[1])
        self.assertEqual((summary["quote_status_counts"], summary["two_sided_quote_count"], summary["quote_age_seconds"]),
                         ({"unavailable": 12}, 0, None))
        self.assertEqual(summary["source_capabilities"]["quote"], "unavailable")

    def test_no_values_or_contract_lists(self):                                                             # 40-42
        snapshot = oc.snapshot("quoted_with_price").to_dict()
        reports = [run("snapshot-summary", "--snapshot", self.file("s.json", snapshot))[1],
                   run("options-intelligence", "--snapshot", self.file("s.json", snapshot), "--output",
                       os.path.join(self.dir, "oi.json"))[1]]
        for report in reports:
            for contract in snapshot["contracts"]:
                self.assertNotIn(contract["identity"]["contract_id"], report)
                self.assertNotIn(contract["identity"]["provider_symbol"], report)
                for value in (contract["quote"]["bid"], contract["quote"]["ask"]):
                    if value is not None:
                        self.assertNotIn(f'"{value}"', report)
            self.assertLess(len(report), 4000)

    def test_output_size_independent_of_chain_size(self):                                                   # 42
        small = oc.snapshot("quoted_with_price").to_dict()
        from tests.options_snapshot_cases import full_snapshot, PROVENANCE
        from tests.test_trade_setup_screening_boundary import scale_records
        large = full_snapshot(scale_records(2000), provenance=dict(PROVENANCE, truncated=False),
                              calendar_state="regular").to_dict()
        a = run("snapshot-summary", "--snapshot", self.file("small.json", small))[1]
        b = run("snapshot-summary", "--snapshot", self.file("large.json", large))[1]
        self.assertLess(abs(len(a) - len(b)), 200)

    def test_never_reads_environment_or_network(self):                                                      # 40, 41
        tree = ast.parse(inspect.getsource(phase10))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attributes & {"environ", "getenv", "putenv"})
        snapshot = self.file("s.json", oc.snapshot("quoted_with_price").to_dict())
        result = subprocess.run([sys.executable, "-m", "live_validation.phase10", "snapshot-summary", "--snapshot",
                                 snapshot], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, OPTIONS_DATA_API_KEY=SECRET, MARKET_DATA_API_KEY=SECRET))
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        code = ("import sys, live_validation.phase10\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'requests', 'urllib3', 'sqlalchemy',"
                " 'psycopg2', 'openai', 'redis', 'persistence', 'evidence', 'evaluation'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


class LiveSequenceTests(ToolTestCase):
    """The operator's live sequence (snapshot -> phase9-v2 OI -> MI -> trade_setup.runner), run on synthetic files."""

    def test_end_to_end(self):
        from tests.options_snapshot_cases import full_snapshot, PROVENANCE
        snapshot = full_snapshot(replay.records(), provenance=dict(PROVENANCE, truncated=False),
                                 calendar_state="regular").to_dict()
        oi, mi = os.path.join(self.dir, "oi.json"), os.path.join(self.dir, "mi.json")
        self.assertEqual(run("options-intelligence", "--snapshot", self.file("s.json", snapshot), "--output", oi)[0], 0)
        self.assertEqual(run("market-intelligence", "--synthesis", os.path.join(ROOT, corpus.synthesis_path(
            "all_bullish")), "--output", mi)[0], 0)
        out = io.StringIO()
        code = runner.main(["--market-intelligence", mi, "--options-intelligence", oi, "--policy",
                            str(replay.path("policy.v2.json")), "--assessment-output",
                            os.path.join(self.dir, "a.json")], out=out, err=io.StringIO())
        self.assertEqual(code, runner.OK)
        with open(os.path.join(self.dir, "a.json"), "rb") as handle:
            self.assertEqual(handle.read(), replay.path("assessment.v2.json").read_bytes())


class LivePolicyTests(unittest.TestCase):
    """The checked-in live-validation policy is an explicit, sealed phase10-policy-v1 (illustrative, not tuned)."""

    def test_live_policy(self):
        from trade_setup.policy import validated_policy
        path = os.path.join(ROOT, "live_validation", "policies", "phase10e_live_validation.policy.json")
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
        policy = validated_policy(data)
        self.assertEqual((policy.policy_format_version, policy.min_dte, policy.max_dte, policy.max_spread_relative,
                          policy.abs_delta_min, policy.abs_delta_max, policy.min_volume, policy.min_open_interest,
                          policy.max_premium_per_contract, policy.allow_unverified_time_basis,
                          policy.require_complete_chain, policy.max_input_gap_seconds),
                         ("phase10-policy-v1", 7, 45, "0.1", "0.25", "0.6", 10, 100, None, True, True, 1800))


if __name__ == "__main__":
    unittest.main()
