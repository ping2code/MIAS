"""Phase 7D runner CLI covering:

- exit codes and strict/partial behavior;
- reproducibility and output formats (pretty, output file, dry run);
- read-only SQL (SQLite and a disposable PostgreSQL);
- zero network, no future leak, and META/NVDA generic behavior.
"""
from datetime import datetime, timedelta, timezone
import io
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from evidence_packet import runner
from evidence_packet.serialization import canonical_json, content_id
from market_data.calendar import default_calendar
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests.test_evidence_packet import mctx
from tests.test_evidence_packet_adapters import CANARY, rss_event, sec_event, technical_rows
from tests.test_evidence_packet_loader import DB

CAL = default_calendar()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AS_OF = "2026-09-23T20:05:00Z"
CREATED = datetime(2026, 9, 23, 20, 1, tzinfo=timezone.utc)
OBS = datetime(2026, 9, 23, 13, 7, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"score", "confidence", "direction", "sentiment", "recommendation", "buy", "sell", "call", "put",
                  "options", "overall", "verdict", "generated_at"}


def seed(db, symbol="META", news=True):
    for row in technical_rows(symbol).values():
        db.snapshot(row, created_at=CREATED)
    if news:
        db.news(rss_event(), OBS)
        db.news(rss_event(url="https://example.com/nvda", headline="Nvidia update", symbols=["NVDA"],
                          direct_symbols=["NVDA"]), OBS)
        db.sec(sec_event(), OBS)


