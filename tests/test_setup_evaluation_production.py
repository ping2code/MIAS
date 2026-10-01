"""Phase 11 production activation: the checked-in production protocol, its pins, the prospective boundary and the
production schedule check, all with the REAL pinned constants (no patching). Synthetic inputs only; no prospective
observation is collected or evaluated here."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from options_data.normalization import assemble
from options_intelligence.builder import build as build_options_intelligence
from setup_evaluation import model as m, rules as r, runner
from setup_evaluation.builder import evaluate
from setup_evaluation.canonical import canonical_json, content_id
from setup_evaluation.protocol import ProtocolError, frozen_fields, make_production_protocol, make_test_protocol, validated_protocol
from setup_evaluation.validation import SetupEvaluationInputError, validated_evaluation
from tests import options_intelligence_cases as oc
from tests import setup_evaluation_cases as c
from tests import trade_setup_cases as ts
from tests.options_snapshot_cases import PROVENANCE
from trade_setup.builder import assess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTION_FILE = os.path.join(ROOT, "setup_evaluation", "protocols", "phase11-evaluation-protocol-v1.production.json")
TEST_PROTOCOL_ID = "sha256:1eebe890749ff6010be09ff28f4a1d7d53f158c1e08794475d48e9a1c1ae79fe"
START = "2026-10-02T13:30:00+00:00"
Z = c.Z
MONDAY_CLOSE = Z(2026, 10, 5, 20, 0)          # session_1 for an assessment at/after the Friday 2026-10-02 open


def production():
    with open(PRODUCTION_FILE, encoding="utf-8") as handle:
        return json.loads(handle.read())


def setup_at(as_of):
    """A sealed setup_candidates assessment whose assessment_as_of is exactly ``as_of`` (the options as_of)."""
    records = [c.record(690), c.record(700)]
    snapshot = assemble("META", as_of, records, provenance=dict(PROVENANCE, truncated=False), calendar_state="regular")
    oi = build_options_intelligence(snapshot, calendar=oc.CALENDAR, format="phase9-v2").to_dict()
    assessment = assess(ts.market_intelligence("all_bullish"), oi, ts.screen_policy()).to_dict()
    assert assessment["outcome"]["status"] == "setup_candidates" and assessment["inputs"]["assessment_as_of"] == as_of.isoformat()
    return assessment


def monday_snapshot():
    inside = Z(2026, 10, 5, 19, 50)
    return c.snapshot(as_of=inside, quotes={s: c.quote_at(observed_at=Z(2026, 10, 5, 19, 49)) for s in (690, 700)})


class ProductionProtocolTests(unittest.TestCase):
    def test_file_validates_and_matches_pins(self):                                                       # 1, 4-7
        data = production()
        self.assertEqual(validated_protocol(data), data)
        self.assertEqual(data["purpose"], "production")
        self.assertEqual(data["protocol_id"], r.PRODUCTION_PROTOCOL_ID)
        self.assertEqual(data["prospective_start"], r.PRODUCTION_PROSPECTIVE_START)
        self.assertEqual(r.PRODUCTION_PROSPECTIVE_START, START)
        parsed = datetime.fromisoformat(START)
        self.assertEqual((parsed.utcoffset().total_seconds(), parsed.isoformat()), (0, START))
        with open(PRODUCTION_FILE, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), canonical_json(data) + "\n")             # canonical bytes
        self.assertEqual(make_production_protocol(START).to_dict(), data)            # rebuilt from the project code

    def test_hash_deterministic_and_distinct(self):                                                       # 2, 3
        self.assertEqual(content_id({k: v for k, v in production().items() if k != "protocol_id"}), r.PRODUCTION_PROTOCOL_ID)
        self.assertEqual({make_production_protocol(START).protocol_id for _ in range(100)}, {r.PRODUCTION_PROTOCOL_ID})
        self.assertNotEqual(r.PRODUCTION_PROTOCOL_ID, TEST_PROTOCOL_ID)
        self.assertEqual(make_test_protocol().protocol_id, TEST_PROTOCOL_ID)

    def test_every_frozen_field(self):                                                                    # 15-18, 20
        data = production()
        self.assertEqual(list(data), sorted(m.PROTOCOL_FIELDS))
        for key, value in frozen_fields().items():
            self.assertEqual(data[key], value, key)
        self.assertEqual((data["protocol_format_version"], data["evaluation_format_version"], data["rules_version"],
                          data["schedule_format_version"]),
                         ("phase11-evaluation-protocol-v1", "phase11-v1", "phase11-rules-v1", "phase11-sessions-v1"))
        self.assertEqual((data["horizons"], data["horizon_rule"], data["window_seconds"], data["window_rule"]),
                         (["session_1", "session_5"], "nth_regular_session_with_open_strictly_after_assessment_as_of",
                          1800, "inclusive_backward_from_target_close"))
        self.assertEqual((data["entry"], data["mark"], data["return_places"], data["return_rounding"]),
                         ("entry_reference_ask", "liquidation_reference_bid", 8, "ROUND_HALF_EVEN"))
        self.assertEqual(data["quote_requirements"], ["quote_present", "time_basis_observed_at", "two_sided", "non_crossed",
                                                      "observed_at_in_window"])
        self.assertEqual(data["exclusions"], ["expiration_settlement", "labels", "mae", "mfe", "portfolio_pnl",
                                              "position_sizing", "ranking", "retrospective_selection"])
        self.assertEqual(data["missing_contract_rule"], "contract_absent_if_complete_chain_else_observation_incomplete")
        self.assertEqual(data["multiplier_rule"], "assessment_candidate_multiplier_never_assumed")
        self.assertEqual((data["candidate_inclusion"], data["invalidation_rule"]),
                         ("all_assessment_candidates_in_canonical_order", "supplied_checks_only"))
        self.assertNotIn("generated_at", data)

    def test_no_field_changes_silently(self):                                                             # 19
        base = production()
        for key in base:
            if key == "protocol_id":
                continue
            changed = deepcopy(base)
            changed[key] = "changed" if not isinstance(base[key], list) else base[key][:-1]
            with self.subTest(field=key):
                self.assertNotEqual(content_id({k: v for k, v in changed.items() if k != "protocol_id"}),
                                    base["protocol_id"])
                with self.assertRaises(ProtocolError):
                    validated_protocol(changed)                                      # id no longer matches
                resealed = dict(changed, protocol_id=content_id({k: v for k, v in changed.items() if k != "protocol_id"}))
                with self.assertRaises(ProtocolError):
                    validated_protocol(resealed)                                     # semantics or pin mismatch

    def test_pin_consistency(self):
        for start in ("2026-10-02T13:30:00Z", "2026-10-02T09:30:00-04:00", "2026-10-05T13:30:00+00:00"):
            with self.subTest(start=start), self.assertRaises(ProtocolError):
                validated_protocol(make_production_protocol(start).to_dict() if start.endswith("+00:00")
                                   else dict(production(), prospective_start=start))


class ProspectiveBoundaryTests(unittest.TestCase):
    def test_before_equal_after(self):                                                                    # 8-10
        with self.assertRaises(SetupEvaluationInputError) as caught:
            evaluate(c.setup(), c.snapshot(), c.schedule(), production(), "session_1")        # assessment 2026-09-30
        self.assertEqual(str(caught.exception), "assessment is before the production prospective_start")
        for as_of in (datetime.fromisoformat(START), Z(2026, 10, 2, 14, 0)):
            with self.subTest(as_of=as_of.isoformat()):
                e = evaluate(setup_at(as_of), monday_snapshot(), c.schedule(), production(), "session_1").to_dict()
                self.assertEqual((e["protocol_ref"]["purpose"], e["horizon"]["target_session_date"]),
                                 ("production", "2026-10-05"))
                self.assertEqual(validated_evaluation(e), e)

    def test_one_microsecond_before_is_ineligible(self):
        with self.assertRaises(SetupEvaluationInputError):
            evaluate(setup_at(Z(2026, 10, 2, 13, 29, 59, 999999)), monday_snapshot(), c.schedule(), production(), "session_1")

    def test_test_protocol_independent(self):                                                             # 14
        e = evaluate(c.setup(), c.snapshot(), c.schedule(), make_test_protocol(), "session_1").to_dict()
        self.assertEqual((e["protocol_ref"]["purpose"], e["protocol_ref"]["protocol_id"]), ("test", TEST_PROTOCOL_ID))


class ProductionScheduleTests(unittest.TestCase):
    """The production calendar check (activation hardening A) under the real pinned protocol."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(data) + "\n")
        return path

    def run_evaluate(self, schedule, name):
        args = ["evaluate", "--assessment", self.write("a.json", setup_at(datetime.fromisoformat(START))),
                "--snapshot", self.write("s.json", monday_snapshot()), "--schedule", self.write(f"{name}.sched.json", schedule),
                "--protocol", PRODUCTION_FILE, "--horizon", "session_1", "--output", os.path.join(self.dir, f"{name}.json")]
        err = io.StringIO()
        return runner.main(args, out=io.StringIO(), err=err), err.getvalue()

    def test_calendar_check_enforced(self):                                                               # 11-13
        from tests.test_setup_evaluation_activation import edited_schedule
        self.assertEqual(self.run_evaluate(c.schedule(), "exact")[0], runner.OK)
        for name, schedule in (
                ("omitted", edited_schedule(lambda rows: rows.pop(next(i for i, row in enumerate(rows) if row[0] == "2026-10-02")))),
                ("early_close", edited_schedule(lambda rows: next(row for row in rows if row[0] == "2026-11-27")
                                                .__setitem__(2, "2026-11-27T21:00:00+00:00")))):
            with self.subTest(case=name):
                code, err = self.run_evaluate(schedule, name)
                self.assertEqual((code, json.loads(err)["error"]), (runner.INVALID, "schedule does not match the XNYS calendar"))


class DeterminismTests(unittest.TestCase):
    def test_load_validate_is_stable(self):
        ids = {validated_protocol(production())["protocol_id"] for _ in range(100)}
        self.assertEqual(ids, {r.PRODUCTION_PROTOCOL_ID})
        reordered = {k: production()[k] for k in reversed(list(production()))}
        self.assertEqual(validated_protocol(reordered)["protocol_id"], r.PRODUCTION_PROTOCOL_ID)

    def test_fresh_processes(self):
        code = ("import json\nfrom setup_evaluation.protocol import validated_protocol\n"
                f"print(validated_protocol(json.load(open({PRODUCTION_FILE!r})))['protocol_id'])")
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "17", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "3", "LANG": "C", "LC_ALL": "C", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=120)
            self.assertEqual(result.stdout.strip(), r.PRODUCTION_PROTOCOL_ID, result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
