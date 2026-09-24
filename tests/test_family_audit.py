"""Phase 2S read-only all-family audit: coexistence, cross-family isolation, integrity and pointer-rule checks."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic import command

from persistence import family_audit
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance
from persistence.repository import EventRepository
from tests import test_persistence_postgres as foundation
from tests.test_persistence import migration_config

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def suites():
    """The six families' pinned readiness corpora (imported lazily; each module owns its replay)."""
    from tests import (test_macro_persistence_readiness as macro, test_treasury_persistence_readiness as treasury,
                       test_geopolitical_persistence_readiness as geo, test_fed_persistence_adapter as fed,
                       test_sec_persistence_adapter as sec, test_news_persistence_adapter as news)
    from tests import (macro_readiness_corpus, treasury_readiness_corpus, geopolitical_readiness_corpus,
                       fed_readiness_corpus, sec_readiness_corpus, news_readiness_corpus)
    return dict(macro=(macro, macro_readiness_corpus), treasury=(treasury, treasury_readiness_corpus),
                geopolitical=(geo, geopolitical_readiness_corpus), fed=(fed, fed_readiness_corpus),
                sec=(sec, sec_readiness_corpus), news=(news, news_readiness_corpus))


class FamilyAuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        self.addCleanup(self.engine.dispose)

    def audit(self):
        with family_audit.read_only(self.engine) as session:
            return family_audit.audit_families(session)

    def replay_all(self):
        with patch("sys.stdout"):
            for module, _ in suites().values():
                self.assertEqual(module.replay(self.engine)["mismatches"], [])

    def test_six_families_coexist_with_their_own_counts(self):
        self.replay_all()
        result = self.audit()
        self.assertEqual((result["integrity_findings"], result["pointer_rule"]["violation_count"]), (0, 0))
        self.assertGreater(result["pointer_rule"]["versions_evaluated"], 30)  # Held versions exist in five families.
        for family, (_, corpus) in suites().items():
            with self.subTest(family=family):
                expected = corpus.load_corpus()[0]["expected_counts"]
                counts = result["families"][family]
                self.assertEqual((counts["events"], counts["versions"], counts["provenance"]),
                                 (expected["events"], expected["event_versions"], expected["event_provenance"]))
                self.assertEqual(counts["current_pointers"], counts["events"])
                self.assertEqual(counts["score_history"] + counts["decision_history"] + counts["ai_history"],
                                 expected["event_history"])

    def record(self, family, key, url, *, identity_version=None, headline="Same-looking headline"):
        normalized = dict(schema_version=1, normalizer_version="t", headline=headline, summary="", source_name="s",
                          publisher="p", canonical_url=url, event_type=next(iter(family_audit.EVENT_TYPES[family])),
                          market_scope=None, published_at=NOW, publication_date=None, timestamp_precision="second",
                          publication_basis="t", stage=None, revision_key=None, attributes={})
        with transaction(self.engine) as session:
            return EventRepository(session).record(
                source_family=family, event_key=key,
                identity_version=identity_version or sorted(family_audit.IDENTITY_VERSIONS[family])[0],
                version_key="v1", normalized=normalized, observed_at=NOW, make_current=True)

    def test_identical_keys_urls_and_times_never_collide_across_families(self):
        url = "https://www.federalreserve.gov/newsevents/pressreleases/mias-fixture.htm"
        rows = [self.record(family, "a" * 64, url) for family in family_audit.FAMILIES]
        self.assertEqual(len({r["event_id"] for r in rows}), 6)  # Same key, URL, headline and time: six events.
        with transaction(self.engine) as session:
            anchors = session.execute(sa.select(events.c.source_family, events.c.current_version_id)).all()
        self.assertEqual({f: v for f, v in anchors}, {family: r["id"] for family, r in zip(family_audit.FAMILIES, rows)})
        self.assertEqual(self.audit()["integrity_findings"], 0)

    def test_real_adapters_same_url_in_fed_and_news_stay_separate(self):
        from persistence.fed_shadow import persist_fed
        from persistence.news_shadow import persist_news
        from tests.test_fed_persistence_adapter import sample as fed_sample
        from tests.test_news_persistence_adapter import sample as news_sample
        fed = fed_sample()
        news = dict(news_sample(), url=fed["url"], headline=fed["headline"] + " as Nvidia rallies",
                    published_at=fed["published_at"])
        first = persist_fed(self.engine, fed, NOW, report=True)["version"]
        second = persist_news(self.engine, news, NOW, report=True)["version"]
        self.assertNotEqual(first["event_id"], second["event_id"])
        result = self.audit()
        self.assertEqual((result["families"]["fed"]["events"], result["families"]["news"]["events"]), (1, 1))
        self.assertEqual(result["integrity_findings"], 0)

    def test_injected_corruption_is_detected_not_repaired(self):
        self.replay_all()
        with transaction(self.engine) as session:  # Deliberate fixture corruption in a scratch database.
            news_version = session.execute(sa.select(event_versions.c.id).join(events, events.c.id == event_versions.c.event_id)
                                           .where(events.c.source_family == "news").limit(1)).scalar_one()
            session.execute(event_versions.update().where(event_versions.c.id == news_version).values(event_type="sec_filing"))
            session.execute(event_provenance.update().where(event_provenance.c.event_version_id == news_version)
                            .values(attributes={"role": "fed_monetary_policy_release"}))
            victim = session.execute(sa.select(events.c.id).where(events.c.source_family == "sec").limit(1)).scalar_one()
            session.execute(events.update().where(events.c.id == victim).values(current_version_id=None))
            session.execute(events.update().where(events.c.source_family == "macro").values(identity_version="macro-v9"))
        result = self.audit()
        findings = {k: v["count"] for k, v in result["integrity"].items() if v["count"]}
        self.assertEqual(set(findings), {"unexpected_event_type", "event_type_family_crossover", "provenance_role_not_of_family",
                                         "missing_current_pointer", "unexpected_identity_version"})
        self.assertEqual(result["integrity"]["event_type_family_crossover"]["sample"], [["sec_filing", ["news", "sec"]]])
        self.assertEqual(result["integrity"]["multiple_current_pointers"]["count"], 0)
        with transaction(self.engine) as session:  # Nothing was repaired.
            self.assertIsNone(session.execute(sa.select(events.c.current_version_id).where(events.c.id == victim)).scalar_one())

    def test_pointer_rule_violation_is_reported(self):
        from persistence.fed_shadow import persist_fed
        from tests.test_fed_persistence_adapter import sample as fed_sample
        base = persist_fed(self.engine, fed_sample("policy_action"), NOW, report=True)["version"]
        newer = persist_fed(self.engine, fed_sample("synthetic_material_revision"), NOW + timedelta(minutes=1), report=True)
        self.assertEqual(newer["promotion"], "newer_material")
        self.assertEqual(self.audit()["pointer_rule"]["violation_count"], 0)
        with transaction(self.engine) as session:  # Point current back at the superseded version (same event: FK allows it).
            session.execute(events.update().where(events.c.id == base["event_id"]).values(current_version_id=base["id"]))
        rule = self.audit()["pointer_rule"]
        self.assertEqual((rule["violation_count"], rule["violations"][0]["family"], rule["violations"][0]["reason"]),
                         (1, "fed", "newer_material"))

    def test_audit_only_reads_and_is_bounded(self):
        self.replay_all()
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.audit()
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN", "SET TRANSACTION")) for s in statements))
        with family_audit.read_only(self.engine) as session:
            limited = family_audit.pointer_rule_check(session, max_events=2)
            self.assertTrue(limited["truncated"])
            with self.assertRaises(ValueError):
                family_audit.pointer_rule_check(session, max_events=0)

    def test_cli_json_usage_and_unconfigured_database(self):
        self.replay_all()
        out = StringIO()
        self.assertEqual(family_audit.main(["--json"], engine=self.engine, out=out, err=StringIO()), 0)
        self.assertEqual(json.loads(out.getvalue())["integrity_findings"], 0)
        out = StringIO()
        self.assertEqual(family_audit.main([], engine=self.engine, out=out, err=StringIO()), 0)
        self.assertTrue(out.getvalue().startswith("[family-audit] integrity findings: 0; pointer rule violations: 0"))
        self.assertEqual(family_audit.main(["--max-events", "0"], engine=self.engine, out=StringIO(), err=StringIO()), 2)
        err = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(family_audit.main([], out=StringIO(), err=err), 3)
        self.assertEqual(err.getvalue().strip(), "database not configured: DATABASE_URL is required")
        err = StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://mias_test_user:pw@127.0.0.1:1/mias_test_x",
                                     "DB_CONNECT_TIMEOUT_SECONDS": "1"}, clear=True):
            self.assertEqual(family_audit.main([], out=StringIO(), err=err), 3)
        self.assertNotIn("pw", err.getvalue())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLFamilyAuditTests(FamilyAuditTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


if __name__ == "__main__":
    unittest.main()