def keys(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (key,)
            yield from keys(child, path + (key,))
    elif isinstance(value, list):
        for child in value:
            yield from keys(child, path + ("[]",))


class Base(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")), \
                patch("market_data.providers.build_provider", side_effect=AssertionError("provider built")):
            return super().run(result)

    def setUp(self):
        self.db = DB()
        self.addCleanup(self.db.close)
        self.ctx_path = self.context_file(mctx())

    def context_file(self, ctx, name="ctx.json"):
        path = os.path.join(self.db.dir.name, name)
        with open(path, "w") as handle:
            json.dump(dict(contexts=[ctx.to_dict()], errors={}), handle)
        return path

    def cli(self, *extra, symbol="META", as_of=AS_OF, ctx=True, engine=True, environ=None):
        argv = ["--symbol", symbol, "--as-of", as_of, *extra]
        if ctx:
            argv += ["--market-context", self.ctx_path if ctx is True else ctx]
        out, err = io.StringIO(), io.StringIO()
        code = runner.main(argv, environ={} if environ is None else environ,
                           engine=self.db.engine if engine else None, calendar=CAL, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class SuccessTests(Base):
    def test_strict_packet_contents_and_boundary(self):
        seed(self.db)
        code, text, err = self.cli()
        self.assertEqual(code, 0, err)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        packet = json.loads(text)
        self.assertEqual((packet["format_version"], packet["symbol"], packet["as_of"]),
                         ("phase7c-v1", "META", "2026-09-23T20:05:00+00:00"))
        self.assertRegex(packet["packet_id"], r"^sha256:[0-9a-f]{64}$")
        body = dict(packet)
        body.pop("packet_id")
        self.assertEqual(content_id(body), packet["packet_id"])
        self.assertEqual({k: v["availability"]["status"] for k, v in packet.items() if k in
                          ("market_context", "technical", "news")},
                         {"market_context": "available", "technical": "available", "news": "available"})
        self.assertEqual([t["interval"] for t in packet["technical"]["timeframes"]], ["1d", "1h", "5m"])
        for t in packet["technical"]["timeframes"]:
            self.assertLessEqual(datetime.fromisoformat(t["bar_end"]), datetime.fromisoformat(packet["as_of"]))
        families = sorted(i["facts"]["family"] for i in packet["news"]["items"])
        self.assertEqual(families, ["news", "sec"])
        self.assertEqual({e["reason"]: e["count"] for e in packet["news"]["excluded"]}, {"symbol_mismatch": 1})
        for path in keys(packet):
            if path[-1].lower() in FORBIDDEN_KEYS:
                self.assertTrue(path[:4] == ("technical", "timeframes", "[]", "row") or path[0] == "market_context", path)
        self.assertNotIn("ai_", text)
        self.assertNotIn(CANARY, text)
        diag = json.loads(err)["diagnostics"]
        self.assertEqual((diag["packet_id"], diag["news"]["included"], diag["sec"]["included"]), (packet["packet_id"], 1, 1))
        self.assertEqual({k: v["status"] for k, v in diag["technical"].items()}, {"1d": "found", "1h": "found", "5m": "found"})
        for secret in ("sqlite:///", "postgresql", CANARY, "ai_"):
            self.assertNotIn(secret, err)

    def test_reproducible_bytes_pretty_and_output_file(self):
        seed(self.db)
        first, second = self.cli()[1], self.cli()[1]
        self.assertEqual(first, second)
        pretty = self.cli("--pretty")[1]
        self.assertNotEqual(pretty, first)
        self.assertEqual(json.loads(pretty), json.loads(first))
        self.assertEqual(canonical_json(json.loads(pretty)) + "\n", first)
        path = os.path.join(self.db.dir.name, "packet.json")
        code, out, _ = self.cli("--output", path)
        self.assertEqual((code, out), (0, ""))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), first.encode("utf-8"))

    def test_empty_news_is_valid(self):
        seed(self.db, news=False)
        code, text, err = self.cli()
        self.assertEqual(code, 0, err)
        news = json.loads(text)["news"]
        self.assertEqual((news["availability"]["status"], news["items"]), ("available_empty", []))

    def test_nvda_generic(self):
        seed(self.db, "NVDA", news=False)
        seed(self.db, news=True)
        path = self.context_file(mctx("NVDA"), "nvda.json")
        code, text, err = self.cli(symbol="nvda", ctx=path)
        self.assertEqual(code, 0, err)
        packet = json.loads(text)
        self.assertEqual(packet["symbol"], "NVDA")
        self.assertEqual([i["facts"]["headline"] for i in packet["news"]["items"]], ["Nvidia update"])
        self.assertEqual({e["reason"]: e["count"] for e in packet["news"]["excluded"]}, {"symbol_mismatch": 2})

    def test_no_future_leak_end_to_end(self):
        seed(self.db)
        before = self.cli()[1]
        future = datetime(2026, 9, 23, 20, 30, tzinfo=timezone.utc)
        self.db.news(rss_event(url="https://example.com/late", headline="Meta late"), future)
        later_rows = technical_rows()
        for row in later_rows.values():  # Same bars recorded again later: duplicates, not new evidence.
            self.db.snapshot(row, created_at=future)
        self.assertEqual(self.cli()[1], before)  # Byte-identical: nothing after as_of leaks in.


class FailureTests(Base):
    def test_input_errors_exit_2(self):
        seed(self.db)
        for argv_as_of, symbol in (("2026-09-23T20:05:00", "META"), ("yesterday", "META"), (AS_OF, "ME;TA--"),
                                   (AS_OF, "meta' OR 1=1")):
            code, _, err = self.cli(symbol=symbol, as_of=argv_as_of)
            self.assertEqual(code, 2, (argv_as_of, symbol))
            self.assertEqual(json.loads(err)["exit_code"], 2)
        self.assertEqual(self.cli("--news-lookback-hours", "0")[0], 2)
        self.assertEqual(self.cli("--news-lookback-hours", "721")[0], 2)
        self.assertEqual(self.cli(ctx=self.context_file(mctx("NVDA"), "nvda.json"))[0], 2)  # Symbol mismatch.
        self.assertEqual(self.cli(as_of="2026-09-23T20:04:00Z")[0], 2)  # Context later than as_of.
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(runner.main(["--symbol", "META"], environ={}, out=io.StringIO(), err=io.StringIO()), 2)

    def test_source_incomplete_exit_3_with_diagnostics(self):
        code, _, err = self.cli()  # Empty database: no technical snapshots.
        report = json.loads(err)
        self.assertEqual((code, report["exit_code"]), (3, 3))
        self.assertEqual(report["diagnostics"]["missing_intervals"], ["1d", "1h", "5m"])
        seed(self.db)
        code, _, err = self.cli(ctx=False)
        self.assertEqual((code, json.loads(err)["diagnostics"]["market_context"]["status"]), (3, "not_supplied"))
        self.assertEqual(self.cli(ctx=self.ctx_path + ".missing")[0], 3)

    def test_allow_partial_is_explicit(self):
        code, text, err = self.cli("--allow-partial", ctx=False)
        self.assertEqual(code, 0, err)
        packet = json.loads(text)
        self.assertEqual(packet["market_context"]["availability"], {"status": "unavailable", "reasons": ["not_supplied"]})
        self.assertEqual(packet["technical"]["availability"]["status"], "unavailable")
        self.assertEqual({t["missing_reason"] for t in packet["technical"]["timeframes"]}, {"not_supplied"})
        self.assertEqual(json.loads(err)["diagnostics"]["missing_intervals"], ["1d", "1h", "5m"])

    def test_integrity_exit_4_and_database_exit_5(self):
        seed(self.db)
        with self.db.engine.begin() as connection:
            connection.execute(sa.text("UPDATE technical_snapshots SET rsi14 = 1.0 WHERE interval = '5m'"))
        self.assertEqual(self.cli()[0], 4)
        code, _, err = self.cli(engine=False, environ={})
        self.assertEqual((code, json.loads(err)["exit_code"]), (5, 5))
        self.assertNotIn("DATABASE_URL=", err)


class DryRunReadOnlyAndIsolationTests(Base):
    def test_dry_run_touches_nothing(self):
        with patch("evidence_packet.loader.load_sources", side_effect=AssertionError("database read")), \
                patch("persistence.database.make_engine", side_effect=AssertionError("engine")):
            code, text, _ = self.cli("--dry-run", engine=False, environ={})
        plan = json.loads(text)
        self.assertEqual((code, plan["mode"], plan["network"], plan["database_reads"]), (0, "dry_run", False, False))
        self.assertEqual((plan["technical_intervals"], plan["news_lookback_hours"], plan["packet_format_version"],
                          plan["market_context"]["status"]), (["1d", "1h", "5m"], 72, "phase7c-v1", "present"))

    def test_only_read_statements_and_no_state_change(self):
        seed(self.db)
        before = self.db.counts()
        statements = []
        sa.event.listen(self.db.engine, "before_cursor_execute",
                        lambda conn, cursor, statement, *a: statements.append(statement.strip().split()[0].upper()))
        self.assertEqual(self.cli()[0], 0)
        self.assertTrue(statements)
        self.assertFalse(set(statements) - {"SELECT", "BEGIN", "SET", "COMMIT", "ROLLBACK"}, set(statements))
        self.assertEqual(self.db.counts(), before)

    def test_runner_never_imports_providers_or_forbidden_modules(self):
        code = ("import sys, evidence_packet.runner, evidence_packet.loader\n"
                "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('collector', 'analyzer', 'alert_engine', "
                "'evidence', 'evaluation', 'openai', 'redis', 'telegram', 'dotenv', 'feedparser', 'requests') "
                "or n in ('shared.config', 'market_data.http', 'market_data.providers'))\nprint(bad)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class PostgresReadOnlyTests(unittest.TestCase):
    def setUp(self):
        from alembic import command
        from tests import test_persistence as unit
        settings = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        self.admin = make_engine(settings)
        self.schema = "mias_phase7d_" + uuid4().hex
        with self.admin.begin() as connection:
            connection.execute(sa.schema.CreateSchema(self.schema))
        self.engine = make_engine(settings)

        @sa.event.listens_for(self.engine, "connect")
        def search_path(connection, _):
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{self.schema}"')
            connection.autocommit = False

        self.addCleanup(self.cleanup)
        with self.engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")

    def cleanup(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(sa.schema.DropSchema(self.schema, cascade=True))
        self.admin.dispose()

    def test_postgres_read_only_transaction_and_reproducible_packet(self):
        db = DB()
        self.addCleanup(db.close)
        db.engine.dispose()
        db.engine = self.engine
        seed(db)
        ctx = os.path.join(db.dir.name, "ctx.json")
        with open(ctx, "w") as handle:
            json.dump(mctx().to_dict(), handle)
        before = db.counts()
        statements = []
        sa.event.listen(self.engine, "before_cursor_execute",
                        lambda conn, cursor, statement, *a: statements.append(" ".join(statement.split())))
        outputs = []
        for _ in range(2):
            out, err = io.StringIO(), io.StringIO()
            code = runner.main(["--symbol", "META", "--as-of", AS_OF, "--market-context", ctx], environ={},
                               engine=self.engine, calendar=CAL, out=out, err=err)
            self.assertEqual(code, 0, err.getvalue())
            outputs.append(out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", statements)
        verbs = {s.split()[0].upper() for s in statements}
        self.assertFalse(verbs - {"SELECT", "SET"}, verbs)
        self.assertEqual(db.counts(), before)
        with self.assertRaises(Exception):  # The read-only transaction itself rejects writes.
            from persistence.news_audit import read_only
            with read_only(self.engine) as session:
                session.execute(sa.text("DELETE FROM technical_snapshots"))
        self.assertEqual(db.counts(), before)


if __name__ == "__main__":
    unittest.main()
