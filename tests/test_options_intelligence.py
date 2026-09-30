"""Phase 9D OptionsIntelligence semantics: schema, references, DTE, strikes, quotes, activity, day sessions, IV,
Greeks, summaries, completeness, attention and provenance."""
from datetime import timedelta
from decimal import Decimal
import json
import unittest

from options_intelligence import model as m
from options_intelligence import rules as r
from options_intelligence.builder import build
from options_intelligence.canonical import canonical_json
from options_intelligence.validation import OptionsIntelligenceError
from tests import options_intelligence_cases as cases
from tests.options_snapshot_cases import AS_OF, chain_record, full_snapshot, live_snapshot

CAL = cases.CALENDAR


def by_id(intelligence):
    return {c.contract_id: c for c in intelligence.contracts}


def attention(intelligence):
    return {a.code: a for a in intelligence.attention}


def quoted():
    return build(cases.snapshot("quoted_with_price"), calendar=CAL)


class SchemaTests(unittest.TestCase):
    def test_exactly_13_fields_and_versions(self):
        d = quoted().to_dict()
        self.assertEqual(list(d), list(m.TOP_LEVEL_FIELDS))
        self.assertEqual((d["options_intelligence_format_version"], d["rules_version"]), ("phase9-v1", "phase9-rules-v1"))
        self.assertNotIn("generated_at", canonical_json(d))

    def test_goldens_byte_exact(self):
        for name in cases.names():
            with self.subTest(case=name):
                self.assertEqual(canonical_json(build(cases.snapshot(name), calendar=CAL).to_dict()) + "\n",
                                 cases.intelligence_path(name).read_text(encoding="utf-8"))
        with_mi = build(cases.snapshot("live_like_meta"), cases.market_intelligence(), calendar=CAL)
        self.assertEqual(canonical_json(with_mi.to_dict()) + "\n",
                         cases.intelligence_path("live_like_meta_with_mi").read_text(encoding="utf-8"))

    def test_snapshot_ref_and_provenance(self):
        snap = cases.snapshot("quoted_with_price")
        i = build(snap, calendar=CAL)
        self.assertEqual(i.snapshot_ref.to_dict(), dict(snapshot_id=snap.snapshot_id,
                                                        snapshot_format_version="phase9-snapshot-v1",
                                                        underlying="META", as_of=snap.as_of))
        self.assertEqual(i.provenance.to_dict(), dict(snapshot_id=snap.snapshot_id, market_intelligence_id=None,
                                                      rules_version="phase9-rules-v1", pointer_version="phase9-pointer-v1"))
        self.assertIsNone(i.market_intelligence_ref)


class MarketIntelligenceReferenceTests(unittest.TestCase):
    def test_valid_reference_only(self):
        mi = cases.market_intelligence()
        snap = cases.snapshot("live_like_meta")
        with_mi, without = build(snap, mi, calendar=CAL), build(snap, calendar=CAL)
        ref = with_mi.market_intelligence_ref
        self.assertEqual((ref.intelligence_id, ref.symbol, ref.as_of, ref.as_of_gap_seconds),
                         (mi.intelligence_id, "META", "2026-09-23T20:05:00+00:00", 585600))
        a, b = with_mi.to_dict(), without.to_dict()
        for key in ("market_intelligence_ref", "provenance", "options_intelligence_id"):
            a.pop(key), b.pop(key)
        self.assertEqual(a, b)  # MarketIntelligence never changes any options fact.
        self.assertEqual(build(snap, mi.to_dict(), calendar=CAL), with_mi)  # typed and dict agree

    def test_invalid_references_fail_closed(self):
        mi = cases.market_intelligence().to_dict()
        cases_ = [
            (mi, live_snapshot([], underlying="NVDA"), "market intelligence symbol does not match the snapshot underlying"),
            (dict(mi, intelligence_id="sha256:" + "0" * 64), live_snapshot([]),
             "market intelligence id does not match its body (tampered or corrupt)"),
            (dict(mi, rules_version="phase8-rules-v9"), live_snapshot([]), "unsupported market intelligence rules version"),
            ({k: v for k, v in mi.items() if k != "attention"}, live_snapshot([]),
             "market intelligence must have exactly the 13 phase8-v1 keys"),
            (mi, early_snapshot(), "market intelligence as_of is later than the snapshot as_of"),
        ]
        for value, snap, message in cases_:
            with self.subTest(message=message), self.assertRaises(OptionsIntelligenceError) as caught:
                build(snap, value, calendar=CAL)
            self.assertEqual(str(caught.exception), message)

    def test_versions_pinned_to_market_intelligence(self):
        from market_intelligence import rules as mir  # Test-only import.
        self.assertEqual(r.SUPPORTED_MARKET_INTELLIGENCE_FORMATS, {mir.INTELLIGENCE_FORMAT_VERSION})
        self.assertEqual(r.SUPPORTED_MARKET_INTELLIGENCE_RULES, {mir.RULES_VERSION})


