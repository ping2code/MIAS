"""Phase 12B alert eligibility: the three closed v1 rules, the AlertEvent contract, a tamper matrix, re-derivation,
determinism and the pure-core boundary. Synthetic and replay inputs only. Nothing here imports the legacy alert
modules, sends anything, or touches Redis, a database or the network."""
import ast
from copy import deepcopy
import inspect
import os
import subprocess
import sys
import tempfile
import unittest

from alert_engine import builder, canonical, model as m, rules as r, validation
from alert_engine.builder import market_pattern_changed, setup_available, setup_invalidated
from alert_engine.canonical import canonical_json, content_id
from alert_engine.validation import AlertInputError, validated_alert, verify_alert
from tests import setup_evaluation_cases as sc
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = (builder, canonical, m, r, validation)


def reseal(data, key="alert_id"):
    data[key] = content_id({k: v for k, v in data.items() if k != key})
    return data


def mi_pair(previous="all_bullish", current="higher_aligned_5m_opposed", previous_seconds=0, current_seconds=86400):
    prev = ts.market_intelligence(previous) if previous_seconds == 0 else ts.later_mi(previous, seconds=previous_seconds)
    return prev, ts.later_mi(current, seconds=current_seconds)


class SetupAvailableTests(unittest.TestCase):
    def test_fires_once_per_assessment(self):
        assessment = sc.setup()
        event = setup_available(assessment)
        self.assertEqual(len(assessment["candidates"]), 5)
        self.assertEqual((event.alert_code, event.subject.kind, event.subject.assessment_id),
                         ("setup_available", "setup", assessment["assessment_id"]))
        self.assertEqual(event.facts, dict(candidate_count=5, eligible_side="call", market_bias="bullish", symbol="META"))
        self.assertEqual(event.as_of, assessment["inputs"]["assessment_as_of"])
        self.assertEqual([x.role for x in event.source_refs], ["current"])
        self.assertIsNone(event.transition)

    def test_no_setup_does_not_fire(self):
        global_gate, _ = ts.screened([ts.record()], max_input_gap_seconds=0)
        screened, _ = ts.screened([ts.record()], max_dte=10)
        for assessment in (global_gate, screened):
            self.assertEqual(assessment.outcome.status, "no_setup")
            self.assertIsNone(setup_available(assessment))

    def test_candidates_never_become_ranking(self):
        assessment = sc.setup()
        text = canonical_json(setup_available(assessment).to_dict())
        for candidate in assessment["candidates"]:
            source = candidate["source"]
            for value in (source["contract_id"], source["provider_symbol"], source["expiration"]):
                self.assertNotIn(value, text)
        one, _ = ts.screened([ts.record()])
        self.assertEqual(setup_available(one).facts["candidate_count"], 1)
        reordered = sc.setup()
        reordered["candidates"].reverse()                          # tampering is an input error, never re-ranking
        with self.assertRaises(AlertInputError):
            setup_available(reordered)


class SetupInvalidatedTests(unittest.TestCase):
    def test_invalidated_fires(self):
        check = sc.check("all_bearish")
        event = setup_invalidated(check)
        self.assertEqual((event.alert_code, event.subject.assessment_id, event.subject.kind),
                         ("setup_invalidated", check["setup_ref"]["assessment_id"], "setup"))
        self.assertEqual([x.role for x in event.source_refs], ["current", "setup"])
        self.assertEqual(event.as_of, check["market_intelligence_ref"]["as_of"])
        self.assertEqual(event.facts, dict(observed_pattern="all_bearish", observed_technical_status="available",
                                           required_pattern="all_bullish", side="call", symbol="META"))

    def test_only_the_upstream_invalidated_result(self):
        for name, result in (("all_bullish", "holds"), ("missing_1d", "not_evaluable"),
                             ("insufficient_data_state", "not_evaluable"), ("higher_aligned_5m_opposed", "invalidated")):
            check = sc.check(name, seconds=2 * 86400)
            with self.subTest(case=name):
                self.assertEqual(check["result"], result)
                self.assertEqual(setup_invalidated(check) is not None, result == "invalidated")

    def test_tampered_check_rejected(self):
        check = sc.check("all_bearish")
        check["result"] = "holds"
        with self.assertRaises(AlertInputError):
            setup_invalidated(check)


