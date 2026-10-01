"""Phase 10D InvalidationCheck: setup and MarketIntelligence input errors, bullish/bearish semantics, contract,
tamper matrix, re-derivation, no reactivation, determinism, boundaries and scale.

New MarketIntelligence inputs are real Phase 8 objects (genuine pattern and technical evidence) whose as_of is moved
later and resealed (``trade_setup_cases.later_mi``).
"""
import ast
from copy import deepcopy
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest

from trade_setup import invalidation, model as m, rules as r
from trade_setup.canonical import canonical_json, content_id
from trade_setup.invalidation import check_invalidation, validated_invalidation, verify_invalidation
from trade_setup.validation import TradeSetupInputError
from tests import trade_setup_cases as cases
from tests.trade_setup_cases import later_mi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED = {
    "call": {"all_bullish": ("holds", "required_pattern_present"),
             "all_bearish": ("invalidated", "required_pattern_absent"),
             "higher_aligned_5m_opposed": ("invalidated", "required_pattern_absent"),
             "mixed_all": ("invalidated", "required_pattern_absent"),
             "meta_real_shaped": ("invalidated", "required_pattern_absent"),
             "insufficient_data_state": ("not_evaluable", "timeframe_evidence_incomplete"),
             "missing_1d": ("not_evaluable", "technical_evidence_not_available"),
             "technical_unavailable": ("not_evaluable", "technical_evidence_not_available")},
}
EXPECTED["put"] = dict(EXPECTED["call"], all_bullish=("invalidated", "required_pattern_absent"),
                       all_bearish=("holds", "required_pattern_present"))


def reseal(data, key):
    data[key] = content_id({k: v for k, v in data.items() if k != key})
    return data


def holds_check():
    return check_invalidation(cases.setup("call"), later_mi("all_bullish")).to_dict()


class SetupInputTests(unittest.TestCase):
    def assertError(self, assessment, message, mi=None):
        with self.assertRaises(TradeSetupInputError) as caught:
            check_invalidation(assessment, mi or later_mi("all_bullish"))
        self.assertEqual(str(caught.exception), message)

    def test_valid_setups(self):                                                                            # 1, 2
        for side, name in (("call", "all_bullish"), ("put", "all_bearish")):
            check = check_invalidation(cases.setup(side), later_mi(name))
            self.assertEqual((check.setup_ref.side, check.result), (side, "holds"))

    def test_no_setup_rejected(self):                                                                       # 3
        no_setup, _ = cases.screened([cases.record()], max_dte=10)
        self.assertEqual(no_setup.outcome.status, "no_setup")
        self.assertError(no_setup, "invalidation requires a setup_candidates assessment")
        global_fail, _ = cases.screened([cases.record()], max_input_gap_seconds=0)
        self.assertError(global_fail, "invalidation requires a setup_candidates assessment")

    def test_zero_candidates_rejected(self):
        data = cases.setup().to_dict()
        data["candidates"] = []
        self.assertError(reseal(data, "assessment_id"), "assessment setup_candidates requires candidates")

    def test_tamper_and_version(self):                                                                      # 4, 5
        data = cases.setup().to_dict()
        data["policy"]["max_dte"] = 90
        self.assertError(data, "assessment id does not match its body (tampered or corrupt)")
        data = cases.setup().to_dict()
        data["assessment_format_version"] = "phase10-v2"
        self.assertError(reseal(data, "assessment_id"), "unsupported assessment format version")

    def test_descriptor_errors(self):                                                                       # 6-11
        message = "assessment market_bias invalidation descriptor is inconsistent"
        for mutate, expected in (
                (lambda d: d["market_bias"].update(invalidation=None), message),
                (lambda d: d["market_bias"]["invalidation"].update(rule="pattern_may_drift"), message),
                (lambda d: d["market_bias"]["invalidation"].update(required_pattern="all_bearish"), message),
                (lambda d: d["market_bias"]["invalidation"].update(established_by="sha256:" + "c" * 64), message),
                (lambda d: d["market_bias"].update(pattern="opposed"),
                 "assessment market_bias is inconsistent with its pattern and technical status"),
                (lambda d: d["market_bias"].update(technical_status="unavailable"),
                 "assessment market_bias is inconsistent with its pattern and technical status")):
            data = cases.setup().to_dict()
            mutate(data)
            with self.subTest(expected=expected):
                self.assertError(reseal(data, "assessment_id"), expected)