def early_snapshot():
    from options_data.normalization import assemble
    from tests.options_snapshot_cases import PROVENANCE
    return assemble("META", AS_OF.replace(year=2026, month=9, day=20), [], provenance=PROVENANCE)


class ContractFactTests(unittest.TestCase):
    def test_dte_and_same_day_expiry(self):
        c = by_id(quoted())
        self.assertEqual((c["META260930P00700000"].dte_calendar_days, c["META260930P00700000"].expires_on_as_of_date),
                         (0, True))
        self.assertEqual((c["META261016C00690000"].dte_calendar_days, c["META261016C00690000"].expires_on_as_of_date),
                         (16, False))

    def test_strike_relation_and_distance(self):
        c = by_id(quoted())
        self.assertEqual([(c[k].strike_relation, c[k].strike_distance, c[k].strike_distance_relative) for k in (
            "META261016C00690000", "META261016C00700000", "META261016C00710000")],
            [("below", "-10", "-0.0142857143"), ("equal", "0", "0"), ("above", "10", "0.0142857143")])
        unavailable = build(cases.snapshot("live_like_meta"), calendar=CAL).contracts[0]
        self.assertEqual((unavailable.strike_relation, unavailable.strike_distance, unavailable.strike_distance_relative),
                         ("unavailable", None, None))
        for label in ("itm", "otm", "atm", "in_the_money", "moneyness"):
            self.assertNotIn(label, canonical_json(quoted().to_dict()).lower())

    def test_quote_states_mid_and_spread(self):
        c = by_id(quoted())
        expected = {
            "META261016C00690000": ("complete", "10.25", "0.3", "0.0292682927", None),
            "META261016C00700000": ("locked", "10.3", "0", "0", None),
            "META261016C00710000": ("crossed", None, None, None, "quote_not_two_sided"),
            "META261016C00720000": ("bid_missing", None, None, None, "quote_not_two_sided"),
            "META261016C00730000": ("ask_missing", None, None, None, "quote_not_two_sided"),
            "META261016C00740000": ("locked", "0", "0", None, "zero_mid"),
            "META261016C00750000": ("excluded_after_as_of", None, None, None, "quote_not_two_sided"),
            "META260930P00700000": ("both_missing", None, None, None, "quote_not_two_sided"),
        }
        for cid, values in expected.items():
            x = c[cid]
            self.assertEqual((x.quote_state, x.mid, x.spread_absolute, x.spread_relative, x.spread_relative_reason),
                             values, cid)
        live = build(cases.snapshot("live_like_meta"), calendar=CAL)
        self.assertEqual({x.quote_state for x in live.contracts}, {"unavailable"})

    def test_quote_state_rule_table(self):
        D = Decimal
        for args, state in (((("present", D("1"), D("2"))), "complete"), (("present", D("2"), D("2")), "locked"),
                            (("present", D("2"), D("1")), "crossed"), (("present", None, D("1")), "bid_missing"),
                            (("present", D("1"), None), "ask_missing"), (("missing", None, None), "both_missing"),
                            (("unavailable", None, None), "unavailable"),
                            (("excluded_after_as_of", None, None), "excluded_after_as_of")):
            self.assertEqual(r.quote_state(*args), state)

    def test_volume_and_open_interest_states(self):
        c = by_id(quoted())
        self.assertEqual((c["META261016C00690000"].volume_state, c["META261016C00690000"].volume_exceeds_open_interest),
                         ("positive", True))
        self.assertEqual((c["META261016C00720000"].volume_state, c["META261016C00720000"].volume_exceeds_open_interest),
                         ("zero", False))
        self.assertEqual((c["META261016C00730000"].volume_state, c["META261016C00730000"].open_interest_state,
                          c["META261016C00730000"].volume_exceeds_open_interest), ("missing", "zero", None))
        for status, value, state in (("unavailable", None, "unavailable"), ("excluded_after_as_of", None,
                                                                             "excluded_after_as_of")):
            self.assertEqual(r.activity_state(status, value), state)
        self.assertNotIn("unusual", canonical_json(quoted().to_dict()))

    def test_day_session_relation_and_age(self):
        c = by_id(quoted())
        self.assertEqual([(c[k].day.session_relation, c[k].day.age_seconds) for k in (
            "META261016C00690000", "META261016C00700000", "META261016C00710000", "META261016C00730000")],
            [("current_session", 60), ("previous_session", 67500), ("older_session", 413100), ("unavailable", None)])
        untimed = build(cases.snapshot("live_like_meta"), calendar=CAL).contracts[0].day
        self.assertEqual((untimed.status, untimed.session_relation), ("present", "unavailable"))


