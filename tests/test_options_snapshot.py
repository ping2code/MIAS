"""Phase 9B OptionsSnapshot: schema, identity, fact status and time basis, cutoff, duplicates, exclusions."""
from copy import deepcopy
from datetime import timedelta
import itertools
import json
import unittest

from options_data import identity as ident
from options_data import model as m
from options_data.canonical import canonical_json, content_id
from options_data.normalization import SnapshotAssemblyError, assemble
from tests.options_snapshot_cases import (AFTER, AS_OF, BEFORE, PROVENANCE, chain_record, full_snapshot,
                                          live_snapshot, meta_chain, nvda_chain, symbol)


def contract(snapshot, contract_id):
    return next(c for c in snapshot.contracts if c.identity.contract_id == contract_id)


def exclusions(snapshot):
    return {e.reason: e.count for e in snapshot.exclusions}


class SchemaTests(unittest.TestCase):
    def test_exact_top_level_schema_and_id(self):
        s = live_snapshot(meta_chain())
        data = s.to_dict()
        self.assertEqual(list(data), list(m.TOP_LEVEL_FIELDS))
        self.assertEqual(len(data), 10)
        self.assertEqual(data["snapshot_format_version"], "phase9-snapshot-v1")
        self.assertEqual(s.snapshot_id, content_id(s.body()))
        self.assertNotIn("generated_at", canonical_json(data))
        self.assertEqual((s.as_of, s.session.session_date, s.session.calendar_state),
                         ("2026-09-30T14:45:00+00:00", "2026-09-30", "regular"))

    def test_contract_groups(self):
        c = live_snapshot(meta_chain()).contracts[0].to_dict()
        self.assertEqual(list(c), ["identity", "terms", "quote", "trade", "day", "open_interest",
                                   "implied_volatility", "greeks"])
        self.assertEqual(list(c["greeks"]), ["status", "delta", "gamma", "theta", "vega", "rho", "observed_at",
                                             "time_basis", "source"])

    def test_canonical_json_matches_mias(self):
        from evidence_packet.serialization import canonical_json as mias  # Test-only comparison.
        data = live_snapshot(meta_chain()).to_dict()
        self.assertEqual(canonical_json(data), mias(data))

    def test_decimal_strings_never_floats(self):
        stack = [live_snapshot(meta_chain() + nvda_chain()[:0]).to_dict()]
        while stack:
            value = stack.pop()
            self.assertNotIsInstance(value, float)
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        with self.subTest("float input is malformed"):
            s = live_snapshot([chain_record(implied_volatility=dict(value=0.41))])
            self.assertEqual((s.contracts, exclusions(s)), ((), {"malformed_record": 1}))


class IdentityTests(unittest.TestCase):
    def test_occ_parsing(self):
        self.assertEqual(ident.parse("O:META261016C00700000"), ("META", ident.date(2026, 10, 16), "call", 700))
        self.assertEqual(ident.parse("NVDA261016P00187500")[3], ident.Decimal("187.5"))
        self.assertEqual(ident.contract_id("NVDA", ident.date(2026, 10, 16), "put", ident.Decimal("187.5")),
                         "NVDA261016P00187500")
        for bad in ("META", "O:META261316C00700000", "O:meta261016C00700000", "O:META261016X00700000", 42, None):
            with self.subTest(bad=bad), self.assertRaises(ident.IdentityError):
                ident.parse(bad)

    def test_provider_symbol_kept_and_id_normalized(self):
        s = live_snapshot([chain_record()])
        i = s.contracts[0].identity
        self.assertEqual((i.contract_id, i.provider_symbol, i.root, i.root_matches_underlying, i.option_type,
                          i.expiration, i.strike),
                         ("META261016C00700000", "O:META261016C00700000", "META", True, "call", "2026-10-16", "700"))
        unprefixed = live_snapshot([dict(chain_record(), provider_symbol="META261016C00700000")])
        self.assertEqual(unprefixed.contracts[0].identity.contract_id, "META261016C00700000")

    def test_cross_check_mismatches(self):
        cases = {
            "identity_mismatch": [chain_record(option_type="put", provider_symbol=symbol("META", "2026-10-16", "call", 700)),
                                  chain_record(strike="705", provider_symbol=symbol("META", "2026-10-16", "call", 700)),
                                  chain_record(expiration="2026-10-17",
                                               provider_symbol=symbol("META", "2026-10-16", "call", 700)),
                                  chain_record(underlying="NVDA")],
            "unsupported_option_type": [chain_record(option_type="straddle")],
            "invalid_strike": [chain_record(strike="0", provider_symbol="O:META261016C00000000")],
            "malformed_record": [dict(chain_record(), provider_symbol="not-a-symbol"), "junk", {},
                                 dict(chain_record(), unknown_field=1)],
            "expired_before_as_of": [chain_record(expiration="2026-09-29")],
        }
        for reason, records in cases.items():
            with self.subTest(reason=reason):
                s = live_snapshot(records)
                self.assertEqual((s.contracts, exclusions(s)), ((), {reason: len(records)}))
        same_day = live_snapshot([chain_record(expiration="2026-09-30")])
        self.assertEqual(len(same_day.contracts), 1)  # Expiring on the as_of date is not yet expired.

    def test_adjusted_nonstandard_root(self):
        adjusted = chain_record("META1", strike=700, underlying="META", shares=50,
                                terms=dict(exercise_style="american", shares_per_contract=50,
                                           deliverables=[dict(kind="equity", symbol="META", amount="50"),
                                                         dict(kind="cash", symbol="USD", amount="125.5")]))
        s = live_snapshot([chain_record(), adjusted])
        self.assertEqual(len(s.contracts), 2)  # Same underlying, expiration, type and strike: different identities.
        a = contract(s, "META1261016C00700000")
        self.assertEqual((a.identity.root, a.identity.root_matches_underlying, a.terms.shares_per_contract),
                         ("META1", False, "50"))
        self.assertEqual([(d.kind, d.symbol, d.amount) for d in a.terms.deliverables],
                         [("cash", "USD", "125.5"), ("equity", "META", "50")])

    def test_calls_and_puts(self):
        s = live_snapshot(meta_chain())
        self.assertEqual({c.identity.option_type for c in s.contracts}, {"call", "put"})
        self.assertEqual(len(s.contracts), 12)

    def test_arbitrary_symbols(self):
        for underlying in ("F", "SPY", "BRK.B", "GOOGL", "TSLA"):
            with self.subTest(underlying=underlying):
                s = live_snapshot([chain_record(underlying, strike=50)], underlying=underlying)
                self.assertEqual((s.underlying, len(s.contracts)), (underlying, 1))