class MarketIntelligenceInputTests(unittest.TestCase):
    def assertError(self, mi, message):
        with self.assertRaises(TradeSetupInputError) as caught:
            check_invalidation(cases.setup(), mi)
        self.assertEqual(str(caught.exception), message)

    def test_same_symbol_and_newer(self):                                                                   # 12, 14
        check = check_invalidation(cases.setup(), later_mi("all_bullish", seconds=1))
        self.assertEqual((check.symbol, check.market_intelligence_ref.symbol), ("META", "META"))
        self.assertGreater(check.market_intelligence_ref.as_of, check.setup_ref.established_as_of)

    def test_symbol_mismatch(self):                                                                         # 13
        self.assertError(later_mi("all_bullish", symbol="NVDA"), "market intelligence symbol does not match the setup")

    def test_same_and_older_as_of(self):                                                                    # 15, 16
        message = "market intelligence is not newer than the market intelligence that established the setup"
        self.assertError(cases.market_intelligence("all_bullish"), message)        # the establishing MI itself
        self.assertError(later_mi("all_bearish", seconds=0), message)               # same instant, different MI
        self.assertError(later_mi("all_bullish", seconds=-1), message)

    def test_anchor_is_the_establishing_mi_not_assessment_as_of(self):
        setup = cases.setup()
        self.assertLess(setup.inputs.market_intelligence_ref.as_of, setup.inputs.assessment_as_of)
        check = check_invalidation(setup, later_mi("all_bullish", seconds=60))  # before assessment_as_of: allowed
        self.assertLess(check.market_intelligence_ref.as_of, check.setup_ref.assessment_as_of)

    def test_mi_tamper_and_version(self):                                                                   # 17, 18
        mi = later_mi("all_bullish")
        mi["timeframe_structure"]["pattern"] = "all_bearish"
        self.assertError(mi, "market intelligence id does not match its body (tampered or corrupt)")
        mi = later_mi("all_bullish")
        mi["rules_version"] = "phase8-rules-v2"
        self.assertError(reseal(mi, "intelligence_id"), "unsupported market intelligence rules version")


class SemanticsTests(unittest.TestCase):
    def test_bullish_and_bearish(self):                                                                     # 19-34
        for side in ("call", "put"):
            setup = cases.setup(side)
            for name, (result, reason) in EXPECTED[side].items():
                with self.subTest(side=side, case=name):
                    check = check_invalidation(setup, later_mi(name))
                    self.assertEqual((check.result, check.reason), (result, reason))
                    self.assertEqual(check.observed_market_state.to_dict(),
                                     dict(zip(("pattern", "technical_status"), cases.OBSERVED_CASES[name])))
                    self.assertEqual(check.required_market_state.required_pattern,
                                     "all_bullish" if side == "call" else "all_bearish")

    def test_decide_covers_every_state(self):
        for required in ("all_bullish", "all_bearish"):
            for pattern in r.MI_PATTERNS:
                for technical in r.MI_TECHNICAL_STATUSES:
                    result, reason, trace = invalidation.decide(required, pattern, technical)
                    expected = ("not_evaluable" if technical != "available" or pattern == "incomplete"
                                else "holds" if pattern == required else "invalidated")
                    self.assertEqual(result, expected, (required, pattern, technical))
                    self.assertEqual(r.INVALIDATION_REASONS[reason], result)
                    self.assertEqual([t.rule for t in trace], [s for s, _ in r.INVALIDATION_STEPS])


