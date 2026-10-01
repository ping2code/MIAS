"""Phase 10C contract screening: every rule, rejection aggregation, candidates, outcome and trace.

Fixtures run synthetic quoted records through the real Phase 9 pipeline (snapshot, then OptionsIntelligence).
The live current plan has no quotes, so it cannot show that screening produces candidates.
"""
from datetime import timedelta
from decimal import Decimal
import unittest

from trade_setup import model as m
from trade_setup import rules as r
from trade_setup import screening
from trade_setup.builder import assess, prescreen, screen, screening_pointers
from trade_setup.validation import TradeSetupInputError, validated_assessment, verify_assessment
from tests import trade_setup_cases as cases
from tests.options_snapshot_cases import AS_OF, BEFORE
from tests.trade_setup_cases import quote, record, verdict

CANDIDATE = "candidate"
UNTIMED = dict(delta="0.55", gamma="0.01", theta="-0.1", vega="0.2")
TIMED_GREEKS = dict(UNTIMED, observed_at=BEFORE)
TIMED_IV = dict(value="0.4", observed_at=BEFORE)
TIMED_OI = dict(value=15000, as_of_date="2026-09-29")


def reasons(*codes):
    return frozenset(codes)


class SideTests(unittest.TestCase):
    def test_side(self):
        self.assertEqual(verdict(record(option_type="call")), CANDIDATE)                                    # 1
        self.assertEqual(verdict(record(option_type="put")), reasons("side_mismatch"))                      # 2
        self.assertEqual(verdict(record(option_type="put"), mi_name="all_bearish"), CANDIDATE)              # 3
        self.assertEqual(verdict(record(option_type="call"), mi_name="all_bearish"), reasons("side_mismatch"))  # 4


class DteTests(unittest.TestCase):
    def test_dte_bounds(self):
        self.assertEqual(verdict(record(), min_dte=16), CANDIDATE)                                          # 5
        self.assertEqual(verdict(record(), max_dte=16), CANDIDATE)                                          # 6
        self.assertEqual(verdict(record(), min_dte=17, max_dte=60), reasons("expiration_outside_policy"))   # 7
        self.assertEqual(verdict(record(), max_dte=15), reasons("expiration_outside_policy"))               # 8

    def test_same_day(self):
        same_day = record(expiration="2026-09-30")
        self.assertEqual(verdict(same_day, allow_same_day_expiry=True), CANDIDATE)                          # 9
        self.assertEqual(verdict(same_day), reasons("same_day_expiry_excluded"))                            # 10
        self.assertEqual(verdict(same_day, min_dte=1), reasons("same_day_expiry_excluded",
                                                               "expiration_outside_policy"))  # neither suppressed


class QuoteTests(unittest.TestCase):
    def test_quote_states(self):
        self.assertEqual(verdict(record()), CANDIDATE)                                                      # 11
        locked = record(quote=quote("5", "5"))
        self.assertEqual(verdict(locked, allow_locked_quote=True), CANDIDATE)                               # 12
        self.assertEqual(verdict(locked), reasons("quote_locked_excluded"))                                 # 13
        self.assertEqual(verdict(record(quote=quote(bid=None))), reasons("quote_one_sided"))                # 14
        self.assertEqual(verdict(record(quote=quote(ask=None))), reasons("quote_one_sided"))                # 15
        self.assertEqual(verdict(record(quote=quote(None, None))), reasons("quote_one_sided"))              # 16
        self.assertEqual(verdict(record(quote=quote("10.2", "10"))), reasons("quote_crossed"))              # 17
        late = record(quote=quote(observed_at=AS_OF + timedelta(seconds=1)))
        self.assertEqual(verdict(late), reasons("quote_after_as_of"))                                       # 19

    def test_quote_unavailable(self):                                                                       # 18
        # Quote unavailability is chain-wide on a real snapshot (and then global gate 7 fails first), so one
        # contract is marked unavailable in an otherwise real, resealed OptionsIntelligence.
        rec = record()
        oi = cases.chain([rec, cases.anchor("call")])
        cid = cases.contract_id(oi, rec)
        edited = cases.resealed_oi(oi, cid, quote_state="unavailable", mid=None, spread_absolute=None,
                                   spread_relative=None, spread_relative_reason="quote_not_two_sided")
        a = assess(cases.market_intelligence("all_bullish"), edited, cases.screen_policy())
        self.assertEqual([x.reason_code for x in a.rejections if cid in x.contract_ids], ["quote_unavailable"])

    def test_mapping_is_frozen(self):
        self.assertEqual(r.QUOTE_STATE_REASONS, {
            "unavailable": "quote_unavailable", "bid_missing": "quote_one_sided", "ask_missing": "quote_one_sided",
            "both_missing": "quote_one_sided", "crossed": "quote_crossed", "locked": "quote_locked_excluded",
            "excluded_after_as_of": "quote_after_as_of"})
        self.assertEqual(set(r.QUOTE_STATE_REASONS) | {"complete"}, set(r.OI_QUOTE_STATES))


