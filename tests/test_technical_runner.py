"""Phase 4B one-shot technical runner, end to end through the Polygon/Massive adapter with a fake HTTP layer."""
from datetime import date, datetime, time
import io
import unittest

from market_data.calendar import default_calendar
from market_data.config import load_market_data_settings
from market_data.models import EXCHANGE_TZ
from market_data.providers.polygon import PolygonProvider
from technical.runner import main
from tests.market_data_fakes import TEST_KEY, FakeResponse, FakeSession, aggregates_route, calendar_bars

CAL = default_calendar()
NOW = datetime.combine(date(2026, 9, 23), time(20), tzinfo=EXCHANGE_TZ)
DAYS = CAL.trading_days(date(2025, 8, 1), date(2026, 9, 23))
DATA = {symbol: {5: calendar_bars(symbol, DAYS[-12:], 5, base=base), 30: calendar_bars(symbol, DAYS, 30, base=base)}
        for symbol, base in (("META", 740.0), ("NVDA", 182.0))}
ENV = dict(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_API_KEY=TEST_KEY, MARKET_DATA_DELAY_SECONDS="0")


def route(url, params):
    symbol = url.split("/ticker/")[1].split("/")[0]
    return aggregates_route(DATA[symbol])(url, params)


def make_provider(session):
    return PolygonProvider(load_market_data_settings(ENV), calendar=CAL, session=session, sleep=lambda s: None,
                           clock=lambda: NOW)


class RunnerTests(unittest.TestCase):
    def test_end_to_end(self):
        session, out = FakeSession(route=route), io.StringIO()
        with self.assertLogs("technical.runner", "INFO") as logs:
            code = main(["--text"], provider=make_provider(session), out=out)
        self.assertEqual(code, 0)
        self.assertEqual(len(session.calls), 4)  # Two requests per symbol: 5m, plus 30m for both 1h and 1d.
        text = "\n".join(logs.output)
        for symbol in ("META", "NVDA"):
            for interval in ("1d", "1h", "5m"):
                self.assertIn(f"event=technical_snapshot symbol={symbol} interval={interval} ", text)
        self.assertIn("last_bar=2026-09-23T15:55:00-04:00", text)
        self.assertIn("last_bar=2026-09-23T00:00:00-04:00", text)  # Today's daily bar completed at the close.
        self.assertIn("event=technical_run_finished symbols=2 failed=0", text)
        self.assertNotIn(TEST_KEY, text + out.getvalue())
        self.assertIn("META\n\nDaily:\n  State: ", out.getvalue())
        self.assertNotIn("buy", (text + out.getvalue()).lower())

    def test_provider_failure_exits_nonzero(self):
        session = FakeSession([FakeResponse(401)] * 4)
        with self.assertLogs("technical.runner", "INFO") as logs:
            code = main(["--timeframes", "5m"], provider=make_provider(session))
        self.assertEqual(code, 1)
        text = "\n".join(logs.output)
        self.assertIn("event=technical_symbol_failed symbol=META kind=auth", text)
        self.assertIn("failed=2", text)
        self.assertNotIn(TEST_KEY, text)

    def test_config_and_usage_errors(self):
        with self.assertLogs("technical.runner", "ERROR"):
            self.assertEqual(main([], environ={}), 2)
        with self.assertLogs("technical.runner", "ERROR"):
            self.assertEqual(main([], environ=dict(MARKET_DATA_PROVIDER="polygon")), 2)
        with self.assertLogs("technical.runner", "ERROR"):
            self.assertEqual(main(["--timeframes", "2h"], environ=ENV), 2)


if __name__ == "__main__":
    unittest.main()