class ContractTests(unittest.TestCase):
    def test_schema(self):                                                                                  # 35-38
        check = check_invalidation(cases.setup(), later_mi("all_bullish"))
        data = check.to_dict()
        self.assertEqual(list(data), list(m.INVALIDATION_FIELDS))
        self.assertEqual(len(data), 12)
        self.assertEqual((data["invalidation_format_version"], data["rules_version"]),
                         ("phase10-invalidation-v1", "phase10-invalidation-rules-v1"))
        self.assertEqual(check.invalidation_id, content_id(check.body()))
        self.assertEqual(check_invalidation(cases.setup(), later_mi("all_bullish")).invalidation_id,
                         check.invalidation_id)
        self.assertNotIn("generated_at", canonical_json(data))

    def test_refs_and_states(self):                                                                         # 39-42
        setup, mi = cases.setup(), later_mi("all_bullish")
        data = check_invalidation(setup, mi).to_dict()
        self.assertEqual(data["setup_ref"], dict(
            assessment_id=setup.assessment_id, assessment_format_version="phase10-v1",
            assessment_rules_version="phase10-rules-v1", policy_id=setup.policy.policy_id, side="call",
            assessment_as_of=setup.inputs.assessment_as_of, established_by=setup.market_bias.invalidation.established_by,
            established_as_of=setup.inputs.market_intelligence_ref.as_of))
        self.assertNotIn(setup.candidates[0].source.contract_id, canonical_json(data))
        self.assertEqual(data["market_intelligence_ref"], dict(
            intelligence_id=mi["intelligence_id"], intelligence_format_version="phase8-v1",
            rules_version="phase8-rules-v1", symbol="META", as_of=mi["synthesis_ref"]["as_of"]))
        self.assertEqual(data["required_market_state"], dict(rule="pattern_must_remain", required_pattern="all_bullish",
                                                             required_technical_status="available"))
        self.assertEqual(data["observed_market_state"], dict(pattern="all_bullish", technical_status="available"))

    def test_vocabularies(self):                                                                            # 43, 44
        self.assertEqual(r.INVALIDATION_RESULTS, ("holds", "invalidated", "not_evaluable"))
        self.assertEqual(r.INVALIDATION_REASONS, {
            "required_pattern_present": "holds", "required_pattern_absent": "invalidated",
            "technical_evidence_not_available": "not_evaluable", "timeframe_evidence_incomplete": "not_evaluable"})

    def test_trace(self):                                                                                   # 45
        expected = {
            "all_bullish": [("pass", None)] * 4 + [("pass", "required_pattern_present")],
            "all_bearish": [("pass", None)] * 4 + [("fail", "required_pattern_absent")],
            "insufficient_data_state": [("pass", None)] * 3 + [("fail", "timeframe_evidence_incomplete"),
                                                               ("not_evaluated", "earlier_step_failed")],
            "missing_1d": [("pass", None)] * 2 + [("fail", "technical_evidence_not_available"),
                                                  ("not_evaluated", "earlier_step_failed"),
                                                  ("not_evaluated", "earlier_step_failed")]}
        for name, steps in expected.items():
            trace = check_invalidation(cases.setup(), later_mi(name)).to_dict()["decision_trace"]
            self.assertEqual([(t["result"], t["reason"]) for t in trace], steps)
            self.assertEqual([t["rule"] for t in trace], ["symbol_match", "as_of_order", "technical_evidence",
                                                          "timeframe_completeness", "pattern_match"])
            for t in trace:
                self.assertTrue(t["pointers"] == sorted(t["pointers"])
                                and all(r.INVALIDATION_POINTER.fullmatch(p) for p in t["pointers"]))

    def test_provenance(self):                                                                              # 46
        setup, mi = cases.setup(), later_mi("all_bullish")
        self.assertEqual(check_invalidation(setup, mi).provenance.to_dict(), dict(
            assessment_id=setup.assessment_id, market_intelligence_id=mi["intelligence_id"],
            established_by=setup.provenance.market_intelligence_id, rules_version="phase10-invalidation-rules-v1",
            pointer_version="phase10-invalidation-pointer-v1"))

    def test_assessment_never_mutated(self):
        setup = cases.setup().to_dict()
        before = canonical_json(setup)
        check_invalidation(setup, later_mi("all_bearish"))
        self.assertEqual(canonical_json(setup), before)
        self.assertNotIn("invalidation_id", setup)