class SpreadTests(unittest.TestCase):
    def test_spread(self):
        wide = record(quote=quote("9", "11"))                                                    # relative 0.2
        self.assertEqual(verdict(wide), CANDIDATE)                                                          # 20
        self.assertEqual(verdict(record(), max_spread_relative="0.02"), CANDIDATE)                          # 21
        self.assertEqual(verdict(record(), max_spread_relative="0.019"), reasons("spread_above_policy"))    # 22
        zero_mid = record(quote=quote("0", "0"))                                           # locked, zero mid
        self.assertEqual(verdict(zero_mid, allow_locked_quote=True, max_spread_relative="0.1"),
                         reasons("spread_relative_unavailable"))                                            # 23
        self.assertEqual(verdict(zero_mid, allow_locked_quote=True), CANDIDATE)

    def test_spread_not_evaluated_without_a_usable_quote(self):
        self.assertEqual(verdict(record(quote=quote("10.2", "10")), max_spread_relative="0.1"),
                         reasons("quote_crossed"))


class DeltaTests(unittest.TestCase):
    def test_delta(self):
        rec = record(greeks=TIMED_GREEKS)                                                      # delta 0.55
        self.assertEqual(verdict(rec), CANDIDATE)                                                           # 24
        self.assertEqual(verdict(rec, abs_delta_min="0.55", abs_delta_max="0.7"), CANDIDATE)                # 25
        self.assertEqual(verdict(rec, abs_delta_min="0.2", abs_delta_max="0.55"), CANDIDATE)                # 26
        self.assertEqual(verdict(rec, abs_delta_min="0.56", abs_delta_max="0.7"), reasons("delta_outside_policy"))  # 27
        self.assertEqual(verdict(rec, abs_delta_min="0.2", abs_delta_max="0.54"), reasons("delta_outside_policy"))  # 28
        self.assertEqual(verdict(record(greeks=False), abs_delta_min="0.2", abs_delta_max="0.7"),
                         reasons("delta_unavailable"))                                                      # 29
        self.assertEqual(verdict(rec, abs_delta_min="0.5", abs_delta_max="0.6"), CANDIDATE)                 # 30
        put = record(option_type="put", greeks=dict(TIMED_GREEKS, delta="-0.45"))
        self.assertEqual(verdict(put, mi_name="all_bearish", abs_delta_min="0.4", abs_delta_max="0.5"), CANDIDATE)  # 31
        self.assertEqual(verdict(put, mi_name="all_bearish", abs_delta_min="0.46", abs_delta_max="0.5"),
                         reasons("delta_outside_policy"))

    def test_out_of_bounds_delta_is_unusable(self):
        wild = record(greeks=dict(TIMED_GREEKS, delta="1.3"))
        self.assertEqual(verdict(wild, abs_delta_min="0", abs_delta_max="1"), reasons("delta_unavailable"))
        self.assertEqual(verdict(wild), CANDIDATE)  # the delta rule is off: an out-of-bounds delta is not screened


