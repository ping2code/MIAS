"""Phase 7H matrices for the Phase 7G EvidenceSynthesis.

- F: tampered or invalid packets fail closed with a stable message;
- G: incomplete-but-valid packets synthesize deterministically;
- J: purity and isolation under blocked I/O;
- M: negative architecture checks (no trading or predictive concepts).
"""
import ast
import builtins
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
import inspect
import io
from itertools import product
import json
import os
import re
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from evidence_packet.serialization import canonical_json, content_id
from evidence_synthesis import builder, canonical, model, replay, rules, runner, validation
from evidence_synthesis.builder import synthesize
from evidence_synthesis.validation import PacketValidationError
from tests import evidence_synthesis_corpus as corpus
from tests.test_evidence_synthesis_replay import with_states

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PURE_MODULES = (builder, canonical, model, rules, validation)


def load(name="meta_real_shaped"):
    return json.loads(corpus.packet_path(name).read_text(encoding="utf-8"))


def resealed(data):
    data["packet_id"] = content_id({k: v for k, v in data.items() if k != "packet_id"})
    return data


def text(s):
    return canonical_json(s.to_dict()) + "\n"


# --------------------------------------------------------------------------- F: invalid matrix

def _frames(d):
    return d["technical"]["timeframes"]


def _ctx(d):
    return d["market_context"]["context"]


def _item(d):
    return d["news"]["items"][0]


# (label, mutation, reseal, exact message). reseal=False keeps the stale packet_id (tampering).
INVALID = [
    ("wrong packet_id", lambda d: d.update(packet_id="sha256:" + "0" * 64), False,
     "packet_id does not match the packet body (tampered or corrupt)"),
    ("modified body, unchanged packet_id", lambda d: d.update(symbol="NVDA"), False,
     "packet_id does not match the packet body (tampered or corrupt)"),
    ("malformed packet_id", lambda d: d.update(packet_id="sha256:XYZ"), False, "packet_id is malformed"),
    ("unsupported format_version", lambda d: d.update(format_version="phase7c-v2"), True,
     "unsupported packet format version"),
    ("missing top-level key", lambda d: d.pop("news"), True,
     "packet must have exactly the 8 phase7c-v1 top-level keys"),
    ("unexpected top-level key", lambda d: d.update(signal="x"), True,
     "packet must have exactly the 8 phase7c-v1 top-level keys"),
    ("malformed as_of", lambda d: d.update(as_of="yesterday"), True, "packet as_of is not ISO 8601"),
    ("naive as_of", lambda d: d.update(as_of="2026-09-23T20:05:00"), True, "packet as_of must be timezone-aware"),
    ("malformed symbol", lambda d: d.update(symbol="meta!"), True, "packet symbol is malformed"),
    ("malformed technical state", lambda d: _frames(d)[0]["row"].update(technical_state="strong_buy"), True,
     "technical.1d has an unknown state"),
    ("malformed technical interval", lambda d: _frames(d)[2].update(interval="4h"), True,
     "technical.timeframes must be exactly 1d, 1h, 5m in order"),
    ("duplicate interval", lambda d: _frames(d).__setitem__(2, deepcopy(_frames(d)[1])), True,
     "technical.timeframes must be exactly 1d, 1h, 5m in order"),
    ("wrong interval order", lambda d: _frames(d).reverse(), True,
     "technical.timeframes must be exactly 1d, 1h, 5m in order"),
    ("row and missing_reason both set", lambda d: _frames(d)[0].update(missing_reason="not_supplied"), True,
     "technical.1d needs exactly one of row/missing_reason"),
    ("float decimal", lambda d: _ctx(d)["symbol_context"].update(return_since_open=-0.02), True,
     "market_context.symbol_context.return_since_open must be a canonical decimal string"),
    ("non-numeric decimal", lambda d: _ctx(d)["symbol_context"].update(return_since_open="abc"), True,
     "market_context.symbol_context.return_since_open must be a canonical decimal string"),
    ("NaN decimal string", lambda d: _ctx(d)["comparisons"][0].update(relative_return="NaN"), True,
     "comparison.relative_return must be a canonical decimal string"),
    ("NaN number anywhere", lambda d: _frames(d)[0]["row"].update(rsi14=float("nan")), False,
     "packet body is not canonical JSON"),
    ("market context not an object", lambda d: d["market_context"].update(context=[]), True,
     "market_context.context must be an object"),
    ("comparisons not a list", lambda d: _ctx(d).update(comparisons={}), True,
     "market_context.comparisons must be a list"),
    ("duplicate comparison", lambda d: _ctx(d)["comparisons"].append(deepcopy(_ctx(d)["comparisons"][0])), True,
     "duplicate market_context comparison"),
    ("unknown basis", lambda d: _ctx(d)["comparisons"][0].update(basis="close"), True,
     "market_context comparison identity is malformed"),
    ("context after as_of", lambda d: _ctx(d).update(as_of="2026-09-23T20:05:01+00:00"), True,
     "market_context.as_of is later than the packet as_of"),
    ("news items not a list", lambda d: d["news"].update(items={}), True, "news items/excluded must be lists"),
    ("news item without facts", lambda d: _item(d).pop("facts"), True, "news item is malformed"),
    ("news observed_at malformed", lambda d: _item(d).update(observed_at="soon"), True,
     "news observed_at is not ISO 8601"),
    ("news status inconsistent", lambda d: d["news"]["availability"].update(status="available_empty"), True,
     "news.availability.status is inconsistent with its items"),
    ("duplicate exclusion reason", lambda d: d["news"].update(excluded=[{"reason": "a", "count": 1},
                                                                        {"reason": "a", "count": 1}]), True,
     "duplicate news exclusion reason"),
    ("zero exclusion count", lambda d: d["news"].update(excluded=[{"reason": "a", "count": 0}]), True,
     "news exclusion count must be at least 1"),
    ("benchmark named self", lambda d: [c.update(benchmark="self") for c in _ctx(d)["comparisons"]], True,
     "market_context comparison identity is malformed"),
    ("bar_end after as_of", lambda d: _frames(d)[0].update(bar_end="2026-09-24T20:00:00+00:00"), True,
     "technical.1d.bar_end is later than the packet as_of"),
    ("published_at after as_of", lambda d: _item(d)["facts"].update(published_at="2026-09-24T00:00:00+00:00"),
     True, "news published_at is later than the packet as_of"),
]


