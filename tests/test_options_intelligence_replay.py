"""Phase 9D OptionsIntelligence: tamper matrix, determinism, order independence, cross-process, sealed no-I/O, import
boundary, live snapshot replay, large chain and forbidden semantics."""
import ast
from copy import deepcopy
from datetime import date, timedelta
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

from options_intelligence import builder, canonical, model, rules, validation
from options_intelligence.builder import build
from options_intelligence.canonical import canonical_json, content_id
from options_intelligence.validation import (OptionsIntelligenceError, validated_options_intelligence,
                                             verify_against_snapshot)
from tests import options_intelligence_cases as cases
from tests.options_snapshot_cases import chain_record, live_snapshot, meta_chain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = cases.CALENDAR
PURE = (builder, canonical, model, rules, validation)
LIVE = {sym: f"/tmp/phase9c_{sym}_snapshot.json" for sym in ("META", "NVDA")}


def golden(name="quoted_with_price"):
    return json.loads(cases.intelligence_path(name).read_text(encoding="utf-8"))


def resealed(data):
    data["options_intelligence_id"] = content_id({k: v for k, v in data.items() if k != "options_intelligence_id"})
    return data


def contract(d, cid):
    return next(c for c in d["contracts"] if c["contract_id"] == cid)


def _set(d, **kw):
    d.update(kw)


C690, C700 = "META261016C00690000", "META261016C00700000"

STRUCTURAL = [
    ("wrong format version", lambda d: _set(d, options_intelligence_format_version="phase9-v9"), True,
     "unsupported options intelligence format version"),
    ("wrong rules version", lambda d: _set(d, rules_version="phase9-rules-v2"), True,
     "unsupported options intelligence rules version"),
    ("missing key", lambda d: d.pop("volatility"), True, "options intelligence must have exactly the 13 phase9-v1 keys"),
    ("extra key", lambda d: _set(d, score=1), True, "options intelligence must have exactly the 13 phase9-v1 keys"),
    ("id mismatch", lambda d: contract(d, C690).update(mid="11"), False,
     "options_intelligence_id does not match the body (tampered or corrupt)"),
    ("malformed id", lambda d: _set(d, options_intelligence_id="x"), False, "options_intelligence_id is malformed"),
    ("malformed snapshot_ref", lambda d: d["snapshot_ref"].update(snapshot_format_version="phase9-snapshot-v2"), True,
     "snapshot_ref is malformed"),
    ("malformed market_intelligence_ref", lambda d: _set(d, market_intelligence_ref=dict(symbol="META")), True,
     "market_intelligence_ref is malformed"),
    ("provenance mismatch", lambda d: d["provenance"].update(snapshot_id="sha256:" + "1" * 64), True,
     "provenance is inconsistent with the references"),
    ("unknown quote_state", lambda d: contract(d, C690).update(quote_state="tight"), True,
     "contract quote_state is not supported"),
    ("unknown strike_relation", lambda d: contract(d, C690).update(strike_relation="atm"), True,
     "contract strike_relation is not supported"),
    ("unknown activity state", lambda d: contract(d, C690).update(volume_state="high"), True,
     "contract activity state is not supported"),
    ("unknown session relation", lambda d: contract(d, C690)["day"].update(session_relation="today"), True,
     "contract session_relation is not supported"),
    ("foreign pointer", lambda d: contract(d, C690).update(source_pointers=[f"contracts[{C700}].quote.bid"]), True,
     "source pointer is malformed or names another contract"),
    ("unsorted contracts", lambda d: d["contracts"].reverse(), True, "contracts are not unique and in canonical order"),
    ("unsorted expirations", lambda d: d["expirations"].reverse(), True, "expirations are not unique and ascending"),
    ("unsorted attention", lambda d: d["attention"].reverse(), True,
     "attention must be sorted by (category, code) and unique"),
    ("attention severity", lambda d: d["attention"][0].update(severity="high"), True,
     "attention entries carry only category, code, scope, count and contract_ids"),
    ("attention wrong category", lambda d: d["attention"][0].update(category="presence"), True,
     "attention code or category is not supported"),
    ("attention count", lambda d: d["attention"][0].update(count=99), True, "attention count is inconsistent"),
]