class TimingTests(unittest.TestCase):
    def test_greeks_timing(self):
        untimed = record()
        on = dict(abs_delta_min="0.2", abs_delta_max="0.7")
        self.assertEqual(verdict(untimed, **on), CANDIDATE)                                                 # 32
        self.assertEqual(verdict(untimed, allow_unverified_time_basis=False, **on),
                         reasons("time_basis_unverified"))                                                  # 33
        self.assertEqual(verdict(record(greeks=TIMED_GREEKS), allow_unverified_time_basis=False, **on), CANDIDATE)

    def test_iv_timing(self):
        self.assertEqual(verdict(record(), require_iv=True), CANDIDATE)                                     # 34
        self.assertEqual(verdict(record(), require_iv=True, allow_unverified_time_basis=False),
                         reasons("time_basis_unverified"))                                                  # 35
        self.assertEqual(verdict(record(implied_volatility=TIMED_IV), require_iv=True,
                                 allow_unverified_time_basis=False), CANDIDATE)

    def test_open_interest_timing(self):
        self.assertEqual(verdict(record(), min_open_interest=100), CANDIDATE)                               # 36
        self.assertEqual(verdict(record(), min_open_interest=100, allow_unverified_time_basis=False),
                         reasons("time_basis_unverified"))                                                  # 37
        self.assertEqual(verdict(record(open_interest=TIMED_OI), min_open_interest=100,
                                 allow_unverified_time_basis=False), CANDIDATE)

    def test_unrelated_unverified_facts_do_not_reject(self):                                               # 38
        self.assertEqual(verdict(record(), allow_unverified_time_basis=False), CANDIDATE)

    def test_one_timing_reason_per_contract(self):
        rec = record()
        policy = cases.screen_policy(allow_unverified_time_basis=False, require_iv=True, min_open_interest=1,
                                     abs_delta_min="0.2", abs_delta_max="0.7")
        oi = cases.chain([rec])
        c = next(x for x in oi["contracts"] if x["provider_symbol"] == rec["provider_symbol"])
        found, _ = screening.evaluate(screening.source_facts(c, True), policy, "call", False)
        self.assertEqual(found, ("time_basis_unverified",))


class IvTests(unittest.TestCase):
    def test_iv(self):
        self.assertEqual(verdict(record(iv=False)), CANDIDATE)                                              # 39
        self.assertEqual(verdict(record(implied_volatility=TIMED_IV), require_iv=True), CANDIDATE)          # 40
        self.assertEqual(verdict(record(iv=False), require_iv=True), reasons("iv_unavailable"))             # 41


class DayTests(unittest.TestCase):
    def test_day_session(self):
        on = dict(require_current_session_day=True)
        self.assertEqual(verdict(record(day="current"), **on), CANDIDATE)                                   # 42
        self.assertEqual(verdict(record(day="previous"), **on), reasons("day_not_current_session"))         # 43
        self.assertEqual(verdict(record(day="older"), **on), reasons("day_not_current_session"))            # 44
        self.assertEqual(verdict(record(day="none"), **on), reasons("day_not_current_session"))             # 45
        self.assertEqual(verdict(record(day="older")), CANDIDATE)                                           # 46


class VolumeTests(unittest.TestCase):
    def test_volume(self):
        self.assertEqual(verdict(record(day="none")), CANDIDATE)                                            # 47
        self.assertEqual(verdict(record(), min_volume=1200), CANDIDATE)                                     # 48
        self.assertEqual(verdict(record(), min_volume=1199), CANDIDATE)                                     # 49
        self.assertEqual(verdict(record(), min_volume=1201), reasons("volume_below_policy"))                # 50
        self.assertEqual(verdict(record(day="none"), min_volume=0), reasons("volume_below_policy"))         # 51

    def test_previous_session_volume_stays_null(self):                                                     # 52
        rec = record(day="previous")
        oi = cases.chain([rec])
        self.assertIsNone(next(c for c in oi["contracts"]
                               if c["provider_symbol"] == rec["provider_symbol"])["current_session_volume"])
        self.assertEqual(verdict(rec, min_volume=1), reasons("volume_below_policy"))


