"""Phase 10B Trade Setup: assessment tamper matrix, determinism, cross-process, sealed no-I/O, import boundary,
frozen upstream constants, and forbidden semantics."""
import ast
from copy import deepcopy
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from trade_setup import builder, canonical, model, policy, rules, validation
from trade_setup.builder import assess
from trade_setup.canonical import canonical_json, content_id
from trade_setup.validation import TradeSetupInputError, validated_assessment, verify_assessment
from tests import trade_setup_cases as cases

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PURE = (builder, canonical, model, policy, rules, validation)


def inputs(mi_name="all_bullish", oi_name="quoted_complete"):
    return deepcopy(cases.market_intelligence(mi_name)), deepcopy(cases.options_intelligence(oi_name))


def assessment(**policy_overrides):
    mi, oi = inputs()
    return assess(mi, oi, cases.policy(**policy_overrides)).to_dict()


def resealed(data):
    data["assessment_id"] = content_id({k: v for k, v in data.items() if k != "assessment_id"})
    return data


STRUCTURAL = [
    ("missing key", lambda d: d.pop("rejections"), True, "assessment must have exactly the 12 phase10-v1 keys"),
    ("extra key", lambda d: d.update(score=1), True, "assessment must have exactly the 12 phase10-v1 keys"),
    ("wrong format", lambda d: d.update(assessment_format_version="phase10-v2"), True,
     "unsupported assessment format version"),
    ("wrong rules", lambda d: d.update(rules_version="phase10-rules-v2"), True, "unsupported assessment rules version"),
    ("id tamper", lambda d: d["market_bias"].update(state="bearish"), False,
     "assessment id does not match its body (tampered or corrupt)"),
    ("policy tamper", lambda d: d["policy"].update(max_dte=365), True,
     "assessment policy is invalid: policy_id does not match the policy (tampered or corrupt)"),
    ("unknown status", lambda d: d["outcome"].update(status="maybe"), True, "assessment outcome status is not supported"),
    ("unsorted reasons", lambda d: d["outcome"].update(status="no_setup", no_setup_reasons=["b_reason", "a_reason"]),
     True, "assessment no_setup_reasons must be sorted, unique and from the closed set"),
    ("status without reasons", lambda d: d["outcome"].update(status="no_setup"), True,
     "assessment outcome is inconsistent with its reasons"),
    ("candidates in 10B", lambda d: d.update(candidates=[{"contract_id": "X"}]), True,
     "phase 10B assessments carry no candidates or rejections"),
    ("inconsistent side", lambda d: d["market_bias"].update(side="put"), True, "assessment market_bias is inconsistent"),
    ("trace reordered", lambda d: d["decision_trace"].reverse(), True, "assessment decision_trace is malformed"),
    ("trace reason", lambda d: d["decision_trace"][0].update(reason="context_gate_blocked"), True,
     "assessment decision_trace reason is inconsistent with its result"),
    ("trace pointer", lambda d: d["decision_trace"][0].update(pointers=["db:table"]), True,
     "assessment decision_trace pointers are malformed"),
    ("reasons vs trace", lambda d: (d["decision_trace"][5].update(result="fail", reason="options_chain_truncated"),
                                    d["outcome"].update(status="no_setup", no_setup_reasons=[])), True,
     "assessment outcome is inconsistent with its reasons"),
    ("provenance", lambda d: d["provenance"].update(policy_id="sha256:" + "0" * 64), True,
     "assessment provenance is inconsistent with its inputs and policy"),
]


class TamperTests(unittest.TestCase):
    def test_structural_matrix(self):
        for label, fn, reseal, message in STRUCTURAL:
            with self.subTest(case=label):
                data = assessment()
                fn(data)
                if reseal:
                    data = resealed(data)
                with self.assertRaises(TradeSetupInputError) as caught:
                    validated_assessment(data)
                self.assertEqual(str(caught.exception), message)

    def test_rederivation_catches_consistent_lies(self):
        mi, oi = inputs()
        data = assessment()
        # A self-consistent but false no_setup (trace, outcome and all structure agree).
        data["decision_trace"][2].update(result="fail", reason="side_not_allowed_by_policy")
        data["outcome"].update(status="no_setup", no_setup_reasons=["side_not_allowed_by_policy"])
        data = resealed(data)
        validated_assessment(data)
        with self.assertRaises(TradeSetupInputError) as caught:
            verify_assessment(data, mi, oi)
        self.assertEqual(str(caught.exception), "assessment does not match its inputs at decision_trace, outcome")
        with self.assertRaises(TradeSetupInputError):
            verify_assessment(assessment(), *inputs("all_bearish"))

    def test_invalid_input_is_never_no_setup(self):
        mi, oi = inputs()
        mi["intelligence_id"] = "sha256:" + "1" * 64
        with self.assertRaises(TradeSetupInputError):
            assess(mi, oi, cases.policy())


