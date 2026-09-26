"""Phase 6 evidence ledger (migration 0007) on SQLite: row building, hashes, idempotency, append-only triggers,
downgrade refusal, reconcile/audit tools. PostgreSQL behaviour is in test_evidence_ledger_postgres."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import unittest
import unittest.mock

import sqlalchemy as sa
from alembic import command

from evidence.registry import ENGINE_VERSION, REGISTRY_HASH, registry_identity
from market_data.calendar import default_calendar
from market_data.config import MarketDataSettings
from market_data.models import EXCHANGE_TZ, Interval, MarketBar
from persistence.config import DatabaseSettings
from persistence.database import PersistenceError, make_engine, transaction
from persistence.models import technical_evidence_ledger
from persistence.technical_evidence_ledger import (EvidenceLedgerRepository, LedgerError, bar_content_hash,
                                                   build_evidence, build_failure, evidence_hash,
                                                   expected_collection_date)
from persistence import technical_evidence_tools as tools
from tests import test_persistence as unit

CAL = default_calendar()
SESSION = date(2026, 9, 28)  # Monday.
COMMIT = "a37f566" + "0" * 33
PROVIDER = MarketDataSettings(provider="polygon", base_url="https://api.polygon.io", timeout_seconds=10, max_retries=2,
                              backoff_seconds=1, max_rate_limit_wait_seconds=60, delay_seconds=900, adjusted=True,
                              include_extended_hours=False, api_key="never-printed-canary-key")
EXPECTED_BARS = {"5m": 78, "1h": 7, "1d": 1}


def session_bars(symbol="META", interval="5m", day=SESSION, shift=Decimal("0")):
    times = CAL.session_times(day)
    parsed = Interval.parse(interval)
    if interval == "1d":
        stamps = [datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ)]
    else:
        stamps, t = [], times.open
        while t < times.close:
            stamps.append(t)
            t += parsed.delta
    bars = []
    for i, stamp in enumerate(stamps):
        price = Decimal("100") + Decimal(i) / 100 + shift
        bars.append(MarketBar(symbol, stamp, interval, price, price + 1, price - 1, price, Decimal("1000.5")))
    return bars


def collected_at_for(day, days_late=0):
    return datetime.combine(CAL.next_trading_day(day), datetime.min.time(), tzinfo=EXCHANGE_TZ).replace(hour=8) \
        + timedelta(days=days_late)


def common(symbol="META", interval="5m", day=SESSION, **overrides):
    values = dict(session_date=day, symbol=symbol, interval=interval, engine_version=ENGINE_VERSION,
                  registry=registry_identity(), provider_settings=PROVIDER, code_commit=COMMIT,
                  collected_at=collected_at_for(day), calendar=CAL)
    values.update(overrides)
    return values


def evidence(symbol="META", interval="5m", day=SESSION, shift=Decimal("0"), **overrides):
    return build_evidence(bars=session_bars(symbol, interval, day, shift), **common(symbol, interval, day, **overrides))


def migrated_sqlite():
    engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
    with engine.begin() as connection:
        command.upgrade(unit.migration_config(connection), "head")
    return engine


def record(engine, row, **kw):
    with transaction(engine) as session:
        return EvidenceLedgerRepository(session).record_collected(row, **kw)


def all_rows(engine):
    with transaction(engine) as session:
        return tools.load_rows(session)


class RowBuildingTests(unittest.TestCase):
    def test_hash_excludes_provenance_and_includes_material(self):
        base = evidence()
        later = evidence(code_commit="b" * 40, collected_at=collected_at_for(SESSION, 3))
        self.assertEqual(base["evidence_hash"], later["evidence_hash"])
        self.assertTrue(later["backfilled"])
        self.assertFalse(base["backfilled"])
        self.assertNotEqual(base["evidence_hash"], evidence(shift=Decimal("0.01"))["evidence_hash"])
        self.assertNotEqual(base["evidence_hash"],
                            evidence(provider_settings=replace(PROVIDER, adjusted=False))["evidence_hash"])
        self.assertNotEqual(base["evidence_hash"], evidence(engine_version="phase4c-v1")["evidence_hash"])
        other = dict(registry_identity(), hash="f" * 64)
        self.assertNotEqual(base["evidence_hash"], evidence(registry=other)["evidence_hash"])
        self.assertEqual(base["bar_count"], 78)
        self.assertEqual(base["registry_hash"], REGISTRY_HASH)
        self.assertEqual(base["expected_collection_date"], date(2026, 9, 29))

    def test_bar_hash_is_order_independent_and_exact(self):
        bars = session_bars()
        h = bar_content_hash(bars, symbol="META", interval="5m", session_date=SESSION)
        self.assertEqual(h, bar_content_hash(list(reversed(bars)), symbol="META", interval="5m", session_date=SESSION))
        bumped = [replace(bars[0], volume=Decimal("1000.50000001"))] + bars[1:]
        self.assertNotEqual(h, bar_content_hash(bumped, symbol="META", interval="5m", session_date=SESSION))

    def test_expected_collection_date_skips_weekend_and_holiday(self):
        self.assertEqual(expected_collection_date(date(2026, 9, 25), CAL), date(2026, 9, 28))
        self.assertEqual(expected_collection_date(date(2026, 11, 25), CAL), date(2026, 11, 27))  # Thanksgiving.

    def test_invalid_rows_are_refused(self):
        with self.assertRaises(LedgerError):
            build_evidence(bars=session_bars("META", "1d"), **common(day=date(2026, 9, 27)))  # Sunday.
        with self.assertRaises(LedgerError):
            build_evidence(bars=session_bars(), **common(interval="30m"))
        with self.assertRaises(LedgerError):
            evidence(code_commit="HEAD")
        with self.assertRaises(LedgerError):
            build_evidence(bars=session_bars("NVDA"), **common("META"))
        with self.assertRaises(LedgerError):
            build_evidence(bars=session_bars(day=date(2026, 9, 29)), **common())
        with self.assertRaises(LedgerError):
            build_evidence(bars=[], **common())
        with self.assertRaises(LedgerError):
            build_evidence(bars=session_bars(), snapshot_timestamp=datetime.now(timezone.utc), **common())
        with self.assertRaises(LedgerError):
            build_failure(error_kind="bogus", **common())

    def test_failure_detail_is_sanitized_and_bounded(self):
        row = build_failure(error_kind="transport", error_detail="apiKey=<secret>\n" + "x" * 500, **common())
        self.assertEqual(len(row["error_detail"]), 200)
        self.assertNotIn("<", row["error_detail"])
        self.assertIsNone(row["evidence_hash"])

    def test_no_secret_or_raw_bar_fields(self):
        row = evidence()
        text = json.dumps(row, default=str)
        self.assertNotIn("never-printed-canary-key", text)
        for forbidden in ("open", "high", "low", "close", "volume", "bars", "api_key", "technical_state", "return"):
            self.assertNotIn(forbidden, row)
        self.assertNotIn("100.01", text)


class RepositorySqliteTests(unittest.TestCase):
    def setUp(self):
        self.engine = migrated_sqlite()
        self.addCleanup(self.engine.dispose)

    def test_insert_duplicate_conflict(self):
        first = record(self.engine, evidence())
        self.assertEqual(first["outcome"], "inserted")
        retry = record(self.engine, evidence(code_commit="c" * 40, collected_at=collected_at_for(SESSION, 2)))
        self.assertEqual(retry, dict(outcome="duplicate", id=first["id"]))
        conflict = record(self.engine, evidence(shift=Decimal("0.02")))
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertEqual(conflict["id"], first["id"])
        again = record(self.engine, evidence(shift=Decimal("0.02")))
        self.assertEqual((again["outcome"], again["conflict_id"]), ("conflict", None))
        rows = all_rows(self.engine)
        self.assertEqual([r["record_status"] for r in rows], ["collected", "conflict"])
        self.assertEqual(rows[0]["code_commit"], COMMIT)  # Original provenance preserved.
        self.assertEqual(rows[1]["conflicts_with"], first["id"])

    def test_other_registry_hash_is_a_separate_identity(self):
        record(self.engine, evidence())
        other = record(self.engine, evidence(registry=dict(registry_identity(), hash="e" * 64)))
        self.assertEqual(other["outcome"], "inserted")

    def test_failures_append_and_success_follows(self):
        with transaction(self.engine) as session:
            repo = EvidenceLedgerRepository(session)
            repo.record_failure(build_failure(error_kind="rate_limit", **common()))
            repo.record_failure(build_failure(error_kind="incomplete_session", **common()))
        self.assertEqual(record(self.engine, evidence())["outcome"], "inserted")
        with transaction(self.engine) as session:
            repo = EvidenceLedgerRepository(session)
            self.assertEqual(len(repo.failures()), 2)
            self.assertEqual(len(repo.accepted(symbol="META", interval="5m")), 1)
            self.assertEqual(repo.conflicts(), [])

    def test_malformed_collected_row_refused(self):
        row = dict(evidence(), bar_count=77)
        with self.assertRaises(LedgerError):
            record(self.engine, row)
        with transaction(self.engine) as session:
            with self.assertRaises(LedgerError):
                EvidenceLedgerRepository(session).record_failure(evidence())

    def test_repository_has_no_mutation_api(self):
        public = {n for n in dir(EvidenceLedgerRepository) if not n.startswith("_")}
        self.assertFalse({n for n in public if any(w in n for w in ("update", "delete", "replace", "upsert"))})

    def test_triggers_block_update_and_delete(self):
        record(self.engine, evidence())
        for statement in (sa.update(technical_evidence_ledger).values(bar_count=1),
                          sa.delete(technical_evidence_ledger)):
            with self.assertRaises(PersistenceError):
                with transaction(self.engine) as session:
                    session.execute(statement)
        self.assertEqual(all_rows(self.engine)[0]["bar_count"], 78)
        with self.engine.connect() as connection:
            self.assertTrue(tools.triggers_present(connection))

    def test_downgrade_refused_with_rows_and_allowed_when_empty(self):
        record(self.engine, evidence())
        with self.assertRaisesRegex(RuntimeError, "refusing to downgrade"):
            with self.engine.begin() as connection:
                command.downgrade(unit.migration_config(connection), "0006_technical_numeric_volume")
        self.assertEqual(len(all_rows(self.engine)), 1)
        empty = migrated_sqlite()
        self.addCleanup(empty.dispose)
        with empty.begin() as connection:
            command.downgrade(unit.migration_config(connection), "0006_technical_numeric_volume")
            self.assertNotIn("technical_evidence_ledger", sa.inspect(connection).get_table_names())
            command.upgrade(unit.migration_config(connection), "head")
            self.assertIn("technical_evidence_ledger", sa.inspect(connection).get_table_names())


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.engine = migrated_sqlite()
        self.addCleanup(self.engine.dispose)
        self.pin = dict(prospective_start_session=SESSION, earliest_evaluation_session=date(2027, 3, 29))

    def fill(self, day, symbols=("META", "NVDA"), intervals=("5m", "1d"), **kw):
        for s in symbols:
            for i in intervals:
                record(self.engine, evidence(s, i, day, **kw))

    def run_tool(self, *argv, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc)):
        out = io.StringIO()
        code = tools.main(list(argv), engine=self.engine, calendar=CAL, now=now, pin=self.pin, out=out)
        return code, json.loads(out.getvalue())

    def test_coverage_counts_sessions_not_outcomes(self):
        symbols, intervals = ("META", "NVDA"), ("5m", "1d")
        self.fill(SESSION)
        self.fill(date(2026, 9, 29), symbols=("META",))
        record(self.engine, evidence("NVDA", "5m", date(2026, 9, 30)))
        record(self.engine, evidence("NVDA", "5m", date(2026, 9, 30), shift=Decimal("1")))
        result = tools.coverage(all_rows(self.engine), start=SESSION, through=date(2026, 10, 4), calendar=CAL,
                                symbols=symbols, intervals=intervals)
        self.assertEqual(result["expected_sessions"], 5)  # Sept 28 - Oct 2; the weekend is never expected.
        self.assertEqual(result["complete_sessions"], 1)
        self.assertEqual(result["complete_session_dates"], ["2026-09-28"])
        self.assertEqual(result["conflicted_identities"], 1)
        self.assertEqual(result["missing_sessions"], ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"])
        for forbidden in ("return", "excess", "verdict", "mean", "state"):
            self.assertFalse(any(forbidden in key for key in result))

    def test_holiday_is_not_expected(self):
        result = tools.coverage([], start=date(2026, 11, 25), through=date(2026, 11, 30), calendar=CAL)
        self.assertEqual(result["expected_sessions"], 3)  # 25, 27 (early close), 30; not Thanksgiving or weekend.

    def test_reconcile_and_clean_audit(self):
        self.fill(SESSION)
        code, result = self.run_tool("reconcile")
        self.assertEqual(code, 0)
        self.assertEqual(result["expected_sessions"], 3)  # Through Sept 30 (Oct 1 12:00 UTC is before the close).
        code, result = self.run_tool("audit")
        self.assertEqual((code, result["problems"], result["triggers_present"]), (0, 0, True))

    def test_audit_detects_tampering_pre_start_and_missing_snapshot(self):
        self.fill(SESSION, symbols=("META",), intervals=("5m",))
        tampered = dict(evidence("NVDA"), bar_count=12)  # Stored hash no longer matches its material.
        early = evidence("META", "1d", date(2026, 9, 25))
        snap = evidence("META", "1h", snapshot_timestamp=datetime(2026, 9, 28, 19, 30, tzinfo=timezone.utc),
                        snapshot_content_hash="d" * 64)
        with transaction(self.engine) as session:
            for row in (tampered, early, snap):
                EvidenceLedgerRepository(session)._insert(row, None)
        code, result = self.run_tool("audit")
        self.assertEqual(code, 1)
        self.assertEqual(len(result["evidence_hash_mismatch"]), 1)
        self.assertEqual(len(result["pre_start"]), 1)
        self.assertEqual(len(result["snapshot_missing"]), 1)

    def test_missing_pin_refuses(self):
        out = io.StringIO()
        from evidence import pin as pin_module
        with unittest.mock.patch.object(pin_module, "load_pin",
                                        side_effect=pin_module.PinError("prospective start is not pinned")):
            code = tools.main(["reconcile"], engine=self.engine, calendar=CAL, out=out)
        self.assertEqual(code, 2)
        self.assertIn("not pinned", out.getvalue())


if __name__ == "__main__":
    unittest.main()