class DuplicateTests(unittest.TestCase):
    def test_identical_duplicates_collapse(self):
        s = live_snapshot([chain_record(), chain_record(), chain_record()])
        self.assertEqual((len(s.contracts), exclusions(s), s.provenance.records_received),
                         (1, {"duplicate_identical": 2}, 3))

    def test_conflicting_duplicates_excluded(self):
        other = chain_record()
        other["open_interest"] = dict(value=16000)
        s = live_snapshot([chain_record(), other, chain_record(strike=710)])
        self.assertEqual(([c.identity.contract_id for c in s.contracts], exclusions(s)),
                         (["META261016C00710000"], {"identity_conflict": 2}))


class FactStatusTests(unittest.TestCase):
    def test_zero_missing_unavailable_are_distinct(self):
        zero = chain_record(day=False, oi=False, iv=False, greeks=False)
        zero["day"] = dict(volume=0)
        s = live_snapshot([zero])
        c = s.contracts[0]
        self.assertEqual((c.day.status, c.day.volume), ("present", "0"))
        self.assertEqual((c.open_interest.status, c.implied_volatility.status, c.greeks.status),
                         ("missing", "missing", "missing"))
        self.assertEqual((c.quote.status, c.trade.status), ("unavailable", "unavailable"))
        self.assertEqual([(x.group, x.status) for x in s.provenance.source_capabilities],
                         [("quote", "unavailable"), ("trade", "unavailable"), ("day", "available"),
                          ("open_interest", "available"), ("implied_volatility", "available"),
                          ("greeks", "available")])
        empty = chain_record(implied_volatility=dict(value=None))
        self.assertEqual(live_snapshot([empty]).contracts[0].implied_volatility.status, "missing")
        with self.assertRaises(SnapshotAssemblyError):  # Data supplied for a group declared unavailable.
            live_snapshot([chain_record(quote=dict(bid="1"))])

    def test_quotes_complete_incomplete_crossed_locked_retained(self):
        records = [chain_record(strike=s, quote=q) for s, q in (
            (690, dict(bid="10.1", ask="10.4", bid_size=5, ask_size=7, observed_at=BEFORE)),
            (700, dict(bid="10.1", observed_at=BEFORE)),
            (710, dict(bid="10.5", ask="10.2", observed_at=BEFORE)),     # crossed
            (720, dict(bid="10.3", ask="10.3", observed_at=BEFORE)),     # locked
            (730, dict(bid="0", ask="0.05")))]                           # zero bid, untimed
        s = full_snapshot(records)
        quotes = {c.identity.strike: c.quote for c in s.contracts}
        self.assertEqual((quotes["700"].bid, quotes["700"].ask, quotes["700"].status), ("10.1", None, "present"))
        self.assertEqual((quotes["710"].bid, quotes["710"].ask), ("10.5", "10.2"))
        self.assertEqual((quotes["720"].bid, quotes["720"].ask), ("10.3", "10.3"))
        self.assertEqual((quotes["730"].bid, quotes["730"].time_basis), ("0", "provider_snapshot_unverified"))
        self.assertEqual((quotes["690"].observed_at, quotes["690"].time_basis),
                         (BEFORE.isoformat(), "observed_at"))
        for q in quotes.values():
            self.assertFalse({"mid", "spread", "quote_state"} & set(q.to_dict()))
        self.assertEqual(full_snapshot([chain_record(trade=dict(price="10.3"))]).contracts[0].quote.status, "missing")

    def test_negative_values_are_malformed(self):
        for group, values in (("quote", dict(bid="-0.1")), ("quote", dict(ask_size=-1)), ("trade", dict(size=-1)),
                              ("day", dict(volume=-5)), ("open_interest", dict(value=-1)),
                              ("implied_volatility", dict(value="-0.2")), ("day", dict(volume="1.5")),
                              ("open_interest", dict(value="3.2"))):
            with self.subTest(group=group, values=values):
                s = full_snapshot([chain_record(**{group: values})])
                self.assertEqual(exclusions(s), {"malformed_record": 1})
        self.assertEqual(full_snapshot([chain_record(greeks=dict(delta="1.7", theta="-5"))]).contracts[0].greeks.delta,
                         "1.7")  # Surprising Greeks are source facts; bounds belong to OptionsIntelligence.


