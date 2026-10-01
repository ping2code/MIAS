"""Phase 11 SetupEvaluation: protocol, schedule and horizons, window, quote and missing-contract statuses,
returns and multipliers, candidate preservation, invalidation relation, contract, tamper, re-derivation,
determinism and boundaries. Synthetic and replay inputs only; no production protocol and no prospective data."""
import ast
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
import tempfile
import unittest

import setup_evaluation
from setup_evaluation import model as m, rules as r
from setup_evaluation.builder import evaluate, verify_evaluation
from setup_evaluation.canonical import canonical_json, content_id
from setup_evaluation.protocol import ProtocolError, make_test_protocol, validated_protocol
from setup_evaluation.schedule import ScheduleError, make_schedule, resolve
from setup_evaluation.validation import SetupEvaluationInputError, premium_return, validated_evaluation
from tests import setup_evaluation_cases as c
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Z = c.Z


def reseal(data, key):
    data[key] = content_id({k: v for k, v in data.items() if k != key})
    return data


def run(snapshot=None, horizon="session_1", checks=(), assessment=None, schedule=None, protocol=None):
    return evaluate(assessment or c.setup(), snapshot or c.snapshot(), schedule or c.schedule(),
                    protocol or c.protocol(), horizon, checks).to_dict()


def outcome(evaluation, strike):
    return next(o for o in evaluation["candidate_outcomes"] if o["contract_id"].endswith(f"C00{strike}000"))


class EvalTestCase(unittest.TestCase):
    def assertInputError(self, message, **kw):
        with self.assertRaises(SetupEvaluationInputError) as caught:
            run(**kw)
        self.assertEqual(str(caught.exception), message)


class ProtocolTests(unittest.TestCase):
    def test_test_protocol(self):
        p = make_test_protocol().to_dict()
        self.assertEqual((p["protocol_format_version"], p["purpose"], p["prospective_start"]),
                         ("phase11-evaluation-protocol-v1", "test", None))
        self.assertEqual(list(p), list(m.PROTOCOL_FIELDS))
        self.assertEqual(validated_protocol(p), p)
        self.assertEqual((p["horizons"], p["window_seconds"], p["mark"], p["return_places"], p["return_rounding"]),
                         (["session_1", "session_5"], 1800, "liquidation_reference_bid", 8, "ROUND_HALF_EVEN"))

    def test_rejections(self):
        p = make_test_protocol().to_dict()
        tampered = dict(p, window_seconds=3600)
        for data, message in (
                (tampered, "protocol_id does not match the protocol (tampered or corrupt)"),
                (reseal(dict(tampered), "protocol_id"), "protocol semantics do not match the frozen phase11-rules-v1 semantics"),
                (reseal(dict(p, horizons=["session_1", "session_5", "session_10"]), "protocol_id"),
                 "protocol semantics do not match the frozen phase11-rules-v1 semantics"),
                (reseal(dict(p, prospective_start="2026-10-01T00:00:00+00:00"), "protocol_id"),
                 "a test protocol has no prospective_start"),
                (reseal(dict(p, purpose="research"), "protocol_id"), "protocol purpose is not supported")):
            with self.subTest(message=message), self.assertRaises(ProtocolError) as caught:
                validated_protocol(data)
            self.assertEqual(str(caught.exception), message)

    def test_production_protocol_not_activated(self):
        self.assertIsNone(r.PRODUCTION_PROTOCOL_ID)
        self.assertIsNone(r.PRODUCTION_PROSPECTIVE_START)
        production = reseal(dict(make_test_protocol().to_dict(), purpose="production",
                                 prospective_start="2026-10-05T13:30:00+00:00"), "protocol_id")
        with self.assertRaises(ProtocolError) as caught:
            validated_protocol(production)
        self.assertEqual(str(caught.exception), "production evaluation protocol is not activated")
        with self.assertRaises(SetupEvaluationInputError) as caught:
            run(protocol=production)
        self.assertEqual(str(caught.exception), "protocol is invalid: production evaluation protocol is not activated")


