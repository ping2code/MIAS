"""Phase 8A isolation: no I/O, strict package boundary, no decision/predictive concepts, cross-process determinism."""
import ast
import builtins
from contextlib import ExitStack
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
import tempfile
import time
import unittest
from unittest.mock import patch

from evidence_synthesis import rules as sr
from evidence_synthesis.builder import synthesize
from evidence_synthesis.canonical import canonical_json
from market_intelligence import builder, canonical, model, rules, validation
from market_intelligence.builder import build
from tests import market_intelligence_cases as cases
from tests.test_evidence_synthesis_replay import with_states

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTION = (builder, canonical, model, rules, validation)


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


def code_names(module):
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


class IsolationTests(unittest.TestCase):
    def test_sealed_build_reproduces_every_golden(self):
        inputs = {name: cases.synthesis(name) for name in cases.names()}
        expected = {name: cases.intelligence_path(name).read_text(encoding="utf-8") for name in cases.names()}
        with sealed():
            produced = {name: canonical_json(build(data).to_dict()) + "\n" for name, data in inputs.items()}
            with self.assertRaises(_Blocked):
                open(os.devnull)
        self.assertEqual(produced, expected)

    def test_no_io_clock_environment_or_process_names(self):
        forbidden = {"open", "environ", "getenv", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                     "perf_counter", "system", "Popen", "run", "socket", "create_connection", "print", "input",
                     "write_text", "write_bytes", "read_text", "mkdir", "unlink", "getpid", "gethostname"}
        for module in PRODUCTION:
            _, used = code_names(module)
            self.assertFalse(used & forbidden, (module.__name__, used & forbidden))

    def test_no_runner_in_phase8a(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "market_intelligence", "runner.py")))


class ImportBoundaryTests(unittest.TestCase):
    def test_direct_imports_are_stdlib_and_evidence_synthesis_only(self):
        stdlib = set(sys.stdlib_module_names)
        for module in PRODUCTION:
            imported, _ = code_names(module)
            self.assertLessEqual(imported - stdlib, {"evidence_synthesis", "market_intelligence"}, module.__name__)

    def test_runtime_adds_only_market_intelligence(self):
        project = "{'market_intelligence', 'evidence_packet', 'market_data', 'market_context', 'technical', " \
                  "'persistence', 'evidence', 'evaluation', 'analyzer', 'collector', 'shared', 'sqlalchemy'}"
        code = ("import sys, evidence_synthesis.builder, evidence_synthesis.validation, evidence_synthesis.model\n"
                "base = {n.split('.')[0] for n in sys.modules}\n"
                "import market_intelligence.builder\n"
                f"print(sorted(({{n.split('.')[0] for n in sys.modules}} - base) & {project}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "['market_intelligence']", result.stderr[-300:])

    def test_lower_layers_never_import_market_intelligence(self):
        code = ("import sys, evidence_packet.assembler, evidence_packet.runner, evidence_synthesis.builder, "
                "evidence_synthesis.runner, evidence_synthesis.replay\n"
                "print(sorted(n for n in sys.modules if n.startswith('market_intelligence')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


FORBIDDEN = {"direction", "score", "confidence", "severity", "conviction", "probability", "forecast", "prediction",
             "predict", "recommendation", "recommend", "signal", "buy", "sell", "hold", "trade", "trading", "option",
             "options", "call", "calls", "put", "puts", "strike", "expiry", "expiration", "target", "stop",
             "priority", "rank", "ranking", "regime", "important", "catalyst", "sentiment"}
FORBIDDEN_PHRASES = ("expected_return", "risk_reward", "position_size", "stop_loss")
# Phase 8B transition code naming a change in the synthesis's own state_direction fact (an upstream contract field),
# required by the Phase 8B code list. It is descriptive, not a decision field.
UPSTREAM_FACT_CODES = {"timeframe_direction_changed"}


def words(identifier):
    return set(re.split(r"[^a-z0-9]+", identifier.lower())) - {""}


def synthesis_field_names():
    """Every key name of the EvidenceSynthesis contract: upstream names Phase 8 may read but never emits."""
    names, stack = set(), [cases.synthesis("meta_real_shaped"), cases.synthesis("market_context_unavailable")]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            names |= set(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return names


class ForbiddenConceptTests(unittest.TestCase):
    DERIVED = {"opposition_shape", "own_return_profile", "relative_return_profile", "category", "code",
               "isolated_interval", "pattern", "news_state", "technical_status", "market_context_status"}

    def outputs(self):
        for name in cases.names():
            yield name, build(cases.synthesis(name)).to_dict()
        base = cases.packet("all_bullish")
        for combo in product(sr.TECHNICAL_STATES[::2], repeat=3):
            yield combo, build(synthesize(with_states(base, combo)).to_dict()).to_dict()

    def test_output_keys_and_phase8_values(self):
        for label, data in self.outputs():
            stack = [data]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertFalse(words(key) & FORBIDDEN, (label, key))
                        self.assertFalse(any(p in key for p in FORBIDDEN_PHRASES), (label, key))
                        if key in self.DERIVED and isinstance(child, str):
                            self.assertFalse(words(child) & FORBIDDEN, (label, key, child))
                        stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)

    def test_rule_enumerations(self):
        for value in (*rules.OPPOSITION_SHAPES, *rules.SIGN_PROFILES, *rules.ATTENTION_CATEGORIES,
                      *rules.ATTENTION_CODES):
            self.assertFalse(words(value) & FORBIDDEN, value)

    def test_production_identifiers(self):
        """Code identifiers and string constants (docstrings excluded) create no decision or predictive concept.
        Names of the EvidenceSynthesis contract (e.g. state_direction, technical_confidence) are upstream fields
        Phase 8 reads; they are allowed only as such."""
        upstream = synthesis_field_names()
        for module in PRODUCTION:
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
                    names = [node.value]  # Identifier-like constants (keys, codes); prose error messages are not fields.
                else:
                    continue
                for name in names:
                    if name not in upstream and name != "state_direction" and name not in UPSTREAM_FACT_CODES:
                        self.assertFalse(words(name) & FORBIDDEN, (module.__name__, name))


class CrossProcessTests(unittest.TestCase):
    def test_fresh_processes_and_environments(self):
        code = ("import hashlib\nfrom evidence_synthesis.canonical import canonical_json\n"
                "from market_intelligence.builder import build\nfrom tests import market_intelligence_cases as c\n"
                "print(hashlib.sha256(''.join(canonical_json(build(c.synthesis(n)).to_dict()) for n in c.names())"
                ".encode()).hexdigest())")
        expected = __import__("hashlib").sha256("".join(
            cases.intelligence_path(n).read_text(encoding="utf-8")[:-1] for n in cases.names()).encode()).hexdigest()
        variants = ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "1", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                    {"PYTHONHASHSEED": "777", "LANG": "C", "MIAS_UNRELATED": "x"})
        for extra in variants:
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT,
                                                                                         **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
