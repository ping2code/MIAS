"""Phase 6 prospective validation.

Covered:

- frozen hashes and immutability;
- the start pin rule;
- gate / no-peek / early-look labelling;
- the H1' and H2' verdict rules;
- hash-verified eligibility;
- the dry-run plan (no network);
- the once-daily scheduler config.
"""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timezone
from decimal import Decimal
import io
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from alembic import command

from evaluation import prospective, prospective_status, prospective_validate
from evidence import pin as pin_module, plan as plan_module
from evidence.registry import (H1P, H2P, HYPOTHESIS_HASHES, PROTOCOL, PROTOCOL_HASH, REGISTRY_HASH, REGISTRY_VERSION,
                               registry_identity)
from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ
from market_data.providers.polygon import PolygonProvider
from persistence.config import DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.technical_evidence_ledger import EvidenceLedgerRepository, build_evidence
from tests import test_persistence as unit
from tests.market_data_fakes import TEST_KEY, FakeSession, aggregates_route, calendar_bars

CAL = default_calendar()
FROZEN = dict(h1="d89efb30b22af34f3ffe6a7b4cd500af8a4a9f95b5f1a903e32f8fec6dca8bca",
              h2="78e82413275f0bc8a7e4500cbbecbb263ed4414fb4b699838a18c7e212838e4a",
              protocol="15587aed49589b8d8535be636b07a3b285cf0c71e0a2b0df63c0d663f349d3c8",
              registry="b1080c2e9347285bd3a56cad55b44e46649ea049bfaaedcc7e15488a45d87acd")
START = date(2026, 9, 1)
NOW = datetime.combine(date(2026, 9, 23), time(20), tzinfo=EXCHANGE_TZ)
PIN = dict(registry_hash=REGISTRY_HASH, prospective_start_session=START, earliest_evaluation_session=date(2027, 3, 1),
           prospective_freeze_commit="a" * 40)
ENV = dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0")


class RegistryTests(unittest.TestCase):
    def test_frozen_hashes(self):
        self.assertEqual(HYPOTHESIS_HASHES, {"H1'": FROZEN["h1"], "H2'": FROZEN["h2"]})
        self.assertEqual((PROTOCOL_HASH, REGISTRY_HASH, REGISTRY_VERSION), (FROZEN["protocol"], FROZEN["registry"],
                                                                            "phase6-v1"))
        self.assertEqual(registry_identity()["hash"], REGISTRY_HASH)

    def test_immutable(self):
        for obj, field in ((H1P, "verdict_rules"), (H2P, "symbols"), (PROTOCOL, "min_complete_sessions")):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, None)

    def test_frozen_universe_and_rules(self):
        self.assertEqual(PROTOCOL.collection_symbols, ("META", "NVDA", "MSFT", "SPY", "JPM", "UNH", "CAT", "XOM"))
        self.assertEqual((H1P.interval, H1P.horizon, H1P.state), (("1d",), (5,), ("bearish_setup",)))
        self.assertEqual(H2P.interval, ("5m", "1h"))
        self.assertEqual((PROTOCOL.min_complete_sessions, PROTOCOL.seed, PROTOCOL.pivot_window), (120, 42042, 2))


