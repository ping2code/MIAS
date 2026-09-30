"""Phase 10B Trade Setup core: policy, input validation and compatibility, market bias, gates, v1/v2 policy
compatibility, the no_setup assessment shell, and the internal pre-screening eligibility result."""
from copy import deepcopy
import unittest

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup.builder import prescreen
from trade_setup.canonical import canonical_json, content_id
from trade_setup.policy import PolicyError, make_policy, validated_policy
from trade_setup.validation import TradeSetupInputError, validated_assessment, verify_assessment
from tests import trade_setup_cases as cases

GAP = cases.MI_AS_OF_GAP


def mi(name="all_bullish"):
    return deepcopy(cases.market_intelligence(name))


def oi(name="quoted_complete", fmt="phase9-v2"):
    return deepcopy(cases.options_intelligence(name, fmt))


def run(mi_name="all_bullish", oi_name="quoted_complete", fmt="phase9-v2", **policy):
    return prescreen(mi(mi_name), oi(oi_name, fmt), cases.policy(**policy))


def eligible(result):
    return isinstance(result, m.PreScreeningEligibility) and result.eligible_for_contract_screening is True


def reseal(data, key):
    data[key] = content_id({k: v for k, v in data.items() if k != key})
    return data


class PolicyTests(unittest.TestCase):
    def test_exact_schema_and_id(self):
        p = cases.policy()
        self.assertEqual(list(p.to_dict()), list(m.POLICY_FIELDS))
        self.assertEqual(p.policy_format_version, "phase10-policy-v1")
        self.assertEqual(p.policy_id, content_id({k: v for k, v in p.to_dict().items() if k != "policy_id"}))
        self.assertEqual(cases.policy().policy_id, p.policy_id)
        self.assertNotEqual(cases.policy(min_dte=1).policy_id, p.policy_id)
        self.assertEqual(validated_policy(p.to_dict()), p)

    def test_unknown_and_missing_keys(self):
        with self.assertRaises(PolicyError):
            make_policy(**dict(cases.BASE_POLICY, rank_by="spread"))
        with self.assertRaises(PolicyError):
            make_policy(**{k: v for k, v in cases.BASE_POLICY.items() if k != "require_iv"})
        sealed = cases.policy().to_dict()
        sealed.pop("max_input_gap_seconds")
        with self.assertRaises(PolicyError):
            validated_policy(sealed)

    def test_field_validation(self):
        bad = [
            dict(allowed_sides=[]), dict(allowed_sides=["put", "call"]), dict(allowed_sides=["call", "straddle"]),
            dict(allowed_sides=["call", "call"]), dict(min_dte=-1), dict(min_dte=10, max_dte=5), dict(max_dte=5.0),
            dict(abs_delta_min=None), dict(abs_delta_min="0.8", abs_delta_max="0.7"), dict(abs_delta_max="1.5"),
            dict(abs_delta_min="-0.1"), dict(abs_delta_min=0.2), dict(max_spread_relative="-0.1"),
            dict(max_spread_relative="0.10"), dict(min_volume=-1), dict(min_volume=True), dict(min_open_interest=1.5),
            dict(max_premium_per_contract="0"), dict(max_input_gap_seconds=-1), dict(max_input_gap_seconds=None),
            dict(require_iv=1), dict(block_on_market_context_opposition="yes")]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(PolicyError):
                cases.policy(**overrides)
        ok = cases.policy(abs_delta_min=None, abs_delta_max=None, max_spread_relative=None, min_volume=0,
                          min_open_interest=0, max_premium_per_contract="0.01", max_input_gap_seconds=0)
        self.assertIsNone(ok.abs_delta_min)

    def test_tampered_policy(self):
        sealed = cases.policy().to_dict()
        sealed["max_dte"] = 90
        with self.assertRaises(PolicyError) as caught:
            validated_policy(sealed)
        self.assertEqual(str(caught.exception), "policy_id does not match the policy (tampered or corrupt)")

    def test_no_forbidden_policy_fields(self):
        for field in m.POLICY_FIELDS:
            for banned in ("rank", "target", "stop", "reward", "risk_reward", "size", "sizing", "account", "sec"):
                self.assertNotIn(banned, field.split("_"), field)