class ScheduleTests(unittest.TestCase):
    def test_schedule_contract(self):
        s = c.schedule()
        self.assertEqual((s["schedule_format_version"], s["calendar"]), ("phase11-sessions-v1", "XNYS"))
        self.assertEqual(s["schedule_id"], content_id({k: v for k, v in s.items() if k != "schedule_id"}))
        for mutate, message in (
                (lambda d: d["sessions"].reverse(), "schedule sessions must be strictly increasing"),
                (lambda d: d["sessions"][0].update(regular_open="2026-09-28T09:30:00-04:00"),
                 "schedule instants must be canonical UTC"),
                (lambda d: d["sessions"][0].update(regular_close=d["sessions"][0]["regular_open"]),
                 "schedule session opens after it closes"),
                (lambda d: d.update(calendar="XLON"), "schedule calendar must be XNYS")):
            data = c.schedule()
            mutate(data)
            with self.subTest(message=message), self.assertRaises(ScheduleError) as caught:
                from setup_evaluation.schedule import validated_schedule
                validated_schedule(reseal(data, "schedule_id"))
            self.assertEqual(str(caught.exception), message)

    def test_horizon_examples(self):
        s = c.schedule()
        for label, as_of, one, five in (
                ("Wednesday 14:00 ET", Z(2026, 9, 30, 18), "2026-10-01", "2026-10-07"),
                ("Wednesday after close", Z(2026, 9, 30, 22), "2026-10-01", "2026-10-07"),
                ("Thursday premarket", Z(2026, 10, 1, 12), "2026-10-01", "2026-10-07"),
                ("Friday after close", Z(2026, 10, 2, 21), "2026-10-05", "2026-10-09"),
                ("exactly at an open", Z(2026, 10, 1, 13, 30), "2026-10-02", "2026-10-08")):
            with self.subTest(label=label):
                self.assertEqual((resolve(s, as_of, 1)["session_date"], resolve(s, as_of, 5)["session_date"]),
                                 (one, five))

    def test_early_close_and_holiday(self):
        s = c.schedule()
        session = resolve(s, Z(2026, 11, 25, 22), 1)          # Wednesday after close; Thursday is Thanksgiving
        self.assertEqual((session["session_date"], session["regular_close"]),
                         ("2026-11-27", "2026-11-27T18:00:00+00:00"))   # early close, 13:00 ET

    def test_coverage(self):
        late = c.schedule(start=date(2026, 10, 2))
        with self.assertRaises(ScheduleError) as caught:
            resolve(late, Z(2026, 9, 30, 18), 1)
        self.assertIn("does not cover the assessment time", str(caught.exception))
        short = c.schedule(end=date(2026, 10, 5))
        with self.assertRaises(ScheduleError) as caught:
            resolve(short, Z(2026, 9, 30, 18), 5)
        self.assertEqual(str(caught.exception), "schedule does not cover the horizon")


class InputTests(EvalTestCase):
    def test_valid_setup(self):
        e = run()
        self.assertEqual((e["setup_ref"]["candidate_count"], e["setup_ref"]["side"], e["horizon"]["name"]), (5, "call", "session_1"))

    def test_no_setup_and_tamper(self):
        no_setup, _ = ts.screened([ts.record()], max_dte=10)
        self.assertInputError("evaluation requires a setup_candidates assessment", assessment=no_setup.to_dict())
        tampered = c.setup()
        tampered["candidates"][0]["derived"]["entry_reference_ask"] = "1"
        self.assertInputError("assessment id does not match its body (tampered or corrupt)", assessment=tampered)
        wrong = reseal(dict(c.setup(), rules_version="phase10-rules-v2"), "assessment_id")
        self.assertInputError("unsupported assessment version", assessment=wrong)

    def test_symbol_and_contract_mismatch(self):
        nvda = ts.chain_record("NVDA", ts.EXPIRY, "call", 190, quote=c.quote_at())
        from options_data.normalization import assemble
        from tests.options_snapshot_cases import PROVENANCE
        snap = assemble("NVDA", c.INSIDE, [nvda], provenance=dict(PROVENANCE, truncated=False),
                        calendar_state="regular").to_dict()
        self.assertInputError("snapshot underlying does not match the setup symbol", snapshot=snap)
        snap = c.snapshot()
        snap["contracts"][0]["identity"]["strike"] = "699"
        from options_data.canonical import content_id as snapshot_id
        snap["snapshot_id"] = snapshot_id({k: v for k, v in snap.items() if k != "snapshot_id"})
        self.assertInputError("snapshot contract identity does not match the assessment candidate", snapshot=snap)
        bad = c.snapshot()
        bad["as_of"] = c.WINDOW_START.isoformat()
        self.assertInputError("snapshot id does not match its body (tampered or corrupt)", snapshot=bad)

    def test_checks_must_belong(self):
        other, _ = ts.screened([ts.record(705)])
        from trade_setup.invalidation import check_invalidation
        foreign = check_invalidation(other, ts.later_mi("all_bearish")).to_dict()
        self.assertInputError("invalidation check does not belong to the assessment", checks=[foreign])
        self.assertInputError("invalidation checks are duplicated", checks=[c.check(), c.check()])