DERIVED = [
    ("wrong DTE", lambda d: contract(d, C690).update(dte_calendar_days=15), "contracts[1].dte_calendar_days"),
    ("wrong strike relation", lambda d: contract(d, C690).update(strike_relation="above"), "contracts[1].strike_relation"),
    ("wrong quote state", lambda d: contract(d, C690).update(quote_state="crossed"), "contracts[1].quote_state"),
    ("wrong mid", lambda d: contract(d, C690).update(mid="10.26"), "contracts[1].mid"),
    ("wrong spread", lambda d: contract(d, C690).update(spread_relative="0.03"), "contracts[1].spread_relative"),
    ("wrong volume total", lambda d: d["activity"].update(call_volume_total="501"), "activity.call_volume_total"),
    ("wrong ratio", lambda d: d["activity"].update(put_call_volume_ratio="2.3"), "activity.put_call_volume_ratio"),
    ("wrong IV median", lambda d: d["volatility"]["overall"].update(median="0.4"), "volatility.overall.median"),
    ("wrong expiration summary", lambda d: d["expirations"][1].update(call_count=6), "expirations[1].call_count"),
    ("wrong completeness", lambda d: d["chain_completeness"].update(contract_count=8),
     "chain_completeness.contract_count"),
    ("dropped attention id", lambda d: d["attention"][0].update(count=0, contract_ids=[]), "attention[0].contract_ids[len]"),
]


class TamperTests(unittest.TestCase):
    def test_structural_matrix(self):
        for label, fn, reseal, message in STRUCTURAL:
            with self.subTest(case=label):
                data = golden()
                fn(data)
                if reseal:
                    data = resealed(data)
                with self.assertRaises(OptionsIntelligenceError) as caught:
                    validated_options_intelligence(data)
                self.assertEqual(str(caught.exception), message)

    def test_derived_matrix_against_snapshot(self):
        snap = cases.snapshot("quoted_with_price")
        self.assertEqual(verify_against_snapshot(golden(), snap, calendar=CAL), golden())
        for label, fn, path in DERIVED:
            with self.subTest(case=label):
                data = golden()
                fn(data)
                data = resealed(data)
                validated_options_intelligence(data)  # Structurally fine: only re-derivation can catch it.
                with self.assertRaises(OptionsIntelligenceError) as caught:
                    verify_against_snapshot(data, snap, calendar=CAL)
                self.assertEqual(str(caught.exception), f"options intelligence does not match its snapshot at {path}")
        with self.assertRaises(OptionsIntelligenceError):  # Built from another snapshot.
            verify_against_snapshot(golden(), cases.snapshot("live_like_meta"), calendar=CAL)

    def test_invalid_snapshot_input(self):
        from options_data.validation import OptionsSnapshotError
        bad = cases.snapshot("quoted_with_price").to_dict()
        bad["underlying"] = "NVDA"
        with self.assertRaises(OptionsSnapshotError):
            build(bad, calendar=CAL)