class CutoffTests(unittest.TestCase):
    def test_group_after_as_of_is_excluded_identity_kept(self):
        for group, values in (("quote", dict(bid="1", ask="1.1", observed_at=AFTER)),
                              ("trade", dict(price="1.05", size=2, observed_at=AFTER)),
                              ("day", dict(volume=3, observed_at=AFTER)),
                              ("implied_volatility", dict(value="0.3", observed_at=AFTER)),
                              ("greeks", dict(delta="0.5", observed_at=AFTER)),
                              ("open_interest", dict(value=10, as_of_date="2026-10-01"))):
            with self.subTest(group=group):
                s = full_snapshot([chain_record(**{group: values})])
                c = s.contracts[0]
                self.assertEqual(getattr(c, group).status, "excluded_after_as_of")
                self.assertEqual(getattr(c, group).time_basis, "unavailable")
                self.assertEqual(c.identity.contract_id, "META261016C00700000")
                self.assertEqual(exclusions(s), {"fact_after_as_of": 1})

    def test_exactly_at_as_of_is_included(self):
        c = full_snapshot([chain_record(day=False, trade=dict(price="1", observed_at=AS_OF))]).contracts[0]
        self.assertEqual((c.trade.status, c.trade.observed_at), ("present", AS_OF.isoformat()))

    def test_naive_time_is_malformed(self):
        s = full_snapshot([chain_record(trade=dict(price="1", observed_at="2026-09-30T10:00:00"))])
        self.assertEqual(exclusions(s), {"malformed_record": 1})


class TimeBasisTests(unittest.TestCase):
    def test_live_like_untimed_groups(self):
        c = live_snapshot([chain_record()]).contracts[0]
        self.assertEqual((c.open_interest.time_basis, c.open_interest.as_of_date), ("provider_snapshot_unverified", None))
        self.assertEqual((c.implied_volatility.time_basis, c.implied_volatility.source),
                         ("provider_snapshot_unverified", "provider"))
        self.assertEqual((c.greeks.time_basis, c.greeks.source, c.greeks.rho), ("provider_snapshot_unverified",
                                                                                "provider", None))
        self.assertEqual(c.day.time_basis, "provider_snapshot_unverified")

    def test_open_interest_with_a_source_date(self):
        c = full_snapshot([chain_record(open_interest=dict(value=100, as_of_date="2026-09-29"))]).contracts[0]
        self.assertEqual((c.open_interest.time_basis, c.open_interest.as_of_date),
                         ("provider_as_of_date", "2026-09-29"))

    def test_timed_iv_and_greeks(self):
        c = full_snapshot([chain_record(implied_volatility=dict(value="0.3", observed_at=BEFORE),
                                        greeks=dict(delta="0.4", rho="0.02", observed_at=BEFORE))]).contracts[0]
        self.assertEqual((c.implied_volatility.time_basis, c.greeks.time_basis, c.greeks.rho),
                         ("observed_at", "observed_at", "0.02"))


class LiveShapeTests(unittest.TestCase):
    def test_meta_like_all_observed_fields(self):
        s = live_snapshot(meta_chain())
        for c in s.contracts:
            self.assertEqual({g: getattr(c, g).status for g in m.FACT_GROUPS},
                             dict(quote="unavailable", trade="unavailable", day="present", open_interest="present",
                                  implied_volatility="present", greeks="present"))
            self.assertEqual((c.terms.exercise_style, c.terms.shares_per_contract), ("american", "100"))
        self.assertEqual(s.exclusions, ())

    def test_nvda_like_partial_iv_greeks(self):
        s = live_snapshot(nvda_chain(), underlying="NVDA")
        statuses = {c.identity.strike: (c.implied_volatility.status, c.greeks.status) for c in s.contracts}
        self.assertEqual(statuses["190"], ("present", "present"))
        self.assertEqual(statuses["187.5"], ("missing", "missing"))
        self.assertEqual(len(s.contracts), 8)