class WindowTests(EvalTestCase):
    def test_boundaries(self):
        self.assertEqual(run(snapshot=c.snapshot(as_of=c.WINDOW_START, quotes={700: c.quote_at(observed_at=c.WINDOW_START)}))
                         ["observation_ref"]["as_of"], c.WINDOW_START.isoformat())                  # lower bound included
        self.assertEqual(run(snapshot=c.snapshot(as_of=c.SESSION_1_CLOSE))["observation_ref"]["as_of"],
                         c.SESSION_1_CLOSE.isoformat())                                              # exact close included
        message = "snapshot is outside the horizon observation window"
        self.assertInputError(message, snapshot=c.snapshot(as_of=c.WINDOW_START - timedelta(microseconds=1)))
        self.assertInputError(message, snapshot=c.snapshot(as_of=c.SESSION_1_CLOSE + timedelta(microseconds=1),
                                                           calendar_state="post"))

    def test_session_must_match(self):
        self.assertInputError("snapshot session is not the regular target session",
                              snapshot=c.snapshot(calendar_state="post"))
        self.assertInputError("snapshot is outside the horizon observation window", horizon="session_5")

    def test_session_5(self):
        snap = c.snapshot(as_of=c.SESSION_5_CLOSE - timedelta(minutes=5),
                          quotes={s: c.quote_at(observed_at=c.SESSION_5_CLOSE - timedelta(minutes=6))
                                  for s in (690, 700, 710, 720, 730)})
        e = run(snapshot=snap, horizon="session_5")
        self.assertEqual((e["horizon"]["target_session_date"], e["horizon"]["target_close"]),
                         ("2026-10-07", c.SESSION_5_CLOSE.isoformat()))
        self.assertEqual(outcome(e, 730)["outcome_status"], "expired_before_target")   # expired 2026-10-02

    def test_expiration_on_target_date_is_evaluated(self):
        setup, _ = ts.screened([ts.record(700, expiration="2026-10-01")])
        snap = c.snapshot(quotes={s: False for s in c.CANDIDATES},
                          extra=[ts.record(700, expiration="2026-10-01", quote=c.quote_at())])
        e = run(snapshot=snap, assessment=setup.to_dict())
        self.assertEqual(e["candidate_outcomes"][0]["outcome_status"], "observed")