class MarketPatternChangedTests(unittest.TestCase):
    def test_a_to_b_and_b_to_a(self):
        prev, cur = mi_pair("all_bullish", "higher_aligned_5m_opposed")
        event = market_pattern_changed(prev, cur)
        self.assertEqual((event.transition.previous, event.transition.current), ("all_bullish", "opposed"))
        self.assertEqual((event.subject.kind, event.subject.assessment_id, event.facts),
                         ("symbol", None, dict(elapsed_seconds=86400, symbol="META")))
        self.assertEqual([x.role for x in event.source_refs], ["current", "previous"])
        self.assertEqual(event.as_of, cur["synthesis_ref"]["as_of"])
        back = market_pattern_changed(*mi_pair("higher_aligned_5m_opposed", "all_bullish", 86400, 2 * 86400))
        self.assertEqual((back.transition.previous, back.transition.current), ("opposed", "all_bullish"))
        self.assertNotEqual(back.alert_id, event.alert_id)

    def test_no_alert_cases(self):
        self.assertIsNone(market_pattern_changed(*mi_pair("all_bullish", "positive_market_return")))  # same pattern,
        # other Phase 8 differences (market context) only
        prev, cur = mi_pair("all_bullish", "higher_aligned_5m_opposed")
        cur["synthesis_ref"]["synthesis_rules_version"] = "phase7g-rules-v2"            # not comparable (Phase 8)
        self.assertIsNone(market_pattern_changed(prev, reseal(cur, "intelligence_id")))

    def test_malformed_pairs_rejected(self):
        prev = ts.market_intelligence("all_bullish")
        for label, pair, message in (
                ("different symbols", (prev, ts.later_mi("higher_aligned_5m_opposed", symbol="NVDA")),
                 "previous and current market intelligence symbols do not match"),
                ("reversed as_of", mi_pair("all_bullish", "higher_aligned_5m_opposed", 86400, -1),
                 "previous market intelligence as_of must be earlier than the current as_of"),
                ("equal as_of", (prev, ts.later_mi("higher_aligned_5m_opposed", seconds=0)),
                 "previous market intelligence as_of must be earlier than the current as_of"),
                ("same object", (prev, prev), "previous and current market intelligence are the same object")):
            with self.subTest(case=label), self.assertRaises(AlertInputError) as caught:
                market_pattern_changed(*pair)
            self.assertEqual(str(caught.exception), message)
        tampered = ts.later_mi("higher_aligned_5m_opposed")
        tampered["timeframe_structure"]["pattern"] = "all_bearish"
        with self.assertRaises(AlertInputError):
            market_pattern_changed(prev, tampered)


class ContractTests(unittest.TestCase):
    def test_schema(self):
        for event in (setup_available(sc.setup()), setup_invalidated(sc.check()), market_pattern_changed(*mi_pair())):
            data = event.to_dict()
            with self.subTest(code=event.alert_code):
                self.assertEqual(list(data), list(m.ALERT_FIELDS))
                self.assertEqual((data["alert_format_version"], data["rules_version"]), ("phase12-v1", "phase12-rules-v1"))
                self.assertEqual(data["alert_id"], content_id({k: v for k, v in data.items() if k != "alert_id"}))
                self.assertEqual(data["provenance"]["pointer_version"], "phase12-pointer-v1")
                self.assertEqual(validated_alert(data), data)
                self.assertEqual([t["rule"] for t in data["decision_trace"]], [s for s, _ in r.SHAPES[data["alert_code"]]["trace"]])
                text = canonical_json(data)
                for banned in ("generated_at", "delivered", "attempt", "telegram", "channel", "message_id", "retry",
                               "severity", "score", "rank", "best", "recommend", "sizing", "target", "stop", "ai_", "http"):
                    self.assertNotIn(banned, text)

    def test_closed_codes(self):
        self.assertEqual(r.ALERT_CODES, ("setup_available", "setup_invalidated", "market_pattern_changed"))

    def test_upstream_constants_pinned(self):
        from evidence_synthesis import rules as syn
        from trade_setup import rules as tr
        self.assertEqual(r.PATTERNS, syn.PATTERNS)
        self.assertEqual(r.ASSESSMENT_VERSIONS, (tr.ASSESSMENT_FORMAT_VERSION, tr.RULES_VERSION))
        self.assertEqual(r.INVALIDATION_VERSIONS, (tr.INVALIDATION_FORMAT_VERSION, tr.INVALIDATION_RULES_VERSION))
        self.assertEqual(r.MI_VERSIONS, (tr.MI_FORMAT_VERSION, tr.MI_RULES_VERSION))
        self.assertEqual(r.TECHNICAL_STATUSES, tr.MI_TECHNICAL_STATUSES)
        self.assertEqual(r.INVALIDATED, tr.INVALIDATED)
        self.assertEqual(r.SETUP_CANDIDATES, tr.SETUP_CANDIDATES)
        data = setup_available(sc.setup()).to_dict()
        from trade_setup.canonical import canonical_json as upstream
        self.assertEqual(canonical_json(data), upstream(data))


def _events():
    return dict(setup_available=setup_available(sc.setup()).to_dict(),
                setup_invalidated=setup_invalidated(sc.check()).to_dict(),
                market_pattern_changed=market_pattern_changed(*mi_pair()).to_dict())


