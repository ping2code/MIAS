"""Phase 11 activation hardening: (A) production SessionSchedule completeness against the XNYS calendar at the I/O
boundary, (B) prospective_start enforcement in construction and validation, (C) complete protocol semantics.

Production is simulated only by patching the pinned constants inside each test. The real constants stay None, and no
production protocol file exists."""
from contextlib import contextmanager
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from setup_evaluation import rules as r, runner
from setup_evaluation.builder import evaluate, verify_evaluation
from setup_evaluation.canonical import canonical_json, content_id
from setup_evaluation.protocol import ProtocolError, make_test_protocol, validated_protocol
from setup_evaluation.schedule import make_schedule
from setup_evaluation.validation import SetupEvaluationInputError, validated_evaluation
from tests import setup_evaluation_cases as c

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_TEST_PROTOCOL_ID = "sha256:188119d41f3c6714f0f751ee582f91f769f4073df27563f4e80d92b20f8cfef2"
ASSESSMENT_AS_OF = "2026-09-30T14:45:00+00:00"


def production_protocol(start=ASSESSMENT_AS_OF):
    data = make_test_protocol().to_dict()
    data.update(purpose="production", prospective_start=start)
    data["protocol_id"] = content_id({k: v for k, v in data.items() if k != "protocol_id"})
    return data


@contextmanager
def activated(protocol):
    """Simulate activation for one test only (the real pins stay None)."""
    with mock.patch.object(r, "PRODUCTION_PROTOCOL_ID", protocol["protocol_id"]), \
            mock.patch.object(r, "PRODUCTION_PROSPECTIVE_START", protocol["prospective_start"]):
        yield


def edited_schedule(edit):
    rows = [[s["session_date"], s["regular_open"], s["regular_close"]] for s in c.schedule()["sessions"]]
    edit(rows)
    return make_schedule([tuple(row) for row in rows]).to_dict()


def raw_schedule(edit):
    """A schedule edited after sealing and resealed (for orderings make_schedule itself would reject)."""
    data = c.schedule()
    edit(data["sessions"])
    data["schedule_id"] = content_id({k: v for k, v in data.items() if k != "schedule_id"})
    return data