class QuoteStatusTests(EvalTestCase):
    def status(self, quote, **kw):
        return outcome(run(snapshot=c.snapshot(quotes={700: quote}, **kw)), 700)["outcome_status"]

    def test_statuses(self):
        early = c.WINDOW_START - timedelta(microseconds=1)
        for label, quote, expected in (
                ("complete", c.quote_at(), "observed"),
                ("locked", c.quote_at("10.6", "10.6"), "observed"),
                ("zero bid", c.quote_at("0", "0.05"), "observed"),
                ("bid missing", c.quote_at(bid=None), "quote_not_two_sided"),
                ("ask missing", c.quote_at(ask=None), "quote_not_two_sided"),
                ("crossed", c.quote_at("10.8", "10.6"), "quote_crossed"),
                ("no quote", None, "quote_unavailable"),
                ("after cutoff", c.quote_at(observed_at=c.INSIDE + timedelta(seconds=1)), "quote_after_cutoff"),
                ("stale by 1 us", c.quote_at(observed_at=early), "quote_stale"),
                ("window start", c.quote_at(observed_at=c.WINDOW_START), "observed"),
                ("untimed", {"bid": "10.5", "ask": "10.7"}, "quote_timing_unverified")):
            with self.subTest(label=label):
                self.assertEqual(self.status(quote), expected)

    def test_unavailable_group(self):
        from options_data.normalization import assemble
        from tests.options_snapshot_cases import PROVENANCE
        records = [c.record(s) for s in (690, 700, 710, 720)]
        for rec in records:
            rec.pop("quote", None)
        snap = assemble("META", c.INSIDE, records,
                        provenance=dict(PROVENANCE, truncated=False), calendar_state="regular",
                        unavailable_groups=("quote", "trade")).to_dict()
        self.assertEqual(outcome(run(snapshot=snap), 700)["outcome_status"], "quote_unavailable")

    def test_only_observed_carries_values(self):
        e = run(snapshot=c.snapshot(quotes={700: c.quote_at(bid=None)}))
        o = outcome(e, 700)
        self.assertEqual((o["liquidation_reference_bid"], o["premium_change"], o["premium_return"], o["return_status"],
                          o["dollar_change_per_contract"], o["dollar_status"]),
                         (None, None, None, "not_observed", None, "not_observed"))
        self.assertIsNotNone(o["observed_quote"])


class MissingContractTests(EvalTestCase):
    def test_complete_vs_truncated(self):
        complete = run(snapshot=c.snapshot(quotes={700: False}))
        truncated = run(snapshot=c.snapshot(quotes={700: False}, truncated=True))
        self.assertEqual(outcome(complete, 700)["outcome_status"], "contract_absent")
        self.assertEqual(outcome(truncated, 700)["outcome_status"], "observation_incomplete")
        self.assertIsNone(outcome(complete, 700)["observed_quote"])


class ReturnTests(EvalTestCase):
    def values(self, bid, **kw):
        o = outcome(run(snapshot=c.snapshot(quotes={700: c.quote_at(bid, "20")}, **kw)), 700)
        return o["premium_change"], o["premium_return"], o["dollar_change_per_contract"]

    def test_signs(self):
        self.assertEqual(self.values("10.5"), ("0.4", "0.03960396", "40"))          # entry reference 10.1
        self.assertEqual(self.values("9.6"), ("-0.5", "-0.04950495", "-50"))
        self.assertEqual(self.values("10.1"), ("0", "0.00000000", "0"))
        e = run(snapshot=c.snapshot(quotes={690: c.quote_at("10.5", "20"), 700: c.quote_at("9.6", "20"),
                                            710: c.quote_at("10.1", "20")}))
        self.assertEqual(e["summary"]["premium_change_sign_counts"], dict(positive=2, negative=1, zero=1))

    def test_quantization(self):
        self.assertEqual(premium_return(Decimal("0.000000025"), Decimal("1")), "0.00000002")   # half-even down
        self.assertEqual(premium_return(Decimal("0.000000035"), Decimal("1")), "0.00000004")   # half-even up
        self.assertEqual(premium_return(Decimal("-0.000000001"), Decimal("1")), "0.00000000")  # no negative zero
        self.assertEqual(premium_return(Decimal("1"), Decimal("3")), "0.33333333")

    def test_zero_entry_and_multipliers(self):
        e = run()
        zero, no_shares = outcome(e, 720), outcome(e, 710)
        self.assertEqual((zero["entry_reference_ask"], zero["premium_return"], zero["return_status"]),
                         ("0", None, "entry_reference_zero"))
        self.assertEqual((no_shares["shares_per_contract"], no_shares["dollar_change_per_contract"],
                          no_shares["dollar_status"]), (None, None, "multiplier_unavailable"))
        changed = outcome(run(snapshot=c.snapshot(shares={700: 10})), 700)
        self.assertEqual((changed["dollar_change_per_contract"], changed["dollar_status"], changed["premium_change"]),
                         (None, "contract_terms_changed", "0.4"))


