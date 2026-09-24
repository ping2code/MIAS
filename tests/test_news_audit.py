"""Phase 2R read-only News audits: URL variants and repeat observations (SQLite and live PostgreSQL)."""
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import news_audit
from persistence.database import transaction
from persistence.news_shadow import persist_news
from tests import news_readiness_corpus as corpus
from tests import test_news_persistence_adapter as adapter
from tests import test_persistence_postgres as foundation
from tests.test_news_persistence_adapter import NOW

STORY = "https://www.example-news.test/markets/mias-fixture-story"


def collected(*entries, feed="google_meta"):
    """Real collector submissions for synthetic entries (fresh in-memory Redis)."""
    return [event for event, _ in corpus.run_collector(feed, [deepcopy(e) for e in entries], corpus.NewsMemoryRedis())[1]]


def variant(title, query, publisher="CNBC"):
    return corpus.entry(title, STORY + query, publisher=publisher)


class NewsAuditTests(unittest.TestCase):
    setUp = adapter.NewsAdapterTests.setUp

    def persist(self, event, observed_at=NOW):
        return persist_news(self.engine, event, observed_at, report=True)

    def persist_all(self, *entries, observed_at=NOW):
        for event in collected(*entries):
            self.persist(event, observed_at)

    def variants(self, **kwargs):
        with news_audit.read_only(self.engine) as session:
            return news_audit.audit_url_variants(session, **kwargs)

    def repeats(self, **kwargs):
        with news_audit.read_only(self.engine) as session:
            return news_audit.audit_repeat_observations(session, **kwargs)

    def test_no_variants_reports_zero(self):
        self.persist_all(corpus.ENTRIES["meta_reuters"], corpus.ENTRIES["meta_yahoo_publisher"], corpus.ENTRIES["no_link"])
        result = self.variants()
        self.assertEqual((result["variant_group_count"], result["groups"], result["events_scanned"]), (0, [], 3))

    def test_query_variants_are_reported_never_merged(self):
        self.persist_all(variant("Meta fixture ad pricing update", "?id=7&utm_source=yahoo"),
                         variant("Instagram fixture creator payouts grow", "?id=7&utm_source=google", publisher="Reuters"),
                         corpus.ENTRIES["meta_reuters"])
        result = self.variants()
        self.assertEqual(result["variant_group_count"], 1)
        group = result["groups"][0]
        self.assertEqual((group["host"], group["path"], group["count"], group["publishers"]),
                         ("www.example-news.test", "/markets/mias-fixture-story", 2, ["CNBC", "Reuters"]))
        self.assertEqual(group["urls"], sorted([STORY + "?id=7&utm_source=google", STORY + "?id=7&utm_source=yahoo"]))
        self.assertEqual(len(set(group["event_ids"])), 2)
        self.assertTrue(group["first_observed"] and group["last_observed"])
        with transaction(self.engine) as session:  # Nothing merged, canonicalized or rewritten.
            self.assertEqual(session.execute(sa.text("SELECT count(*) FROM events")).scalar_one(), 3)

    def test_case_only_url_differences_are_one_identity_not_a_variant(self):
        upper = variant("Meta fixture ad pricing update", "?id=7")
        lower = dict(upper, link=upper["link"].replace("www.example-news.test", "WWW.EXAMPLE-NEWS.TEST"),
                     title="Meta fixture ad pricing update (case)")
        self.persist_all(upper)
        self.persist_all(lower)
        self.assertEqual(self.variants()["variant_group_count"], 0)

    def test_variant_paging_is_bounded_and_deterministic(self):
        titles = ["Meta fixture alpha story", "Instagram fixture beta story", "WhatsApp fixture gamma story"]
        self.persist_all(*(variant(t, f"?v={i}") for i, t in enumerate(titles)))
        self.assertEqual(self.variants(batch_size=1), self.variants())
        limited = self.variants(max_events=2, batch_size=1)
        self.assertEqual((limited["truncated"], limited["events_scanned"], limited["groups"][0]["count"]), (True, 2, 2))
        for bad in (dict(max_events=0), dict(batch_size=0), dict(batch_size=5001), dict(max_events="1")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.variants(**bad)

    def test_repeat_observations_with_version_and_provenance_counts(self):
        events = collected(corpus.ENTRIES["meta_reuters"]) + collected(corpus.ENTRIES["stale"], feed="google_nvda")
        for event in events:
            self.persist(event, NOW)
        self.persist(events[0], NOW + timedelta(days=1, minutes=5))  # After Redis expiry.
        rewrite = collected(corpus.ENTRIES["headline_rewrite"], feed="yahoo")[0]
        self.persist(collected(corpus.ENTRIES["nvda_yahoo"], feed="yahoo")[0], NOW)
        self.persist(rewrite, NOW + timedelta(hours=2))  # Same URL, changed headline.
        other_feed = collected(corpus.ENTRIES["stale"], feed="google_meta")[0]
        self.persist(other_feed, NOW + timedelta(days=2))  # Same article via another feed.
        result = self.repeats()
        self.assertEqual((result["events_scanned"], result["repeated_events"], result["note"]), (3, 3, news_audit.REPEAT_NOTE))
        by_url = {e["canonical_url"]: e for e in result["events"]}
        meta = by_url[corpus.ENTRIES["meta_reuters"]["link"]]
        self.assertEqual((meta["versions"], meta["provenance"], meta["span_seconds"]), (1, 1, 86700))
        self.assertEqual((meta["observation_count"], meta["duplicate_count"]), (None, None))  # Never inferred.
        self.assertEqual(by_url[corpus.ENTRIES["nvda_yahoo"]["link"]]["versions"], 2)
        self.assertEqual(by_url[corpus.ENTRIES["stale"]["link"]]["provenance"], 2)
        self.persist(collected(corpus.ENTRIES["related_cnbc"], feed="google_nvda")[0], NOW)
        everything = self.repeats(repeated_only=False)
        self.assertEqual((everything["events_scanned"], len(everything["events"]),
                          sum(not e["repeat_observed"] for e in everything["events"])), (4, 4, 1))
        self.assertEqual(self.repeats(batch_size=1), self.repeats())

    def test_audits_only_read(self):
        self.persist_all(variant("Meta fixture alpha story", "?a=1"), variant("Instagram fixture beta story", "?a=2"))
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.variants()
            self.repeats(repeated_only=False)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN", "SET TRANSACTION")) for s in statements),
                        statements)

    def test_cli_json_usage_and_unconfigured_database(self):
        self.persist_all(variant("Meta fixture alpha story", "?a=1"), variant("Instagram fixture beta story", "?a=2"))
        out, err = StringIO(), StringIO()
        self.assertEqual(news_audit.main(["url-variants", "--json"], engine=self.engine, out=out, err=err), 0)
        self.assertEqual(json.loads(out.getvalue())["variant_group_count"], 1)
        out = StringIO()
        self.assertEqual(news_audit.main(["repeats"], engine=self.engine, out=out, err=err), 0)
        self.assertEqual(out.getvalue().splitlines()[0], "[repeats] repeated_events: 0")
        self.assertEqual(news_audit.main(["repeats", "--batch-size", "0"], engine=self.engine, out=out, err=err), 2)
        with patch("sys.stderr"):
            self.assertEqual(news_audit.main(["unknown"], out=out, err=err), 2)
        err = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(news_audit.main(["repeats"], out=StringIO(), err=err), 3)
        self.assertEqual(err.getvalue().strip(), "database not configured: DATABASE_URL is required")
        err = StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://mias_test_user:pw@127.0.0.1:1/mias_test_x",
                                     "DB_CONNECT_TIMEOUT_SECONDS": "1"}, clear=True):
            self.assertEqual(news_audit.main(["url-variants"], out=StringIO(), err=err), 3)
        self.assertNotIn("pw", err.getvalue())
        self.assertNotIn("mias_test_user", err.getvalue())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLNewsAuditTests(NewsAuditTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


if __name__ == "__main__":
    unittest.main()