class OpenInterestTests(unittest.TestCase):
    def test_open_interest(self):
        self.assertEqual(verdict(record(oi=False)), CANDIDATE)                                              # 53
        self.assertEqual(verdict(record(), min_open_interest=15000), CANDIDATE)                             # 54
        self.assertEqual(verdict(record(), min_open_interest=14999), CANDIDATE)                             # 55
        self.assertEqual(verdict(record(), min_open_interest=15001), reasons("open_interest_below_policy"))  # 56
        self.assertEqual(verdict(record(oi=False), min_open_interest=0), reasons("open_interest_below_policy"))  # 57

    def test_below_minimum_and_unverified_both_apply(self):
        self.assertEqual(verdict(record(), min_open_interest=20000, allow_unverified_time_basis=False),
                         reasons("open_interest_below_policy", "time_basis_unverified"))


class PremiumTests(unittest.TestCase):
    """entry_reference_ask = mid + spread_absolute / 2; max loss = ask * shares_per_contract (never assumed)."""

    def test_cap(self):
        self.assertEqual(verdict(record()), CANDIDATE)                                                      # 58
        self.assertEqual(verdict(record(), max_premium_per_contract="1010"), CANDIDATE)                     # 59
        self.assertEqual(verdict(record(), max_premium_per_contract="5000"), CANDIDATE)                     # 60
        self.assertEqual(verdict(record(), max_premium_per_contract="1009.99"), reasons("premium_above_policy"))  # 61

    def test_calculation(self):
        a, _ = cases.screened([record()], max_premium_per_contract="1010")                                  # 62
        self.assertEqual(a.candidates[0].derived.to_dict(), dict(entry_reference_ask="10.1",
                                                                 max_loss_per_contract="1010",
                                                                 premium_risk_status="computed"))
        locked = record(quote=quote("5", "5"))                                                              # 63
        a, _ = cases.screened([locked], allow_locked_quote=True, max_premium_per_contract="500")
        self.assertEqual(a.candidates[0].derived.to_dict(), dict(entry_reference_ask="5", max_loss_per_contract="500",
                                                                 premium_risk_status="computed"))
        self.assertEqual(verdict(locked, allow_locked_quote=True, max_premium_per_contract="499.99"),
                         reasons("premium_above_policy"))
        odd = record(quote=quote("1.05", "1.15"), shares=10)
        a, _ = cases.screened([odd])
        self.assertEqual((a.candidates[0].derived.entry_reference_ask, a.candidates[0].derived.max_loss_per_contract),
                         ("1.15", "11.5"))

    def test_missing_multiplier(self):                                                                      # 64
        no_shares = record(shares=None)
        self.assertEqual(verdict(no_shares, max_premium_per_contract="1000000"), reasons("multiplier_unavailable"))
        self.assertEqual(verdict(record(shares=None, quote=quote("10.2", "10")), max_premium_per_contract="1"),
                         reasons("quote_crossed"))  # the cap is not reached without a usable quote
        a, _ = cases.screened([no_shares])
        self.assertEqual(a.candidates[0].derived.to_dict(), dict(entry_reference_ask="10.1", max_loss_per_contract=None,
                                                                 premium_risk_status="multiplier_unavailable"))

    def test_v1_candidates_have_no_multiplier(self):
        a, _ = cases.screened([record()], fmt="phase9-v1")
        c = a.candidates[0]
        self.assertEqual((c.source.shares_per_contract, c.source.current_session_volume, c.derived.premium_risk_status),
                         (None, None, "multiplier_unavailable"))


class MultiFailureTests(unittest.TestCase):
    def test_every_reason_retained(self):                                                                   # 65, 66
        bad = record(option_type="put", expiration="2026-09-30", quote=quote("10.2", "10"), greeks=False, iv=False,
                     day="older", oi=False, shares=None)
        expected = reasons("side_mismatch", "expiration_outside_policy", "same_day_expiry_excluded", "quote_crossed",
                           "delta_unavailable", "iv_unavailable", "day_not_current_session", "volume_below_policy",
                           "open_interest_below_policy")
        self.assertEqual(verdict(bad, min_dte=1, abs_delta_min="0.2", abs_delta_max="0.7", require_iv=True,
                                 require_current_session_day=True, min_volume=1, min_open_interest=1,
                                 max_premium_per_contract="1000"), expected)

    def test_no_duplicate_reason_per_contract(self):                                                        # 67
        a, oi = cases.screened([record(greeks=False, quote=quote(bid=None))], abs_delta_min="0.2",
                               abs_delta_max="0.7")
        for x in a.rejections:
            self.assertEqual(len(set(x.contract_ids)), len(x.contract_ids))
        rec = oi["contracts"][0]
        found, _ = screening.evaluate(screening.source_facts(rec, True), cases.screen_policy(
            abs_delta_min="0.2", abs_delta_max="0.7"), "call", False)
        self.assertEqual(len(found), len(set(found)))


