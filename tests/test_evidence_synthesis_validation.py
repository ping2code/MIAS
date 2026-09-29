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


class AssemblerInvariantTests(unittest.TestCase):
    """Phase 7H findings: resealed packets that the Phase 7C assembler can never produce are rejected, not repaired."""

    def assertRejected(self, fn, fragment):
        data = load()
        fn(data)
        data = resealed(data)
        before = deepcopy(data)
        with self.assertRaises(PacketValidationError) as caught:
            synthesize(data)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(data, before)  # Never repaired or reordered.

    def test_p1_duplicate_exclusion_reasons(self):
        for order in ((1, 2), (2, 1)):
            with self.subTest(order=order):
                self.assertRejected(lambda d: d["news"].update(excluded=[
                    {"reason": "symbol_mismatch", "count": n} for n in order]), "duplicate news exclusion reason")

    def test_p2_reserved_benchmark_name(self):
        def rename(name):
            def fn(d):
                for comparison in d["market_context"]["context"]["comparisons"]:
                    comparison["benchmark"] = name
            return fn
        for name in ("self", "qqq", "", "TOOLONGTICKER"):
            with self.subTest(name=name):
                self.assertRejected(rename(name), "comparison identity is malformed")

    def test_p3_technical_bar_end_after_as_of(self):
        self.assertRejected(lambda d: d["technical"]["timeframes"][2].update(bar_end="2026-09-23T20:05:01+00:00"),
                            "technical.5m.bar_end is later than the packet as_of")

    def test_p4_news_after_as_of(self):
        item = lambda d, i: d["news"]["items"][i]  # 0: timestamped news, 1: date-only SEC.
        cases_ = [
            (lambda d: item(d, 0)["facts"].update(published_at="2026-09-23T20:05:01+00:00"), "published_at is later"),
            (lambda d: item(d, 0).update(observed_at="2026-09-23T20:05:01+00:00"), "observed_at is later"),
            (lambda d: item(d, 1)["facts"].update(publication_date="2026-09-24"), "publication_date is not before"),
            # Phase 7C after_as_of rule: a date-only publication on as_of's New York date is not strictly before it.
            (lambda d: item(d, 1)["facts"].update(publication_date="2026-09-23"), "publication_date is not before"),
        ]
        for fn, fragment in cases_:
            with self.subTest(fragment=fragment):
                self.assertRejected(fn, fragment)

    def test_p7_exclusion_counts(self):
        for count, fragment in ((0, "at least 1"), (-3, "at least 1"), (1.0, "malformed"), ("2", "malformed"),
                                (True, "malformed"), (None, "malformed")):
            with self.subTest(count=count):
                self.assertRejected(lambda d: d["news"].update(excluded=[{"reason": "x", "count": count}]), fragment)

    def test_boundaries_still_valid(self):
        """Evidence exactly at as_of, and a date-only publication the day before, are valid (Phase 7C boundaries)."""
        data = load()
        data["technical"]["timeframes"][2]["bar_end"] = data["as_of"]
        data["news"]["items"][0]["facts"]["published_at"] = data["as_of"]
        data["news"]["items"][0]["observed_at"] = data["as_of"]
        data["news"]["excluded"] = [{"reason": "a", "count": 1}, {"reason": "b", "count": 7}]
        synthesize(resealed(data))


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
