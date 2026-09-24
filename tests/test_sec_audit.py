"""Phase 2P read-only SEC audits: shared accessions and repeat observations (SQLite and live PostgreSQL)."""
from datetime import timedelta
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import sqlalchemy as sa

from persistence import sec_audit
from persistence.database import transaction
from persistence.sec_shadow import persist_sec
from tests import sec_readiness_corpus as corpus
from tests import test_persistence_postgres as foundation
from tests import test_sec_persistence_adapter as adapter
from tests.test_sec_persistence_adapter import NOW

F = corpus.FILINGS


def collected(*filings):
    """Real collector submissions for the given raw filings (fresh in-memory Redis)."""
    return corpus.run_collector([dict(f) for f in filings], corpus.SecMemoryRedis())[1]


def joint(filing, symbol="NVDA"):
    return dict(filing, symbol=symbol)


class SecAuditTests(unittest.TestCase):
    setUp = adapter.SecAdapterTests.setUp

    def persist(self, event, observed_at=NOW):
        return persist_sec(self.engine, event, observed_at, report=True)

    def persist_all(self, *filings, observed_at=NOW):
        for event, _ in collected(*filings):
            self.persist(event, observed_at)

    def shared(self, **kwargs):
        with sec_audit.read_only(self.engine) as session:
            return sec_audit.audit_shared_accessions(session, **kwargs)

    def repeats(self, **kwargs):
        with sec_audit.read_only(self.engine) as session:
            return sec_audit.audit_repeat_observations(session, **kwargs)

    def test_no_shared_accessions_reports_zero(self):
        self.persist_all(F["meta_8k"], F["meta_8k_same_day"], F["meta_8ka"], F["nvda_8k"])
        self.assertEqual(self.shared(), dict(shared_accession_count=0, truncated=False, rows_scanned=0, groups=[]))

    def test_same_accession_under_two_issuers_is_reported_not_merged(self):
        self.persist_all(F["meta_form4"], joint(F["meta_form4"]), F["meta_8k"])
        result = self.shared()
        self.assertEqual(result["shared_accession_count"], 1)
        group = result["groups"][0]
        self.assertEqual((group["accession"], group["count"], group["symbols"], group["forms"]),
                         ("0001209191-26-054321", 2, ["META", "NVDA"], ["4"]))
        self.assertEqual(len(group["urls"]), 2)  # Issuer-specific archive paths.
        self.assertEqual(len(set(group["event_ids"])), 2)
        self.assertEqual(sorted(m["symbols"] for m in group["members"]), [["META"], ["NVDA"]])
        with transaction(self.engine) as session:  # Nothing merged or rewritten.
            self.assertEqual(session.execute(sa.text("SELECT count(*) FROM events")).scalar_one(), 3)

    def test_shared_paging_is_bounded_and_deterministic(self):
        pairs = [F["meta_form4"], F["meta_form4_same_day"], F["meta_form144"]]
        self.persist_all(*pairs, *(joint(f) for f in pairs))
        full = self.shared(batch_size=1)
        self.assertEqual([g["accession"] for g in full["groups"]], sorted(f["accession_number"] for f in pairs))
        self.assertEqual(full, self.shared(batch_size=500))
        truncated = self.shared(max_groups=2, batch_size=1)
        self.assertEqual((truncated["truncated"], len(truncated["groups"])), (True, 2))
        for bad in (dict(max_groups=0), dict(batch_size=0), dict(batch_size=5001), dict(max_groups="1")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.shared(**bad)

    def test_repeat_observations_use_durable_first_and_last_seen(self):
        submissions = collected(F["meta_8k"], F["meta_10q"])
        for event, _ in submissions:
            self.persist(event, NOW)
        self.persist(submissions[0][0], NOW + timedelta(days=1, minutes=5))  # Seen again after Redis expiry.
        self.persist(submissions[0][0], NOW + timedelta(days=2))  # And again after Redis loss.
        result = self.repeats()
        self.assertEqual((result["events_scanned"], result["repeated_events"], result["note"]), (2, 1, sec_audit.REPEAT_NOTE))
        [item] = result["events"]
        self.assertEqual((item["accession"], item["forms"], item["versions"], item["repeat_observed"]),
                         ("0001326801-26-000101", ["8-K"], 1, True))
        self.assertEqual(item["span_seconds"], 2 * 86400)
        self.assertEqual((item["observation_count"], item["duplicate_count"]), (None, None))  # Never inferred.
        self.assertTrue(item["first_observed"].startswith("2026-09-23T12:00"))
        everything = self.repeats(repeated_only=False)
        self.assertEqual([e["repeat_observed"] for e in everything["events"]], [True, False])

    def test_repeat_paging_is_bounded(self):
        submissions = collected(F["meta_8k"], F["meta_10q"], F["meta_10k"])
        for index, (event, _) in enumerate(submissions):
            self.persist(event, NOW + timedelta(minutes=index))
            self.persist(event, NOW + timedelta(hours=1, minutes=index))
        self.assertEqual(self.repeats(batch_size=1), self.repeats())
        limited = self.repeats(max_events=2, batch_size=1)
        self.assertEqual((limited["events_scanned"], limited["truncated"], len(limited["events"])), (2, True, 2))

    def test_audits_only_read(self):
        self.persist_all(F["meta_form4"], joint(F["meta_form4"]))
        statements = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.engine, "before_cursor_execute", observe)
        try:
            self.shared()
            self.repeats(repeated_only=False)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", observe)
        self.assertTrue(statements)
        self.assertTrue(all(s.lstrip().upper().startswith(("SELECT", "BEGIN", "SET TRANSACTION")) for s in statements),
                        statements)

    def test_cli_json_usage_and_unconfigured_database(self):
        self.persist_all(F["meta_form4"], joint(F["meta_form4"]))
        out, err = StringIO(), StringIO()
        self.assertEqual(sec_audit.main(["shared-accessions", "--json"], engine=self.engine, out=out, err=err), 0)
        self.assertEqual(json.loads(out.getvalue())["shared_accession_count"], 1)
        out = StringIO()
        self.assertEqual(sec_audit.main(["repeats"], engine=self.engine, out=out, err=err), 0)
        self.assertEqual(out.getvalue().splitlines()[0], "[repeats] repeated_events: 0")
        self.assertEqual(sec_audit.main(["repeats", "--batch-size", "0"], engine=self.engine, out=out, err=err), 2)
        with patch("sys.stderr"):
            self.assertEqual(sec_audit.main(["unknown"], out=out, err=err), 2)
        err = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sec_audit.main(["repeats"], out=StringIO(), err=err), 3)
        self.assertEqual(err.getvalue().strip(), "database not configured: DATABASE_URL is required")
        err = StringIO()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://mias_test_user:pw@127.0.0.1:1/mias_test_x",
                                     "DB_CONNECT_TIMEOUT_SECONDS": "1"}, clear=True):
            self.assertEqual(sec_audit.main(["shared-accessions"], out=StringIO(), err=err), 3)
        self.assertNotIn("pw", err.getvalue())
        self.assertNotIn("mias_test_user", err.getvalue())


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLSecAuditTests(SecAuditTests):
    setUp = foundation.PostgreSQLTests.setUp
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


if __name__ == "__main__":
    unittest.main()