class InvalidMatrixTests(unittest.TestCase):
    def test_every_invalid_case_fails_closed_with_a_stable_message(self):
        for label, fn, reseal, message in INVALID:
            with self.subTest(case=label):
                data = load()
                fn(data)
                if reseal:
                    data = resealed(data)
                before = deepcopy(data)
                with self.assertRaises(PacketValidationError) as caught:
                    synthesize(data)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(data, before)  # Never repaired.

    def test_non_packet_inputs(self):
        for value in (None, [], "packet", 1, b"{}"):
            with self.assertRaises(PacketValidationError) as caught:
                synthesize(value)
            self.assertEqual(str(caught.exception), "input must be an EvidencePacket or its canonical dict")

    def test_invalid_is_reported_the_same_by_both_clis(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bad.packet.json")
            data = load()
            data["symbol"] = "NVDA"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            for main in (runner.main, replay.main):
                err = io.StringIO()
                self.assertEqual(main(["--packet", path], out=io.StringIO(), err=err), 2)
                self.assertEqual(json.loads(err.getvalue())["error"], "invalid_packet")


# --------------------------------------------------------------------------- G: incomplete but valid

class IncompleteMatrixTests(unittest.TestCase):
    CASES = {
        # name: (technical status, technical_missing intervals, market ctx, news status, extra contradiction codes)
        "unavailable_timeframe": ("partial", ["5m"], True, "available", {"timeframe_unavailable"}),
        "missing_1h": ("partial", ["1h"], True, "available", {"timeframe_unavailable"}),
        "missing_1d": ("partial", ["1d"], True, "available", {"timeframe_unavailable"}),
        "missing_1d_5m": ("partial", ["1d", "5m"], True, "available", {"timeframe_unavailable"}),
        "technical_unavailable": ("unavailable", ["1d", "1h", "5m"], True, "available", {"timeframe_unavailable"}),
        "market_context_unavailable": ("available", [], False, "available", {"market_context_unavailable"}),
        "news_unavailable": ("available", [], True, "unavailable", {"news_unavailable"}),
        "empty_news": ("available", [], True, "available_empty", set()),
        "market_context_and_news_unavailable": ("available", [], False, "unavailable",
                                                {"market_context_unavailable", "news_unavailable"}),
        "benchmark_unavailable": ("available", [], True, "available", set()),
        "insufficient_data_state": ("available", [], True, "available", {"timeframe_unavailable"}),
    }

    def test_incomplete_packets_synthesize_with_explicit_facts(self):
        for name, (tech, missing, context, news, codes) in self.CASES.items():
            with self.subTest(case=name):
                data = load(name)
                s = synthesize(data)
                self.assertEqual(text(synthesize(deepcopy(data))), text(s))
                self.assertEqual(s.completeness.technical, tech)
                self.assertEqual([t.interval for t in s.completeness.technical_missing], missing)
                self.assertEqual(s.market_context.available, context)
                self.assertEqual(s.news.availability, news)
                found = {c.code for c in s.contradictions}
                self.assertTrue(codes <= found, found)
                if missing or name == "insufficient_data_state":
                    self.assertEqual(s.timeframe_alignment.pattern, "incomplete")
                for t in s.timeframes:
                    if not t.available:
                        self.assertEqual((t.state_direction, t.state, t.age_seconds), ("unavailable", None, None))
                        self.assertIsNotNone(t.missing_reason)

    def test_empty_news_is_not_unavailable_news(self):
        empty, unavailable = synthesize(load("empty_news")), synthesize(load("news_unavailable"))
        self.assertEqual((empty.news.item_count, empty.news.newest_publication_timestamp), (0, None))
        self.assertNotIn("news_unavailable", {c.code for c in empty.contradictions})
        self.assertIn("news_unavailable", {c.code for c in unavailable.contradictions})


# --------------------------------------------------------------------------- J: isolation

class _Blocked(Exception):
    pass


def _blocked(*args, **kwargs):
    raise _Blocked("blocked side effect")


class _GuardedEnviron(dict):
    def _deny(self, *args, **kwargs):
        raise _Blocked("environment read")
    __getitem__ = get = __contains__ = __iter__ = keys = items = values = copy = _deny


class _Clockless(datetime):
    now = utcnow = today = classmethod(_blocked)


class _Dateless(date):
    today = classmethod(_blocked)


def sealed():
    """Block network, database, files, subprocesses, clock, environment and process identity."""
    import sqlalchemy
    stack = ExitStack()
    for target, name in ((socket, "socket"), (socket, "create_connection"), (socket, "gethostname"),
                         (builtins, "open"), (io, "open"), (os, "open"), (os, "system"), (os, "getpid"),
                         (os, "getenv"), (subprocess, "Popen"), (subprocess, "run"), (time, "time"),
                         (time, "time_ns"), (time, "monotonic"), (time, "perf_counter"),
                         (sqlalchemy, "create_engine"), (sqlalchemy.engine, "create_engine")):
        stack.enter_context(patch.object(target, name, _blocked))
    stack.enter_context(patch.object(os, "environ", _GuardedEnviron()))
    stack.enter_context(patch.object(validation, "datetime", _Clockless))
    stack.enter_context(patch.object(validation, "date", _Dateless))
    return stack


class IsolationTests(unittest.TestCase):
    def test_sealed_environment_still_reproduces_every_golden(self):
        packets = {name: load(name) for name in corpus.names()}
        expected = {name: corpus.synthesis_path(name).read_text(encoding="utf-8") for name in corpus.names()}
        base = load("all_bullish")
        combos = [with_states(base, combo) for combo in product(rules.TECHNICAL_STATES[:4], repeat=3)]
        with sealed():
            produced = {name: text(synthesize(data)) for name, data in packets.items()}
            for data in combos:
                synthesize(data)
            with self.assertRaises(_Blocked):  # The seal is real.
                open(os.devnull)
        self.assertEqual(produced, expected)

    def test_production_imports(self):
        code = ("import sys, evidence_synthesis.builder, evidence_synthesis.replay, evidence_synthesis.runner\n"
                "roots = {n.split('.')[0] for n in sys.modules}\n"
                "print(sorted(roots & {'technical', 'persistence', 'evidence', 'evaluation', 'analyzer', 'collector',"
                " 'sqlalchemy', 'redis', 'openai', 'telegram', 'requests', 'dotenv'}), 'shared.config' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[] False", result.stderr[-300:])

    SIDE_EFFECT_NAMES = {"open", "environ", "getenv", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                         "perf_counter", "system", "Popen", "run", "socket", "create_connection", "print", "input",
                         "write_text", "write_bytes", "mkdir", "unlink", "remove", "rename", "getpid", "gethostname"}

    def test_pure_module_code(self):
        """AST scan (docstrings are not code): only pure imports, and no I/O, clock or environment names."""
        allowed = {"datetime", "re", "decimal", "collections", "dataclasses", "hashlib", "json", "evidence_packet",
                   "evidence_synthesis", "market_data"}
        for module in PURE_MODULES:
            imported, used = code_names(module)
            self.assertLessEqual(imported, allowed, module.__name__)
            self.assertFalse(used & self.SIDE_EFFECT_NAMES, (module.__name__, used & self.SIDE_EFFECT_NAMES))

    def test_replay_tool_only_reads(self):
        imported, used = code_names(replay)
        self.assertLessEqual(imported, {"argparse", "hashlib", "json", "pathlib", "sys", "evidence_synthesis"})
        self.assertFalse(used & self.SIDE_EFFECT_NAMES, used & self.SIDE_EFFECT_NAMES)


def code_names(module):
    """(imported top-level modules, every Name/Attribute identifier) of a module's code."""
    tree = ast.parse(inspect.getsource(module))
    imported, used = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return imported, used


# --------------------------------------------------------------------------- M: negative architecture

FORBIDDEN = {"buy", "sell", "long", "short", "trade", "trading", "option", "options", "call", "calls", "put", "puts",
             "probability", "forecast", "prediction", "predict", "recommendation", "recommend", "target",
             "stop", "score", "signal"}
FORBIDDEN_PHRASES = ("stop_loss", "expected_return")
# Phase 7C packet field names that synthesis reads (never emits): the collector's upstream news heuristic section,
# and validation's local alias for that section.
PACKET_FIELDS_READ = {"upstream_score", ("evidence_synthesis.validation", "score")}


def words(identifier):
    return set(re.split(r"[^a-z0-9]+", identifier.lower())) - {""}


class NegativeArchitectureTests(unittest.TestCase):
    def outputs(self):
        base = load("all_bullish")
        for name in corpus.names():
            yield name, synthesize(load(name)).to_dict()
        for combo in product(rules.TECHNICAL_STATES, repeat=3):
            yield combo, synthesize(with_states(base, combo)).to_dict()

    def test_output_keys_and_derived_values(self):
        derived = {"state_direction", "relation", "pattern", "value_sign", "code", "availability",
                   "market_context", "technical", "news", "reference", "basis"}
        for label, data in self.outputs():
            stack = [data]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertFalse(words(key) & FORBIDDEN, (label, key))
                        self.assertFalse(any(p in key for p in FORBIDDEN_PHRASES), (label, key))
                        if key in derived and isinstance(child, str):
                            self.assertFalse(words(child) & FORBIDDEN, (label, key, child))
                        stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)

    def test_enumerations_are_descriptive(self):
        for value in (*rules.PATTERNS, *rules.CONTRADICTION_CODES, rules.BULLISH, rules.BEARISH,
                      rules.NON_DIRECTIONAL, rules.UNAVAILABLE, rules.AGREE, rules.OPPOSE, rules.POSITIVE,
                      rules.NEGATIVE, rules.ZERO):
            self.assertFalse(words(value) & FORBIDDEN, value)

    def test_production_identifiers(self):
        """Code identifiers and string constants (docstrings excluded) never name a trading or predictive concept."""
        for module in (*PURE_MODULES, replay, runner):
            tree = ast.parse(inspect.getsource(module))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body
                          and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.Attribute):
                    names = [node.attr]
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    names = [node.name]
                elif isinstance(node, ast.arg):
                    names = [node.arg]
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings \
                        and re.fullmatch(r"[a-z_]+", node.value):
                    names = [node.value]
                else:
                    continue
                for name in names:
                    if name not in PACKET_FIELDS_READ and (module.__name__, name) not in PACKET_FIELDS_READ:
                        self.assertFalse(words(name) & FORBIDDEN, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