class CandidateTests(unittest.TestCase):
    def test_one_and_many(self):
        a, _ = cases.screened([record()])
        self.assertEqual(len(a.candidates), 1)                                                              # 68
        recs = [record(700), record(90), record(705, expiration="2026-10-23"), record(695, expiration="2026-10-23"),
                record(1000)]
        a, oi = cases.screened(recs)
        self.assertEqual(len(a.candidates), 5)                                                              # 69
        order = [(c.source.expiration, c.source.strike) for c in a.candidates]                              # 70
        self.assertEqual(order, [("2026-10-16", "90"), ("2026-10-16", "700"), ("2026-10-16", "1000"),
                                 ("2026-10-23", "695"), ("2026-10-23", "705")])

    def test_policy_checks(self):                                                                           # 71
        policy = dict(abs_delta_min="0.2", abs_delta_max="0.7", max_spread_relative="0.1", require_iv=True,
                      require_current_session_day=True, min_volume=1, min_open_interest=1,
                      max_premium_per_contract="2000")
        a, _ = cases.screened([record()], **policy)
        checks = [k.to_dict() for k in a.candidates[0].policy_checks]
        self.assertEqual([k["rule"] for k in checks], list(r.SCREENING_RULE_NAMES))
        for k in checks:
            self.assertEqual(set(k), {"rule", "result", "policy_field", "source_pointers"})
            self.assertEqual(k["result"], "pass")
            self.assertTrue(all(r.POINTER.fullmatch(p) for p in k["source_pointers"]))
        minimal, _ = cases.screened([record()])
        self.assertEqual([k.rule for k in minimal.candidates[0].policy_checks],
                         ["side", "dte_minimum", "dte_maximum", "same_day_expiry", "quote_state"])

    def test_candidate_shape_has_no_ranking(self):                                                          # 72, 73
        a, _ = cases.screened([record(), record(710)])
        for c in a.to_dict()["candidates"]:
            self.assertEqual(set(c), {"source", "derived", "policy_checks"})
            self.assertEqual(list(c["source"]), list(m.CandidateSource.__dataclass_fields__))
            self.assertEqual(set(c["derived"]), {"entry_reference_ask", "max_loss_per_contract", "premium_risk_status"})
            for banned in ("rank", "score", "confidence", "best", "weight", "priority", "probability"):
                self.assertNotIn(banned, str(sorted(c)) + str(sorted(c["source"])) + str(sorted(c["derived"])))

    def test_source_facts_are_copied(self):
        rec = record(greeks=TIMED_GREEKS, implied_volatility=TIMED_IV, open_interest=TIMED_OI)
        a, oi = cases.screened([rec])
        c, source = a.candidates[0].source, next(x for x in oi["contracts"] if x["provider_symbol"] == rec["provider_symbol"])
        self.assertEqual((c.mid, c.spread_absolute, c.spread_relative, c.delta, c.greeks_time_basis, c.implied_volatility,
                          c.iv_time_basis, c.open_interest_value, c.open_interest_time_basis, c.shares_per_contract),
                         (source["mid"], source["spread_absolute"], source["spread_relative"], "0.55", "observed_at",
                          "0.4", "observed_at", "15000", "provider_as_of_date", "100"))