class CandidateSetTests(EvalTestCase):
    def test_every_candidate_in_canonical_order(self):
        e, setup = run(), c.setup()
        self.assertEqual([o["contract_id"] for o in e["candidate_outcomes"]],
                         [x["source"]["contract_id"] for x in setup["candidates"]])
        self.assertEqual(e["summary"]["candidate_count"], len(setup["candidates"]))

    def test_no_retrospective_omission_or_reordering(self):
        setup, snap, sched, proto = c.setup(), c.snapshot(), c.schedule(), c.protocol()
        real = evaluate(setup, snap, sched, proto, "session_1").to_dict()
        dropped = deepcopy(real)
        dropped["candidate_outcomes"].pop(0)
        dropped["setup_ref"]["candidate_count"] -= 1
        dropped["summary"]["candidate_count"] -= 1
        dropped["summary"]["outcome_status_counts"] = [dict(key="observed", count=4)]
        dropped = reseal(dropped, "evaluation_id")
        validated_evaluation(dropped)
        with self.assertRaises(SetupEvaluationInputError) as caught:
            verify_evaluation(dropped, setup, snap, sched, proto)
        self.assertEqual(str(caught.exception), "evaluation does not match its inputs at candidate_outcomes, setup_ref, summary")
        reordered = deepcopy(real)
        reordered["candidate_outcomes"].reverse()
        reordered = reseal(reordered, "evaluation_id")
        with self.assertRaises(SetupEvaluationInputError):
            verify_evaluation(reordered, setup, snap, sched, proto)

    def test_no_ranking_fields(self):
        e = run()
        keys, stack = set(), [e]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                keys |= set(v)
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
        banned = {"rank", "score", "best", "winner", "win", "loss", "success", "failure", "label", "average", "mean",
                  "sizing", "position", "stop", "reward", "pnl", "profit", "recommendation"}
        self.assertFalse({w for k in keys for w in k.split("_")} & banned)


class InvalidationRelationTests(EvalTestCase):
    def test_relations(self):
        self.assertEqual(run()["invalidation_relation"], dict(status="not_evaluated", check_ids=[], invalidating_check_ids=[]))
        before = c.check("all_bearish", seconds=86400)              # MI 2026-09-24, before the target close
        holds = c.check("all_bullish", seconds=2 * 86400)
        after = c.check("all_bearish", seconds=9 * 86400)           # MI 2026-10-02, after the 2026-10-01 close
        rel = run(checks=[before, holds])["invalidation_relation"]
        self.assertEqual((rel["status"], rel["invalidating_check_ids"]), ("invalidated_by_target", [before["invalidation_id"]]))
        self.assertEqual(rel["check_ids"], sorted([before["invalidation_id"], holds["invalidation_id"]]))
        self.assertEqual(run(checks=[holds])["invalidation_relation"]["status"], "no_invalidation_observed")
        # Supplied checks only: an invalidation after the target close does not count, and the absence of a
        # qualifying supplied check is not proof that no invalidation occurred.
        self.assertEqual(run(checks=[after, holds])["invalidation_relation"]["status"], "no_invalidation_observed")

    def test_relation_does_not_change_outcomes(self):
        plain, related = run(), run(checks=[c.check()])
        self.assertEqual(plain["candidate_outcomes"], related["candidate_outcomes"])
        self.assertEqual(plain["summary"], related["summary"])