STRUCTURAL = [
    ("missing field", "setup_available", lambda d: d.pop("facts"), "alert must have exactly the 11 phase12-v1 keys"),
    ("extra field", "setup_available", lambda d: d.update(severity="high"), "alert must have exactly the 11 phase12-v1 keys"),
    ("format version", "setup_available", lambda d: d.update(alert_format_version="phase12-v2"), "unsupported alert format version"),
    ("rules version", "setup_available", lambda d: d.update(rules_version="phase12-rules-v2"), "unsupported alert rules version"),
    ("unknown code", "setup_available", lambda d: d.update(alert_code="setup_upgraded"), "alert code is not supported"),
    ("subject kind", "setup_available", lambda d: d["subject"].update(kind="symbol"), "alert subject is malformed"),
    ("subject setup id", "setup_available", lambda d: d["subject"].update(assessment_id="sha256:" + "0" * 64),
     "alert subject does not match the setup source"),
    ("symbol subject with setup", "market_pattern_changed", lambda d: d["subject"].update(assessment_id="sha256:" + "0" * 64),
     "alert subject is malformed"),
    ("wrong role", "setup_available", lambda d: d["source_refs"][0].update(role="previous"), "alert source_refs roles are malformed"),
    ("wrong object kind", "setup_invalidated", lambda d: d["source_refs"][1].update(object_kind="market_intelligence"),
     "alert source_ref is malformed"),
    ("bad source id", "setup_available", lambda d: d["source_refs"][0].update(id="x"), "alert source_ref is malformed"),
    ("source version", "market_pattern_changed", lambda d: d["source_refs"][0].update(rules_version="phase8-rules-v2"),
     "alert source_ref is malformed"),
    ("reordered refs", "market_pattern_changed", lambda d: d["source_refs"].reverse(), "alert source_refs roles are malformed"),
    ("as_of mismatch", "setup_available", lambda d: d.update(as_of="2026-09-30T14:46:00+00:00"),
     "alert as_of must equal the current source as_of"),
    ("previous not earlier", "market_pattern_changed", lambda d: d["source_refs"][1].update(as_of=d["as_of"]),
     "alert previous source must be a distinct, earlier object"),
    ("illegal transition", "market_pattern_changed", lambda d: d["transition"].update(current=d["transition"]["previous"]),
     "alert transition is illegal"),
    ("unknown pattern", "market_pattern_changed", lambda d: d["transition"].update(current="mostly_bullish"),
     "alert transition is illegal"),
    ("transition on setup", "setup_available", lambda d: d.update(transition=dict(previous="opposed", current="all_bullish")),
     "alert transition is illegal for this alert code"),
    ("extra fact", "setup_available", lambda d: d["facts"].update(best_strike="700"), "alert facts do not match the alert code"),
    ("fact symbol", "setup_available", lambda d: d["facts"].update(symbol="NVDA"), "alert facts symbol does not match the subject"),
    ("side vs bias", "setup_available", lambda d: d["facts"].update(eligible_side="put"), "alert facts are illegal for setup_available"),
    ("zero candidates", "setup_available", lambda d: d["facts"].update(candidate_count=0), "alert facts are illegal for setup_available"),
    ("required pattern", "setup_invalidated", lambda d: d["facts"].update(required_pattern="all_bearish"),
     "alert facts are illegal for setup_invalidated"),
    ("observed holds", "setup_invalidated", lambda d: d["facts"].update(observed_pattern="all_bullish"),
     "alert facts are illegal for setup_invalidated"),
    ("elapsed", "market_pattern_changed", lambda d: d["facts"].update(elapsed_seconds=1), "alert facts are illegal for market_pattern_changed"),
    ("trace", "setup_available", lambda d: d["decision_trace"].reverse(), "alert decision_trace is inconsistent"),
    ("trace result", "setup_invalidated", lambda d: d["decision_trace"][1].update(result="fail"), "alert decision_trace is inconsistent"),
    ("provenance", "market_pattern_changed", lambda d: d["provenance"].update(source_ids=[]), "alert provenance is inconsistent"),
]