class IvAndGreeksTests(unittest.TestCase):
    def test_iv_copied_with_status(self):
        c = by_id(quoted())
        self.assertEqual(c["META261016P00700000"].implied_volatility.to_dict(),
                         dict(status="present", value="0.39", time_basis="provider_snapshot_unverified"))
        self.assertEqual(c["META260930P00700000"].implied_volatility.status, "missing")
        nvda = build(cases.snapshot("live_like_nvda"), calendar=CAL)
        self.assertEqual({x.implied_volatility.status for x in nvda.contracts}, {"present", "missing"})

    def test_greeks_availability_bounds_and_rho(self):
        c = by_id(quoted())
        full = c["META261016P00700000"].greeks
        self.assertEqual((full.available_fields, full.missing_fields, full.rho), (r.GREEK_FIELDS, (), "-0.12"))
        partial = c["META261016C00690000"].greeks
        self.assertEqual((partial.missing_fields, partial.rho), (("rho",), None))
        self.assertEqual(c["META261016C00740000"].greeks.out_of_bounds_fields, ("delta", "gamma"))
        self.assertEqual(c["META260930P00700000"].greeks.status, "missing")
        self.assertEqual(r.greeks_out_of_bounds("put", {"delta": Decimal("0.2")}), ["delta"])
        self.assertEqual(r.greeks_out_of_bounds("call", {"delta": Decimal("1"), "theta": Decimal("-9"),
                                                         "vega": Decimal("0")}), [])

    def test_median_odd_even(self):
        D = Decimal
        self.assertEqual(r.median([D("0.3"), D("0.1"), D("0.2")]), D("0.2"))
        self.assertEqual(r.median([D("0.4"), D("0.1"), D("0.2"), D("0.3")]), D("0.25"))
        chain = [chain_record(strike=s, implied_volatility=dict(value=v)) for s, v in
                 ((690, "0.30"), (700, "0.10"), (710, "0.20"), (720, "0.40"))]
        summary = build(live_snapshot(chain), calendar=CAL).volatility.overall
        self.assertEqual((summary.available_count, summary.min, summary.max, summary.median), (4, "0.1", "0.4", "0.25"))


