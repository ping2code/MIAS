"""Phase 6 fail-closed provider guard.

Evidence collection runs only with the frozen ``PROTOCOL.provider`` (``polygon``;
the ``massive`` alias resolves to it). ``MARKET_DATA_PROVIDER=massive_stocks``
with the ledger enabled is refused with exit 2 (``reason=evidence_config``)
before any provider request or ledger write. Behaviour with ``polygon`` is
unchanged, and check-only mode (which writes nothing) is not guarded.

Sockets are disabled throughout.
"""
from datetime import date
import logging
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from alembic import command

from evidence import pin as pin_module
from evidence.registry import PROTOCOL, PROTOCOL_HASH, REGISTRY_HASH
from market_data.config import load_market_data_settings
from market_data.providers.massive import MassiveStocksProvider
from persistence import technical_evidence_tools as tools
from persistence import technical_shadow
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.technical_settings import load_technical_persistence_settings
from technical import runner
from tests import test_persistence as unit
from tests.market_data_fakes import TEST_KEY, FakeSession
from tests.test_technical_runner import CAL, ENV, NOW, make_provider, route

PIN = dict(registry_hash=REGISTRY_HASH, prospective_start_session=date(2026, 9, 17),
           earliest_evaluation_session=date(2027, 3, 17), prospective_freeze_commit="a" * 40)


class ProviderGuardTests(unittest.TestCase):
    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled in tests")), \
                patch.object(socket, "create_connection", side_effect=OSError("network disabled in tests")):
            return super().run(result)

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
        for target in (patch.object(pin_module, "load_pin", return_value=PIN),
                       patch.object(pin_module, "current_commit", return_value="b" * 40)):
            target.start()
            self.addCleanup(target.stop)

    def run_runner(self, environ, provider=None, argv=("--symbols", "META,NVDA")):
        technical_shadow._after_fork()
        writer = patch.object(technical_shadow, "_configured_writer", lambda: technical_shadow.TechnicalSnapshotWriter(
            engine_factory=lambda: self.engine, capacity=64))
        with writer, self.assertLogs("technical.runner", "INFO") as logs:
            code = runner.main(list(argv), environ=environ, provider=provider)
        technical_shadow._after_fork()
        return code, "\n".join(logs.output)

    def rows(self):
        with transaction(self.engine) as session:
            return tools.load_rows(session)

    def test_massive_stocks_with_ledger_fails_closed_before_any_request(self):
        environ = dict(self.env, MARKET_DATA_PROVIDER="massive_stocks")
        with patch("market_data.providers.build_provider", side_effect=AssertionError("provider must not be built")):
            code, logs = self.run_runner(environ)  # Provider from the environment: refused before it is built.
        self.assertEqual(code, 2)
        self.assertIn("reason=evidence_config detail=MARKET_DATA_PROVIDER_differs_from_the_frozen_Phase_6_provider", logs)
        session = FakeSession(route=route)
        injected = MassiveStocksProvider(load_market_data_settings(environ), calendar=CAL, session=session,
                                         sleep=lambda s: None, clock=lambda: NOW)
        code, logs = self.run_runner(environ, provider=injected)
        self.assertEqual((code, session.calls), (2, []))
        self.assertEqual(self.rows(), [])
        self.assertNotIn(TEST_KEY, logs)

    def test_polygon_and_massive_alias_unchanged(self):
        code, logs = self.run_runner(self.env, provider=make_provider(FakeSession(route=route)))
        self.assertEqual(code, 0, logs)
        self.assertEqual(len(self.rows()), 2 * 3 * 5)
        self.assertEqual({r["provider"] for r in self.rows()}, {"polygon"})
        persistence = load_technical_persistence_settings(self.env)
        for value in ("polygon", "massive", "Massive"):
            run = runner._prepare_evidence(dict(self.env, MARKET_DATA_PROVIDER=value), persistence, ["META"],
                                           check=False)
            self.assertIsInstance(run, runner.EvidenceRun)
            run.engine.dispose()

    def test_guard_scope(self):
        persistence = load_technical_persistence_settings(self.env)
        stocks = dict(self.env, MARKET_DATA_PROVIDER="massive_stocks")
        with self.assertRaises(runner.EvidenceSetupError):
            runner._prepare_evidence(stocks, persistence, ["META"], check=False)
        self.assertTrue(runner._prepare_evidence(stocks, persistence, ["META"], check=True).check)  # Writes nothing.
        off = dict(stocks, TECHNICAL_EVIDENCE_LEDGER_ENABLED="false")
        self.assertIsNone(runner._prepare_evidence(off, persistence, ["META"], check=False))  # Ledger off: no guard.

    def test_frozen_identity_unchanged(self):
        self.assertEqual(PROTOCOL.provider, "polygon")
        self.assertEqual((PROTOCOL_HASH, REGISTRY_HASH),
                         ("15587aed49589b8d8535be636b07a3b285cf0c71e0a2b0df63c0d663f349d3c8",
                          "b1080c2e9347285bd3a56cad55b44e46649ea049bfaaedcc7e15488a45d87acd"))


if __name__ == "__main__":
    unittest.main()