class InputValidationTests(unittest.TestCase):
    def assertInputError(self, market, options, message, policy=None):
        with self.assertRaises(TradeSetupInputError) as caught:
            prescreen(market, options, policy or cases.policy())
        self.assertEqual(str(caught.exception), message)

    def test_valid_inputs(self):
        a = run()
        self.assertEqual((a.inputs.symbol, a.inputs.input_gap_seconds, a.inputs.assessment_as_of),
                         ("META", GAP, "2026-09-30T14:45:00+00:00"))

    def test_tampered_and_malformed_inputs(self):
        tampered_mi = mi()
        tampered_mi["timeframe_structure"]["pattern"] = "all_bearish"
        self.assertInputError(tampered_mi, oi(), "market intelligence id does not match its body (tampered or corrupt)")
        tampered_oi = oi()
        tampered_oi["contracts"][0]["quote_state"] = "complete"
        self.assertInputError(mi(), tampered_oi, "options intelligence id does not match its body (tampered or corrupt)")
        self.assertInputError(reseal(dict(mi(), rules_version="phase8-rules-v2"), "intelligence_id"), oi(),
                              "unsupported market intelligence rules version")
        self.assertInputError(mi(), reseal(dict(oi(), rules_version="phase9-rules-v1"), "options_intelligence_id"),
                              "unsupported options intelligence rules version")
        bad_pattern = mi()
        bad_pattern["timeframe_structure"]["pattern"] = "mostly_bullish"
        self.assertInputError(reseal(bad_pattern, "intelligence_id"), oi(),
                              "market intelligence timeframe pattern is not supported")
        missing_v2 = oi()
        missing_v2["contracts"][0].pop("shares_per_contract")
        self.assertInputError(mi(), reseal(missing_v2, "options_intelligence_id"),
                              "options intelligence contract fields do not match its format version")
        unsorted = oi()
        unsorted["contracts"].reverse()
        self.assertInputError(mi(), reseal(unsorted, "options_intelligence_id"),
                              "options intelligence contracts are not unique and in canonical order")
        for bad in (None, [], "mi"):
            with self.assertRaises(TradeSetupInputError):
                prescreen(bad, oi(), cases.policy())

    def test_symbol_mismatch_is_an_error(self):
        self.assertInputError(mi(), oi("nvda"), "market intelligence and options intelligence symbols do not match")

    def test_input_gap(self):
        self.assertTrue(eligible(run(max_input_gap_seconds=GAP)))
        late = run(max_input_gap_seconds=GAP - 1)
        self.assertEqual((late.outcome.status, late.outcome.no_setup_reasons),
                         ("no_setup", ("inputs_not_contemporaneous",)))
        self.assertEqual(run(max_input_gap_seconds=10 ** 7).inputs.input_gap_seconds, GAP)


class MarketBiasTests(unittest.TestCase):
    def test_locked_mapping(self):
        expect = {
            "all_bullish": ("bullish", "call", None),
            "all_bearish": ("bearish", "put", None),
            "higher_aligned_5m_opposed": ("conflicting", None, "market_evidence_conflicting"),
            "unavailable_timeframe": ("insufficient", None, "market_evidence_insufficient"),
            "non_directional": ("non_directional", None, "market_evidence_non_directional"),
            "meta_real_shaped": ("partially_directional", None, "market_evidence_partially_directional"),
            "technical_unavailable": ("insufficient", None, "market_evidence_insufficient"),
        }
        for name, (state, side, reason) in expect.items():
            with self.subTest(case=name):
                a = run(name)
                self.assertEqual((a.market_bias.state, a.market_bias.side), (state, side))
                if reason:
                    self.assertIn(reason, a.outcome.no_setup_reasons)
                    self.assertIsNone(a.market_bias.invalidation)
                else:
                    self.assertEqual(a.market_bias.invalidation.to_dict(),
                                     dict(rule="pattern_must_remain", required_pattern=f"all_{state}",
                                          established_by=mi(name)["intelligence_id"]))

    def test_partial_technical_is_insufficient(self):
        data = mi("all_bullish")
        data["evidence_coverage"]["technical_status"] = "partial"
        a = prescreen(reseal(data, "intelligence_id"), oi(), cases.policy())
        self.assertEqual((a.market_bias.state, a.market_bias.pattern), ("insufficient", "all_bullish"))