class TamperTests(unittest.TestCase):
    def test_structural_matrix(self):
        events = _events()
        for label, code, mutate, message in STRUCTURAL:
            data = deepcopy(events[code])
            mutate(data)
            with self.subTest(case=label), self.assertRaises(AlertInputError) as caught:
                validated_alert(reseal(data))
            self.assertEqual(str(caught.exception), message)

    def test_id_tamper(self):
        data = _events()["setup_available"]
        data["facts"]["candidate_count"] = 4
        with self.assertRaises(AlertInputError) as caught:
            validated_alert(data)
        self.assertEqual(str(caught.exception), "alert id does not match its body (tampered or corrupt)")

    def test_resealed_forgeries_need_rederivation(self):
        assessment = sc.setup()
        forged = setup_available(assessment).to_dict()
        forged["facts"]["candidate_count"] = 4
        forged = reseal(forged)
        validated_alert(forged)
        with self.assertRaises(AlertInputError) as caught:
            verify_alert(forged, assessment=assessment)
        self.assertEqual(str(caught.exception), "alert does not match its inputs at facts")
        prev, cur = mi_pair()
        forged = market_pattern_changed(prev, cur).to_dict()
        forged["transition"] = dict(previous="all_bullish", current="all_bearish")
        forged = reseal(forged)
        validated_alert(forged)
        with self.assertRaises(AlertInputError):
            verify_alert(forged, previous=prev, current=cur)
        check = sc.check("all_bullish", 2 * 86400)               # holds: the rule does not fire on these inputs
        forged = setup_invalidated(sc.check()).to_dict()
        with self.assertRaises(AlertInputError) as caught:
            verify_alert(forged, check=check)
        self.assertEqual(str(caught.exception), "alert does not re-derive: its rule does not fire on these inputs")

    def test_verify_accepts_the_real_events(self):
        assessment, check, (prev, cur) = sc.setup(), sc.check(), mi_pair()
        self.assertTrue(verify_alert(setup_available(assessment).to_dict(), assessment=assessment))
        self.assertTrue(verify_alert(setup_invalidated(check).to_dict(), check=check))
        self.assertTrue(verify_alert(market_pattern_changed(prev, cur).to_dict(), previous=prev, current=cur))


class DeterminismTests(unittest.TestCase):
    def test_repeats_and_dict_order(self):
        expected = {k: canonical_json(v) for k, v in _events().items()}
        for _ in range(50):
            self.assertEqual({k: canonical_json(v) for k, v in _events().items()}, expected)

        def rev(v):
            if isinstance(v, dict):
                return {k: rev(v[k]) for k in reversed(list(v))}
            return [rev(x) for x in v] if isinstance(v, list) else v
        prev, cur = mi_pair()
        self.assertEqual(canonical_json(setup_available(rev(sc.setup())).to_dict()), expected["setup_available"])
        self.assertEqual(canonical_json(setup_invalidated(rev(sc.check())).to_dict()), expected["setup_invalidated"])
        self.assertEqual(canonical_json(market_pattern_changed(rev(prev), rev(cur)).to_dict()), expected["market_pattern_changed"])

    def test_fresh_processes(self):
        code = ("from tests.test_alert_eligibility import _events\n"
                "e = _events()\nprint(' '.join(e[k]['alert_id'] for k in sorted(e)))")
        expected = " ".join(v["alert_id"] for _, v in sorted(_events().items()))
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "11", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "7", "LANG": "C", "LC_ALL": "C", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class BoundaryTests(unittest.TestCase):
    ALLOWED = {"alert_engine.canonical", "alert_engine.rules", "alert_engine.model", "alert_engine.validation",
               "alert_engine.builder", "alert_engine", "trade_setup.validation", "trade_setup.invalidation"}

    def test_core_imports_and_calls(self):
        stdlib = set(sys.stdlib_module_names) - {"os", "io", "socket", "subprocess", "tempfile", "pathlib", "shutil",
                                                  "sqlite3", "urllib", "http", "time", "random"}
        for module in CORE:
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], stdlib, module.__name__)
                elif isinstance(node, ast.ImportFrom):
                    self.assertTrue(node.module.split(".")[0] in stdlib or node.module in self.ALLOWED,
                                    (module.__name__, node.module))
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(name, {"open", "print", "now", "utcnow", "today", "getenv", "time", "sleep"},
                                     (module.__name__, name))
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("environ", "argv"), module.__name__)

    def test_runtime_imports(self):
        code = ("import sys, alert_engine.builder, alert_engine.validation\n"
                "_ = alert_engine.validation.validated_assessment, alert_engine.validation.validated_check\n"
                "import trade_setup.validation, trade_setup.invalidation\n"
                "bad = {'requests', 'redis', 'sqlalchemy', 'dotenv', 'shared', 'collector', 'orchestrator', 'openai',"
                " 'analyzer', 'persistence', 'market_data', 'options_data', 'options_intelligence', 'market_intelligence',"
                " 'evidence', 'evaluation', 'socket'}\n"
                "legacy = {'alert_engine.decision_engine', 'alert_engine.formatter', 'alert_engine.telegram_notifier'}\n"
                "print(sorted(({n.split('.')[0] for n in sys.modules} & bad) | (set(sys.modules) & legacy)))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_no_evaluative_identifiers(self):
        banned = {"rank", "ranking", "best", "score", "severity", "recommend", "recommendation", "sizing", "target",
                  "stop", "telegram", "delivery", "delivered", "retry", "channel", "ai"}
        for module in CORE:
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
                if isinstance(name, str):
                    self.assertFalse(set(name.lower().split("_")) & banned, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