class DeterminismTests(unittest.TestCase):
    def test_100_repeats_and_typed_dict(self):
        snap = cases.snapshot("quoted_with_price")
        expected = cases.intelligence_path("quoted_with_price").read_text(encoding="utf-8")
        outputs = {canonical_json(build(json.loads(canonical_json(snap.to_dict())), calendar=CAL).to_dict()) + "\n"
                   for _ in range(100)}
        self.assertEqual(outputs, {expected})
        self.assertEqual(build(snap, calendar=CAL), build(snap.to_dict(), calendar=CAL))

    def test_snapshot_and_dict_order_independence(self):
        records = cases.quoted_chain()
        from tests.options_snapshot_cases import full_snapshot
        a = build(full_snapshot(records, underlying_price=cases.PRICE, calendar_state="regular"), calendar=CAL)
        b = build(full_snapshot(list(reversed(records)), underlying_price=cases.PRICE, calendar_state="regular"),
                  calendar=CAL)
        self.assertEqual(a.options_intelligence_id, b.options_intelligence_id)

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value
        snap = cases.snapshot("quoted_with_price").to_dict()
        self.assertEqual(build(reversed_keys(snap), calendar=CAL).options_intelligence_id, a.options_intelligence_id)

    def test_fresh_processes_and_environments(self):
        code = ("import hashlib\nfrom options_intelligence.builder import build\n"
                "from options_intelligence.canonical import canonical_json\n"
                "from tests import options_intelligence_cases as c\n"
                "print(hashlib.sha256(''.join(canonical_json(build(c.snapshot(n), calendar=c.CALENDAR).to_dict()) "
                "for n in c.names()).encode()).hexdigest())")
        import hashlib
        expected = hashlib.sha256("".join(cases.intelligence_path(n).read_text(encoding="utf-8")[:-1]
                                          for n in cases.names()).encode()).hexdigest()
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "5", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                      {"PYTHONHASHSEED": "7717", "LANG": "C", "TZ": "UTC", "MIAS_UNRELATED": "x"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class IsolationTests(unittest.TestCase):
    def test_sealed_build_and_verification(self):
        from tests.test_market_intelligence_isolation import sealed
        snaps = {name: cases.snapshot(name) for name in cases.names()}
        mi = cases.market_intelligence()
        expected = {name: cases.intelligence_path(name).read_text(encoding="utf-8") for name in cases.names()}
        with sealed():
            produced = {name: canonical_json(build(s, calendar=CAL).to_dict()) + "\n" for name, s in snaps.items()}
            with_mi = build(snaps["live_like_meta"], mi, calendar=CAL)
            verify_against_snapshot(with_mi, snaps["live_like_meta"], mi, calendar=CAL)
        self.assertEqual(produced, expected)

    def test_pure_code_has_no_io_and_narrow_imports(self):
        forbidden_calls = {"open", "getenv", "now", "utcnow", "today", "time", "time_ns", "monotonic", "perf_counter",
                           "system", "Popen", "run", "socket", "print", "write_text", "read_text", "getpid",
                           "gethostname"}
        allowed_modules = {"options_data.model", "options_data.validation", "options_data.canonical",
                           "options_intelligence", "market_data.models", "market_data.calendar"}
        for module in PURE:
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(name, forbidden_calls, (module.__name__, name))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("environ", "argv"), module.__name__)
                if isinstance(node, ast.ImportFrom) and node.module.split(".")[0] not in sys.stdlib_module_names:
                    targets = {f"{node.module}.{a.name}" for a in node.names} if node.module == "options_data" \
                        else {node.module}
                    self.assertTrue(all(t in allowed_modules or t.startswith("options_intelligence") for t in targets),
                                    (module.__name__, targets))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, module.__name__)

    def test_runtime_import_boundary(self):
        code = ("import sys, options_intelligence.builder, options_intelligence.validation\n"
                "print(sorted(n for n in sys.modules if n.split('.')[0] in {'persistence', 'sqlalchemy', 'evidence', "
                "'evaluation', 'evidence_packet', 'evidence_synthesis', 'market_intelligence', 'openai', 'requests', "
                "'redis', 'exchange_calendars', 'market_context', 'technical'} or n in {'options_data.massive', "
                "'options_data.runner', 'options_data.provider', 'options_data.config', 'market_data.http'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


@unittest.skipUnless(all(os.path.exists(p) for p in LIVE.values()), "local Phase 9C live snapshots not present")
class LiveReplayTests(unittest.TestCase):
    """Local /tmp artifacts only (never committed); metadata assertions only, no prices."""

    @classmethod
    def setUpClass(cls):
        from market_data.calendar import default_calendar
        cls.calendar = default_calendar()
        cls.results = {}
        for sym, path in LIVE.items():
            with open(path, encoding="utf-8") as handle:
                snap = json.load(handle)
            started = time.perf_counter()
            intelligence = build(snap, calendar=cls.calendar)
            cls.results[sym] = (snap, intelligence, time.perf_counter() - started)

    def test_current_plan_realities(self):
        for sym, (snap, i, _) in self.results.items():
            with self.subTest(symbol=sym):
                comp = {g.group: {c.key: c.count for c in g.counts} for g in i.chain_completeness.status_counts}
                self.assertEqual(set(comp["quote"]), {"unavailable"})
                self.assertEqual(set(comp["trade"]), {"unavailable"})
                self.assertEqual(i.underlying.price_status, "unavailable")
                self.assertEqual(set(comp["open_interest"]), {"present"})
                self.assertIn("missing", comp["implied_volatility"])
                sessions = {c.key for c in i.chain_completeness.day_session_relation_counts}
                self.assertIn("current_session", sessions)
                self.assertTrue(sessions - {"current_session"})
                self.assertEqual({c.strike_relation for c in i.contracts}, {"unavailable"})
                self.assertEqual(i.chain_completeness.contract_count, len(snap["contracts"]))

    def test_replay_deterministic_and_verifiable(self):
        for sym, (snap, i, _) in self.results.items():
            with self.subTest(symbol=sym):
                again = build(snap, calendar=self.calendar)
                self.assertEqual(again.options_intelligence_id, i.options_intelligence_id)
                verify_against_snapshot(i, snap, calendar=self.calendar)


class LargeChainTests(unittest.TestCase):
    def test_ten_thousand_contracts(self):
        expirations = [(date(2026, 10, 2) + timedelta(weeks=n)).isoformat() for n in range(25)]
        records = [chain_record("META", e, kind, 400 + 2.5 * n, iv=n % 3 != 0, greeks=n % 3 != 0)
                   for e in expirations for kind in ("call", "put") for n in range(200)]
        snap = live_snapshot(records)
        started = time.perf_counter()
        intelligence = build(snap, calendar=CAL)
        elapsed = time.perf_counter() - started
        self.assertEqual((len(intelligence.contracts), len(intelligence.expirations)), (10000, 25))
        self.assertLess(elapsed, 180)  # Characterization guard only.
        self.assertGreater(len(canonical_json(intelligence.to_dict())), 1_000_000)


FORBIDDEN = {"score", "confidence", "severity", "prediction", "predict", "forecast", "recommendation", "recommend",
             "signal", "buy", "sell", "best", "rank", "ranking", "select", "selection", "target", "stop", "probability",
             "tradeable", "priority", "cheap", "expensive", "sentiment", "itm", "otm", "atm", "moneyness"}
FORBIDDEN_PHRASES = ("expected_return", "liquidity_grade", "wide_spread", "unusual")


def words(identifier):
    return set(re.split(r"[^a-z0-9]+", identifier.lower())) - {""}


class ForbiddenSemanticsTests(unittest.TestCase):
    def test_output_keys_and_vocabularies(self):
        for name in cases.names():
            stack = [golden(name)]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertFalse(words(key) & FORBIDDEN, (name, key))
                        self.assertFalse(any(p in key for p in FORBIDDEN_PHRASES), (name, key))
                        stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)
        for value in (*rules.QUOTE_STATES, *rules.ACTIVITY_STATES, *rules.SESSION_RELATIONS, *rules.STRIKE_RELATIONS,
                      *rules.ATTENTION_CODES, *rules.ATTENTION_CATEGORIES):
            self.assertFalse(words(value) & FORBIDDEN, value)
            self.assertFalse(any(p in value for p in FORBIDDEN_PHRASES), value)

    def test_code_identifiers(self):
        for module in PURE:
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                names = [node.id] if isinstance(node, ast.Name) else [node.attr] if isinstance(node, ast.Attribute) \
                    else [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) \
                    else [node.arg] if isinstance(node, ast.arg) else []
                for name in names:
                    self.assertFalse(words(name) & FORBIDDEN, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