class ContractTests(EvalTestCase):
    def test_schema(self):
        e = run()
        self.assertEqual(list(e), list(m.EVALUATION_FIELDS))
        self.assertEqual(len(e), 13)
        self.assertEqual((e["evaluation_format_version"], e["rules_version"]), ("phase11-v1", "phase11-rules-v1"))
        self.assertEqual(e["evaluation_id"], content_id({k: v for k, v in e.items() if k != "evaluation_id"}))
        self.assertNotIn("generated_at", canonical_json(e))
        self.assertEqual(list(e["candidate_outcomes"][0]), list(m.OUTCOME_FIELDS))
        snap, setup = c.snapshot(), c.setup()
        e = run(snapshot=snap)
        self.assertEqual(e["observation_ref"], dict(snapshot_id=snap["snapshot_id"], snapshot_format_version="phase9-snapshot-v1",
                                                    underlying="META", as_of=snap["as_of"], session_date="2026-10-01",
                                                    truncated=False))
        self.assertEqual(e["setup_ref"], dict(assessment_id=setup["assessment_id"], assessment_format_version="phase10-v1",
                                              assessment_rules_version="phase10-rules-v1",
                                              policy_id=setup["policy"]["policy_id"], symbol="META", side="call",
                                              assessment_as_of=setup["inputs"]["assessment_as_of"], candidate_count=5))
        self.assertEqual(e["protocol_ref"], dict(protocol_format_version="phase11-evaluation-protocol-v1",
                                                 protocol_id=c.protocol()["protocol_id"], purpose="test"))
        self.assertEqual(e["provenance"]["protocol_id"], c.protocol()["protocol_id"])
        self.assertEqual([t["rule"] for t in e["decision_trace"]], list(r.TRACE_RULES))


STRUCTURAL = [
    ("extra key", lambda d: d.update(score=1), "evaluation must have exactly its contract keys"),
    ("version", lambda d: d.update(rules_version="phase11-rules-v2"), "unsupported evaluation version"),
    ("mark", lambda d: d["candidate_outcomes"][1].update(liquidation_reference_bid="11"),
     "evaluation outcome values do not re-derive from the copied facts"),
    ("return", lambda d: d["candidate_outcomes"][1].update(premium_return="0.5"),
     "evaluation outcome values do not re-derive from the copied facts"),
    ("dollar", lambda d: d["candidate_outcomes"][1].update(dollar_change_per_contract="400"),
     "evaluation outcome values do not re-derive from the copied facts"),
    ("multiplier use", lambda d: d["candidate_outcomes"][3].update(dollar_change_per_contract="40", dollar_status="computed"),
     "evaluation outcome values do not re-derive from the copied facts"),
    ("status", lambda d: d["candidate_outcomes"][1].update(outcome_status="quote_stale"),
     "evaluation outcome status is inconsistent with its observed quote"),
    ("missing-contract status", lambda d: d["candidate_outcomes"][0].update(outcome_status="observation_incomplete"),
     "evaluation missing-contract status is inconsistent with the snapshot completeness"),
    ("values on unobserved", lambda d: d["candidate_outcomes"][0].update(premium_change="1"),
     "evaluation carries outcome values for a candidate that was not observed"),
    ("horizon", lambda d: d["horizon"].update(window_start="2026-10-01T19:00:00+00:00"), "evaluation horizon is inconsistent"),
    ("horizon name", lambda d: d["horizon"].update(name="session_10"), "evaluation horizon is malformed"),
    ("observation too early", lambda d: d["observation_ref"].update(as_of="2026-10-01T19:29:59+00:00"),
     "evaluation observation is outside the horizon window"),
    ("symbol", lambda d: d["setup_ref"].update(symbol="NVDA"), "evaluation symbols are inconsistent"),
    ("count", lambda d: d["setup_ref"].update(candidate_count=6),
     "evaluation candidate_outcomes do not match the setup candidate count"),
    ("duplicate", lambda d: d["candidate_outcomes"].__setitem__(2, deepcopy(d["candidate_outcomes"][1])),
     "evaluation candidates are not unique"),
    ("summary", lambda d: d["summary"]["premium_change_sign_counts"].update(positive=9), "evaluation summary does not re-derive"),
    ("relation", lambda d: d["invalidation_relation"].update(status="invalidated_by_target"),
     "evaluation invalidation_relation is inconsistent"),
    ("trace", lambda d: d["decision_trace"].reverse(), "evaluation decision_trace is inconsistent"),
    ("provenance", lambda d: d["provenance"].update(snapshot_id="sha256:" + "0" * 64), "evaluation provenance is inconsistent"),
    ("protocol purpose", lambda d: d["protocol_ref"].update(purpose="production"),
     "evaluation protocol is not an activated production protocol"),
]


