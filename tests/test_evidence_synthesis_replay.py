"""Phase 7H replay validation of the Phase 7G EvidenceSynthesis (validation only; no new semantics).

Covered:

- A: the static contract, pinned directly from code;
- B: the replay corpus (the 21 required scenarios), byte-exact;
- C: all 729 technical-state combinations through the complete builder;
- D and I: repeated, typed/JSON/round-trip and cross-process determinism;
- E: order-independence;
- K: hash and canonicalization stability;
- L: contradiction validation;
- O: the read-only replay tool.

The 729-combination counts are validation counts only, not market statistics.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import io
from itertools import product
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evidence_packet.serialization import canonical_json, content_id
from evidence_synthesis import replay, rules
from evidence_synthesis.builder import synthesize
from persistence.technical_snapshot_repository import content_hash
from tests import evidence_synthesis_cases as golden
from tests import evidence_synthesis_corpus as corpus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENTITY_FIELDS = ("synthesis_id", "packet_ref", "provenance")
RELATIONS = (rules.AGREE, rules.OPPOSE, rules.NON_DIRECTIONAL, rules.UNAVAILABLE)
DIRECTIONS = (rules.BULLISH, rules.BEARISH, rules.NON_DIRECTIONAL, rules.UNAVAILABLE)


def load(name):
    return json.loads(corpus.packet_path(name).read_text(encoding="utf-8"))


def expected_text(name):
    return corpus.synthesis_path(name).read_text(encoding="utf-8")


def text(synthesis):
    return canonical_json(synthesis.to_dict()) + "\n"


def resealed(data):
    data["packet_id"] = content_id({k: v for k, v in data.items() if k != "packet_id"})
    return data


def content(synthesis):
    """Synthesis content without the packet/synthesis identity fields."""
    data = synthesis.to_dict()
    for key in IDENTITY_FIELDS:
        data.pop(key)
    return data


def with_states(base, combo):
    data = deepcopy(base)
    for frame, state in zip(data["technical"]["timeframes"], combo):
        frame["row"]["technical_state"] = state
        frame["row"]["content_hash"] = content_hash(frame["row"])
    return resealed(data)


def expected_direction(state):
    """Independent restatement of the frozen direction table (no rules.py call)."""
    if state == "insufficient_data":
        return "unavailable"
    if state in ("bullish_setup", "bullish_momentum", "breakout_watch"):
        return "bullish"
    if state in ("bearish_setup", "bearish_momentum", "breakdown_watch"):
        return "bearish"
    return "non_directional"


# --------------------------------------------------------------------------- A: static contract

class StaticContractTests(unittest.TestCase):
    def test_contract_pinned_from_code(self):
        from evidence_synthesis import model as m
        from dataclasses import fields
        self.assertEqual((rules.SYNTHESIS_FORMAT_VERSION, rules.RULES_VERSION, rules.PACKET_FORMAT_VERSION,
                          rules.MARKET_CONTEXT_FORMAT_VERSION), ("phase7g-v1", "phase7g-rules-v1", "phase7c-v1",
                                                                  "phase7b-v1"))
        self.assertEqual([f.name for f in fields(m.EvidenceSynthesis)], [
            "synthesis_format_version", "synthesis_id", "rules_version", "packet_ref", "completeness", "timeframes",
            "timeframe_relations", "timeframe_alignment", "market_context", "news", "contradictions", "provenance"])
        self.assertEqual([f.name for f in fields(m.PacketRef)],
                         ["packet_id", "packet_format_version", "symbol", "as_of"])
        self.assertEqual(rules.INTERVALS, ("1d", "1h", "5m"))
        self.assertEqual(rules.PAIRS, (("1d", "1h"), ("1h", "5m"), ("1d", "5m")))
        self.assertEqual(rules.BASES, ("prev_close", "open"))
        self.assertEqual(rules.PATTERNS, ("all_bullish", "all_bearish", "all_non_directional", "opposed",
                                          "partially_directional", "incomplete"))
        self.assertEqual(rules.CONTRADICTION_CODES, (
            "timeframe_opposition", "market_context_opposes_timeframe", "timeframe_unavailable",
            "market_context_unavailable", "news_unavailable", "context_session_mismatch", "comparison_misaligned"))
        self.assertNotIn("technical_state_mixed", rules.CONTRADICTION_CODES)

    def test_canonical_hash_algorithm(self):
        s = synthesize(load("meta_real_shaped"))
        body = canonical_json(s.body()).encode("utf-8")
        self.assertEqual(s.synthesis_id, "sha256:" + hashlib.sha256(body).hexdigest())


# --------------------------------------------------------------------------- B: corpus

class CorpusTests(unittest.TestCase):
    def test_required_scenarios_are_covered(self):
        self.assertEqual(len(corpus.REQUIRED), 21)
        self.assertLessEqual(set(corpus.REQUIRED.values()), set(corpus.names()))
        self.assertEqual(len(corpus.names()), 24)

    def test_corpus_byte_exact_and_builders_unchanged(self):
        for name in corpus.names():
            with self.subTest(case=name):
                self.assertEqual(corpus.builder(name)().to_dict(), load(name))
                self.assertEqual(text(synthesize(load(name))), expected_text(name))

    def test_explicit_scenario_semantics(self):
        """Hand-derived expectations for each required scenario (pattern, directions, contradiction codes)."""
        bull3 = ["bullish"] * 3
        expect = {
            "unavailable_news": ("partially_directional", None, {"market_context_opposes_timeframe",
                                                                  "news_unavailable"}),
            "benchmark_comparison_unavailable": ("partially_directional", None, {"market_context_opposes_timeframe"}),
            "benchmark_comparison_misaligned": ("partially_directional", None, {"comparison_misaligned"}),
            "context_opposes_one_timeframe": ("partially_directional",
                                              ["bullish", "non_directional", "non_directional"],
                                              {"market_context_opposes_timeframe"}),
            "context_session_mismatch": ("partially_directional", None, {"context_session_mismatch"}),
            "insufficient_data_state": ("incomplete", ["bullish", "unavailable", "bullish"],
                                        {"market_context_opposes_timeframe", "timeframe_unavailable"}),
            "mixed_state": ("all_non_directional", ["non_directional"] * 3, set()),
            "range_state": ("all_non_directional", ["non_directional"] * 3, set()),
            "zero_market_return": ("all_bullish", bull3, set()),
            "positive_market_return": ("all_bullish", bull3, set()),
            "negative_market_return": ("all_bullish", bull3, {"market_context_opposes_timeframe"}),
        }
        for scenario, (pattern, directions, codes) in expect.items():
            with self.subTest(scenario=scenario):
                s = synthesize(load(corpus.REQUIRED[scenario]))
                self.assertEqual(s.timeframe_alignment.pattern, pattern)
                if directions:
                    self.assertEqual([t.state_direction for t in s.timeframes], directions)
                self.assertEqual({c.code for c in s.contradictions}, codes)

    def test_market_return_signs(self):
        signs = lambda name: {r.value_sign for r in synthesize(load(name)).market_context.references}
        self.assertEqual(signs("zero_market_return"), {"zero"})
        self.assertEqual(signs("positive_market_return"), {"positive"})
        self.assertEqual(signs("all_bullish"), {"negative"})
        zero = synthesize(load("zero_market_return"))
        self.assertEqual({r.relation for r in zero.market_context.relations}, {"non_directional"})

    def test_benchmark_unavailable_and_opposition_scope(self):
        s = synthesize(load("benchmark_unavailable"))
        spy = [r for r in s.market_context.references if r.reference == "SPY"]
        self.assertEqual([(r.value_sign, r.unavailable_reasons) for r in spy],
                         [("unavailable", ("session_mismatch",))] * 2)
        self.assertEqual({r.relation for r in s.market_context.relations if r.reference == "SPY"}, {"unavailable"})
        one = {c.subjects[0] for c in synthesize(load("context_opposes_one_timeframe")).contradictions}
        many = {c.subjects[0] for c in synthesize(load("all_bullish")).contradictions}
        self.assertEqual((one, many), ({"1d"}, {"1d", "1h", "5m"}))


# --------------------------------------------------------------------------- C: 729 combinations

class ExhaustiveReplayTests(unittest.TestCase):
    # sha256 over the 729 synthesis_ids (one per line, product order). A determinism anchor, not a statistic.
    DIGEST = "e700a81722b38fd14f241246505dd9e7b9a44314099d47fa46656d16059830ed"

    @classmethod
    def setUpClass(cls):
        base = load("all_bullish")  # Market context: every reference negative (4 references).
        cls.results = []
        for combo in product(rules.TECHNICAL_STATES, repeat=3):
            s = synthesize(with_states(base, combo))
            cls.results.append((combo, s, canonical_json(s.to_dict())))

    def test_every_combination_synthesizes_with_valid_enums(self):
        self.assertEqual(len(self.results), 729)
        for combo, s, _ in self.results:
            self.assertIn(s.timeframe_alignment.pattern, rules.PATTERNS)
            self.assertTrue({r.relation for r in s.timeframe_relations} <= set(RELATIONS))
            self.assertTrue({r.relation for r in s.market_context.relations} <= set(RELATIONS))
            self.assertEqual([t.state_direction for t in s.timeframes], [expected_direction(x) for x in combo])

    def test_contradictions_follow_the_rules_exactly(self):
        for combo, s, _ in self.results:
            dirs = dict(zip(rules.INTERVALS, (expected_direction(x) for x in combo)))
            expected = {("timeframe_unavailable", (i,)) for i, d in dirs.items() if d == "unavailable"}
            expected |= {("timeframe_opposition", pair) for pair in rules.PAIRS
                         if {dirs[pair[0]], dirs[pair[1]]} == {"bullish", "bearish"}}
            expected |= {("market_context_opposes_timeframe", (i, ref, basis)) for i, d in dirs.items()
                         if d == "bullish" for ref in ("self", "QQQ") for basis in rules.BASES}
            self.assertEqual({(c.code, c.subjects) for c in s.contradictions}, expected, combo)
            self.assertEqual(len(s.contradictions), len(expected))  # No duplicates.

    def test_mixed_and_range_never_create_contradictions(self):
        for combo, s, _ in self.results:
            quiet = {i for i, x in zip(rules.INTERVALS, combo) if x in ("mixed", "range")}
            for c in s.contradictions:
                self.assertFalse(quiet & set(c.subjects), (combo, c))

    def test_insufficient_data_is_incomplete(self):
        for combo, s, _ in self.results:
            if "insufficient_data" not in combo:
                continue
            self.assertEqual(s.timeframe_alignment.pattern, "incomplete")
            missing = {i for i, x in zip(rules.INTERVALS, combo) if x == "insufficient_data"}
            for r in s.timeframe_relations:
                if {r.first, r.second} & missing:
                    self.assertEqual(r.relation, "unavailable")
            self.assertEqual({r.relation for r in s.market_context.relations if r.interval in missing},
                             {"unavailable"})

    def test_summary_counts(self):
        """Validation counts only (hand-derived from 3 bullish, 3 bearish, 2 non-directional, 1 unavailable)."""
        patterns = Counter(s.timeframe_alignment.pattern for _, s, _ in self.results)
        self.assertEqual(patterns, {"incomplete": 217, "opposed": 270, "partially_directional": 180,
                                    "all_bullish": 27, "all_bearish": 27, "all_non_directional": 8})
        relations = Counter(r.relation for _, s, _ in self.results for r in s.timeframe_relations)
        self.assertEqual(relations, {"unavailable": 459, "non_directional": 756, "agree": 486, "oppose": 486})

    def test_digest_and_uniqueness(self):
        ids = [s.synthesis_id for _, s, _ in self.results]
        self.assertEqual(len(set(ids)), 729)
        self.assertEqual(hashlib.sha256("\n".join(ids).encode()).hexdigest(), self.DIGEST)
        for _, s, serialized in self.results:
            self.assertEqual(json.loads(serialized)["synthesis_id"], s.synthesis_id)
            self.assertEqual(s.synthesis_id, content_id(s.body()))


# --------------------------------------------------------------------------- D / I: determinism

class DeterminismTests(unittest.TestCase):
    def test_100_repetitions_per_fixture(self):
        for name in corpus.names():
            with self.subTest(case=name):
                raw = corpus.packet_path(name).read_text(encoding="utf-8")
                outputs = {text(synthesize(json.loads(raw))) for _ in range(100)}
                self.assertEqual(outputs, {expected_text(name)})

    def test_typed_dict_and_round_trip_agree(self):
        for name in corpus.names():
            with self.subTest(case=name):
                typed = corpus.builder(name)()
                forms = (typed, typed.to_dict(), json.loads(canonical_json(typed.to_dict())), load(name))
                self.assertEqual({text(synthesize(form)) for form in forms}, {expected_text(name)})

    def test_fresh_processes_and_environments(self):
        """Three fresh interpreters, differing hash seed, cwd, hostname and unrelated variables: identical bytes."""
        variants = ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "1", "HOSTNAME": "elsewhere", "TZ": "Asia/Tokyo"},
                    {"PYTHONHASHSEED": "4242", "LANG": "C", "MIAS_UNRELATED": "x", "TZ": "UTC"})
        for directory in corpus.CORPUS_DIRS:
            with self.subTest(corpus=directory.name):
                out = io.StringIO()
                self.assertEqual(replay.main(["--corpus", str(directory), "--repeat", "3"], out=out), 0)
                outputs = set()
                for extra in variants:
                    with tempfile.TemporaryDirectory() as cwd:
                        env = dict(os.environ, PYTHONPATH=ROOT, **extra)
                        result = subprocess.run([sys.executable, "-m", "evidence_synthesis.replay", "--corpus",
                                                 str(directory), "--repeat", "3"], cwd=cwd, env=env,
                                                capture_output=True, text=True, timeout=300)
                    self.assertEqual(result.returncode, 0, result.stderr[-300:])
                    outputs.add(result.stdout)
                self.assertEqual(outputs, {out.getvalue()})


# --------------------------------------------------------------------------- E: order-independence

def reversed_keys(value):
    if isinstance(value, dict):
        return {k: reversed_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(v) for v in value]
    return value


class OrderIndependenceTests(unittest.TestCase):
    def test_dict_key_order_keeps_identity(self):
        """Dict key order (including provenance and comparison objects) is not semantic: same synthesis_id."""
        for name in corpus.names():
            with self.subTest(case=name):
                data = load(name)
                self.assertEqual(text(synthesize(reversed_keys(data))), expected_text(name))

    def test_list_order_changes_only_identity(self):
        """List order is part of the packet's own identity (packet_id hashes it), so reordering a list gives a
        different packet. The synthesized content must still be identical: the builder canonicalizes it."""
        base = load("meta_real_shaped")
        base["news"]["excluded"] = [{"reason": "near_duplicate_suppressed", "count": 2},
                                    {"reason": "symbol_mismatch", "count": 10}]
        base = resealed(base)
        perturbations = {
            "news items": lambda d: d["news"]["items"].reverse(),
            "news exclusions": lambda d: d["news"]["excluded"].reverse(),
            "benchmark comparisons": lambda d: d["market_context"]["context"]["comparisons"].reverse(),
            "relevance symbols": lambda d: [i["relevance"]["symbols"].reverse() for i in d["news"]["items"]],
        }
        for label, fn in perturbations.items():
            with self.subTest(perturbation=label):
                data = deepcopy(base)
                fn(data)
                a, b = synthesize(base), synthesize(resealed(data))
                self.assertEqual(content(a), content(b))
                if data["packet_id"] != base["packet_id"]:
                    self.assertNotEqual(a.synthesis_id, b.synthesis_id)

    def test_availability_reason_order(self):
        base = load("missing_1d_5m")
        self.assertEqual(len(base["technical"]["availability"]["reasons"]), 2)
        data = deepcopy(base)
        data["technical"]["availability"]["reasons"].reverse()
        self.assertEqual(content(synthesize(base)), content(synthesize(resealed(data))))

    def test_timeframe_order_is_contractual(self):
        """The only intentionally semantic order: technical timeframes must be exactly 1d, 1h, 5m (else invalid)."""
        from evidence_synthesis.validation import PacketValidationError
        data = load("all_bullish")
        data["technical"]["timeframes"].reverse()
        with self.assertRaises(PacketValidationError):
            synthesize(resealed(data))


# --------------------------------------------------------------------------- K: hash / canonicalization

def walk(value, path=""):
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")


class HashAndCanonicalTests(unittest.TestCase):
    def test_canonical_json_properties(self):
        self.assertEqual(canonical_json({"b": [1, (2, 3)], "a": "é"}), '{"a":"\\u00e9","b":[1,[2,3]]}')
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})
        for name in corpus.names():
            raw = expected_text(name)
            raw.encode("utf-8")
            self.assertTrue(raw.isascii())
            self.assertEqual(canonical_json(json.loads(raw)) + "\n", raw)

    def test_no_floats_only_json_native_values(self):
        for name in corpus.names():
            for path, value in walk(synthesize(load(name)).to_dict()):
                self.assertNotIsInstance(value, float, (name, path))
                self.assertIsInstance(value, (dict, list, str, int, bool, type(None)), (name, path))

    def test_output_orderings(self):
        for name in corpus.names():
            s = synthesize(load(name))
            self.assertEqual([t.interval for t in s.timeframes], list(rules.INTERVALS))
            self.assertEqual([(r.first, r.second) for r in s.timeframe_relations], list(rules.PAIRS))
            keys = [(c.code, c.subjects) for c in s.contradictions]
            self.assertEqual(keys, sorted(keys))
            refs = [(r.reference != "self", r.reference, rules.BASES.index(r.basis))
                    for r in s.market_context.references]
            self.assertEqual(refs, sorted(refs))
            self.assertEqual(s.provenance.source_domains, ("market_context", "technical", "news"))
            self.assertEqual(list(s.news.sources), sorted(s.news.sources))

    def test_identity_changes(self):
        data = load("meta_real_shaped")
        base = synthesize(data)
        with patch.object(rules, "RULES_VERSION", "phase7g-rules-v2"):
            self.assertNotEqual(synthesize(data).synthesis_id, base.synthesis_id)
        with patch.object(rules, "SYNTHESIS_FORMAT_VERSION", "phase7g-v2"):
            self.assertNotEqual(synthesize(data).synthesis_id, base.synthesis_id)
        other = deepcopy(data)
        other["provenance"]["note"] = "different packet, same evidence"
        changed = synthesize(resealed(other))
        self.assertNotEqual(changed.synthesis_id, base.synthesis_id)
        self.assertEqual(content(changed), content(base))
        self.assertEqual(synthesize(reversed_keys(data)).synthesis_id, base.synthesis_id)


# --------------------------------------------------------------------------- L: contradictions

POINTER = re.compile(r"(?P<domain>technical|market_context|news)(?P<rest>.*)")


def resolve(packet, pointer):
    """True if a synthesis pointer names a location that exists in the packet."""
    if pointer in ("market_context.availability", "news.availability"):
        return True
    match = re.fullmatch(r"technical\.(1d|1h|5m)\.(bar_end|missing_reason|row\.\w+)", pointer)
    if match:
        frame = next(f for f in packet["technical"]["timeframes"] if f["interval"] == match[1])
        key = match[2]
        if key.startswith("row."):
            return frame["row"] is not None and key[4:] in frame["row"]
        return frame[key] is not None
    context = packet["market_context"]["context"]
    match = re.fullmatch(r"market_context\.context\.symbol_context\.(\w+)", pointer)
    if match:
        return context is not None and match[1] in context["symbol_context"]
    if pointer == "market_context.context.session_date":
        return context is not None and context.get("session_date") is not None
    match = re.fullmatch(r"market_context\.context\.comparisons\[([^:\]]+):(\w+)\]\.relative_return", pointer)
    if match:
        return context is not None and any(c["benchmark"] == match[1] and c["basis"] == match[2]
                                           for c in context["comparisons"])
    match = re.fullmatch(r"news\.items\[([^:\]]+):([^\]]+)\]", pointer)
    if match:
        return any(i["identity_version"] == match[1] and i["event_key"] == match[2]
                   for i in packet["news"]["items"])
    return False


class ContradictionTests(unittest.TestCase):
    EXPECTED = {
        "timeframe_opposition": ("higher_aligned_5m_opposed", ("1d", "5m"),
                                 ("technical.1d.row.technical_state", "technical.5m.row.technical_state")),
        "market_context_opposes_timeframe": ("context_opposes_one_timeframe", ("1d", "QQQ", "open"),
                                             ("technical.1d.row.technical_state",
                                              "market_context.context.comparisons[QQQ:open].relative_return")),
        "timeframe_unavailable": ("unavailable_timeframe", ("5m",), ("technical.5m.missing_reason",)),
        "market_context_unavailable": ("market_context_unavailable", ("market_context",),
                                       ("market_context.availability",)),
        "news_unavailable": ("news_unavailable", ("news",), ("news.availability",)),
        "context_session_mismatch": ("context_session_mismatch", ("market_context", "1d"),
                                     ("market_context.context.session_date", "technical.1d.bar_end")),
        "comparison_misaligned": ("benchmark_misaligned", ("SPY", "open"),
                                  ("market_context.context.comparisons[SPY:open].relative_return",)),
    }

    def test_each_code_independently(self):
        self.assertEqual(set(self.EXPECTED), set(rules.CONTRADICTION_CODES))
        for code, (name, subjects, pointers) in self.EXPECTED.items():
            with self.subTest(code=code):
                found = [c for c in synthesize(load(name)).contradictions if c.code == code]
                self.assertIn((subjects, pointers), [(c.subjects, c.pointers) for c in found])

    def test_insufficient_data_unavailable_pointer(self):
        s = synthesize(load("insufficient_data_state"))
        [c] = [c for c in s.contradictions if c.code == "timeframe_unavailable"]
        self.assertEqual((c.subjects, c.pointers), (("1h",), ("technical.1h.bar_end", "technical.1h.row.technical_state")))

    def test_sorted_unique_and_pointers_resolve(self):
        for name in corpus.names():
            with self.subTest(case=name):
                data = load(name)
                s = synthesize(data)
                keys = [(c.code, c.subjects) for c in s.contradictions]
                self.assertEqual(keys, sorted(set(keys)))
                for c in s.contradictions:
                    self.assertTrue(c.pointers)
                    for pointer in c.pointers:
                        self.assertTrue(resolve(data, pointer), (c.code, pointer))
                pointers = [p for t in s.timeframes for p in t.sources]
                pointers += [p for r in s.market_context.references for p in r.sources] + list(s.news.sources)
                for pointer in pointers:
                    self.assertTrue(resolve(data, pointer), pointer)
                for relation in s.timeframe_relations:
                    for interval, pointer in zip((relation.first, relation.second), relation.sources):
                        # A relation with a missing timeframe is "unavailable"; its pointer names the absent row.
                        self.assertTrue(resolve(data, pointer) or relation.relation == "unavailable", pointer)

    def test_disagreement_exposed_never_resolved(self):
        """Opposition is listed; the facts on both sides are kept as-is and nothing picks a side."""
        for name in ("higher_aligned_5m_opposed", "lower_aligned_1d_opposed", "all_bullish"):
            data = load(name)
            s = synthesize(data)
            self.assertTrue(any(c.code.endswith("opposition") or "opposes" in c.code for c in s.contradictions))
            for frame, facts in zip(data["technical"]["timeframes"], s.timeframes):
                self.assertEqual(facts.state, frame["row"]["technical_state"])
            self.assertEqual(len(s.to_dict()), 12)  # No verdict, winner or resolution field exists.


# --------------------------------------------------------------------------- O: replay tool

class ReplayToolTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        return replay.main(argv, out=out, err=err), out.getvalue(), err.getvalue()

    def test_single_packet_and_corpus(self):
        code, out, err = self.run_cli(["--packet", str(corpus.packet_path("all_bullish")), "--repeat", "5"])
        self.assertEqual((code, err), (0, ""))
        [result] = json.loads(out)["results"]
        self.assertEqual((result["repeat_count"], result["identical"], result["expected"]), (5, True, "match"))
        self.assertEqual(result["synthesis_id"], json.loads(expected_text("all_bullish"))["synthesis_id"])
        self.assertEqual(result["synthesis_sha256"], hashlib.sha256(expected_text("all_bullish").encode()).hexdigest())
        code, out, _ = self.run_cli(["--corpus", str(corpus.REPLAY_FIXTURES), "--repeat", "2"])
        document = json.loads(out)
        self.assertEqual((code, len(document["results"]), document["all_identical"], document["all_expected_match"]),
                         (0, 15, True, True))
        self.assertEqual(out, canonical_json(document) + "\n")
        self.assertNotIn(ROOT, out)  # No absolute paths in the report.

    def test_mismatch_absent_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = os.path.join(directory, "x.packet.json")
            with open(packet, "w", encoding="utf-8") as handle:
                handle.write(corpus.packet_path("all_bullish").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(self.run_cli(["--packet", packet, "--repeat", "1"])[1])["results"][0]
                             ["expected"], "absent")
            with open(os.path.join(directory, "x.synthesis.json"), "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            code, out, _ = self.run_cli(["--corpus", directory, "--repeat", "1"])
            self.assertEqual((code, json.loads(out)["all_expected_match"]), (1, False))
            bad = json.loads(corpus.packet_path("all_bullish").read_text(encoding="utf-8"))
            bad["symbol"] = "NVDA"
            with open(packet, "w", encoding="utf-8") as handle:
                json.dump(bad, handle)
            code, out, err = self.run_cli(["--packet", packet])
            self.assertEqual((code, out, json.loads(err)), (2, "", {"error": "invalid_packet", "file": "x.packet.json"}))
            with open(packet, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            self.assertEqual(json.loads(self.run_cli(["--packet", packet])[2])["error"], "unreadable_packet")
            empty = os.path.join(directory, "empty")
            os.mkdir(empty)
            self.assertEqual(json.loads(self.run_cli(["--corpus", empty])[2])["error"], "empty_corpus")
        self.assertEqual(self.run_cli(["--packet", "x", "--repeat", "0"])[0], 2)
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_cli([])[0], 2)
            self.assertEqual(self.run_cli(["--packet", "a", "--corpus", "b"])[0], 2)


if __name__ == "__main__":
    unittest.main()