class RejectionTests(unittest.TestCase):
    def setUp(self):
        recs = [record(700), record(710, option_type="put"), record(720, quote=quote(bid=None)),
                record(730, quote=quote("10.2", "10"), option_type="put"), record(740, quote=quote(None, None))]
        self.a, self.oi = cases.screened(recs)
        self.ids = {rec["strike"]: cases.contract_id(self.oi, rec) for rec in recs}

    def test_aggregation(self):                                                                             # 74-77
        data = {x.reason_code: list(x.contract_ids) for x in self.a.rejections}
        self.assertEqual(list(data), sorted(data))
        self.assertEqual(data["quote_one_sided"], sorted([self.ids["720"], self.ids["740"]]))
        self.assertEqual(data["side_mismatch"], sorted([self.ids["710"], self.ids["730"],
                                                        cases.contract_id(self.oi, cases.anchor("call"))]))
        self.assertEqual(data["quote_crossed"], [self.ids["730"]])
        for x in self.a.rejections:
            self.assertEqual(x.count, len(x.contract_ids))
            self.assertEqual(list(x.contract_ids), sorted(set(x.contract_ids)))

    def test_candidates_absent_from_rejections(self):                                                      # 78
        rejected = {i for x in self.a.rejections for i in x.contract_ids}
        self.assertEqual([c.source.contract_id for c in self.a.candidates], [self.ids["700"]])
        self.assertNotIn(self.ids["700"], rejected)
        self.assertEqual(len(rejected) + len(self.a.candidates), len(self.oi["contracts"]))

    def test_reason_vocabulary(self):
        self.assertEqual(len(r.REJECTION_REASONS), 19)
        self.assertEqual(r.REJECTION_REASONS[-1], "multiplier_unavailable")
        for rule, field, pointers in r.SCREENING_RULES:
            self.assertIn(field, m.POLICY_FIELDS)


class OutcomeTests(unittest.TestCase):
    def test_setup_candidates(self):                                                                        # 79, 80
        a, _ = cases.screened([record()])
        self.assertEqual((a.outcome.status, a.outcome.no_setup_reasons), ("setup_candidates", ()))

    def test_no_candidates(self):                                                                           # 81, 82
        a, _ = cases.screened([record()], max_dte=10)
        self.assertEqual((a.outcome.status, a.outcome.no_setup_reasons, a.candidates),
                         ("no_setup", ("no_candidate_satisfies_policy",), ()))

    def test_execution_data_unavailable_only_when_justified(self):                                          # 83
        one_sided = [record(700, quote=quote(bid=None)), record(710, quote=quote("10.2", "10"))]
        a, _ = cases.screened(one_sided)  # the put anchor carries the chain's only usable quote
        self.assertEqual(a.outcome.no_setup_reasons, ("execution_data_unavailable", "no_candidate_satisfies_policy"))
        a, _ = cases.screened([record(greeks=TIMED_GREEKS)], abs_delta_min="0.8", abs_delta_max="0.9")
        self.assertEqual(a.outcome.no_setup_reasons, ("no_candidate_satisfies_policy",))

    def test_source_timing_unverified_only_when_justified(self):                                            # 84
        a, _ = cases.screened([record()], require_iv=True, allow_unverified_time_basis=False)
        self.assertEqual(a.outcome.no_setup_reasons, ("no_candidate_satisfies_policy", "source_timing_unverified"))
        a, _ = cases.screened([record()], require_iv=True, allow_unverified_time_basis=False, max_dte=10)
        self.assertEqual(a.outcome.no_setup_reasons, ("no_candidate_satisfies_policy",))
        a, _ = cases.screened([record(), record(710, implied_volatility=TIMED_IV)], require_iv=True,
                              allow_unverified_time_basis=False)
        self.assertEqual(a.outcome.status, "setup_candidates")  # never added when candidates exist

    def test_global_failure_never_screens(self):
        a, _ = cases.screened([record()], max_input_gap_seconds=0)
        self.assertEqual((a.outcome.status, a.candidates, a.rejections, a.decision_trace[-1].reason),
                         ("no_setup", (), (), "global_gate_failed"))


