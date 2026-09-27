"""Phase 7D loader: MarketContext file, durable technical selection and news/SEC as-of reconstruction.

The database is a file SQLite, seeded only through the existing MIAS writers. Replay and no-future-leak scenarios
are covered, and sockets are disabled.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
import socket
import tempfile
import unittest
import uuid
from unittest.mock import patch

import sqlalchemy as sa

from evidence_packet import loader as L
from evidence_packet import models as m
from market_data.models import EXCHANGE_TZ
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import event_history, event_versions, metadata
from persistence.news_shadow import persist_news
from persistence.sec_shadow import persist_sec
from persistence.technical_snapshot_repository import TechnicalSnapshotRepository, snapshot_row
from technical.engine import TechnicalEngine
from tests.market_data_fakes import calendar_bars
from tests.test_evidence_packet import mctx
from tests.test_evidence_packet_adapters import CANARY, rss_event, sec_event, technical_rows
from market_data.calendar import default_calendar

CAL = default_calendar()
DAY = date(2026, 9, 23)
AS_OF = datetime(2026, 9, 23, 20, 5, tzinfo=timezone.utc)
UTC = timezone.utc


def et(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EXCHANGE_TZ)


def five_minute_rows(symbol="META"):
    """snapshot_row dicts for the last three 5m bars of 2026-09-23 (starts 15:45, 15:50, 15:55 ET)."""
    days = CAL.trading_days(date(2026, 9, 8), DAY)
    bars = calendar_bars(symbol, days, 5, base=740.0)
    snaps = TechnicalEngine(calendar=CAL).replay(bars)
    return [snapshot_row(s, provider="polygon", engine_version="phase4c-v2", provider_delay_seconds=900,
                         warmup_start=bars[0].timestamp, warmup_bars=len(bars), session_type="regular")
            for s in snaps[-3:]]


class DB:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.engine = make_engine(DatabaseSettings(url=f"sqlite:///{os.path.join(self.dir.name, 'm.db')}",
                                                   sqlite_enabled=True))
        metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()
        self.dir.cleanup()

    def snapshot(self, row, created_at):
        with transaction(self.engine) as session:
            return TechnicalSnapshotRepository(session).store(row, now=created_at)

    def news(self, event, observed_at, recorded_at=None, outcome="processed"):
        version = persist_news(self.engine, dict(event, news_collector_outcome=outcome), observed_at)
        self._stamp(version["id"], recorded_at or observed_at)
        return version

    def sec(self, event, observed_at, recorded_at=None):
        version = persist_sec(self.engine, event, observed_at)
        self._stamp(version["id"], recorded_at or observed_at)
        return version

    def _stamp(self, version_id, recorded_at):
        """Test-only: the repository stamps recorded_at with the wall clock; replay needs fixture times."""
        with transaction(self.engine) as session:
            session.execute(event_versions.update().where(event_versions.c.id == version_id).values(recorded_at=recorded_at))
            session.execute(event_history.update().where(event_history.c.event_version_id == version_id)
                            .values(recorded_at=recorded_at))

    def counts(self):
        with self.engine.connect() as connection:
            return {t.name: connection.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                    for t in metadata.sorted_tables}


class Base(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)

    def setUp(self):
        self.db = DB()
        self.addCleanup(self.db.close)

    def load(self, as_of=AS_OF, hours=72):
        return L.load_sources(self.db.engine, symbol="META", as_of=as_of, lookback_hours=hours, calendar=CAL)


class MarketContextFileTests(Base):
    def write(self, data):
        path = os.path.join(self.db.dir.name, "ctx.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def test_round_trip_single_and_runner_wrapper(self):
        ctx = mctx()
        for data in (ctx.to_dict(), dict(contexts=[mctx("NVDA").to_dict(), ctx.to_dict()], errors={})):
            loaded = L.load_market_context(self.write(data), symbol="META", as_of=AS_OF)
            self.assertEqual(loaded, ctx)
            self.assertEqual(loaded.to_dict(), ctx.to_dict())
        self.assertIsInstance(loaded.symbol_context.return_since_prev_close, type(ctx.symbol_context.return_since_prev_close))

    def test_mismatches_and_errors(self):
        path = self.write(mctx().to_dict())
        with self.assertRaises(L.InputError):
            L.load_market_context(path, symbol="NVDA", as_of=AS_OF)
        with self.assertRaises(L.InputError):
            L.load_market_context(path, symbol="META", as_of=AS_OF - timedelta(minutes=1))  # context.now 20:05Z.
        with self.assertRaises(L.SourceIncomplete):
            L.load_market_context(path + ".missing", symbol="META", as_of=AS_OF)
        bad = mctx().to_dict()
        bad["symbol_context"]["return_since_prev_close"] = "-0.02100"  # Not canonical: round-trip must fail.
        with self.assertRaises(L.IntegrityFailure):
            L.load_market_context(self.write(bad), symbol="META", as_of=AS_OF)
        extra = dict(mctx().to_dict(), verdict="bullish")
        with self.assertRaises(L.InputError):
            L.load_market_context(self.write(extra), symbol="META", as_of=AS_OF)
        with self.assertRaises(L.InputError):
            L.load_market_context(self.write(dict(contexts=[])), symbol="META", as_of=AS_OF)


class TechnicalLoadTests(Base):
    def test_all_intervals_loaded_and_verified(self):
        rows = technical_rows()
        for row in rows.values():
            self.db.snapshot(row, created_at=datetime(2026, 9, 23, 20, 1, tzinfo=UTC))
        loaded, diag, _ = self.load()
        self.assertEqual(loaded, rows)  # Exactly the durable row contract Phase 7C expects.
        self.assertEqual({k: v["status"] for k, v in diag.items()}, {"1d": "found", "1h": "found", "5m": "found"})

    def test_latest_eligible_inclusive_boundary_and_future_exclusion(self):
        early, middle, late = five_minute_rows()  # Bars end 15:50, 15:55, 16:00 ET.
        for row in (early, middle, late):
            self.db.snapshot(row, created_at=et(16, 1))
        pick = lambda as_of: self.load(as_of=as_of)[0].get("5m", {}).get("snapshot_timestamp")  # noqa: E731
        self.assertEqual(pick(et(16, 5)), late["snapshot_timestamp"])
        self.assertEqual(pick(et(15, 57)), None)  # Every row was recorded at 16:01 (created_at gate).

    def test_bar_end_cutoff_is_inclusive(self):
        early, middle, late = five_minute_rows()
        for row in (early, middle, late):
            self.db.snapshot(row, created_at=et(15, 0))  # Recorded "early" so only bar_end gates.
        pick = lambda as_of: self.load(as_of=as_of)[0]["5m"]["snapshot_timestamp"]  # noqa: E731
        self.assertEqual(pick(et(15, 55)), middle["snapshot_timestamp"])  # bar_end == as_of is eligible.
        self.assertEqual(pick(et(15, 54, )), early["snapshot_timestamp"])
        self.assertEqual(pick(et(15, 59)), middle["snapshot_timestamp"])  # 16:00 bar_end is in the future.

    def test_missing_intervals_reported(self):
        self.db.snapshot(technical_rows()["1d"], created_at=datetime(2026, 9, 23, 20, 1, tzinfo=UTC))
        loaded, diag, _ = self.load()
        self.assertEqual(set(loaded), {"1d"})
        self.assertEqual((diag["1h"]["status"], diag["5m"]["status"]), ("missing", "missing"))

    def test_corrupt_stored_row_is_integrity_failure(self):
        self.db.snapshot(technical_rows()["1h"], created_at=datetime(2026, 9, 23, 20, 1, tzinfo=UTC))
        with self.db.engine.begin() as connection:
            connection.execute(sa.text("UPDATE technical_snapshots SET rsi14 = 1.0"))
        with self.assertRaises(L.IntegrityFailure):
            self.load()


class EventLoadTests(Base):
    OBS = datetime(2026, 9, 23, 13, 7, tzinfo=UTC)

    def test_rss_and_sec_reconstructed_with_existing_identities(self):
        self.db.news(rss_event(), self.OBS)
        self.db.sec(sec_event(), self.OBS)
        _, _, events = self.load()
        by_family = {i.family: i for i in events.inputs}
        from evidence_packet.adapters import adapt_news_event
        rss = adapt_news_event(by_family["news"])
        sec = adapt_news_event(by_family["sec"])
        self.assertEqual((rss.identity_version, sec.identity_version, sec.event_key), ("news-url-v1", "sec-v1", "b" * 64))
        self.assertEqual((rss.facts.published_at, rss.upstream_score.impact_score, rss.delivery.alert_decision),
                         (datetime(2026, 9, 23, 13, 5, tzinfo=UTC), 75, "ALERT"))
        self.assertEqual((sec.facts.publication_date, sec.facts.accession_number), (date(2026, 9, 22), "0001326801-26-000123"))
        self.assertEqual(events.diagnostics["news"], dict(loaded=1, outside_lookback=0, no_provenance=0, passed=1))
        self.assertNotIn(CANARY, json.dumps([i.event for i in events.inputs], default=str))  # Never stored anyway.
        self.assertFalse(any(k.startswith("ai_") for i in events.inputs for k in i.event))

    def test_fingerprint_fallback_identity(self):
        self.db.news(rss_event(url="N/A"), self.OBS)
        from evidence_packet.adapters import adapt_news_event
        item = adapt_news_event(self.load()[2].inputs[0])
        self.assertEqual((item.identity_version, item.event_key), ("news-fingerprint-v1", "a" * 64))

    def test_latest_version_at_or_before_as_of_not_current_pointer(self):
        v1 = self.db.news(rss_event(), self.OBS)
        v2 = self.db.news(rss_event(headline="Meta announces AI partnership (updated)",
                                    published_at="2026-09-23T15:00:00+00:00"), datetime(2026, 9, 23, 15, 2, tzinfo=UTC))
        with self.db.engine.connect() as connection:
            current = connection.execute(sa.text("SELECT current_version_id FROM events")).scalar_one()
        self.assertEqual(uuid.UUID(str(current)), uuid.UUID(v2["id"]))  # The durable pointer moved to the later version.
        at_14 = self.load(as_of=datetime(2026, 9, 23, 14, tzinfo=UTC))[2].inputs
        self.assertEqual([i.event["headline"] for i in at_14], ["Meta announces AI partnership"])
        at_16 = self.load(as_of=datetime(2026, 9, 23, 16, tzinfo=UTC))[2].inputs
        self.assertEqual([i.event["headline"] for i in at_16], ["Meta announces AI partnership (updated)"])
        self.assertTrue(v1["id"] != v2["id"])

    def test_history_recorded_after_as_of_is_ignored(self):
        self.db.news(rss_event(), self.OBS, recorded_at=datetime(2026, 9, 23, 19, tzinfo=UTC))
        early = self.load(as_of=datetime(2026, 9, 23, 18, tzinfo=UTC))[2].inputs[0].event
        late = self.load(as_of=AS_OF)[2].inputs[0].event
        self.assertNotIn("impact_score", early)
        self.assertIsNone(early["alert_decision"])
        self.assertEqual((late["impact_score"], late["alert_decision"]), (75, "ALERT"))

    def test_publication_defines_window_observed_at_is_upper_bound_only(self):
        # Published inside the window (2026-09-20 20:05Z, 2026-09-23 20:05Z], observed BEFORE the window start:
        # previously missed by an observed_at window; now included because publication defines the window.
        self.db.news(rss_event(url="https://example.com/early-seen", headline="Meta early seen",
                               published_at="2026-09-23T10:00:00+00:00"), datetime(2026, 9, 19, 9, tzinfo=UTC))
        # Published before the window (observed inside it): not a candidate.
        self.db.news(rss_event(url="https://example.com/old", published_at="2026-09-19T10:00:00+00:00"),
                     datetime(2026, 9, 21, 10, tzinfo=UTC))
        # Observed after as_of: never loaded (observed_at is the availability upper bound).
        self.db.news(rss_event(url="https://example.com/future"), datetime(2026, 9, 23, 20, 30, tzinfo=UTC))
        # Date-only SEC filing on the window-start date: inside by publication date.
        self.db.sec(sec_event(published_at="2026-09-20"), datetime(2026, 9, 20, 22, tzinfo=UTC))
        _, _, events = self.load()
        self.assertEqual(sorted(i.event["headline"] for i in events.inputs),
                         ["META filed SEC Form 8-K", "Meta early seen"])
        self.assertEqual(events.diagnostics["news"], dict(loaded=1, outside_lookback=0, no_provenance=0, passed=1))
        self.assertEqual(events.diagnostics["sec"], dict(loaded=1, outside_lookback=0, no_provenance=0, passed=1))
        wide = self.load(hours=720)[2]
        self.assertEqual(sorted(i.event["url"] for i in wide.inputs if i.family == "news"),
                         ["https://example.com/early-seen", "https://example.com/old"])

    def test_latest_version_outside_window_is_counted(self):
        self.db.news(rss_event(published_at="2026-09-23T10:00:00+00:00"), datetime(2026, 9, 23, 10, 5, tzinfo=UTC))
        # A later version of the same URL (available by as_of) carries an older publication time.
        self.db.news(rss_event(headline="Meta corrected", published_at="2026-09-10T10:00:00+00:00"),
                     datetime(2026, 9, 23, 11, tzinfo=UTC))
        _, _, events = self.load()
        self.assertEqual(events.inputs, ())
        self.assertEqual(events.diagnostics["news"], dict(loaded=1, outside_lookback=1, no_provenance=0, passed=0))

    def test_unknown_publication_is_bounded_by_observation(self):
        self.db.news(rss_event(url="https://example.com/raw-recent", published_at="garbled"),
                     datetime(2026, 9, 23, 12, tzinfo=UTC))
        self.db.news(rss_event(url="https://example.com/raw-ancient", published_at="garbled"),
                     datetime(2026, 9, 1, 12, tzinfo=UTC))
        _, _, events = self.load()
        self.assertEqual([i.event["url"] for i in events.inputs], ["https://example.com/raw-recent"])

    def test_near_duplicate_outcome_is_carried_for_phase7c_accounting(self):
        event = {k: v for k, v in rss_event().items() if k not in ("impact_score", "impact_level", "score_reasons",
                                                                    "original_impact_score", "quality_adjustment",
                                                                    "alert_decision")}
        self.db.news(event, self.OBS, outcome="near_duplicate_suppressed")
        self.assertEqual(self.load()[2].inputs[0].collector_outcome, "near_duplicate_suppressed")

    def test_deterministic_input_order(self):
        for n in (3, 1, 2):
            self.db.news(rss_event(url=f"https://example.com/{n}", headline=f"Meta {n}"), self.OBS + timedelta(minutes=n))
        self.db.sec(sec_event(), self.OBS)
        from evidence_packet.adapters import adapt_news_event
        inputs = self.load()[2].inputs
        keys = [(i.family, adapt_news_event(i).identity_version, adapt_news_event(i).event_key) for i in inputs]
        self.assertEqual(len(keys), 4)
        self.assertEqual(keys, sorted(keys))  # (family, identity_version, event_key), never database row order.
        self.assertEqual(self.load()[2], self.load()[2])


class PurityTests(unittest.TestCase):
    def test_phase7c_modules_stay_io_free(self):
        import inspect
        from evidence_packet import adapters, assembler, models, serialization
        for module in (adapters, assembler, models, serialization):
            source = inspect.getsource(module)
            for token in ("open(", "make_engine", "sa.select", "session.execute", "datetime.now", "socket",
                          "evidence_packet.loader", "evidence_packet.runner"):
                self.assertNotIn(token, source, (module.__name__, token))


if __name__ == "__main__":
    unittest.main()
