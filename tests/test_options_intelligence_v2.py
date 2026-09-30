"""phase9-v2 OptionsIntelligence: the bounded Phase 10 compatibility amendment (additive; phase9-v1 unchanged)."""
import json
import unittest

from options_intelligence import rules
from options_intelligence.builder import build
from options_intelligence.canonical import canonical_json, content_id
from options_intelligence.validation import (OptionsIntelligenceError, validated_options_intelligence,
                                             verify_against_snapshot)
from tests import options_intelligence_cases as cases
from tests.options_snapshot_cases import chain_record, full_snapshot

CAL = cases.CALENDAR
V2_FIELDS = {"current_session_volume", "open_interest_value", "open_interest_time_basis", "shares_per_contract"}


def v2(name="quoted_with_price"):
    return build(cases.snapshot(name), calendar=CAL, format="phase9-v2")


def by_id(intelligence):
    return {c.contract_id: c for c in intelligence.contracts}


class VersionTests(unittest.TestCase):
    def test_v1_default_and_bytes_unchanged(self):
        for name in cases.names():
            with self.subTest(case=name):
                v1 = build(cases.snapshot(name), calendar=CAL)
                self.assertEqual(v1.options_intelligence_format_version, "phase9-v1")
                self.assertEqual(canonical_json(v1.to_dict()) + "\n",
                                 cases.intelligence_path(name).read_text(encoding="utf-8"))
                self.assertEqual(build(cases.snapshot(name), calendar=CAL, format="phase9-v1"), v1)

    def test_v2_versions_goldens_and_ids(self):
        for name in cases.names():
            with self.subTest(case=name):
                intelligence = v2(name)
                data = intelligence.to_dict()
                self.assertEqual((data["options_intelligence_format_version"], data["rules_version"],
                                  data["provenance"]["rules_version"]), ("phase9-v2", "phase9-rules-v2",
                                                                         "phase9-rules-v2"))
                self.assertEqual(len(data), 13)
                self.assertEqual(canonical_json(data) + "\n",
                                 cases.intelligence_v2_path(name).read_text(encoding="utf-8"))
                self.assertEqual(intelligence.options_intelligence_id, content_id(intelligence.body()))
                self.assertNotEqual(intelligence.options_intelligence_id,
                                    build(cases.snapshot(name), calendar=CAL).options_intelligence_id)
        with self.assertRaises(OptionsIntelligenceError):
            build(cases.snapshot("empty_chain"), calendar=CAL, format="phase9-v3")

    def test_exact_contract_additions(self):
        v1_keys = set(build(cases.snapshot("quoted_with_price"), calendar=CAL).to_dict()["contracts"][0])
        for contract in v2().to_dict()["contracts"]:
            self.assertEqual(set(contract) - v1_keys, V2_FIELDS)
            self.assertEqual(v1_keys - set(contract), set())


class DerivationTests(unittest.TestCase):
    def test_current_session_volume_only(self):
        c = by_id(v2())
        self.assertEqual(c["META261016C00690000"].current_session_volume, "500")    # current session
        self.assertEqual(c["META261016P00700000"].current_session_volume, "800")    # current session
        self.assertEqual(c["META261016C00720000"].current_session_volume, "0")      # current, present zero
        self.assertIsNone(c["META261016C00700000"].current_session_volume)          # previous session
        self.assertIsNone(c["META261016C00710000"].current_session_volume)          # older session
        self.assertIsNone(c["META261016C00730000"].current_session_volume)          # no day record
        self.assertIsNone(c["META261016C00740000"].current_session_volume)          # untimed day (unavailable)
        self.assertEqual(c["META261016C00700000"].day.session_relation, "previous_session")

    def test_open_interest_and_multiplier(self):
        c = by_id(v2())
        self.assertEqual((c["META261016C00690000"].open_interest_value, c["META261016C00690000"].open_interest_time_basis,
                          c["META261016C00690000"].shares_per_contract), ("400", "provider_snapshot_unverified", "100"))
        self.assertEqual(c["META261016C00730000"].open_interest_value, "0")
        no_terms = chain_record(terms=dict(exercise_style="american"))
        no_oi = chain_record(strike=710, oi=False)
        contracts = by_id(build(full_snapshot([no_terms, no_oi]), calendar=CAL, format="phase9-v2"))
        self.assertIsNone(contracts["META261016C00700000"].shares_per_contract)     # never assumed to be 100
        self.assertEqual((contracts["META261016C00710000"].open_interest_value,
                          contracts["META261016C00710000"].open_interest_time_basis), (None, "unavailable"))

    def test_pointers_cover_new_fields(self):
        c = by_id(v2())["META261016C00690000"]
        self.assertIn("contracts[META261016C00690000].terms.shares_per_contract", c.source_pointers)
        self.assertIn("contracts[META261016C00690000].open_interest.time_basis", c.source_pointers)


class ValidationTests(unittest.TestCase):
    def test_structural_and_rederivation(self):
        snap = cases.snapshot("quoted_with_price")
        data = v2().to_dict()
        self.assertEqual(verify_against_snapshot(data, snap, calendar=CAL), data)
        self.assertEqual(json.loads(canonical_json(data)), data)

    def test_mixed_formats_rejected(self):
        def resealed(d):
            d["options_intelligence_id"] = content_id({k: v for k, v in d.items() if k != "options_intelligence_id"})
            return d
        v1 = build(cases.snapshot("quoted_with_price"), calendar=CAL).to_dict()
        v1_claiming_v2 = dict(v1, options_intelligence_format_version="phase9-v2", rules_version="phase9-rules-v2",
                              provenance=dict(v1["provenance"], rules_version="phase9-rules-v2"))
        data = v2().to_dict()
        for c in data["contracts"]:
            c.pop("shares_per_contract")
        for bad, message in ((resealed(v1_claiming_v2),
                              "contract fields do not match the options intelligence format version"),
                             (resealed(data), "contract fields do not match the options intelligence format version")):
            with self.assertRaises(OptionsIntelligenceError) as caught:
                validated_options_intelligence(bad)
            self.assertEqual(str(caught.exception), message)
        wrong = v2().to_dict()
        wrong["contracts"][1]["current_session_volume"] = "501"
        with self.assertRaises(OptionsIntelligenceError):
            verify_against_snapshot(resealed(wrong), cases.snapshot("quoted_with_price"), calendar=CAL)

    def test_deterministic(self):
        self.assertEqual({v2().options_intelligence_id for _ in range(20)}, {v2().options_intelligence_id})
        self.assertEqual(rules.FORMATS, {"phase9-v1": "phase9-rules-v1", "phase9-v2": "phase9-rules-v2"})


if __name__ == "__main__":
    unittest.main()
