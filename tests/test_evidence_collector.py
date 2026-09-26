"""Phase 6 daily evidence collection through ``technical.runner`` (fake vendor HTTP, file SQLite migrated to head).

Covered:

- the ledger is disabled by default, and technical output is identical with it off
  or on;
- fail-closed setup: no pin, no shadow persistence, wrong engine, universe, no commit;
- sessions on or after the start only; idempotent reruns;
- incomplete sessions and provider failures become terminal failure rows;
- the snapshot reference is set only for the latest session and verified by the audit;
- check-only mode writes nothing;
- the runner never imports ``evaluation``.
"""
from datetime import date, datetime
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from evidence import collector, pin as pin_module
from evidence.registry import REGISTRY_HASH
from market_data.models import Interval
from persistence import technical_evidence_tools as tools
from persistence import technical_shadow
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import technical_evidence_ledger, technical_snapshots
from tests import test_persistence as unit
from tests.market_data_fakes import TEST_KEY, FakeResponse, FakeSession
from tests.test_evidence_ledger import session_bars
from tests.test_technical_runner import CAL, ENV, NOW, make_provider, route

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = date(2026, 9, 17)  # Thursday: 17, 18, 21, 22, 23 are the eligible sessions at NOW (Sept 23 20:00 ET).
PIN = dict(registry_hash=REGISTRY_HASH, prospective_start_session=START,
           earliest_evaluation_session=date(2027, 3, 17), prospective_freeze_commit="a" * 40)
COMMIT = "b" * 40


class PlanningTests(unittest.TestCase):
    def test_expected_bar_counts(self):
        self.assertEqual([collector.expected_bar_count(date(2026, 9, 28), l, CAL) for l in ("5m", "1h", "1d")],
                         [78, 7, 1])
        self.assertEqual([collector.expected_bar_count(date(2026, 11, 27), l, CAL) for l in ("5m", "1h", "1d")],
                         [42, 4, 1])  # Day after Thanksgiving: early close.

    def test_session_items_start_accepted_incomplete_and_snapshot(self):
        days = CAL.trading_days(date(2026, 9, 14), date(2026, 9, 23))
        series = [b for d in days for b in session_bars("META", "5m", d)]
        series = [b for b in series if b.timestamp != session_bars("META", "5m", date(2026, 9, 21))[10].timestamp]
        context = {"5m": dict(series=series, warmup_start=datetime.combine(days[0], datetime.min.time(),
                                                                           tzinfo=NOW.tzinfo))}
        stamp = session_bars("META", "5m", date(2026, 9, 23))[-1].timestamp
        items = collector.session_items("META", context, CAL, NOW, start=START,
                                        accepted={(date(2026, 9, 18), "META", "5m")},
                                        latest_snapshots={"5m": (stamp, "c" * 64)})
        self.assertEqual([i["session"].day for i in items], [17, 21, 22, 23])  # Pre-start and accepted skipped.
        self.assertEqual([i["complete"] for i in items], [True, False, True, True])
        self.assertEqual([i["snapshot"] is not None for i in items], [False, False, False, True])
        # Before the Sept 23 close the latest session is Sept 22, and the Sept 23 snapshot is not attached to it.
        early = collector.session_items("META", context, CAL, NOW.replace(hour=15), start=START,
                                        latest_snapshots={"5m": (stamp, "c" * 64)})
        self.assertEqual(early[-1]["session"], date(2026, 9, 22))
        self.assertIsNone(early[-1]["snapshot"])

    def test_missing_items_for_failed_symbol(self):
        starts = {"5m": datetime(2026, 9, 10, tzinfo=NOW.tzinfo), "1d": datetime(2025, 1, 2, tzinfo=NOW.tzinfo)}
        items = collector.missing_items("META", starts, starts, CAL, NOW, start=START,
                                        accepted={(START, "META", "1d")})
        self.assertEqual(len(items), 5 + 4)

    def test_settings(self):
        self.assertFalse(collector.load_evidence_settings({}).enabled)
        self.assertTrue(collector.load_evidence_settings(dict(TECHNICAL_EVIDENCE_LEDGER_ENABLED="true")).enabled)
        with self.assertRaises(collector.EvidenceConfigError):
            collector.load_evidence_settings(dict(TECHNICAL_EVIDENCE_LEDGER_ENABLED="yes"))


class RunnerLedgerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.url = f"sqlite:///{os.path.join(directory.name, 'ledger.db')}"
        self.engine = make_engine(DatabaseSettings(url=self.url, sqlite_enabled=True))
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")
        logging.getLogger("technical_shadow").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("technical_shadow").setLevel, logging.NOTSET)
        self.env = dict(ENV, TECHNICAL_EVIDENCE_LEDGER_ENABLED="true", TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true",
                        DATABASE_URL=self.url, DB_ALLOW_SQLITE="true")

    def run_runner(self, argv=(), environ=None, *, pin=PIN, commit=COMMIT, session=None):
        from technical.runner import main
        technical_shadow._after_fork()
        session = session or FakeSession(route=route)
        out = io.StringIO()
        load = patch.object(pin_module, "load_pin", return_value=pin) if pin is not None else \
            patch.object(pin_module, "load_pin", side_effect=pin_module.PinError("prospective start is not pinned"))
        writer = patch.object(technical_shadow, "_configured_writer", lambda: technical_shadow.TechnicalSnapshotWriter(
            engine_factory=lambda: self.engine, capacity=64))
        with load, writer, patch.object(pin_module, "current_commit", return_value=commit), \
                self.assertLogs("technical.runner", "INFO") as logs:
            code = main(["--symbols", "META,NVDA", *argv], environ=self.env if environ is None else environ,
                        provider=make_provider(session), out=out)
        technical_shadow._after_fork()
        return code, logs.output, out.getvalue(), session

    def rows(self):
        with transaction(self.engine) as session:
            return tools.load_rows(session)

    def test_disabled_by_default_and_output_parity(self):
        off = self.run_runner(environ=dict(ENV))
        on = self.run_runner()
        technical = lambda logs: [l for l in logs if "event=technical_snapshot" in l]  # noqa: E731
        self.assertEqual(off[0], 0)
        self.assertEqual(technical(off[1]), technical(on[1]))
        self.assertEqual(off[2], on[2])
        self.assertFalse(any("event=evidence_ledger" in l for l in off[1]))

    def test_collects_prospective_sessions_idempotently(self):
        code, logs, _, session = self.run_runner()
        self.assertEqual(code, 0, logs)
        self.assertEqual(len(session.calls), 4)  # The ledger adds no vendor requests.
        rows = self.rows()
        self.assertEqual(len(rows), 2 * 3 * 5)
        self.assertTrue(all(r["record_status"] == "collected" and r["market_session_date"] >= START for r in rows))
        self.assertEqual({r["bar_count"] for r in rows if r["interval"] == "5m"}, {78})
        self.assertEqual({r["bar_count"] for r in rows if r["interval"] == "1h"}, {7})
        self.assertEqual({r["code_commit"] for r in rows}, {COMMIT})
        referenced = [r for r in rows if r["snapshot_timestamp"] is not None]
        self.assertEqual(len(referenced), 6)
        self.assertEqual({r["market_session_date"] for r in referenced}, {date(2026, 9, 23)})
        self.assertIn("inserted=30 duplicate=0 conflict=0 failed=0", "\n".join(logs))
        code, logs, _, _ = self.run_runner()
        self.assertEqual(code, 0)
        self.assertIn("skipped_accepted=30 inserted=0", "\n".join(logs))
        self.assertEqual(len(self.rows()), 30)
        with transaction(self.engine) as db:
            self.assertEqual(db.execute(sa.select(sa.func.count()).select_from(technical_snapshots)).scalar_one(), 6)
        out = io.StringIO()
        self.assertEqual(tools.main(["audit"], engine=self.engine, calendar=CAL, pin=PIN, out=out), 0, out.getvalue())
        out = io.StringIO()
        tools.main(["reconcile", "--through", "2026-09-23"], engine=self.engine, calendar=CAL, pin=dict(
            PIN, prospective_start_session=START), out=out)
        report = json.loads(out.getvalue())
        self.assertEqual((report["expected_identities"], report["accepted_identities"]), (8 * 3 * 5, 30))
        self.assertEqual({m["symbol"] for m in report["missing_identities"]}, {"MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"})
        self.assertEqual(report["complete_sessions"], 0)  # A session is complete only with the full frozen universe.
        text = json.dumps(self.rows(), default=str)
        self.assertNotIn(TEST_KEY, text)

    def test_fail_closed_setup_makes_no_requests(self):
        cases = [dict(pin=None), dict(commit=None),
                 dict(environ=dict(self.env, TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="false")),
                 dict(environ=dict(self.env, TECHNICAL_SNAPSHOT_ENGINE_VERSION="phase4c-v1")),
                 dict(environ=dict(self.env, TECHNICAL_EVIDENCE_LEDGER_ENABLED="yes")),
                 dict(environ={k: v for k, v in self.env.items() if k != "DATABASE_URL"}),
                 dict(argv=["--symbols", "TSLA"])]
        for case in cases:
            with self.subTest(case=str(case)[:80]):
                session = FakeSession(route=route)
                code, logs, _, _ = self.run_runner(session=session, **case)
                self.assertEqual(code, 2)
                self.assertEqual(session.calls, [])
                self.assertIn("reason=evidence_config", "\n".join(logs))
        self.assertEqual(self.rows(), [])

    def test_provider_failure_records_terminal_failures(self):
        code, logs, _, _ = self.run_runner(["--timeframes", "5m"], session=FakeSession([FakeResponse(401)] * 4))
        self.assertEqual(code, 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2 * 5)  # One per missing eligible identity, not per HTTP retry.
        self.assertEqual({(r["record_status"], r["error_kind"]) for r in rows}, {("failed", "provider_auth")})
        self.assertNotIn(TEST_KEY, json.dumps(rows, default=str))
        code, logs, _, _ = self.run_runner(["--timeframes", "5m"])
        self.assertEqual(code, 0)
        self.assertEqual(sum(r["record_status"] == "collected" for r in self.rows()), 10)

    def test_incomplete_session_fails_closed(self):
        def gappy(url, params):
            response = route(url, params)
            body = json.loads(response.text)
            if "/5/minute/" in url:
                body["results"] = [r for r in body["results"]
                                   if r["t"] != int(datetime(2026, 9, 22, 11, 0, tzinfo=NOW.tzinfo).timestamp() * 1000)]
                body["resultsCount"] = len(body["results"])
            return FakeResponse(200, body)
        code, logs, _, _ = self.run_runner(["--timeframes", "5m"], session=FakeSession(route=gappy))
        self.assertEqual(code, 1)
        failed = [r for r in self.rows() if r["record_status"] == "failed"]
        self.assertEqual({(r["market_session_date"], r["error_kind"]) for r in failed},
                         {(date(2026, 9, 22), "incomplete_session")})
        self.assertEqual(len(failed), 2)
        self.assertIn("bars 77 expected 78", failed[0]["error_detail"])

    def test_check_mode_writes_nothing_and_needs_no_pin(self):
        env = dict(ENV)  # No database, no ledger flag.
        code, logs, out, _ = self.run_runner(["--evidence-check"], environ=env, pin=None)
        self.assertEqual(code, 0, logs)
        report = json.loads(out)
        self.assertEqual((report["mode"], report["pinned"]), ("operational_validation", False))
        self.assertEqual(report["per_symbol_interval"]["META 5m"]["complete"], 10)
        self.assertEqual(report["per_symbol_interval"]["META 1h"]["sessions"], 90)
        self.assertEqual(self.rows(), [])
        for forbidden in ("close", "return", "state", "excess", TEST_KEY):
            self.assertNotIn(forbidden, out.replace("closed", ""))

    def test_runner_never_imports_evaluation(self):
        code = ("import sys, technical.runner, evidence.collector, evidence.pin, evidence.registry, "
                "persistence.technical_evidence_ledger, persistence.technical_evidence_tools\n"
                "print(sorted(m for m in sys.modules if m.startswith('evaluation')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