class TamperTests(unittest.TestCase):
    def test_structural_matrix(self):
        for label, mutate, message in STRUCTURAL:
            data = run()
            mutate(data)
            with self.subTest(case=label), self.assertRaises(SetupEvaluationInputError) as caught:
                validated_evaluation(reseal(data, "evaluation_id"))
            self.assertEqual(str(caught.exception), message)

    def test_id_tamper(self):
        data = run()
        data["summary"]["candidate_count"] = 4
        with self.assertRaises(SetupEvaluationInputError) as caught:
            validated_evaluation(data)
        self.assertEqual(str(caught.exception), "evaluation id does not match its body (tampered or corrupt)")

    def test_resealed_forgery_needs_rederivation(self):
        setup, snap, sched, proto = c.setup(), c.snapshot(), c.schedule(), c.protocol()
        real = evaluate(setup, snap, sched, proto, "session_1").to_dict()
        self.assertEqual(verify_evaluation(real, setup, snap, sched, proto), real)
        forged = deepcopy(real)
        o = forged["candidate_outcomes"][1]                       # a false, self-consistent better bid
        o["observed_quote"]["bid"] = o["liquidation_reference_bid"] = "10.6"
        o.update(premium_change="0.5", premium_return="0.04950495", dollar_change_per_contract="50")
        forged = reseal(forged, "evaluation_id")
        validated_evaluation(forged)
        with self.assertRaises(SetupEvaluationInputError) as caught:
            verify_evaluation(forged, setup, snap, sched, proto)
        self.assertEqual(str(caught.exception), "evaluation does not match its inputs at candidate_outcomes")
        forged = deepcopy(real)
        forged["candidate_outcomes"][1]["entry_reference_ask"] = "10"   # candidate tamper, values recomputed
        forged["candidate_outcomes"][1].update(premium_change="0.5", premium_return="0.05000000", dollar_change_per_contract="50")
        forged = reseal(forged, "evaluation_id")
        validated_evaluation(forged)
        with self.assertRaises(SetupEvaluationInputError):
            verify_evaluation(forged, setup, snap, sched, proto)
        with self.assertRaises(SetupEvaluationInputError):          # a different schedule or protocol
            verify_evaluation(real, setup, snap, c.schedule(end=date(2026, 12, 30)), proto)