class DeterminismTests(unittest.TestCase):
    def test_100_repeats_and_dict_order(self):
        expected = canonical_json(assessment())
        self.assertEqual({canonical_json(assessment()) for _ in range(100)}, {expected})

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        mi, oi = inputs()
        p = cases.policy().to_dict()
        self.assertEqual(canonical_json(assess(reversed_keys(mi), reversed_keys(oi), reversed_keys(p)).to_dict()),
                         expected)
        from trade_setup.policy import make_policy
        shuffled = dict(reversed(list(cases.BASE_POLICY.items())))
        self.assertEqual(make_policy(**shuffled), cases.policy())

    def test_fresh_processes_and_environments(self):
        code = ("from tests import trade_setup_cases as c\nfrom trade_setup.builder import assess\n"
                "print(assess(c.market_intelligence('all_bullish'), c.options_intelligence('quoted_complete'), "
                "c.policy()).assessment_id)")
        expected = assess(*inputs(), cases.policy()).assessment_id
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "11", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                      {"PYTHONHASHSEED": "65003", "LANG": "C", "TZ": "UTC", "MIAS_UNRELATED": "x"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class BoundaryTests(unittest.TestCase):
    def test_sealed_no_io(self):
        from tests.test_market_intelligence_isolation import sealed
        mi, oi = inputs()
        p = cases.policy()
        expected = assess(mi, oi, p).to_dict()
        with sealed():
            produced = assess(mi, oi, p).to_dict()
            verify_assessment(produced, mi, oi)
        self.assertEqual(produced, expected)

    def test_ast_imports_and_calls(self):
        stdlib = set(sys.stdlib_module_names)
        forbidden_calls = {"open", "getenv", "now", "utcnow", "today", "time", "time_ns", "system", "Popen", "run",
                           "socket", "print", "getpid", "gethostname"}
        for module in PURE:
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], stdlib, module.__name__)
                elif isinstance(node, ast.ImportFrom):
                    root = node.module.split(".")[0]
                    self.assertTrue(root in stdlib or root == "trade_setup", (module.__name__, node.module))
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(name, forbidden_calls, (module.__name__, name))
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("environ", "argv"), module.__name__)

    def test_runtime_imports(self):
        code = ("import sys, trade_setup.builder, trade_setup.validation, trade_setup.policy\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'market_intelligence', 'options_intelligence', "
                "'options_data', 'market_data', 'evidence_packet', 'evidence_synthesis', 'evidence', 'evaluation', "
                "'persistence', 'sqlalchemy', 'requests', 'openai', 'redis', 'technical', 'market_context'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


class FrozenUpstreamTests(unittest.TestCase):
    """Tests may import upstream packages to prove the local frozen copies match; trade_setup never does."""

    def test_market_intelligence_constants(self):
        from market_intelligence import model as mim, rules as mir
        from evidence_synthesis import rules as sr
        from dataclasses import fields
        self.assertEqual((rules.MI_FORMAT_VERSION, rules.MI_RULES_VERSION), (mir.INTELLIGENCE_FORMAT_VERSION,
                                                                            mir.RULES_VERSION))
        self.assertEqual(rules.MI_TOP_LEVEL, tuple(f.name for f in fields(mim.MarketIntelligence)))
        self.assertEqual(rules.MI_SYNTHESIS_REF, tuple(f.name for f in fields(mim.SynthesisRef)))
        self.assertEqual(rules.MI_PATTERNS, sr.PATTERNS)
        self.assertEqual(rules.MI_CONFLICT_CODES, mir.CONFLICT_CODES)
        self.assertEqual(rules.MI_ATTENTION_CODES, mir.ATTENTION_CODES)

    def test_options_intelligence_constants(self):
        from options_intelligence import model as oim, rules as oir
        from dataclasses import fields
        self.assertEqual(rules.OI_FORMATS, oir.FORMATS)
        self.assertEqual(rules.OI_TOP_LEVEL, oim.TOP_LEVEL_FIELDS)
        self.assertEqual(rules.OI_SNAPSHOT_REF, tuple(f.name for f in fields(oim.SnapshotRef)))
        self.assertEqual(rules.OI_CONTRACT_V1, tuple(f.name for f in fields(oim.Contract)))
        self.assertEqual(rules.OI_CONTRACT_V2_EXTRA, oir.V2_CONTRACT_FIELDS)
        self.assertEqual((rules.OI_QUOTE_STATES, rules.OI_ACTIVITY_STATES, rules.OI_SESSION_RELATIONS),
                         (oir.QUOTE_STATES, oir.ACTIVITY_STATES, oir.SESSION_RELATIONS))
        from options_data import model as om
        self.assertEqual(rules.OI_TIME_BASES, om.TIME_BASES)

    def test_canonical_helpers(self):
        from decimal import Decimal
        from market_data.models import SYMBOL, format_decimal
        from options_data.canonical import canonical_json as upstream_json
        data = assessment()
        self.assertEqual(canonical_json(data), upstream_json(data))
        for value in ("0.10", "1E+3", "-0", "12.5000", "0.0001"):
            self.assertEqual(canonical.format_decimal(Decimal(value)), format_decimal(Decimal(value)))
        self.assertEqual(rules.SYMBOL.pattern, SYMBOL.pattern)


FORBIDDEN = {"score", "confidence", "rank", "ranking", "best", "top", "pick", "recommend", "recommendation", "signal",
             "prediction", "forecast", "probability", "target", "stop", "reward", "sizing", "size", "allocation",
             "buy", "sell", "sentiment", "weight", "weighted"}


def words(identifier):
    """Identifier words; the structural phrase "top_level" (schema shape, not a top pick) is exempt."""
    return set(re.split(r"[^a-z0-9]+", identifier.lower().replace("top_level", ""))) - {""}


class ForbiddenSemanticsTests(unittest.TestCase):
    def test_keys_and_vocabularies(self):
        stack = [assessment(), assessment(allowed_sides=["put"])]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertFalse(words(key) & FORBIDDEN, key)
                    stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
        for value in (*rules.NO_SETUP_REASONS, *rules.BIAS_STATES, *rules.OUTCOME_STATUSES, *rules.TRACE_RULES,
                      *rules.NOT_EVALUATED_REASONS):
            self.assertFalse(words(value) & FORBIDDEN, value)

    def test_identifiers(self):
        for module in PURE:
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                names = [node.id] if isinstance(node, ast.Name) else [node.attr] if isinstance(node, ast.Attribute) \
                    else [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) \
                    else [node.arg] if isinstance(node, ast.arg) else []
                for name in names:
                    self.assertFalse(words(name) & FORBIDDEN, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