class SummaryTests(unittest.TestCase):
    def test_expiration_summaries(self):
        exps = {e.expiration: e for e in quoted().expirations}
        e = exps["2026-10-16"]
        self.assertEqual((e.dte_calendar_days, e.contract_count, e.call_count, e.put_count, e.distinct_strike_count,
                          e.strike_min, e.strike_max, e.paired_strike_count),
                         (16, 8, 7, 1, 7, "690", "750", 1))
        self.assertEqual({c.key: c.count for c in e.quote_state_counts},
                         dict(ask_missing=1, bid_missing=1, complete=2, crossed=1, excluded_after_as_of=1, locked=2))
        self.assertEqual((exps["2026-09-30"].paired_strike_count, exps["2026-09-30"].put_count), (0, 1))
        self.assertEqual(list(exps), sorted(exps))

    def test_chain_activity_and_ratios(self):
        a = quoted().activity
        self.assertEqual((a.volume_basis, a.call_volume_total, a.put_volume_total, a.call_volume_records,
                          a.put_volume_records, a.volume_records_not_current_session),
                         ("current_session_day_records", "500", "1100", 2, 2, 4))
        self.assertEqual((a.put_call_volume_ratio, a.put_call_open_interest_ratio), ("2.2", "0.3978779841"))

    def test_zero_denominator_and_unavailable_totals(self):
        zero_calls = [chain_record(strike=700, day=cases.day(cases.AS_OF - timedelta(minutes=1), 0)),
                      chain_record(strike=700, option_type="put", day=cases.day(cases.AS_OF - timedelta(minutes=1), 5),
                                   open_interest=dict(value=3))]
        zero_calls[0]["open_interest"] = dict(value=0)
        a = build(full_snapshot(zero_calls), calendar=CAL).activity
        self.assertEqual((a.put_call_volume_ratio, a.put_call_volume_ratio_reason), (None, "zero_denominator"))
        self.assertEqual((a.put_call_open_interest_ratio, a.put_call_open_interest_ratio_reason),
                         (None, "zero_denominator"))
        live = build(cases.snapshot("live_like_meta"), calendar=CAL).activity  # untimed day records
        self.assertEqual((live.call_volume_total, live.put_call_volume_ratio_reason), (None, "total_unavailable"))

    def test_completeness_counts_and_carry_through(self):
        snap = live_snapshot([chain_record(), chain_record(), "junk"],
                             provenance=dict(provider="massive", truncated=True))
        comp = build(snap, calendar=CAL).chain_completeness
        self.assertEqual((comp.contract_count, comp.records_received, comp.truncated),
                         (1, 3, True))
        self.assertEqual({c.key: c.count for c in comp.exclusions}, dict(duplicate_identical=1, malformed_record=1))
        self.assertEqual({c.key: c.count for c in comp.contracts_with},
                         dict(quote=0, trade=0, day=1, open_interest=1, implied_volatility=1, greeks=1))
        self.assertEqual([g.group for g in comp.status_counts],
                         ["quote", "trade", "day", "open_interest", "implied_volatility", "greeks"])


class AttentionTests(unittest.TestCase):
    def test_each_code_independently(self):
        q, live = attention(quoted()), attention(build(live_snapshot([chain_record(), "junk"]), calendar=CAL))
        expect = {
            "quote_incomplete": (q, 4), "crossed_quote": (q, 1), "locked_quote": (q, 2), "iv_unavailable": (q, 1),
            "greeks_unavailable": (q, 1), "greeks_out_of_bounds": (q, 1), "no_volume": (q, 2),
            "no_open_interest": (q, 1), "day_not_current_session": (q, 5), "facts_excluded_after_as_of": (q, 1),
            "time_basis_unverified": (q, 9), "underlying_price_unavailable": (live, 1), "records_excluded": (live, 1),
        }
        self.assertEqual(set(expect), set(r.ATTENTION_CODES))
        for code, (flags, count) in expect.items():
            with self.subTest(code=code):
                self.assertEqual((flags[code].count, flags[code].category), (count, r.ATTENTION_CODES[code]))
        self.assertEqual(q["crossed_quote"].contract_ids, ("META261016C00710000",))
        self.assertNotIn("underlying_price_unavailable", q)

    def test_no_severity_priority_or_score(self):
        for a in quoted().attention:
            self.assertEqual(set(a.to_dict()), {"category", "code", "scope", "count", "contract_ids"})
        keys = [(a.category, a.code) for a in quoted().attention]
        self.assertEqual(keys, sorted(keys))
        for banned in ("wide_spread", "poor_liquidity", "good_liquidity", "tradeable", "avoid"):
            self.assertNotIn(banned, r.ATTENTION_CODES)


class ProvenanceTests(unittest.TestCase):
    def test_pointers_resolve_to_snapshot_keys(self):
        snap = cases.snapshot("quoted_with_price").to_dict()
        contracts = {c["identity"]["contract_id"]: c for c in snap["contracts"]}
        for c in quoted().contracts:
            for pointer in c.source_pointers:
                if pointer == "as_of":
                    self.assertIn("as_of", snap)
                elif pointer == "underlying_price.value":
                    self.assertIsNotNone(snap["underlying_price"]["value"])
                else:
                    cid, rest = pointer[len("contracts["):].split("].", 1)
                    group, field = rest.split(".")
                    self.assertEqual(cid, c.contract_id)
                    self.assertIsNotNone(contracts[cid][group][field], pointer)


if __name__ == "__main__":
    unittest.main()