class TraceTests(unittest.TestCase):
    def test_trace(self):
        mi, oi = cases.market_intelligence("all_bullish"), cases.chain([record(), cases.anchor("call")])
        policy = cases.screen_policy(max_spread_relative="0.1")
        eligibility = prescreen(mi, oi, policy)
        a = assess(mi, oi, policy)
        self.assertEqual(a.decision_trace[:7], eligibility.decision_trace)                                  # 85
        self.assertEqual(a.decision_trace[7].to_dict(), dict(                                               # 86
            step=8, rule="contract_screening", result="pass", reason="candidates_available",
            pointers=["oi:contracts[*]", "policy:allow_locked_quote", "policy:allow_same_day_expiry",
                      "policy:allowed_sides", "policy:max_dte", "policy:max_spread_relative", "policy:min_dte"]))
        b = assess(mi, oi, cases.screen_policy(max_dte=10))
        self.assertEqual((b.decision_trace[7].result, b.decision_trace[7].reason),                          # 87
                         ("fail", "no_candidate_satisfies_policy"))
        self.assertEqual(list(b.decision_trace[7].pointers), list(screening_pointers(cases.screen_policy())))  # 88
        self.assertNotIn(a.candidates[0].source.contract_id, str(a.to_dict()["decision_trace"]))


class EntryPointTests(unittest.TestCase):
    def test_screen_fails_closed(self):
        mi, oi = cases.market_intelligence("all_bullish"), cases.chain([record(), cases.anchor("call")])
        eligibility = prescreen(mi, oi, cases.screen_policy())
        for bad, message in (
                (cases.chain([record(705), cases.anchor("call")]),
                 "options intelligence does not match the pre-screening eligibility"),):
            with self.assertRaises(TradeSetupInputError) as caught:
                screen(eligibility, bad)
            self.assertEqual(str(caught.exception), message)
        with self.assertRaises(TradeSetupInputError):
            screen(assess(mi, oi, cases.screen_policy(max_input_gap_seconds=0)), oi)
        from dataclasses import replace
        v1 = cases.chain([record(), cases.anchor("call")], fmt="phase9-v1")
        v1_eligibility = prescreen(mi, v1, cases.screen_policy())
        forged = replace(v1_eligibility, policy=cases.screen_policy(min_volume=1))
        with self.assertRaises(TradeSetupInputError) as caught:
            screen(forged, v1)
        self.assertEqual(str(caught.exception), "policy_requires_phase9_v2")
        with self.assertRaises(TradeSetupInputError):
            screen(replace(eligibility, eligible_side="put"), oi)

    def test_assess_validates_and_verifies(self):
        for kw in (dict(), dict(max_dte=10), dict(allow_locked_quote=True, max_premium_per_contract="600")):
            a, oi = cases.screened([record(), record(710, quote=quote("5", "5"))], **kw)
            data = a.to_dict()
            self.assertEqual(validated_assessment(data), data)
            self.assertEqual(verify_assessment(data, cases.market_intelligence("all_bullish"), oi), data)

    def test_snapshot_record_order_is_irrelevant(self):
        recs = [record(700), record(710, option_type="put"), record(720, quote=quote(bid=None)), record(90)]
        a, _ = cases.screened(recs)
        b, _ = cases.screened(list(reversed(recs)))
        self.assertEqual(a, b)


class ScreeningOrderTests(unittest.TestCase):
    def test_frozen_order(self):
        self.assertEqual(r.SCREENING_RULE_NAMES, (
            "side", "dte_minimum", "dte_maximum", "same_day_expiry", "quote_state", "spread_availability",
            "spread_threshold", "delta_minimum", "delta_maximum", "greeks_time_basis", "iv_availability",
            "iv_time_basis", "day_session", "volume_threshold", "open_interest_threshold", "open_interest_time_basis",
            "multiplier_availability", "premium_cap"))

    def test_reasons_follow_rule_order(self):
        bad = record(option_type="put", expiration="2026-09-30", quote=quote("10.2", "10"), iv=False)
        oi = cases.chain([bad])
        found, _ = screening.evaluate(screening.source_facts(oi["contracts"][0], True),
                                      cases.screen_policy(min_dte=1, require_iv=True), "call", False)
        self.assertEqual(found, ("side_mismatch", "expiration_outside_policy", "same_day_expiry_excluded",
                                 "quote_crossed", "iv_unavailable"))

    def test_exact_decimal_comparison(self):
        self.assertEqual(Decimal("0.02"), Decimal("0.020"))
        self.assertEqual(verdict(record(), max_spread_relative="0.02"), CANDIDATE)


if __name__ == "__main__":
    unittest.main()