STRUCTURAL = [
    ("extra key", lambda d: d.update(score=1), "invalidation check must have exactly the 12 phase10-invalidation-v1 keys"),
    ("format", lambda d: d.update(invalidation_format_version="phase10-invalidation-v2"),
     "unsupported invalidation format version"),
    ("rules", lambda d: d.update(rules_version="phase10-invalidation-rules-v2"), "unsupported invalidation rules version"),
    ("observed pattern", lambda d: d["observed_market_state"].update(pattern="all_bearish"),                  # 47
     "invalidation result and reason are inconsistent with the observed market state"),
    ("technical status", lambda d: d["observed_market_state"].update(technical_status="partial"),             # 48
     "invalidation result and reason are inconsistent with the observed market state"),
    ("unknown pattern", lambda d: d["observed_market_state"].update(pattern="mostly_bullish"),
     "invalidation observed_market_state is malformed"),
    ("result", lambda d: d.update(result="invalidated"),                                                      # 49
     "invalidation result and reason are inconsistent with the observed market state"),
    ("reason", lambda d: d.update(reason="timeframe_evidence_incomplete"),                                    # 50
     "invalidation result and reason are inconsistent with the observed market state"),
    ("unknown result", lambda d: d.update(result="valid"), "invalidation result or reason is not supported"),
    ("setup side", lambda d: d["setup_ref"].update(side="put"),                                                # 51
     "invalidation required_market_state is inconsistent with the setup side"),
    ("setup ref keys", lambda d: d["setup_ref"].pop("policy_id"), "invalidation setup_ref is malformed"),
    ("setup version", lambda d: d["setup_ref"].update(assessment_rules_version="phase10-rules-v2"),
     "invalidation setup_ref is malformed"),
    ("required rule", lambda d: d["required_market_state"].update(rule="pattern_may_drift"),
     "invalidation required_market_state is inconsistent with the setup side"),
    ("mi older", lambda d: d["market_intelligence_ref"].update(as_of=d["setup_ref"]["established_as_of"]),     # 52
     "invalidation market intelligence is not newer than the establishing market intelligence"),
    ("mi symbol", lambda d: d["market_intelligence_ref"].update(symbol="NVDA"), "invalidation symbols are inconsistent"),
    ("mi version", lambda d: d["market_intelligence_ref"].update(rules_version="phase8-rules-v2"),
     "invalidation market_intelligence_ref is malformed"),
    ("trace order", lambda d: d["decision_trace"].reverse(), "invalidation decision_trace is inconsistent"),   # 53
    ("trace pointer", lambda d: d["decision_trace"][0].update(pointers=["oi:contracts[*]"]),
     "invalidation decision_trace is inconsistent"),
    ("trace result", lambda d: d["decision_trace"][4].update(result="fail", reason="required_pattern_absent"),
     "invalidation decision_trace is inconsistent"),
    ("provenance", lambda d: d["provenance"].update(established_by="sha256:" + "d" * 64),                      # 54
     "invalidation provenance is inconsistent"),
    ("provenance version", lambda d: d["provenance"].update(pointer_version="phase10-pointer-v1"),
     "invalidation provenance is inconsistent"),
]


class TamperTests(unittest.TestCase):
    def test_structural_matrix(self):
        for label, mutate, message in STRUCTURAL:
            data = holds_check()
            mutate(data)
            with self.subTest(case=label), self.assertRaises(TradeSetupInputError) as caught:
                validated_invalidation(reseal(data, "invalidation_id"))
            self.assertEqual(str(caught.exception), message)

    def test_id_tamper(self):
        data = holds_check()
        data["result"] = "invalidated"
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_invalidation(data)
        self.assertEqual(str(caught.exception), "invalidation check id does not match its body (tampered or corrupt)")

    def test_self_consistent_forgeries_need_rederivation(self):                                              # 55
        setup, mi = cases.setup(), later_mi("all_bullish")
        real = check_invalidation(setup, mi).to_dict()
        self.assertEqual(verify_invalidation(real, setup, mi), real)
        # A false "invalidated" with a matching observed state, reason and trace.
        forged = deepcopy(real)
        forged["observed_market_state"]["pattern"] = "all_bearish"
        result, reason, trace = invalidation.decide("all_bullish", "all_bearish", "available")
        forged.update(result=result, reason=reason, decision_trace=[t.to_dict() for t in trace])
        forged = reseal(forged, "invalidation_id")
        validated_invalidation(forged)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_invalidation(forged, setup, mi)
        self.assertEqual(str(caught.exception),
                         "invalidation check does not match its inputs at decision_trace, observed_market_state, "
                         "reason, result")
        # A consistent swap of the referenced MarketIntelligence id.
        swapped = deepcopy(real)
        swapped["market_intelligence_ref"]["intelligence_id"] = "sha256:" + "e" * 64
        swapped["provenance"]["market_intelligence_id"] = "sha256:" + "e" * 64
        swapped = reseal(swapped, "invalidation_id")
        validated_invalidation(swapped)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_invalidation(swapped, setup, mi)
        self.assertEqual(str(caught.exception),
                         "invalidation check does not match its inputs at market_intelligence_ref, provenance")
        with self.assertRaises(TradeSetupInputError):
            verify_invalidation(real, cases.setup("put"), later_mi("all_bearish"))


class NoReactivationTests(unittest.TestCase):
    """Checks are stateless point-in-time facts. Downstream, the earliest invalidated check is terminal."""

    @staticmethod
    def lifecycle(checks):
        """The documented downstream rule (not part of trade_setup): the setup ends at its earliest invalidation."""
        for check in sorted(checks, key=lambda c: c["market_intelligence_ref"]["as_of"]):
            if check["result"] == "invalidated":
                return "invalidated", check["invalidation_id"]
        return "not_invalidated", None

    def test_t2_holds_does_not_revive_t1(self):                                                             # 61-63
        setup = cases.setup()
        t1 = check_invalidation(setup, later_mi("all_bearish", seconds=86400)).to_dict()
        t2 = check_invalidation(setup, later_mi("all_bullish", seconds=2 * 86400)).to_dict()
        self.assertEqual((t1["result"], t2["result"]), ("invalidated", "holds"))
        self.assertEqual(t2["setup_ref"], t1["setup_ref"])                   # the same setup, unchanged
        self.assertNotIn(t1["invalidation_id"], canonical_json(t2))           # no chaining: T2 knows nothing of T1
        self.assertEqual(self.lifecycle([t2, t1]), ("invalidated", t1["invalidation_id"]))