class ScheduleCompletenessTests(unittest.TestCase):
    """Fix A: under a production protocol, evaluate and verify require the exact XNYS sessions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.protocol = production_protocol()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(data) + "\n")
        return path

    def evaluate(self, schedule, protocol=None, output="e.json"):
        protocol = protocol or self.protocol
        args = ["evaluate", "--assessment", self.write("a.json", c.setup()), "--snapshot", self.write("s.json", c.snapshot()),
                "--schedule", self.write("sched.json", schedule), "--protocol", self.write("p.json", protocol),
                "--horizon", "session_1", "--output", os.path.join(self.dir, output)]
        out, err = io.StringIO(), io.StringIO()
        return runner.main(args, out=out, err=err), out.getvalue(), err.getvalue()

    def test_exact_calendar_schedule_accepted(self):                                                       # 1
        with activated(self.protocol):
            code, out, err = self.evaluate(c.schedule())
        self.assertEqual(code, runner.OK, err)
        self.assertEqual(json.loads(out)["protocol_purpose"], "production")

    def test_tampered_schedules_rejected(self):                                                            # 2-8
        mismatch = "schedule does not match the XNYS calendar"
        cases = [
            ("omitted middle session", edited_schedule(lambda rows: rows.pop(next(
                i for i, row in enumerate(rows) if row[0] == "2026-10-02"))), mismatch),
            ("extra non-session (Saturday)", edited_schedule(lambda rows: rows.insert(next(
                i for i, row in enumerate(rows) if row[0] == "2026-10-05"),
                ["2026-10-03", "2026-10-03T13:30:00+00:00", "2026-10-03T20:00:00+00:00"])), mismatch),
            ("altered regular_open", edited_schedule(lambda rows: rows[3].__setitem__(1, "2026-10-01T13:31:00+00:00")),
             mismatch),
            ("altered regular_close", edited_schedule(lambda rows: rows[3].__setitem__(2, "2026-10-01T20:01:00+00:00")),
             mismatch),
            ("altered early close", edited_schedule(lambda rows: next(row for row in rows if row[0] == "2026-11-27")
                                                    .__setitem__(2, "2026-11-27T21:00:00+00:00")), mismatch),
            ("reordered", raw_schedule(lambda sessions: sessions.__setitem__(slice(1, 3), sessions[2:0:-1])),
             "schedule is invalid: schedule sessions must be strictly increasing"),
            ("duplicate row", raw_schedule(lambda sessions: sessions.insert(1, dict(sessions[1]))),
             "schedule is invalid: schedule sessions must be strictly increasing"),
        ]
        for label, schedule, message in cases:
            with self.subTest(case=label), activated(self.protocol):
                code, out, err = self.evaluate(schedule, output=f"{label}.json")
                self.assertEqual((code, json.loads(err)["error"]), (runner.INVALID, message))
                self.assertFalse(os.path.exists(os.path.join(self.dir, f"{label}.json")))

    def test_omission_would_shift_the_horizon(self):
        gapped = edited_schedule(lambda rows: rows.pop(next(i for i, row in enumerate(rows) if row[0] == "2026-10-02")))
        from setup_evaluation.schedule import resolve
        from datetime import datetime
        anchor = datetime.fromisoformat(ASSESSMENT_AS_OF)
        self.assertEqual((resolve(c.schedule(), anchor, 5)["session_date"], resolve(gapped, anchor, 5)["session_date"]),
                         ("2026-10-07", "2026-10-08"))

    def test_test_protocol_schedules_remain_usable(self):                                                 # 9
        gapped = edited_schedule(lambda rows: rows.pop(next(i for i, row in enumerate(rows) if row[0] == "2026-10-02")))
        code, _, err = self.evaluate(gapped, protocol=c.protocol())
        self.assertEqual(code, runner.OK, err)

    def test_production_verify_performs_the_calendar_check(self):                                        # 10
        with activated(self.protocol):
            self.assertEqual(self.evaluate(c.schedule())[0], runner.OK)
            base = ["verify", "--evaluation", os.path.join(self.dir, "e.json"), "--assessment", os.path.join(self.dir, "a.json"),
                    "--snapshot", os.path.join(self.dir, "s.json"), "--protocol", os.path.join(self.dir, "p.json")]
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(runner.main(base + ["--schedule", os.path.join(self.dir, "sched.json")], out=out, err=err),
                             runner.OK)
            self.assertEqual(json.loads(out.getvalue())["result"], "VERIFIED")
            altered = self.write("altered.json", edited_schedule(lambda rows: rows[0].__setitem__(2, "2026-09-28T20:01:00+00:00")))
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(runner.main(base + ["--schedule", altered], out=out, err=err), runner.INVALID)
            self.assertEqual(json.loads(err.getvalue())["error"], "schedule does not match the XNYS calendar")


class ProspectiveStartTests(unittest.TestCase):
    """Fix B: under a production protocol, assessment_as_of >= prospective_start."""

    def build(self, start):
        protocol = production_protocol(start)
        with activated(protocol):
            return evaluate(c.setup(), c.snapshot(), c.schedule(), protocol, "session_1").to_dict()

    def test_before_equal_after(self):                                                                    # 1-3
        with self.assertRaises(SetupEvaluationInputError) as caught:
            self.build("2026-09-30T14:45:00.000001+00:00")
        self.assertEqual(str(caught.exception), "assessment is before the production prospective_start")
        self.assertEqual(self.build(ASSESSMENT_AS_OF)["protocol_ref"]["purpose"], "production")       # equal: accepted
        self.assertEqual(self.build("2026-09-30T14:44:59+00:00")["protocol_ref"]["purpose"], "production")

    def test_test_protocol_unaffected(self):                                                              # 4
        self.assertEqual(evaluate(c.setup(), c.snapshot(), c.schedule(), c.protocol(), "session_1").protocol_ref.purpose,
                         "test")

    def test_validation_rejects_pre_start_production_evaluations(self):                                   # 5
        protocol = production_protocol(ASSESSMENT_AS_OF)
        with activated(protocol):
            sealed = evaluate(c.setup(), c.snapshot(), c.schedule(), protocol, "session_1").to_dict()
            self.assertEqual(validated_evaluation(sealed), sealed)
        # The same sealed evaluation, checked while a later prospective_start is pinned.
        with mock.patch.object(r, "PRODUCTION_PROTOCOL_ID", protocol["protocol_id"]), \
                mock.patch.object(r, "PRODUCTION_PROSPECTIVE_START", "2026-10-01T00:00:00+00:00"):
            with self.assertRaises(SetupEvaluationInputError) as caught:
                validated_evaluation(sealed)
            self.assertEqual(str(caught.exception), "evaluation assessment is before the production prospective_start")
            with self.assertRaises(SetupEvaluationInputError):
                verify_evaluation(sealed, c.setup(), c.snapshot(), c.schedule(), protocol)

    def test_no_timezone_ambiguity(self):                                                                 # 6
        for start in ("2026-09-30T10:45:00-04:00", "2026-09-30T14:45:00", "2026-09-30T14:45:00Z", "2026-09-30"):
            protocol = production_protocol(start)
            with self.subTest(start=start), activated(protocol), self.assertRaises(ProtocolError) as caught:
                validated_protocol(protocol)
            self.assertEqual(str(caught.exception), "production prospective_start must be canonical UTC")


class ProtocolContentTests(unittest.TestCase):
    """Fix C: the protocol records every frozen semantic explicitly."""

    def test_fields_and_values(self):
        p = make_test_protocol().to_dict()
        self.assertEqual((p["evaluation_format_version"], p["rules_version"], p["schedule_format_version"]),
                         ("phase11-v1", "phase11-rules-v1", "phase11-sessions-v1"))
        self.assertEqual(p["missing_contract_rule"], "contract_absent_if_complete_chain_else_observation_incomplete")
        self.assertEqual(p["multiplier_rule"], "assessment_candidate_multiplier_never_assumed")
        self.assertEqual(p["exclusions"], ["expiration_settlement", "labels", "mae", "mfe", "portfolio_pnl",
                                           "position_sizing", "ranking", "retrospective_selection"])
        self.assertEqual(p["exclusions"], sorted(set(p["exclusions"])))
        for field in ("horizons", "window_seconds", "mark", "entry", "quote_requirements", "return_places",
                      "return_rounding", "outcome_statuses", "candidate_inclusion", "invalidation_rule"):
            self.assertIn(field, p)

    def test_hash(self):
        first, second = make_test_protocol().protocol_id, make_test_protocol().protocol_id
        self.assertEqual(first, second)
        self.assertNotEqual(first, OLD_TEST_PROTOCOL_ID)
        p = make_test_protocol().to_dict()
        changed = dict(p, exclusions=p["exclusions"][:-1])
        changed["protocol_id"] = content_id({k: v for k, v in changed.items() if k != "protocol_id"})
        self.assertNotEqual(changed["protocol_id"], p["protocol_id"])
        with self.assertRaises(ProtocolError) as caught:
            validated_protocol(changed)
        self.assertEqual(str(caught.exception), "protocol semantics do not match the frozen phase11-rules-v1 semantics")
        self.assertNotEqual(production_protocol()["protocol_id"], p["protocol_id"])

    def test_only_the_pinned_production_protocol(self):
        # Any production protocol other than the pinned one is rejected (here: a different prospective_start).
        with self.assertRaises(ProtocolError) as caught:
            validated_protocol(production_protocol())
        self.assertEqual(str(caught.exception), "protocol is not the pinned production protocol")
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        production_files = []
        for path in tracked:
            if path.endswith(".json"):
                with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as handle:
                    if '"purpose":"production"' in handle.read().replace(" ", ""):
                        production_files.append(path)
        self.assertIn(production_files, ([], ["setup_evaluation/protocols/phase11-evaluation-protocol-v1.production.json"]))


if __name__ == "__main__":
    unittest.main()