class PinTests(unittest.TestCase):
    def test_start_rule(self):
        cases = [(datetime(2026, 9, 25, 20, 47, tzinfo=EXCHANGE_TZ), "2026-09-28", "2027-03-29"),
                 (datetime(2026, 9, 28, 9, 29, 59, tzinfo=EXCHANGE_TZ), "2026-09-28", "2027-03-29"),
                 (datetime(2026, 9, 28, 9, 30, tzinfo=EXCHANGE_TZ), "2026-09-29", "2027-03-29"),
                 (datetime(2026, 11, 25, 18, tzinfo=EXCHANGE_TZ), "2026-11-27", "2027-05-27")]
        for moment, start, earliest in cases:
            pin = pin_module.build_pin("c" * 40, moment, CAL)
            self.assertEqual((pin["prospective_start_session"], pin["earliest_evaluation_session"]), (start, earliest))
            self.assertEqual(pin_module.validate_pin(pin, CAL)["prospective_start_session"], date.fromisoformat(start))

    def test_pin_is_validated(self):
        pin = pin_module.build_pin("c" * 40, datetime(2026, 9, 25, 20, 47, tzinfo=EXCHANGE_TZ), CAL)
        for bad in (dict(pin, prospective_start_session="2026-09-25"), dict(pin, registry_hash="0" * 64),
                    dict(pin, earliest_evaluation_session="2027-01-04"), dict(pin, prospective_freeze_commit="abc"),
                    dict(pin, min_complete_sessions=60), {k: v for k, v in pin.items() if k != "freeze_commit_time"},
                    None):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(pin_module.PinError):
                pin_module.validate_pin(bad, CAL)
        with self.assertRaises(pin_module.PinError):
            pin_module.load_pin("/nonexistent/prospective_start.json", CAL)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(pin, handle)
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(pin_module.load_pin(handle.name, CAL)["registry_hash"], REGISTRY_HASH)

    def test_committed_pin(self):
        """The repository pin: freeze commit f5d9e6b (2026-09-25 21:26:42 ET) -> start 2026-09-28, gate 2027-03-29."""
        pin = pin_module.load_pin(calendar=CAL)
        self.assertEqual(pin["prospective_freeze_commit"], "f5d9e6b54bf9fb8712d877f08501e3688d307786")
        self.assertEqual((pin["prospective_start_session"], pin["earliest_evaluation_session"]),
                         (date(2026, 9, 28), date(2027, 3, 29)))
        self.assertEqual(pin["registry_hash"], FROZEN["registry"])

    def test_add_months_clamps(self):
        self.assertEqual(pin_module.add_months(date(2026, 8, 31), 6), date(2027, 2, 28))
        self.assertEqual(pin_module.add_months(date(2026, 12, 15), 6), date(2027, 6, 15))


class GateTests(unittest.TestCase):
    def test_gate_requires_date_and_sessions(self):
        self.assertFalse(prospective.gate(PIN, 200, date(2027, 2, 26))["open"])
        self.assertFalse(prospective.gate(PIN, 119, date(2027, 6, 1))["open"])
        self.assertTrue(prospective.gate(PIN, 120, date(2027, 3, 1))["open"])
        message = prospective.locked_message(prospective.gate(PIN, 17, date(2026, 9, 25)))
        self.assertEqual(message, "Evaluation locked. Earliest evaluation date: 2027-03-01. Eligible sessions: 17 / 120. "
                                  "No hypothesis statistics were computed.")


def moves(center, n=40, seed=1, spread=0.002):
    return list(np.random.default_rng(seed).normal(center, spread, n))


class VerdictRuleTests(unittest.TestCase):
    def h1_input(self, effect, symbols=H1P.symbols, n=120, every=6):
        data = {}
        for i, symbol in enumerate(symbols):
            rng = np.random.default_rng(100 + i)
            states = ["bearish_setup" if t % every == 0 else "neutral" for t in range(n)]
            returns = [float(rng.normal(0, 0.01)) + (effect if s == "bearish_setup" else 0) for s in states]
            data[symbol] = (states, returns)
        return data

    def test_h1_rules(self):
        strong = prospective.evaluate_h1(self.h1_input(0.02))
        self.assertEqual(strong["verdict"], "SUPPORTED")
        self.assertEqual(len(strong["qualifying_symbols"]), 6)
        self.assertEqual(prospective.evaluate_h1(self.h1_input(-0.02))["verdict"], "UNSUPPORTED")
        self.assertEqual(prospective.evaluate_h1(self.h1_input(0.02, symbols=H1P.symbols[:3]))["verdict"],
                         "INSUFFICIENT")
        self.assertEqual(prospective.evaluate_h1(self.h1_input(0.02, n=24))["verdict"], "INSUFFICIENT")  # 4 obs each.
        self.assertEqual(prospective.evaluate_h1(self.h1_input(0.02)), strong)  # Seeded: deterministic.
        ignored = dict(self.h1_input(0.02), NVDA=(["bearish_setup"] * 50, [0.5] * 50))
        self.assertEqual(prospective.evaluate_h1(ignored)["pooled_excess"], strong["pooled_excess"])  # Not in H1'.

    def h2_input(self, specs):
        per = {}
        for i, (state, kind, center) in enumerate(specs):
            key = (H2P.symbols[i % 8], "5m" if i < 8 else "1h")
            cells, lags, _ = per.setdefault(key, ({}, {}, 0.004))
            cells[(state, kind)] = moves(center, seed=i)
            lags[(state, kind)] = [2] * 40
        return per

    def test_h2_rules(self):
        aligned = [("bullish_setup", "low", 0.01), ("bearish_setup", "high", -0.01)] * 2
        self.assertEqual(prospective.evaluate_h2(self.h2_input(aligned))["verdict"], "SUPPORTED")
        wrong = [("bullish_setup", "low", -0.0001), ("bearish_setup", "high", 0.0001)] * 2
        self.assertEqual(prospective.evaluate_h2(self.h2_input(wrong))["verdict"], "UNSUPPORTED")
        self.assertEqual(prospective.evaluate_h2(self.h2_input(aligned[:3]))["verdict"], "INSUFFICIENT")
        capped = aligned * 2 + [("bullish_setup", "low", -0.01)]  # 8/9 consistent, one significant opposite.
        result = prospective.evaluate_h2(self.h2_input(capped))
        self.assertEqual((result["verdict"], result["evaluable_cells"]), ("MIXED", 9))
        small = prospective.evaluate_h2(self.h2_input([("bullish_setup", "low", 0.0002)] * 4))  # Below 0.25 x ref.
        self.assertEqual(small["verdict"], "UNSUPPORTED")
        counter = prospective.evaluate_h2(self.h2_input(aligned + [("bullish_setup", "high", 0.5)]))
        self.assertEqual(len(counter["counter_kind_cells_descriptive"]), 1)
        self.assertEqual(counter["evaluable_cells"], 4)  # Counter-kind cells never vote.