class GateTests(unittest.TestCase):
    def test_side_gates(self):
        self.assertEqual(run(allowed_sides=["put"]).outcome.no_setup_reasons, ("side_not_allowed_by_policy",))
        self.assertEqual(run("all_bearish", allowed_sides=["call"]).outcome.no_setup_reasons,
                         ("side_not_allowed_by_policy",))
        self.assertTrue(eligible(run("all_bearish", allowed_sides=["put"])))

    def test_context_gates(self):
        # all_bullish carries both market_context_opposition_present and market_context_not_current.
        self.assertEqual(run(block_on_market_context_opposition=True).outcome.no_setup_reasons, ("context_gate_blocked",))
        self.assertEqual(run(block_on_market_context_not_current=True).outcome.no_setup_reasons,
                         ("context_gate_blocked",))
        clean = run("bullish_current_no_opposition", block_on_market_context_opposition=True,
                    block_on_market_context_not_current=True)
        self.assertTrue(eligible(clean))
        self.assertEqual([s.result for s in clean.decision_trace if s.rule.startswith("context")], ["pass", "pass"])

    def test_sec_filing_never_blocks(self):
        codes = {a["code"] for a in mi("bullish_current_no_opposition")["attention"]}
        self.assertIn("sec_filing_present", codes)
        every_gate = run("bullish_current_no_opposition", block_on_market_context_opposition=True,
                         block_on_market_context_not_current=True, require_complete_chain=True)
        self.assertTrue(eligible(every_gate))

    def test_chain_and_execution_gates(self):
        self.assertEqual(run(oi_name="quoted_truncated", require_complete_chain=True).outcome.no_setup_reasons,
                         ("options_chain_truncated",))
        self.assertTrue(eligible(run(oi_name="quoted_truncated")))
        self.assertEqual(run(oi_name="live_like_no_quotes").outcome.no_setup_reasons, ("execution_data_unavailable",))
        self.assertEqual(run(oi_name="locked_only").outcome.no_setup_reasons, ("execution_data_unavailable",))
        self.assertTrue(eligible(run(oi_name="locked_only", allow_locked_quote=True)))

    def test_all_reasons_collected(self):
        a = run("higher_aligned_5m_opposed", oi_name="quoted_truncated", require_complete_chain=True,
                max_input_gap_seconds=0)
        self.assertEqual(a.outcome.no_setup_reasons, ("inputs_not_contemporaneous", "market_evidence_conflicting",
                                                      "options_chain_truncated"))

    def test_screening_reasons_not_manufactured(self):
        for a in (run(allowed_sides=["put"]), run(oi_name="live_like_no_quotes"), run("non_directional")):
            self.assertFalse({"no_candidate_satisfies_policy", "source_timing_unverified"} & set(a.outcome.no_setup_reasons))
            self.assertEqual(a.decision_trace[-1].to_dict(), dict(step=8, rule="contract_screening", result="not_evaluated",
                                                                   reason="global_gate_failed",
                                                                   pointers=["oi:contracts[*]"]))


class PolicyCompatibilityTests(unittest.TestCase):
    def test_v1_with_numeric_rules_is_an_error(self):
        for overrides in (dict(min_volume=10), dict(min_open_interest=100), dict(max_premium_per_contract="500")):
            with self.subTest(overrides=overrides), self.assertRaises(TradeSetupInputError) as caught:
                run(fmt="phase9-v1", **overrides)
            self.assertEqual(str(caught.exception), "policy_requires_phase9_v2")

    def test_v1_without_numeric_rules_and_v2_with_them(self):
        v1 = run(fmt="phase9-v1")
        self.assertEqual((v1.execution_readiness.numeric_activity_facts_available,
                          v1.execution_readiness.shares_per_contract_present_count), (False, None))
        v2 = run(min_volume=10, min_open_interest=100, max_premium_per_contract="2000")
        self.assertTrue(eligible(v2))
        self.assertEqual((v2.execution_readiness.numeric_activity_facts_available,
                          v2.execution_readiness.shares_per_contract_present_count), (True, 9))