class DeterminismTests(unittest.TestCase):
    def test_repeats_and_dict_order(self):
        setup, snap, sched, proto, chk = c.setup(), c.snapshot(), c.schedule(), c.protocol(), c.check()
        expected = canonical_json(evaluate(setup, snap, sched, proto, "session_1", [chk]).to_dict())
        self.assertEqual({canonical_json(evaluate(setup, snap, sched, proto, "session_1", [chk]).to_dict())
                          for _ in range(100)}, {expected})

        def rev(v):
            if isinstance(v, dict):
                return {k: rev(v[k]) for k in reversed(list(v))}
            return [rev(x) for x in v] if isinstance(v, list) else v
        self.assertEqual(canonical_json(evaluate(rev(setup), rev(snap), rev(sched), rev(proto), "session_1",
                                                 [rev(chk)]).to_dict()), expected)

    def test_fresh_processes(self):
        code = ("from tests import setup_evaluation_cases as c\nfrom setup_evaluation.builder import evaluate\n"
                "print(evaluate(c.setup(), c.snapshot(), c.schedule(), c.protocol(), 'session_1', [c.check()]).evaluation_id)")
        expected = evaluate(c.setup(), c.snapshot(), c.schedule(), c.protocol(), "session_1", [c.check()]).evaluation_id
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "9", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "123", "LANG": "C", "LC_ALL": "C"},
                      {"PYTHONHASHSEED": "55", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1", "TZ": "UTC"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class BoundaryTests(unittest.TestCase):
    def core_modules(self):
        return [importlib.import_module(f"setup_evaluation.{i.name}") for i in pkgutil.iter_modules(setup_evaluation.__path__)
                if i.name != "runner"]

    def test_core_imports_and_calls(self):
        stdlib = set(sys.stdlib_module_names) - {"os", "io", "socket", "subprocess", "tempfile", "pathlib", "shutil",
                                                  "sqlite3", "urllib", "http", "random", "time"}
        for module in self.core_modules():
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], stdlib, module.__name__)
                elif isinstance(node, ast.ImportFrom):
                    self.assertTrue(node.module.split(".")[0] in stdlib or node.module.startswith("setup_evaluation"),
                                    (module.__name__, node.module))
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                    self.assertNotIn(name, {"open", "print", "now", "utcnow", "today", "getenv", "float", "time"},
                                     (module.__name__, name))
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("environ", "argv"), module.__name__)

    def test_runtime_imports(self):
        code = ("import sys, setup_evaluation.builder, setup_evaluation.validation\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'trade_setup', 'options_data', 'options_intelligence',"
                " 'market_intelligence', 'market_data', 'exchange_calendars', 'evidence', 'evaluation', 'evidence_packet',"
                " 'evidence_synthesis', 'persistence', 'sqlalchemy', 'requests', 'socket', 'openai', 'redis'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_no_tuning_or_trading_semantics(self):
        banned = {"rank", "score", "best", "winner", "win", "loss", "success", "failure", "label", "average", "sizing",
                  "position", "stop", "reward", "tune", "tuning", "optimize", "threshold", "recommend"}
        for module in self.core_modules():
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
                if isinstance(name, str):
                    self.assertFalse(set(name.lower().split("_")) & banned, (module.__name__, name))


class UpstreamPinTests(unittest.TestCase):
    def test_local_constants_match_upstream(self):
        from options_data import model as om
        from options_data.canonical import canonical_json as upstream_json
        from trade_setup import model as tm, rules as tr
        self.assertEqual(r.ASSESSMENT_TOP_LEVEL, tm.TOP_LEVEL_FIELDS)
        self.assertEqual((r.ASSESSMENT_FORMAT_VERSION, r.ASSESSMENT_RULES_VERSION), (tr.ASSESSMENT_FORMAT_VERSION, tr.RULES_VERSION))
        self.assertEqual(r.INVALIDATION_TOP_LEVEL, tm.INVALIDATION_FIELDS)
        self.assertEqual((r.INVALIDATION_FORMAT_VERSION, r.INVALIDATION_RULES_VERSION, r.INVALIDATION_RESULTS),
                         (tr.INVALIDATION_FORMAT_VERSION, tr.INVALIDATION_RULES_VERSION, tr.INVALIDATION_RESULTS))
        self.assertEqual((r.SNAPSHOT_FORMAT_VERSION, r.SNAPSHOT_TOP_LEVEL, r.FACT_STATUSES, r.TIME_BASES, r.CALENDAR_STATES),
                         (om.SNAPSHOT_FORMAT_VERSION, om.TOP_LEVEL_FIELDS, om.FACT_STATUSES, om.TIME_BASES, om.CALENDAR_STATES))
        data = run()
        self.assertEqual(canonical_json(data), upstream_json(data))

    def test_phase6_isolation(self):
        for module in BoundaryTests().core_modules():
            source = inspect.getsource(module)
            self.assertNotIn("import evidence", source)
            self.assertNotIn("from evaluation", source)


class ScaleTests(unittest.TestCase):
    def test_many_candidates(self):
        import time
        from tests.test_trade_setup_screening_boundary import POLICY, scale_records
        from trade_setup.builder import assess
        from options_data.normalization import assemble
        from tests.options_snapshot_cases import PROVENANCE
        records = scale_records(2000)
        setup = assess(ts.market_intelligence("all_bullish"), ts.chain(records), ts.screen_policy(**POLICY)).to_dict()
        later = [dict(rec, quote=c.quote_at()) for rec in records]
        snap = assemble("META", c.INSIDE, later, provenance=dict(PROVENANCE, truncated=False),
                        calendar_state="regular").to_dict()
        start = time.perf_counter()
        e = evaluate(setup, snap, c.schedule(), c.protocol(), "session_1").to_dict()
        built = time.perf_counter() - start
        self.assertEqual(e["summary"]["candidate_count"], len(setup["candidates"]))
        self.assertGreater(len(setup["candidates"]), 200)
        self.assertLess(built, 30)


if __name__ == "__main__":
    unittest.main()