def build_provider(session):
    return PolygonProvider(load_market_data_settings(ENV), calendar=CAL, session=session, sleep=lambda s: None,
                           clock=lambda: NOW)


class ExplodingProvider:
    def __getattr__(self, name):
        raise AssertionError(f"provider touched before the gate: {name}")


class ValidateCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        days = CAL.trading_days(date(2024, 6, 3), NOW.date())
        cls.data = {s: {5: calendar_bars(s, days[-30:], 5, base=100.0 + 17 * i),
                        30: calendar_bars(s, days, 30, base=100.0 + 17 * i)}
                    for i, s in enumerate(PROTOCOL.collection_symbols)}
        session = FakeSession(route=cls.route)
        cls.series = prospective_validate.fetch_series(build_provider(session), PROTOCOL.collection_symbols, START, CAL)
        cls.requests = len(session.calls)

    @classmethod
    def route(cls, url, params):
        symbol = url.split("/ticker/")[1].split("/")[0]
        return aggregates_route(cls.data[symbol])(url, params)

    def setUp(self):
        self.engine = make_engine(DatabaseSettings(url="sqlite://", sqlite_enabled=True))
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as connection:
            command.upgrade(unit.migration_config(connection), "head")
        settings = load_market_data_settings(ENV)
        self.sessions = CAL.trading_days(START, NOW.date())
        with transaction(self.engine) as session:
            repo = EvidenceLedgerRepository(session)
            for (symbol, label), bars in self.series.items():
                for day in self.sessions:
                    day_bars = [b for b in bars if b.timestamp.astimezone(EXCHANGE_TZ).date() == day]
                    if (symbol, label, day) == ("META", "1d", date(2026, 9, 10)):  # Vendor later revised this bar.
                        day_bars = [day_bars[0].__class__(**dict(vars(day_bars[0]), close=day_bars[0].close
                                                                 + Decimal("0.01")))]
                    repo.record_collected(build_evidence(
                        bars=day_bars, session_date=day, symbol=symbol, interval=label, engine_version="phase4c-v2",
                        registry=registry_identity(), provider_settings=settings, code_commit="d" * 40,
                        collected_at=datetime.combine(CAL.next_trading_day(day), time(8), tzinfo=EXCHANGE_TZ),
                        calendar=CAL))

    def run(self, result=None):
        with patch.object(socket, "socket", side_effect=OSError("network disabled")):
            return super().run(result)

    def validate(self, argv=(), provider=None):
        out = io.StringIO()
        code = prospective_validate.main(list(argv), environ=ENV, engine=self.engine, calendar=CAL, now=NOW, pin=PIN,
                                         provider=provider or ExplodingProvider(), out=out)
        return code, out.getvalue()

    def test_locked_by_default_touches_nothing(self):
        code, text = self.validate()
        self.assertEqual(code, 3)
        self.assertEqual(text.strip(), "Evaluation locked. Earliest evaluation date: 2027-03-01. Eligible sessions: "
                                       "16 / 120. No hypothesis statistics were computed.")

    def test_status_is_no_peek(self):
        out = io.StringIO()
        self.assertEqual(prospective_status.main([], engine=self.engine, calendar=CAL, now=NOW, pin=PIN, out=out), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["gate"]["status"], report["gate"]["eligible_sessions"]), ("LOCKED", 16))
        self.assertEqual(report["coverage"]["accepted_identities"], 16 * 24)
        self.assertEqual(report["registry_hash"], REGISTRY_HASH)
        text = out.getvalue().lower()
        for forbidden in ("verdict", "excess", "return", "mean", "bearish", "bullish", "p97"):
            self.assertNotIn(forbidden, text)

    def test_early_look_is_labelled_and_hash_mismatch_excluded(self):
        code, text = self.validate(["--early-look-override"], provider=build_provider(FakeSession(route=self.route)))
        self.assertEqual(code, 0, text[-500:])
        result = json.loads(text)
        self.assertEqual(result["evaluation_label"], "NON_PREREGISTERED_EARLY_LOOK")
        for hypothesis in result["hypotheses"].values():
            self.assertEqual(hypothesis["evaluation_label"], "NON_PREREGISTERED_EARLY_LOOK")
        self.assertIn("not a preregistered result", result["note"])
        self.assertEqual(result["excluded_sessions"], [dict(session="2026-09-10", symbol="META", interval="1d",
                                                            reason="refetched_bar_hash_mismatch")])
        self.assertEqual(result["hypotheses"]["H1'"]["verdict"], "INSUFFICIENT")  # 16 sessions: far too few.
        for key in ("registry_hash", "protocol_hash", "hypothesis_hashes", "prospective_freeze_commit", "code_commit",
                    "prospective_format_version", "seed", "through_session"):
            self.assertIn(key, result)
        self.assertEqual(result["hypothesis_hashes"], HYPOTHESIS_HASHES)
        self.assertNotIn(TEST_KEY, text)
        self.assertEqual(self.requests, 16)  # 8 symbols x (5m + 30m), no pagination in the fake.

    def stubbed(self, argv=(), **patches):
        seen = {}

        def evaluate(series, accepted, complete, **kw):
            seen.update(accepted=accepted, complete=complete)
            return dict(verdict="STUB"), dict(verdict="STUB"), []
        with patch.object(prospective_validate, "evaluate", evaluate), \
                patch.object(prospective_validate, "fetch_series", lambda *a: {}):
            for target, name, value in patches.get("extra", ()):
                patch.object(target, name, value).start()
            try:
                code, text = self.validate(argv, provider=build_provider(FakeSession(route=self.route)))
            finally:
                patch.stopall()
        return code, json.loads(text), seen

    def test_open_gate_is_labelled_preregistered(self):
        code, result, seen = self.stubbed(extra=[(prospective, "gate", lambda pin, n, today: dict(
            open=True, eligible_sessions=n))])
        self.assertEqual((code, result["evaluation_label"]), (0, "PREREGISTERED_PROSPECTIVE"))
        self.assertEqual(result["hypotheses"]["H1'"]["evaluation_label"], "PREREGISTERED_PROSPECTIVE")
        self.assertEqual(len(seen["complete"]), 16)
        self.assertEqual(len(seen["accepted"]), 16 * 24)

    def test_other_registry_rows_are_not_evidence(self):
        code, result, seen = self.stubbed(["--early-look-override"],
                                          extra=[(prospective_validate, "REGISTRY_HASH", "0" * 64)])
        self.assertEqual((code, seen["accepted"]), (0, {}))