class AssessmentShellTests(unittest.TestCase):
    def test_exact_schema_and_identity(self):
        a = run(allowed_sides=["put"])
        data = a.to_dict()
        self.assertEqual(list(data), list(m.TOP_LEVEL_FIELDS))
        self.assertEqual((data["assessment_format_version"], data["rules_version"]), ("phase10-v1", "phase10-rules-v1"))
        self.assertEqual(a.assessment_id, content_id(a.body()))
        self.assertNotIn("generated_at", canonical_json(data))
        self.assertEqual((data["candidates"], data["rejections"], data["outcome"]["status"]), ([], [], "no_setup"))
        self.assertEqual(validated_assessment(data), data)
        self.assertEqual(verify_assessment(data, mi(), oi()), data)

    def test_provenance_and_readiness(self):
        a = run()
        self.assertEqual(a.provenance.to_dict(), dict(
            market_intelligence_id=mi()["intelligence_id"], options_intelligence_id=oi()["options_intelligence_id"],
            options_intelligence_format_version="phase9-v2", policy_id=cases.policy().policy_id,
            rules_version="phase10-rules-v1", pointer_version="phase10-pointer-v1"))
        e = a.execution_readiness
        self.assertEqual((e.contract_count, e.truncated, e.usable_two_sided_quote_count),
                         (9, False, 2))
        self.assertEqual({c.key: c.count for c in e.day_session_relation_counts},
                         dict(current_session=4, previous_session=1, older_session=1, unavailable=3))

    def test_trace_covers_every_gate(self):
        trace = run(allowed_sides=["put"]).decision_trace
        self.assertEqual([s.rule for s in trace], list(r.TRACE_RULES))
        self.assertEqual([s.step for s in trace], list(range(1, 9)))

    def test_every_reachable_reason(self):
        produced = set()
        for kwargs in (dict(mi_name="unavailable_timeframe"), dict(mi_name="higher_aligned_5m_opposed"),
                       dict(mi_name="non_directional"), dict(mi_name="meta_real_shaped"), dict(allowed_sides=["put"]),
                       dict(block_on_market_context_opposition=True), dict(max_input_gap_seconds=0),
                       dict(oi_name="live_like_no_quotes"), dict(oi_name="quoted_truncated", require_complete_chain=True)):
            produced |= set(run(**kwargs).outcome.no_setup_reasons)
        self.assertEqual(produced, set(r.NO_SETUP_REASONS) - {"source_timing_unverified",
                                                               "no_candidate_satisfies_policy"})
        self.assertNotIn("underlying_price_unavailable", r.NO_SETUP_REASONS)


