"""Phase 4C technical snapshot persistence on SQLite: row mapping, identity, idempotency, conflicts, settings,
shadow writer accounting, runner on/off parity, and the read-only tools."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import io
import json
import logging
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from market_data.calendar import default_calendar
from market_data.models import EXCHANGE_TZ
from persistence import technical_shadow
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import metadata, technical_snapshot_conflicts, technical_snapshots
from persistence.technical_settings import TechnicalPersistenceConfigError, load_technical_persistence_settings
from persistence.technical_snapshot_repository import (MATERIAL_FIELDS, SnapshotRowError, TechnicalSnapshotRepository,
                                                       content_hash, persist_technical_snapshot, snapshot_row,
                                                       validate_row)
from persistence.technical_snapshot_tools import audit_rows, expected_grid, main as tools_main, reconcile_rows
from technical.engine import TechnicalEngine
from technical.models import TechnicalConfig
from tests.technical_fixtures import SCENARIOS

CAL = default_calendar()


_TMP = []


def sqlite_engine():
    """A file-backed SQLite database: the shadow writer uses its own thread (in-memory SQLite is thread-bound)."""
    import os
    import tempfile
    directory = tempfile.TemporaryDirectory()
    _TMP.append(directory)
    engine = make_engine(DatabaseSettings(url=f"sqlite:///{os.path.join(directory.name, 'tech.db')}",
                                          sqlite_enabled=True))
    metadata.create_all(engine)
    return engine


def snapshots(name="breakout_high_volume", symbol="META"):
    bars = SCENARIOS[name](symbol)
    return bars, TechnicalEngine(calendar=CAL).replay(bars)


def row_for(snapshot, bars, **overrides):
    values = dict(provider="polygon", engine_version="phase4c-v2", provider_delay_seconds=900,
                  warmup_start=bars[0].timestamp, warmup_bars=len(bars), session_type="regular")
    values.update(overrides)
    return snapshot_row(snapshot, **values)


def count(engine, table=technical_snapshots):
    with transaction(engine) as session:
        return session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


class RowMappingTests(unittest.TestCase):
    def test_row_maps_scalars_and_json(self):
        bars, snaps = snapshots()
        snap = snaps[57]
        row = row_for(snap, bars)
        self.assertEqual((row["symbol"], row["interval"], row["technical_state"], row["confidence"]),
                         ("META", "5m", "breakout_watch", "HIGH"))
        self.assertEqual(row["snapshot_timestamp"], snap.timestamp.astimezone(timezone.utc).isoformat())
        self.assertEqual((row["ema20"], row["rsi14"], row["atr14"], row["vwap"]),
                         (snap.ema["ema20"], snap.rsi, snap.atr, snap.vwap))
        self.assertEqual(row["breakout_level"], snap.breakout_level)
        self.assertEqual(row["reasons"], list(snap.signal.reasons))
        self.assertEqual(row["support_levels"], json.loads(json.dumps(list(snap.support_levels))))
        self.assertEqual((row["ema_alignment"], row["vwap_position"]), (snap.ema_state["alignment"],
                                                                         snap.vwap_state["position"]))
        self.assertTrue(row["is_completed_bar"])
        self.assertNotIn("index", row)
        self.assertEqual(len(row["content_hash"]), 64)
        validate_row(row)

    def test_hash_is_deterministic_and_material(self):
        bars, snaps = snapshots()
        a, b = row_for(snaps[-1], bars), row_for(snaps[-1], bars)
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(row_for(snaps[-1], bars, provider_delay_seconds=0)["content_hash"], a["content_hash"])
        self.assertNotEqual(row_for(snaps[-1], bars, warmup_bars=10)["content_hash"], a["content_hash"])
        changed = replace(snaps[-1], rsi=snaps[-1].rsi + 1e-9)
        self.assertNotEqual(row_for(changed, bars)["content_hash"], a["content_hash"])
        self.assertEqual(set(MATERIAL_FIELDS) - set(a), set())

    def test_non_default_configuration_is_rejected(self):
        bars = SCENARIOS["range"]("META")
        snap = TechnicalEngine(TechnicalConfig(ema_periods=(5, 10, 20, 50))).analyze(bars)
        with self.assertRaises(SnapshotRowError):
            row_for(snap, bars)
        snap = TechnicalEngine(TechnicalConfig(rsi_period=7)).analyze(bars)
        with self.assertRaises(SnapshotRowError):
            row_for(snap, bars)

    def test_volume_is_exact_canonical_text(self):
        bars, snaps = snapshots()
        snap = replace(snaps[-1], volume=__import__("decimal").Decimal("1234567.1234567891230"))
        row = row_for(snap, bars)
        self.assertEqual(row["volume"], "1234567.123456789123")  # Exact; canonical (no exponent, no trailing zeros).
        self.assertEqual(row_for(replace(snaps[-1], volume=__import__("decimal").Decimal("1E+3")), bars)["volume"], "1000")
        self.assertIsInstance(row["average_volume"], float)
        for bad in ("1E+3", "1000.0", "-1", "NaN", 1000, 1000.5):
            with self.subTest(volume=bad), self.assertRaises(SnapshotRowError):
                validate_row(dict(row, volume=bad))

    def test_validate_row(self):
        bars, snaps = snapshots()
        row = row_for(snaps[-1], bars)
        for mutate in (dict(technical_state="BUY"), dict(confidence="certain"), dict(interval="2h"),
                       dict(price=row["price"] + 1)):
            with self.subTest(mutate=mutate), self.assertRaises(SnapshotRowError):
                validate_row(dict(row, **mutate))
        with self.assertRaises(SnapshotRowError):
            validate_row({k: v for k, v in row.items() if k != "reasons"})


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.bars, self.snaps = snapshots()

    def test_same_snapshot_twice_is_one_row(self):
        row = row_for(self.snaps[-1], self.bars)
        first, second = persist_technical_snapshot(self.engine, row), persist_technical_snapshot(self.engine, row)
        self.assertEqual((first["outcome"], second["outcome"], first["id"]), ("inserted", "duplicate", second["id"]))
        self.assertEqual(count(self.engine), 1)

    def test_conflict_is_rejected_recorded_once_and_existing_row_untouched(self):
        row = row_for(self.snaps[-1], self.bars)
        persist_technical_snapshot(self.engine, row)
        rewrite = row_for(self.snaps[-1], self.bars, warmup_bars=999)
        first = persist_technical_snapshot(self.engine, rewrite)
        again = persist_technical_snapshot(self.engine, rewrite)
        self.assertEqual((first["outcome"], first["conflict_recorded"]), ("conflict", True))
        self.assertEqual((again["outcome"], again["conflict_recorded"]), ("conflict", False))
        self.assertEqual((count(self.engine), count(self.engine, technical_snapshot_conflicts)), (1, 1))
        with transaction(self.engine) as session:
            repo = TechnicalSnapshotRepository(session)
            stored = repo.snapshots("META", "5m")[0]
            self.assertEqual((stored["content_hash"], stored["warmup_bars"]), (row["content_hash"], row["warmup_bars"]))
            conflict = repo.conflicts("META", "5m")[0]
            self.assertEqual((conflict["existing_hash"], conflict["rejected_hash"]),
                             (row["content_hash"], rewrite["content_hash"]))
            self.assertEqual(conflict["rejected_snapshot"]["warmup_bars"], 999)

    def test_distinct_identities_are_separate_rows(self):
        base = row_for(self.snaps[-1], self.bars)
        nvda_bars, nvda = snapshots(symbol="NVDA")
        rows = [base, row_for(self.snaps[-2], self.bars), row_for(nvda[-1], nvda_bars),
                row_for(self.snaps[-1], self.bars, engine_version="phase4c-v3")]
        daily = replace(self.snaps[-1], interval="1d",
                        timestamp=datetime.combine(date(2026, 9, 21), time(0), tzinfo=EXCHANGE_TZ))
        rows.append(row_for(daily, self.bars, session_type=None))
        outcomes = [persist_technical_snapshot(self.engine, r)["outcome"] for r in rows]
        self.assertEqual(outcomes, ["inserted"] * 5)
        self.assertEqual(count(self.engine), 5)
        with transaction(self.engine) as session:
            repo = TechnicalSnapshotRepository(session)
            self.assertEqual(len(repo.snapshots("META", "5m")), 3)
            self.assertEqual(len(repo.snapshots("META", "5m", engine_version="phase4c-v2")), 2)
            window = repo.snapshots("META", "5m", start=self.snaps[-1].timestamp,
                                    end=self.snaps[-1].timestamp + timedelta(minutes=5))
            self.assertEqual(len(window), 2)  # Both engine versions of the last bar.

    def test_json_and_timestamps_round_trip(self):
        row = row_for(self.snaps[57], self.bars)
        persist_technical_snapshot(self.engine, row)
        with transaction(self.engine) as session:
            stored = TechnicalSnapshotRepository(session).snapshots("META", "5m")[0]
        from persistence.technical_snapshot_repository import row_from_db
        self.assertEqual(content_hash(row_from_db(stored)), row["content_hash"])  # Recomputable from storage.
        self.assertEqual(float(stored["relative_volume"]), row["relative_volume"])
        self.assertEqual(stored["snapshot_timestamp"], self.snaps[57].timestamp)
        self.assertEqual(stored["support_levels"], row["support_levels"])
        self.assertEqual(stored["reasons"], row["reasons"])
        self.assertEqual(stored["evidence"], row["evidence"])


class MigrationChainTests(unittest.TestCase):
    def test_chain_is_linear_through_0006(self):
        from alembic.script import ScriptDirectory
        from tests.test_persistence import migration_config
        script = ScriptDirectory.from_config(migration_config())
        chain = [r.revision for r in reversed(list(script.walk_revisions()))]
        self.assertEqual(chain[2:], ["0003_geo_anchor_registry", "0004_technical_snapshots",
                                     "0005_technical_fractional_vol", "0006_technical_numeric_volume"])
        self.assertEqual(script.get_heads(), ["0006_technical_numeric_volume"])


class SettingsTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        settings = load_technical_persistence_settings({})
        self.assertEqual((settings.enabled, settings.queue_size, settings.engine_version, settings.drain_timeout_seconds),
                         (False, 256, "phase4c-v2", 10.0))
        custom = load_technical_persistence_settings(dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="TRUE",
                                                          TECHNICAL_SNAPSHOT_QUEUE_SIZE="16",
                                                          TECHNICAL_SNAPSHOT_ENGINE_VERSION="phase4c-v2.1"))
        self.assertEqual((custom.enabled, custom.queue_size, custom.engine_version), (True, 16, "phase4c-v2.1"))
        for env in (dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="1"), dict(TECHNICAL_SNAPSHOT_QUEUE_SIZE="15"),
                    dict(TECHNICAL_SNAPSHOT_QUEUE_SIZE="4097"), dict(TECHNICAL_SNAPSHOT_QUEUE_SIZE="x"),
                    dict(TECHNICAL_SNAPSHOT_ENGINE_VERSION="Phase 4C"), dict(TECHNICAL_SNAPSHOT_DRAIN_TIMEOUT_SECONDS="31")):
            with self.subTest(env=env), self.assertRaises(TechnicalPersistenceConfigError):
                load_technical_persistence_settings(env)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        self.bars, self.snaps = snapshots()
        logging.getLogger("technical_shadow").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("technical_shadow").setLevel, logging.NOTSET)

    def test_accounting_persisted_duplicate_conflict(self):
        writer = technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: self.engine, capacity=16)
        rows = [row_for(s, self.bars) for s in self.snaps[-3:]]
        for row in rows + [rows[0], row_for(self.snaps[-1], self.bars, warmup_bars=1)]:
            self.assertTrue(writer.submit(row))
        result = writer.shutdown(drain=True, timeout=5)
        stats = result["stats"]
        self.assertTrue(result["stopped"])
        self.assertEqual((stats["queued"], stats["persisted"], stats["duplicate"], stats["conflict"], stats["failed"]),
                         (5, 4, 1, 1, 1))
        self.assertEqual(count(self.engine), 3)

    def test_queue_full_is_counted_and_submit_never_blocks(self):
        gate = __import__("threading").Event()

        def slow_engine():
            gate.wait(5)
            return self.engine
        writer = technical_shadow.TechnicalSnapshotWriter(engine_factory=slow_engine, capacity=16)
        rows = [row_for(s, self.bars) for s in self.snaps[-20:]]
        accepted = [writer.submit(r) for r in rows]
        gate.set()
        stats = writer.shutdown(drain=True, timeout=5)["stats"]
        self.assertGreaterEqual(accepted.count(False), 3)
        self.assertEqual(stats["dropped_queue_full"], accepted.count(False))
        self.assertEqual(stats["persisted"], accepted.count(True))

    def test_database_failure_is_counted_not_raised(self):
        def broken():
            raise RuntimeError("database down")
        writer = technical_shadow.TechnicalSnapshotWriter(engine_factory=broken, capacity=16)
        self.assertTrue(writer.submit(row_for(self.snaps[-1], self.bars)))
        stats = writer.shutdown(drain=True, timeout=5)["stats"]
        self.assertEqual((stats["failed"], stats["persisted"]), (1, 0))

    def test_invalid_row_is_counted(self):
        writer = technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: self.engine, capacity=16)
        writer.submit(dict(row_for(self.snaps[-1], self.bars), technical_state="BUY"))
        stats = writer.shutdown(drain=True, timeout=5)["stats"]
        self.assertEqual((stats["invalid_row"], stats["failed"]), (1, 1))
        self.assertEqual(count(self.engine), 0)


class RunnerParityTests(unittest.TestCase):
    """In-process: the runner's technical output is identical with persistence off and on (and on with a dead DB)."""

    def run_runner(self, environ, writer_factory=None):
        from technical.runner import main
        from tests.test_technical_runner import ENV, make_provider, route
        from tests.market_data_fakes import FakeSession
        technical_shadow._after_fork()  # Fresh module lifecycle for each run.
        out = io.StringIO()
        patches = [patch.object(technical_shadow, "_configured_writer", writer_factory)] if writer_factory else []
        for p in patches:
            p.start()
        try:
            with self.assertLogs("technical.runner", "INFO") as logs:
                code = main(["--text"], environ=dict(ENV, **environ), provider=make_provider(FakeSession(route=route)),
                            out=out)
        finally:
            for p in patches:
                p.stop()
            technical_shadow._after_fork()
        technical = [l for l in logs.output if "event=technical_snapshot" in l or "event=technical_run_finished" in l]
        persistence = [l for l in logs.output if "event=technical_persistence" in l]
        return code, technical, out.getvalue(), persistence

    def test_parity_on_off_and_outage(self):
        engine = sqlite_engine()
        logging.getLogger("technical_shadow").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("technical_shadow").setLevel, logging.NOTSET)
        off = self.run_runner({})
        on = self.run_runner(dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true"),
                             lambda: technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: engine, capacity=16))

        def dead():
            raise RuntimeError("database unavailable")
        outage = self.run_runner(dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true"),
                                 lambda: technical_shadow.TechnicalSnapshotWriter(engine_factory=dead, capacity=16))
        self.assertEqual(off[:3], on[:3])
        self.assertEqual(off[:3], outage[:3])
        self.assertEqual(off[0], 0)
        self.assertEqual(off[3], [])
        self.assertIn("persisted=6 duplicate=0 conflict=0 failed=0", on[3][0])
        self.assertIn("persisted=0 duplicate=0 conflict=0 failed=6", outage[3][0])
        self.assertEqual(count(engine), 6)  # META/NVDA x 1d/1h/5m latest completed snapshots.
        again = self.run_runner(dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true"),
                                lambda: technical_shadow.TechnicalSnapshotWriter(engine_factory=lambda: engine, capacity=16))
        self.assertIn("persisted=6 duplicate=6 conflict=0", again[3][0])  # Same window: idempotent rerun.
        self.assertEqual(count(engine), 6)

    def test_invalid_persistence_config_exits_2(self):
        from technical.runner import main
        with self.assertLogs("technical.runner", "ERROR"):
            self.assertEqual(main([], environ=dict(TECHNICAL_SNAPSHOT_QUEUE_SIZE="1")), 2)


