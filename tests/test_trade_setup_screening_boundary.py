"""Phase 10C screening: tamper matrix, re-derivation, determinism, cross-process, sealed no-I/O, Phase 10D
boundary, and 8,000 / 10,000-contract scale."""
import ast
from copy import deepcopy
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest

from trade_setup import builder, model, rules, screening
from trade_setup.builder import assess
from trade_setup.canonical import canonical_json, content_id
from trade_setup.validation import TradeSetupInputError, validated_assessment, verify_assessment
from tests import trade_setup_cases as cases
from tests.trade_setup_cases import quote, record

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDS = [record(700), record(710), record(720, quote=quote(bid=None)), record(730, option_type="put"),
           record(740, quote=quote("10.2", "10"))]
POLICY = dict(max_spread_relative="0.1", max_premium_per_contract="1500")


def screened():
    a, oi = cases.screened(RECORDS, **POLICY)
    return a.to_dict(), oi


def resealed(data):
    data["assessment_id"] = content_id({k: v for k, v in data.items() if k != "assessment_id"})
    return data


def ids(oi):
    return {rec["strike"]: cases.contract_id(oi, rec) for rec in RECORDS}


def without(data, cid):
    """Remove one contract id from every rejection (dropping emptied reasons)."""
    kept = []
    for x in data["rejections"]:
        x["contract_ids"] = [i for i in x["contract_ids"] if i != cid]
        x["count"] = len(x["contract_ids"])
        if x["contract_ids"]:
            kept.append(x)
    data["rejections"] = kept


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.data, self.oi = screened()
        self.ids = ids(self.oi)
        self.mi = cases.market_intelligence("all_bullish")

    def assertStructural(self, mutate, message):
        data = deepcopy(self.data)
        mutate(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(resealed(data))
        self.assertEqual(str(caught.exception), message)

    def test_baseline(self):
        self.assertEqual(self.data["outcome"]["status"], "setup_candidates")
        self.assertEqual([c["source"]["strike"] for c in self.data["candidates"]], ["700", "710"])
        self.assertEqual(verify_assessment(self.data, self.mi, self.oi), self.data)

    def test_forged_candidate(self):                                                                        # 89
        def violates(d):
            d["candidates"][0]["source"]["quote_state"] = "crossed"
        self.assertStructural(violates, "assessment candidate does not satisfy the policy")

        def wrong_side(d):
            d["candidates"][0]["source"]["option_type"] = "put"
        self.assertStructural(wrong_side, "assessment candidate identity is inconsistent")

        def derived(d):
            d["candidates"][0]["derived"]["max_loss_per_contract"] = "1"
        self.assertStructural(derived, "assessment candidate derived facts are inconsistent")

        def checks(d):
            d["candidates"][0]["policy_checks"].pop()
        self.assertStructural(checks, "assessment candidate does not satisfy the policy")

        # Self-consistent forgery: a rejected contract presented as a candidate. Only re-derivation catches it.
        data = deepcopy(self.data)
        fake = deepcopy(data["candidates"][1])
        fake["source"].update(contract_id=self.ids["720"], strike="720", provider_symbol="O:" + self.ids["720"])
        data["candidates"].append(fake)
        without(data, self.ids["720"])
        data = resealed(data)
        validated_assessment(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_assessment(data, self.mi, self.oi)
        self.assertEqual(str(caught.exception), "assessment does not match its inputs at candidates, rejections")

    def test_forged_rejection(self):                                                                        # 90
        def unknown(d):
            d["rejections"][0]["reason_code"] = "illiquid"
        self.assertStructural(unknown, "assessment rejection is malformed")
        data = deepcopy(self.data)
        data["rejections"].append(dict(reason_code="spread_above_policy", count=1, contract_ids=[self.ids["720"]]))
        data["rejections"].sort(key=lambda x: x["reason_code"])
        data = resealed(data)
        validated_assessment(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_assessment(data, self.mi, self.oi)
        self.assertEqual(str(caught.exception), "assessment does not match its inputs at rejections")

    def test_wrong_count(self):                                                                             # 91
        def count(d):
            d["rejections"][0]["count"] += 1
        self.assertStructural(count, "assessment rejection is malformed")

    def test_wrong_candidate_order(self):                                                                   # 92
        def order(d):
            d["candidates"].reverse()
        self.assertStructural(order, "assessment candidates are not unique and in canonical order")

    def test_wrong_rejection_order(self):                                                                   # 93
        def order(d):
            d["rejections"].reverse()
        self.assertStructural(order, "assessment rejections are not unique and in reason order")

        def ids_order(d):
            x = next(x for x in d["rejections"] if x["count"] > 1)
            x["contract_ids"].reverse()
        self.assertStructural(ids_order, "assessment rejection is malformed")

    def test_candidate_also_rejected(self):                                                                 # 94
        def both(d):
            x = d["rejections"][0]
            x["contract_ids"] = sorted(x["contract_ids"] + [self.ids["700"]])
            x["count"] = len(x["contract_ids"])
        self.assertStructural(both, "assessment candidate also appears in rejections")

    def test_partition(self):
        def dropped(d):
            without(d, self.ids["720"])
        self.assertStructural(dropped, "assessment candidates and rejections do not partition the contracts")

    def test_wrong_outcome(self):                                                                           # 95
        def no_setup(d):
            d["outcome"] = dict(status="no_setup", no_setup_reasons=["no_candidate_satisfies_policy"])
        self.assertStructural(no_setup, "assessment contract_screening step is inconsistent with its outcome")

        def empty(d):
            d["candidates"] = []
        self.assertStructural(empty, "assessment setup_candidates requires candidates")

        def extra_reason(d):
            d["outcome"] = dict(status="no_setup", no_setup_reasons=["context_gate_blocked",
                                                                     "no_candidate_satisfies_policy"])
            d["decision_trace"][-1].update(result="fail", reason="no_candidate_satisfies_policy")
            d["candidates"] = []
        self.assertStructural(extra_reason, "assessment screening no_setup reasons are inconsistent")

    def test_wrong_step_8(self):                                                                            # 96
        def result(d):
            d["decision_trace"][-1].update(result="fail", reason="no_candidate_satisfies_policy")
        self.assertStructural(result, "assessment contract_screening step is inconsistent with its outcome")

        def gate(d):
            d["decision_trace"][-1].update(result="not_evaluated", reason="global_gate_failed")
        self.assertStructural(gate, "assessment contract_screening step is inconsistent with the global gates")

        def pointers(d):
            d["decision_trace"][-1]["pointers"] = ["oi:contracts[*]"]
        self.assertStructural(pointers, "assessment contract_screening pointers are inconsistent with the policy")

    def test_policy_swap_is_caught(self):
        data = deepcopy(self.data)
        data["policy"] = cases.screen_policy(**dict(POLICY, max_premium_per_contract="1000")).to_dict()
        data["provenance"]["policy_id"] = data["policy"]["policy_id"]
        data["decision_trace"][-1]["pointers"] = list(builder.screening_pointers(
            cases.screen_policy(**dict(POLICY, max_premium_per_contract="1000"))))
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(resealed(data))
        self.assertEqual(str(caught.exception), "assessment candidate does not satisfy the policy")

    def test_rederivation_reproduces_everything(self):
        rebuilt = assess(self.mi, self.oi, self.data["policy"]).to_dict()
        self.assertEqual(rebuilt, self.data)


class DeltaValidationTests(unittest.TestCase):
    """Candidate deltas are held to the Phase 9 bound (call [0, 1], put [-1, 0]); nothing is clamped or recomputed."""
    DELTA = dict(abs_delta_min="0.2", abs_delta_max="0.7")

    def candidate_assessment(self, side):
        mi_name = "all_bullish" if side == "call" else "all_bearish"
        delta = "0.55" if side == "call" else "-0.45"
        rec = record(option_type=side, greeks=dict(delta=delta, gamma="0.01", theta="-0.1", vega="0.2"))
        a, oi = cases.screened([rec], mi_name=mi_name, **self.DELTA)
        return a.to_dict(), oi, cases.market_intelligence(mi_name)

    def assertRejected(self, data):
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(resealed(data))
        self.assertEqual(str(caught.exception), "assessment candidate does not satisfy the policy")

    def test_sign_invalid_call_delta(self):                                                                 # 1
        data, _, _ = self.candidate_assessment("call")
        data["candidates"][0]["source"]["delta"] = "-0.5"
        self.assertRejected(data)

    def test_sign_invalid_put_delta(self):                                                                  # 2
        data, _, _ = self.candidate_assessment("put")
        data["candidates"][0]["source"]["delta"] = "0.5"
        self.assertRejected(data)

    def test_valid_deltas_pass(self):                                                                       # 3, 4
        for side, delta in (("call", "0.55"), ("put", "-0.45")):
            data, oi, mi = self.candidate_assessment(side)
            self.assertEqual(data["candidates"][0]["source"]["delta"], delta)
            self.assertEqual(validated_assessment(data), data)
            self.assertEqual(verify_assessment(data, mi, oi), data)

    def test_rederivation_catches_in_bounds_tampering(self):                                                # 5
        data, oi, mi = self.candidate_assessment("call")
        data["candidates"][0]["source"]["delta"] = "0.6"
        data = resealed(data)
        validated_assessment(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_assessment(data, mi, oi)
        self.assertEqual(str(caught.exception), "assessment does not match its inputs at candidates")

    def test_out_of_bounds_screening_unchanged(self):
        wild = record(greeks=dict(delta="-0.5", gamma="0.01", theta="-0.1", vega="0.2"))  # call with a put-signed delta
        a, oi = cases.screened([wild], **self.DELTA)
        cid = cases.contract_id(oi, wild)
        self.assertEqual([x.reason_code for x in a.rejections if cid in x.contract_ids], ["delta_unavailable"])
        b, oi = cases.screened([wild])  # delta rule off: delta is not screened, and the candidate validates
        data = b.to_dict()
        self.assertEqual(data["candidates"][0]["source"]["delta"], "-0.5")
        self.assertEqual(verify_assessment(data, cases.market_intelligence("all_bullish"), oi), data)

    def test_schema_unchanged(self):                                                                        # 6
        data, _, _ = self.candidate_assessment("call")
        self.assertEqual(set(data["candidates"][0]), {"source", "derived", "policy_checks"})
        self.assertEqual(list(data["candidates"][0]["source"]), [
            "contract_id", "provider_symbol", "option_type", "expiration", "strike", "dte_calendar_days", "quote_state",
            "mid", "spread_absolute", "spread_relative", "delta", "greeks_time_basis", "implied_volatility",
            "iv_time_basis", "volume_state", "open_interest_state", "day_session_relation", "current_session_volume",
            "open_interest_value", "open_interest_time_basis", "shares_per_contract"])
        self.assertEqual(list(data["candidates"][0]["derived"]),
                         ["entry_reference_ask", "max_loss_per_contract", "premium_risk_status"])
        self.assertEqual(set(data["rejections"][0]), {"reason_code", "count", "contract_ids"})
        self.assertEqual(len(rules.REJECTION_REASONS), 19)

    def test_bound_matches_phase9(self):
        from decimal import Decimal
        from options_intelligence.rules import greeks_out_of_bounds
        for side in ("call", "put"):
            for text in ("-1.5", "-1", "-0.5", "0", "0.5", "1", "1.5"):
                self.assertEqual(screening.delta_out_of_bounds(side, text),
                                 "delta" in greeks_out_of_bounds(side, {"delta": Decimal(text)}), (side, text))
        self.assertFalse(screening.delta_out_of_bounds("call", None))


class PremiumRiskStatusTests(unittest.TestCase):
    """Frozen phase10-v1: computed | multiplier_unavailable (A, B, C)."""

    def test_semantics(self):
        self.assertEqual(rules.PREMIUM_RISK_STATUSES, ("computed", "multiplier_unavailable"))
        a, _ = cases.screened([record()])                                                                   # A
        self.assertEqual(a.candidates[0].derived.to_dict(), dict(entry_reference_ask="10.1",
                                                                 max_loss_per_contract="1010",
                                                                 premium_risk_status="computed"))
        b, oi = cases.screened([record(shares=None)])                                                       # B
        self.assertEqual(b.candidates[0].derived.to_dict(), dict(entry_reference_ask="10.1", max_loss_per_contract=None,
                                                                 premium_risk_status="multiplier_unavailable"))
        c, oi = cases.screened([record(shares=None)], max_premium_per_contract="5000")                      # C
        self.assertEqual((c.candidates, c.outcome.status), ((), "no_setup"))
        self.assertIn("multiplier_unavailable", [x.reason_code for x in c.rejections])

    def test_forged_case_c_candidate_is_rejected(self):
        b, _ = cases.screened([record(shares=None)])
        data = b.to_dict()
        data["policy"] = cases.screen_policy(max_premium_per_contract="5000").to_dict()
        data["provenance"]["policy_id"] = data["policy"]["policy_id"]
        data["decision_trace"][-1]["pointers"] = list(builder.screening_pointers(
            cases.screen_policy(max_premium_per_contract="5000")))
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(resealed(data))
        self.assertEqual(str(caught.exception), "assessment candidate does not satisfy the policy")


class DeterminismTests(unittest.TestCase):
    def test_100_repeats(self):                                                                             # 97
        a, oi = screened()
        mi = cases.market_intelligence("all_bullish")
        expected = canonical_json(a)
        self.assertEqual({canonical_json(assess(mi, oi, cases.screen_policy(**POLICY)).to_dict())
                          for _ in range(100)}, {expected})

    def test_contract_and_dict_order(self):                                                                 # 98, 99
        a, _ = screened()
        b, oi = cases.screened(list(reversed(RECORDS)), **POLICY)
        self.assertEqual(canonical_json(b.to_dict()), canonical_json(a))

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        mi = cases.market_intelligence("all_bullish")
        c = assess(reversed_keys(mi), reversed_keys(oi), reversed_keys(cases.screen_policy(**POLICY).to_dict()))
        self.assertEqual(canonical_json(c.to_dict()), canonical_json(a))

    def test_fresh_processes_and_environments(self):                                                        # 100-106
        code = ("from tests import test_trade_setup_screening_boundary as t\n"
                "print(t.screened()[0]['assessment_id'])")
        expected = screened()[0]["assessment_id"]
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "7", "TZ": "America/New_York"},
                      {"PYTHONHASHSEED": "4242", "LANG": "C", "LC_ALL": "C"},
                      {"PYTHONHASHSEED": "99", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1", "TZ": "Asia/Tokyo"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class BoundaryTests(unittest.TestCase):
    def test_sealed_no_io(self):                                                                            # 107
        from tests.test_market_intelligence_isolation import sealed
        expected, oi = screened()
        mi, policy = cases.market_intelligence("all_bullish"), cases.screen_policy(**POLICY)
        with sealed():
            produced = assess(mi, oi, policy).to_dict()
            verify_assessment(produced, mi, oi)
        self.assertEqual(produced, expected)

    def test_screening_module_imports(self):                                                                # 108
        tree = ast.parse(inspect.getsource(screening))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertTrue(node.module.split(".")[0] in sys.stdlib_module_names or node.module.startswith(
                    "trade_setup"), node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)

    def test_runtime_imports(self):                                                                         # 109
        code = ("import sys, trade_setup.builder, trade_setup.screening, trade_setup.validation\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'market_intelligence', 'options_intelligence',"
                " 'options_data', 'market_data', 'evidence_packet', 'evidence_synthesis', 'evidence', 'evaluation',"
                " 'persistence', 'sqlalchemy', 'requests', 'openai', 'redis'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_no_phase10d_or_ranking_concepts(self):                                                         # 110-113
        banned = {"rank", "ranking", "score", "confidence", "best", "sizing", "size", "position", "quantity",
                  "account", "capital", "allocation", "target", "stop", "reward", "invalidation_check",
                  "probability", "expected", "edge"}
        a, _ = screened()
        stack, keys = [a], set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                keys |= set(value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        words = {w for k in keys for w in k.split("_")} | keys
        self.assertFalse(words & banned, words & banned)
        for module in (screening, builder, model, rules):
            source = inspect.getsource(module)
            self.assertNotIn("InvalidationCheck", source)
            for node in ast.walk(ast.parse(source)):
                name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
                if isinstance(name, str):
                    self.assertFalse(set(name.lower().split("_")) & banned, (module.__name__, name))


def scale_records(n):
    """n contracts (calls and puts over 10 expirations); a quarter of each quote kind."""
    expirations = ["2026-10-02", "2026-10-09", "2026-10-16", "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13",
                   "2026-11-20", "2026-11-27", "2026-12-18"]
    kinds = [quote(), quote("5", "5"), quote(bid=None), quote("9", "11")]
    per = n // (len(expirations) * 2)
    return [record(400 + i / 2, side, expiration, quote=kinds[i % 4])
            for expiration in expirations for side in ("call", "put") for i in range(per)]


class ScaleTests(unittest.TestCase):
    def check(self, n):
        oi = cases.chain(scale_records(n))
        self.assertEqual(len(oi["contracts"]), n)
        mi = cases.market_intelligence("all_bullish")
        policy = cases.screen_policy(**POLICY)
        start = time.perf_counter()
        a = assess(mi, oi, policy).to_dict()
        built = time.perf_counter() - start
        verify_assessment(a, mi, oi)
        rejected = {i for x in a["rejections"] for i in x["contract_ids"]}
        self.assertEqual(len(rejected) + len(a["candidates"]), n)
        self.assertEqual(a["outcome"]["status"], "setup_candidates")
        self.assertLess(built, 60)  # characterization bound only: about 1.5 s for 10,000 contracts

    def test_8000(self):                                                                                    # 114
        self.check(8000)

    def test_10000(self):                                                                                   # 115
        self.check(10000)


if __name__ == "__main__":
    unittest.main()