class OutcomeContractTests(unittest.TestCase):
    """phase10-v1 has exactly two outcomes; 10B seals only global-gate no_setup, and a global pass is internal."""

    def test_frozen_two_value_vocabulary(self):
        self.assertEqual(r.OUTCOME_STATUSES, ("setup_candidates", "no_setup"))
        self.assertFalse(hasattr(r, "CONTRACT_SCREENING_PENDING"))
        self.assertNotIn("deferred_to_phase10c", r.NOT_EVALUATED_REASONS)

    def test_interim_status_rejected(self):
        data = run(allowed_sides=["put"]).to_dict()
        data["outcome"] = dict(status="contract_screening_pending", no_setup_reasons=[])
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(reseal(data, "assessment_id"))
        self.assertEqual(str(caught.exception), "assessment outcome status is not supported")

    def test_setup_candidates_not_available_in_10b(self):
        data = run(allowed_sides=["put"]).to_dict()
        data["outcome"] = dict(status="setup_candidates", no_setup_reasons=[])
        with self.assertRaises(TradeSetupInputError) as caught:
            validated_assessment(reseal(data, "assessment_id"))
        self.assertEqual(str(caught.exception), "setup_candidates requires contract screening (Phase 10C)")

    def test_every_global_failure_is_a_sealed_no_setup(self):
        for kwargs in (dict(mi_name="unavailable_timeframe"), dict(mi_name="higher_aligned_5m_opposed"),
                       dict(mi_name="non_directional"), dict(mi_name="meta_real_shaped"), dict(allowed_sides=["put"]),
                       dict(block_on_market_context_opposition=True), dict(block_on_market_context_not_current=True),
                       dict(max_input_gap_seconds=0), dict(oi_name="live_like_no_quotes"),
                       dict(oi_name="locked_only"), dict(oi_name="quoted_truncated", require_complete_chain=True)):
            with self.subTest(**kwargs):
                a = run(**kwargs)
                self.assertIsInstance(a, m.TradeSetupAssessment)
                self.assertEqual((a.outcome.status, a.candidates, a.rejections), ("no_setup", (), ()))
                self.assertTrue(a.outcome.no_setup_reasons)
                self.assertEqual(a, run(**kwargs))
                self.assertEqual(verify_assessment(a.to_dict(), mi(kwargs.get("mi_name", "all_bullish")),
                                                   oi(kwargs.get("oi_name", "quoted_complete"))), a.to_dict())

    def test_global_pass_is_pre_screening_eligibility(self):
        e = run()
        self.assertTrue(eligible(e))
        self.assertNotIsInstance(e, m.TradeSetupAssessment)
        data = e.to_dict()
        self.assertEqual(list(data), ["eligible_for_contract_screening", "eligible_side", "policy", "inputs",
                                      "market_bias", "execution_readiness", "decision_trace", "provenance"])
        for absent in ("assessment_id", "assessment_format_version", "rules_version", "outcome", "candidates",
                       "rejections"):
            self.assertNotIn(absent, data)
        self.assertEqual((e.eligible_side, e.market_bias.side, e.market_bias.state), ("call", "call", "bullish"))
        self.assertEqual(run("all_bearish", allowed_sides=["put"]).eligible_side, "put")
        self.assertEqual([s.rule for s in e.decision_trace], list(r.GLOBAL_GATE_RULES))
        self.assertTrue(all(s.result in ("pass", "not_evaluated") for s in e.decision_trace))
        self.assertEqual(e.policy, cases.policy())
        self.assertEqual(e.provenance.policy_id, cases.policy().policy_id)
        with self.assertRaises(Exception):
            e.eligible_side = "put"

    def test_eligibility_is_deterministic(self):
        first = canonical_json(run().to_dict())
        self.assertEqual({canonical_json(run().to_dict()) for _ in range(20)}, {first})
        self.assertEqual(run(), run())

    def test_no_screening_in_10b(self):
        # A global pass carries only chain-level facts; nothing per contract is chosen or rejected.
        e = run(min_volume=10 ** 9, max_premium_per_contract="0.01", abs_delta_min="0.99", abs_delta_max="1")
        self.assertTrue(eligible(e))
        self.assertEqual(e.execution_readiness, run().execution_readiness)

    def test_forged_no_setup_for_a_passing_input_is_rejected(self):
        data = run(allowed_sides=["put"]).to_dict()
        data["policy"] = cases.policy().to_dict()
        data["provenance"]["policy_id"] = data["policy"]["policy_id"]
        data["decision_trace"][2].update(result="fail", reason="side_not_allowed_by_policy")
        data = reseal(data, "assessment_id")
        validated_assessment(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_assessment(data, mi(), oi())
        self.assertEqual(str(caught.exception),
                         "assessment does not match its inputs: every global gate passes (contract screening required)")

    def test_screening_step_requires_a_failed_global_gate(self):
        for step in (dict(result="pass", reason=None), dict(result="not_evaluated", reason="policy_disabled")):
            data = run(allowed_sides=["put"]).to_dict()
            data["decision_trace"][-1].update(step)
            with self.subTest(step=step), self.assertRaises(TradeSetupInputError) as caught:
                validated_assessment(reseal(data, "assessment_id"))
            self.assertEqual(str(caught.exception),
                             "assessment contract_screening step requires a failed global gate (screening is Phase 10C)")


if __name__ == "__main__":
    unittest.main()
