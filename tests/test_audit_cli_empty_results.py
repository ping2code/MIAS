"""Phase 7E regression: human-readable audit CLI output with zero and non-zero results.

Covers ``persistence.news_audit`` (url-variants, repeats) and ``persistence.sec_audit`` (shared-accessions,
repeats), on SQLite and, when configured, a disposable PostgreSQL.

Before the fix, a zero-result ``url-variants`` or ``shared-accessions`` printed its count and then raised
``TypeError``. The printer chose ``result.get("groups") or result.get("events")``, and an empty ``groups`` list
fell through to a key those commands never return. The audits themselves were always correct.
"""
from io import StringIO
import json
import os
import unittest

from persistence import news_audit, sec_audit
from tests import test_news_audit as news_tests
from tests import test_news_persistence_adapter as news_adapter
from tests import test_persistence_postgres as foundation
from tests import test_sec_audit as sec_tests
from tests import test_sec_persistence_adapter as sec_adapter


def run(module, argv, engine):
    out, err = StringIO(), StringIO()
    code = module.main(argv, engine=engine, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class NewsAuditCliTests(unittest.TestCase):
    setUp = news_adapter.NewsAdapterTests.setUp
    persist = news_tests.NewsAuditTests.persist
    persist_all = news_tests.NewsAuditTests.persist_all

    def test_zero_results_human_output_exits_zero(self):
        self.assertEqual(run(news_audit, ["url-variants"], self.engine),
                         (0, "[url-variants] variant_group_count: 0\n", ""))
        self.assertEqual(run(news_audit, ["repeats"], self.engine), (0, "[repeats] repeated_events: 0\n", ""))
        # Rows exist but nothing qualifies: still zero groups, still clean.
        self.persist_all(news_tests.corpus.ENTRIES["meta_reuters"])
        self.assertEqual(run(news_audit, ["url-variants"], self.engine),
                         (0, "[url-variants] variant_group_count: 0\n", ""))

    def test_non_empty_human_output_unchanged_and_json_unchanged(self):
        self.persist_all(news_tests.variant("Meta fixture ad pricing update", "?id=7&utm_source=yahoo"),
                         news_tests.variant("Instagram fixture creator payouts grow", "?id=7&utm_source=google",
                                            publisher="Reuters"))
        code, human, _ = run(news_audit, ["url-variants"], self.engine)
        _, raw, _ = run(news_audit, ["url-variants", "--json"], self.engine)
        result = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertEqual(human.splitlines(), ["[url-variants] variant_group_count: 1"] +
                         [json.dumps(g, sort_keys=True, default=str) for g in result["groups"]])
        with news_audit.read_only(self.engine) as session:
            expected = news_audit.audit_url_variants(session)
        self.assertEqual(raw, json.dumps(expected, sort_keys=True, indent=2, default=str) + "\n")
        code, human, _ = run(news_audit, ["repeats", "--all"], self.engine)
        _, raw, _ = run(news_audit, ["repeats", "--all", "--json"], self.engine)
        events = json.loads(raw)["events"]
        self.assertEqual((code, len(events)), (0, 2))
        self.assertEqual(human.splitlines()[1:], [json.dumps(e, sort_keys=True, default=str) for e in events])

    def test_json_zero_results_unchanged(self):
        _, raw, _ = run(news_audit, ["url-variants", "--json"], self.engine)
        self.assertEqual((json.loads(raw)["variant_group_count"], json.loads(raw)["groups"]), (0, []))


class SecAuditCliTests(unittest.TestCase):
    setUp = sec_adapter.SecAdapterTests.setUp
    persist = sec_tests.SecAuditTests.persist
    persist_all = sec_tests.SecAuditTests.persist_all

    def test_zero_results_human_output_exits_zero(self):
        self.assertEqual(run(sec_audit, ["shared-accessions"], self.engine),
                         (0, "[shared-accessions] shared_accession_count: 0\n", ""))
        self.assertEqual(run(sec_audit, ["repeats"], self.engine), (0, "[repeats] repeated_events: 0\n", ""))
        self.persist_all(sec_tests.F["meta_8k"])
        self.assertEqual(run(sec_audit, ["shared-accessions"], self.engine),
                         (0, "[shared-accessions] shared_accession_count: 0\n", ""))

    def test_non_empty_human_output_unchanged_and_json_unchanged(self):
        self.persist_all(sec_tests.F["meta_form4"], sec_tests.joint(sec_tests.F["meta_form4"]))
        code, human, _ = run(sec_audit, ["shared-accessions"], self.engine)
        _, raw, _ = run(sec_audit, ["shared-accessions", "--json"], self.engine)
        result = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertEqual(human.splitlines(), ["[shared-accessions] shared_accession_count: 1"] +
                         [json.dumps(g, sort_keys=True, default=str) for g in result["groups"]])
        with sec_audit.read_only(self.engine) as session:
            expected = sec_audit.audit_shared_accessions(session)
        self.assertEqual(raw, json.dumps(expected, sort_keys=True, indent=2, default=str) + "\n")
        code, human, _ = run(sec_audit, ["repeats", "--all"], self.engine)
        _, raw, _ = run(sec_audit, ["repeats", "--all", "--json"], self.engine)
        events = json.loads(raw)["events"]
        self.assertEqual((code, len(events)), (0, 2))
        self.assertEqual(human.splitlines()[1:], [json.dumps(e, sort_keys=True, default=str) for e in events])

    def test_json_zero_results_unchanged(self):
        _, raw, _ = run(sec_audit, ["shared-accessions", "--json"], self.engine)
        self.assertEqual(json.loads(raw), dict(shared_accession_count=0, truncated=False, rows_scanned=0, groups=[]))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLNewsAuditCliTests(NewsAuditCliTests):
    setUp = foundation.PostgreSQLTests.setUp
    persist = news_tests.NewsAuditTests.persist
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgreSQLSecAuditCliTests(SecAuditCliTests):
    setUp = foundation.PostgreSQLTests.setUp
    persist = sec_tests.SecAuditTests.persist
    cleanup_schema = foundation.PostgreSQLTests.cleanup_schema


if __name__ == "__main__":
    unittest.main()