class DeterminismTests(unittest.TestCase):
    def test_100_repeats_and_dict_order(self):                                                              # 64, 65
        setup, mi = cases.setup().to_dict(), later_mi("meta_real_shaped")
        expected = canonical_json(check_invalidation(setup, mi).to_dict())
        self.assertEqual({canonical_json(check_invalidation(setup, mi).to_dict()) for _ in range(100)}, {expected})

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        self.assertEqual(canonical_json(check_invalidation(reversed_keys(setup), reversed_keys(mi)).to_dict()), expected)

    def test_fresh_processes(self):                                                                         # 66-72
        code = ("from tests import trade_setup_cases as c\nfrom trade_setup.invalidation import check_invalidation\n"
                "print(check_invalidation(c.setup(), c.later_mi('higher_aligned_5m_opposed')).invalidation_id)")
        expected = check_invalidation(cases.setup(), later_mi("higher_aligned_5m_opposed")).invalidation_id
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "13", "TZ": "Asia/Kolkata"},
                      {"PYTHONHASHSEED": "777", "LANG": "C", "LC_ALL": "C"},
                      {"PYTHONHASHSEED": "31", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1", "TZ": "UTC"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class BoundaryTests(unittest.TestCase):
    def test_sealed_no_io(self):                                                                            # 73
        from tests.test_market_intelligence_isolation import sealed
        setup, mi = cases.setup().to_dict(), later_mi("all_bearish")
        expected = check_invalidation(setup, mi).to_dict()
        with sealed():
            produced = check_invalidation(setup, mi).to_dict()
            verify_invalidation(produced, setup, mi)
        self.assertEqual(produced, expected)

    def test_ast_imports(self):                                                                             # 74
        for node in ast.walk(ast.parse(inspect.getsource(invalidation))):
            if isinstance(node, ast.ImportFrom):
                self.assertTrue(node.module.split(".")[0] in sys.stdlib_module_names
                                or node.module.startswith("trade_setup"), node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)

    def test_runtime_imports(self):                                                                         # 75-81
        code = ("import sys, trade_setup.invalidation\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'market_intelligence', 'options_intelligence',"
                " 'options_data', 'market_data', 'evidence_packet', 'evidence_synthesis', 'evidence', 'evaluation',"
                " 'persistence', 'sqlalchemy', 'requests', 'openai', 'redis', 'urllib3', 'http'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_no_ranking_sizing_target_stop_reward(self):                                                    # 82-86
        banned = {"rank", "ranking", "score", "confidence", "best", "sizing", "size", "position", "quantity", "account",
                  "capital", "target", "stop", "reward", "price", "premium", "pnl", "profit", "probability"}
        keys, stack = set(), [holds_check()]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                keys |= set(value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        self.assertFalse({w for k in keys for w in k.split("_")} & banned)
        for node in ast.walk(ast.parse(inspect.getsource(invalidation))):
            name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
            if isinstance(name, str):
                self.assertFalse(set(name.lower().split("_")) & banned, name)


class ScaleTests(unittest.TestCase):
    def test_output_independent_of_candidate_count(self):                                                   # 87, 88
        from tests.test_trade_setup_screening_boundary import POLICY, scale_records
        small = cases.setup()
        oi = cases.chain(scale_records(10000))
        from trade_setup.builder import assess
        large = assess(cases.market_intelligence("all_bullish"), oi, cases.screen_policy(**POLICY))
        self.assertGreater(len(large.candidates), 1000)
        mi = later_mi("all_bearish")
        start = time.perf_counter()
        check = check_invalidation(large, mi)
        built = time.perf_counter() - start
        verify_invalidation(check.to_dict(), large, mi)
        small_check = check_invalidation(small, mi)
        self.assertEqual(len(canonical_json(check.to_dict())), len(canonical_json(small_check.to_dict())))
        self.assertLess(built, 30)  # characterization bound only: input validation is linear, the check is not


if __name__ == "__main__":
    unittest.main()