class ToolsTests(unittest.TestCase):
    NOW = datetime(2026, 9, 23, 20, tzinfo=EXCHANGE_TZ)

    def stored(self, engine, rows, created_at=None):
        with transaction(engine) as session:
            repo = TechnicalSnapshotRepository(session)
            for row in rows:
                repo.store(row, now=created_at or self.NOW)

    def test_expected_grid(self):
        grid = expected_grid(CAL, "1h", date(2026, 11, 25), date(2026, 11, 30), self.NOW.replace(month=12))
        self.assertEqual(len(grid), 7 + 4)  # Wednesday full day; Thanksgiving closed; Friday half-day.
        self.assertEqual(len(expected_grid(CAL, "1d", date(2026, 9, 21), date(2026, 9, 26), self.NOW)), 3)
        self.assertEqual(len(expected_grid(CAL, "5m", date(2026, 9, 23), date(2026, 9, 24),
                                           datetime(2026, 9, 23, 10, tzinfo=EXCHANGE_TZ))), 6)

    def test_reconcile_and_audit_clean_and_problems(self):
        engine = sqlite_engine()
        bars, snaps = snapshots("range")
        self.stored(engine, [row_for(s, bars) for s in snaps[-10:]])
        out = io.StringIO()
        code = tools_main(["reconcile", "--symbol", "META", "--interval", "5m", "--start", "2026-09-21", "--end",
                           "2026-09-22"], engine=engine, calendar=CAL, now=self.NOW, out=out)
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["expected"], report["stored_identities"], len(report["missing"]),
                          report["unexpected"], report["duplicates"], report["conflicts"]), (0, 78, 10, 68, [], [], 0))
        out = io.StringIO()
        self.assertEqual(tools_main(["audit"], engine=engine, calendar=CAL, now=self.NOW, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())["problems"], 0)
        # Problems: a conflict, an unexpected engine version, a bar persisted before it completed.
        self.stored(engine, [row_for(snaps[-1], bars, warmup_bars=5)])
        self.stored(engine, [row_for(snaps[-1], bars, engine_version="phase9")])
        early = datetime(2026, 9, 21, 9, 31, tzinfo=EXCHANGE_TZ)
        self.stored(engine, [row_for(snaps[0], bars)], created_at=early)
        out = io.StringIO()
        self.assertEqual(tools_main(["audit"], engine=engine, calendar=CAL, now=self.NOW, out=out), 1)
        report = json.loads(out.getvalue())
        self.assertEqual((report["conflicts"], report["unexpected_engine_versions"], len(report["persisted_before_bar_completed"])),
                         (1, ["phase9"], 1))
        out = io.StringIO()
        tools_main(["status"], engine=engine, calendar=CAL, now=self.NOW, out=out)
        status = json.loads(out.getvalue())
        self.assertEqual(status["conflicts"], 1)
        self.assertEqual(sorted(g["engine_version"] for g in status["groups"]), ["phase4c-v2", "phase9"])

    def test_audit_rows_detects_invalid_values_and_hash_mismatch(self):
        engine = sqlite_engine()
        bars, snaps = snapshots("range")
        self.stored(engine, [row_for(snaps[-1], bars)])
        with transaction(engine) as session:
            rows = [dict(r._mapping) for r in session.execute(sa.select(technical_snapshots))]
        rows[0] = dict(rows[0], momentum="euphoric", ema20=None, rsi14=None, price=rows[0]["price"] + 1,
                       snapshot_timestamp=rows[0]["snapshot_timestamp"] + timedelta(minutes=2))
        report, problems = audit_rows(rows + [dict(rows[0])], [], CAL, engine_versions=["phase4c-v2"],
                                      providers=["tiingo"])
        self.assertEqual(len(report["duplicate_identity_groups"]), 1)
        self.assertEqual(report["invalid_enum_values"][0]["fields"], ["momentum"])
        self.assertEqual(len(report["missing_required_metrics"]), 2)
        self.assertEqual(len(report["timestamps_off_grid"]), 2)
        self.assertEqual(len(report["content_hash_mismatches"]), 2)
        self.assertEqual(report["unexpected_providers"], ["polygon"])
        self.assertGreater(problems, 0)

    def test_legacy_v1_rows_verified_not_flagged(self):
        from persistence.technical_snapshot_repository import MATERIAL_FIELDS, canonical_json
        engine = sqlite_engine()
        bars, snaps = snapshots("range")
        row = row_for(snaps[-1], bars, engine_version="phase4c-v1")
        legacy = dict(row, volume=float(row["volume"]))  # How 0005-era rows were hashed (volume as a JSON float).
        legacy["content_hash"] = content_hash(json.loads(canonical_json({k: legacy[k] for k in MATERIAL_FIELDS})))
        values = dict(legacy, id="00000000-0000-0000-0000-000000000001", created_at=self.NOW,
                      snapshot_timestamp=datetime.fromisoformat(row["snapshot_timestamp"]),
                      warmup_start=datetime.fromisoformat(row["warmup_start"]),
                      volume=__import__("decimal").Decimal(row["volume"]),
                      average_volume=__import__("decimal").Decimal(repr(row["average_volume"])),
                      relative_volume=__import__("decimal").Decimal(repr(row["relative_volume"])))
        with transaction(engine) as session:
            session.execute(sa.insert(technical_snapshots).values(**values))
        out = io.StringIO()
        self.assertEqual(tools_main(["audit"], engine=engine, calendar=CAL, now=self.NOW, out=out), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["legacy_v1_hash_rows"], report["content_hash_mismatches"]), (1, []))

    def test_reconcile_rows_pure(self):
        a, b = datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc), datetime(2026, 9, 21, 13, 35, tzinfo=timezone.utc)
        report = reconcile_rows([dict(snapshot_timestamp=a), dict(snapshot_timestamp=a),
                                 dict(snapshot_timestamp=b + timedelta(minutes=1))], [], [a, b])
        self.assertEqual((report["missing"], len(report["unexpected"]), len(report["duplicates"])), ([b.isoformat()], 1, 1))

    def test_tools_usage_and_config_errors(self):
        self.assertEqual(tools_main(["reconcile"], environ={}, out=io.StringIO()), 2)
        self.assertEqual(tools_main(["status"], environ={}, out=io.StringIO()), 2)  # DATABASE_URL required.
        self.assertEqual(tools_main(["bogus"], environ={}, out=io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