class UnderlyingPriceTests(unittest.TestCase):
    def test_unavailable_by_default_and_with_reason(self):
        self.assertEqual(live_snapshot([]).underlying_price.to_dict(),
                         dict(status="unavailable", value=None, observed_at=None, source=None, reason="not_supplied"))
        s = live_snapshot([], underlying_price=dict(reason="not_in_options_chain"))
        self.assertEqual(s.underlying_price.reason, "not_in_options_chain")

    def test_present_and_after_as_of(self):
        p = live_snapshot([], underlying_price=dict(value="705.1", observed_at=BEFORE, source="massive_stocks"))
        self.assertEqual((p.underlying_price.status, p.underlying_price.value), ("present", "705.1"))
        late = live_snapshot([], underlying_price=dict(value="705.1", observed_at=AFTER, source="massive_stocks"))
        self.assertEqual((late.underlying_price.status, late.underlying_price.reason),
                         ("unavailable", "observed_after_as_of"))
        for bad in (dict(value="0", observed_at=BEFORE, source="x"), dict(value="1", source="x"),
                    dict(value="1", observed_at=BEFORE), dict(reason="")):
            with self.subTest(bad=bad), self.assertRaises(SnapshotAssemblyError):
                live_snapshot([], underlying_price=bad)


class ScopeAndProvenanceTests(unittest.TestCase):
    def test_scope_canonicalized(self):
        s = live_snapshot([], scope=dict(contract_types=["put", "call", "put"], expiration_from="2026-10-01",
                                         expiration_through="2026-12-31", provider_page_limit=250))
        self.assertEqual(s.scope.to_dict(), dict(contract_types=["call", "put"], expiration_from="2026-10-01",
                                                 expiration_through="2026-12-31", provider_page_limit=250,
                                                 provider_result_limit=None))

    def test_provenance_has_no_secret_fields_and_counts(self):
        s = live_snapshot(meta_chain() + ["junk"])
        p = s.provenance.to_dict()
        self.assertEqual(p["records_received"], 13)
        self.assertFalse({"api_key", "authorization", "url", "headers"} & set(p))
        with self.assertRaises(SnapshotAssemblyError):
            live_snapshot([], provenance=dict(PROVENANCE, api_key="x"))

    def test_caller_errors(self):
        for kwargs in (dict(underlying="meta"), dict(as_of=AS_OF.replace(tzinfo=None)),
                       dict(calendar_state="lunch"), dict(unavailable_groups=("sentiment",))):
            with self.subTest(kwargs=kwargs), self.assertRaises(SnapshotAssemblyError):
                assemble(kwargs.get("underlying", "META"), kwargs.get("as_of", AS_OF), [], provenance=PROVENANCE,
                         calendar_state=kwargs.get("calendar_state"),
                         unavailable_groups=kwargs.get("unavailable_groups", ()))


class OrderIndependenceTests(unittest.TestCase):
    def test_every_permutation_same_bytes(self):
        records = meta_chain()[:5]
        expected = canonical_json(live_snapshot(records).to_dict())
        for order in itertools.permutations(records):
            self.assertEqual(canonical_json(live_snapshot(list(order)).to_dict()), expected)

    def test_provider_order_with_exclusions_and_duplicates(self):
        records = meta_chain() + [chain_record(), "junk", chain_record(expiration="2026-09-01")]
        a = live_snapshot(records)
        b = live_snapshot(list(reversed(records)))
        self.assertEqual(a.snapshot_id, b.snapshot_id)

    def test_dict_key_order(self):
        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        records = meta_chain()
        self.assertEqual(live_snapshot(reversed_keys(deepcopy(records))).snapshot_id, live_snapshot(records).snapshot_id)

    def test_canonical_contract_order(self):
        ids = [c.identity.contract_id for c in live_snapshot(meta_chain()).contracts]
        self.assertEqual(ids[:3], ["META261016C00690000", "META261016C00700000", "META261016C00710000"])
        self.assertEqual(ids[3], "META261016P00690000")
        self.assertTrue(ids[-1].startswith("META261023P"))
        numeric = live_snapshot([chain_record("NVDA", strike=s) for s in (1000, 95, 187.5)], underlying="NVDA")
        self.assertEqual([c.identity.strike for c in numeric.contracts], ["95", "187.5", "1000"])


if __name__ == "__main__":
    unittest.main()
