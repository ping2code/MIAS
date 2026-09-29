"""Phase 7G validation: fail-closed for invalid packets, valid-but-incomplete packets accepted, nothing repaired."""
from copy import deepcopy
import json
import unittest

from evidence_packet.serialization import content_id
from evidence_synthesis.builder import synthesize
from evidence_synthesis.validation import PacketValidationError, validated_packet
from tests import evidence_synthesis_cases as cases


def load(name="meta_real_shaped"):
    return json.loads(cases.packet_path(name).read_text(encoding="utf-8"))


def resealed(data):
    """Recompute packet_id so a test exercises structural checks rather than the hash check."""
    data = deepcopy(data)
    data["packet_id"] = content_id({k: v for k, v in data.items() if k != "packet_id"})
    return data


class InvalidTests(unittest.TestCase):
    def assertInvalid(self, data, fragment=""):
        with self.assertRaises(PacketValidationError) as caught:
            synthesize(data)
        self.assertIn(fragment, str(caught.exception))

    def test_tampered_body_with_old_packet_id(self):
        data = load()
        data["market_context"]["context"]["symbol_context"]["return_since_prev_close"] = "0.5"
        self.assertInvalid(data, "tampered")
        data = load()
        data["technical"]["timeframes"][0]["row"]["technical_state"] = "bearish_setup"
        self.assertInvalid(data, "tampered")
        data = load()
        data["packet_id"] = "sha256:" + "0" * 64
        self.assertInvalid(data, "tampered")

    def test_unsupported_version_and_keys(self):
        self.assertInvalid(resealed(dict(load(), format_version="phase7c-v2")), "format version")
        bad = load()
        del bad["provenance"]
        self.assertInvalid(bad, "8 phase7c-v1 top-level keys")
        self.assertInvalid(dict(load(), options={}), "8 phase7c-v1 top-level keys")
        self.assertInvalid(dict(load(), packet_id="md5:abc"), "packet_id is malformed")
        for wrong in (None, [], "packet", 42):
            self.assertInvalid(wrong, "EvidencePacket")

    def test_structural_problems_even_when_resealed(self):
        def mutate(fn):
            data = load()
            fn(data)
            return resealed(data)
        cases_ = [
            (lambda d: d.update(symbol="meta"), "symbol"),
            (lambda d: d.update(as_of="2026-09-23T20:05:00"), "timezone"),
            (lambda d: d["technical"]["timeframes"].reverse(), "1d, 1h, 5m"),
            (lambda d: d["technical"]["timeframes"][1]["row"].update(technical_state="buy"), "unknown state"),
            (lambda d: d["technical"]["timeframes"][1]["row"].update(confidence="VERY_HIGH"), "confidence"),
            (lambda d: d["technical"]["timeframes"][2]["row"].update(symbol="NVDA"), "mismatch"),
            (lambda d: d["technical"]["availability"].update(status="partial"), "inconsistent"),
            (lambda d: d["market_context"]["availability"].update(status="unavailable"), "must not carry"),
            (lambda d: d["market_context"]["context"].update(context_format_version="phase7b-v9"), "format version"),
            (lambda d: d["market_context"]["context"]["symbol_context"].update(return_since_open=1.5), "decimal"),
            (lambda d: d["market_context"]["context"].update(as_of="2026-09-24T00:00:00+00:00"), "later"),
            (lambda d: d["news"]["availability"].update(status="available_empty"), "inconsistent"),
            (lambda d: d["news"]["items"][0]["facts"].update(family="macro"), "family"),
            (lambda d: d["news"]["items"].append(deepcopy(d["news"]["items"][0])), "duplicate"),
            (lambda d: d["news"].update(extra=[]), "unexpected keys"),
        ]
        for fn, fragment in cases_:
            with self.subTest(fragment=fragment):
                self.assertInvalid(mutate(fn), fragment)

    def test_nan_is_rejected(self):
        data = load()
        data["technical"]["timeframes"][0]["row"]["rsi14"] = float("nan")
        self.assertInvalid(data, "canonical")


class IncompleteButValidTests(unittest.TestCase):
    def test_incomplete_domains_are_valid(self):
        for name in ("unavailable_timeframe", "market_context_unavailable", "empty_news"):
            with self.subTest(case=name):
                self.assertIs(validated_packet(load(name)).__class__, dict)
                synthesize(load(name))

    def test_all_technical_missing_and_news_unavailable(self):
        from tests.test_evidence_packet import packet
        s = synthesize(packet(technical_rows=None, news=None))
        self.assertEqual((s.completeness.technical, s.timeframe_alignment.pattern, s.news.availability),
                         ("unavailable", "incomplete", "unavailable"))

    def test_validation_never_mutates(self):
        data = load()
        before = deepcopy(data)
        validated_packet(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