class DryRunAndSchedulerTests(unittest.TestCase):
    def test_plan_without_network(self):
        out = io.StringIO()
        with patch.object(socket, "socket", side_effect=OSError("network disabled")), \
                patch.object(pin_module, "load_pin", side_effect=pin_module.PinError("not pinned")):
            self.assertEqual(plan_module.main([], out=out), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["network"], report["identities_per_session"]), (False, 24))
        self.assertEqual(report["requests"]["requests_per_day_upper_bound"], 80)
        self.assertEqual(report["registry_hash"], REGISTRY_HASH)
        self.assertIsNotNone(report["start_candidate_if_frozen_at_head"])
        self.assertNotIn(TEST_KEY, out.getvalue())

    def test_once_daily_config_accepted_and_disabled_by_default(self):
        from orchestrator import registry
        from orchestrator.config import load_settings
        daily = dict(TECHNICAL_INTERVAL_SECONDS="86400", TECHNICAL_TIMEOUT_SECONDS="1800")
        definition = {d.name: d for d in registry.build_definitions(load_settings(daily))}["technical"]
        self.assertEqual((definition.enabled, definition.interval_seconds, definition.timeout_seconds),
                         (False, 86400, 1800))
        from evidence.collector import load_evidence_settings
        self.assertFalse(load_evidence_settings({}).enabled)


if __name__ == "__main__":
    unittest.main()
