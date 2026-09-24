"""Phase 4C real-process test: ``technical.runner`` in a subprocess (fake vendor HTTP), shadow persistence off/on/outage.

It needs a disposable PostgreSQL (TEST_DATABASE_URL, mias_test_* only). A dedicated database
``mias_test_p4c_<hex>`` is created, migrated to head and dropped. Redis is not used by the technical runner.

Verified:

- the technical output (snapshot log lines and text view) is identical with persistence off, on, and on with the
  database unreachable;
- enabled persistence writes one row per symbol/timeframe, and a rerun is idempotent;
- an outage is counted and never changes the exit code;
- the queue drains, and there are no Telegram/OpenAI/network attempts.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import sqlalchemy as sa
from alembic import command

from orchestrator.job_runner import ROOT
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests import test_persistence as unit
from tests.market_data_fakes import TEST_KEY

STAMP = re.compile(r"^\S+ \S+ ")  # "date time" prefix of the child's log format.


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Disposable TEST_DATABASE_URL required")
class TechnicalRunnerProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = require_test_database(DatabaseSettings(url=os.environ["TEST_DATABASE_URL"]))
        cls.name = "mias_test_p4c_" + uuid4().hex[:12]
        cls.admin = sa.create_engine(base.url, isolation_level="AUTOCOMMIT")
        with cls.admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{cls.name}"'))
        cls.url = base.url.set(database=cls.name)
        engine = make_engine(require_test_database(DatabaseSettings(url=cls.url)))
        with engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")
        engine.dispose()

    @classmethod
    def tearDownClass(cls):
        with cls.admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))
        cls.admin.dispose()

    def run_runner(self, **extra):
        reports = self.enterContext(tempfile.TemporaryDirectory())
        env = dict(PATH=os.environ["PATH"], HOME=os.environ.get("HOME", "/tmp"), PYTHONDONTWRITEBYTECODE="1",
                   MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0",
                   MIAS_TEST_TECHNICAL_NOW="2026-09-23T20:00:00-04:00", MIAS_TEST_SHIM_REPORT=reports,
                   MIAS_TEST_SHIM_ALLOW_LOOPBACK="true", **extra)
        result = subprocess.run([sys.executable, "-m", "tests.technical_runner_shim", "--text"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=180)
        with open(os.path.join(reports, "technical.runner.json")) as handle:
            report = json.load(handle)
        lines = [STAMP.sub("", line) for line in result.stderr.splitlines()]
        technical = [l for l in lines if "event=technical_snapshot" in l or "event=technical_run_finished" in l]
        persistence = [l for l in lines if "event=technical_persistence" in l]
        self.assertNotIn(TEST_KEY, result.stdout + result.stderr)
        self.assertEqual((report["telegram_attempts"], report["openai_attempts"], report["network_attempts"]), (0, 0, 0))
        return result.returncode, technical, result.stdout, persistence

    def rows(self):
        engine = make_engine(DatabaseSettings(url=self.url))
        try:
            with engine.connect() as connection:
                return connection.execute(sa.text(
                    "SELECT symbol, interval, technical_state FROM technical_snapshots ORDER BY 1, 2")).all()
        finally:
            engine.dispose()

    def test_output_parity_persistence_and_outage(self):
        off = self.run_runner()
        enabled = dict(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true",
                       DATABASE_URL=self.url.render_as_string(hide_password=False))
        on = self.run_runner(**enabled)
        rerun = self.run_runner(**enabled)
        reserved = self.enterContext(socket.socket())
        reserved.bind(("127.0.0.1", 0))
        dead_url = self.url.set(host="127.0.0.1", port=reserved.getsockname()[1]).render_as_string(hide_password=False)
        outage = self.run_runner(TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED="true", DATABASE_URL=dead_url)
        self.assertEqual(off[0], 0)
        self.assertEqual(len(off[1]), 7)  # 2 symbols x 3 timeframes + run summary.
        for other in (on, rerun, outage):
            self.assertEqual(other[:3], off[:3])  # Exit code, technical log lines and text view are identical.
        self.assertEqual(off[3], [])
        self.assertIn("queued=6 persisted=6 duplicate=0 conflict=0 failed=0", on[3][0])
        self.assertIn("drained=true", on[3][0])
        self.assertIn("queued=6 persisted=6 duplicate=6 conflict=0 failed=0", rerun[3][0])
        self.assertIn("persisted=0 duplicate=0 conflict=0 failed=6", outage[3][0])
        rows = self.rows()
        self.assertEqual([(s, i) for s, i, _ in rows],
                         [("META", "1d"), ("META", "1h"), ("META", "5m"), ("NVDA", "1d"), ("NVDA", "1h"), ("NVDA", "5m")])
        states = {f"{s} {i}": state for s, i, state in rows}
        for line in off[1][:6]:
            symbol = re.search(r"symbol=(\S+)", line).group(1)
            interval = re.search(r"interval=(\S+)", line).group(1)
            self.assertIn(f"state={states[f'{symbol} {interval}']} ", line)  # Persisted state equals logged state.


if __name__ == "__main__":
    unittest.main()
