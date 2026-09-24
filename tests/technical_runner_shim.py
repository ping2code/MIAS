"""Test-only launcher: run ``technical.runner`` in a real subprocess with a fake market data HTTP layer.

    python -m tests.technical_runner_shim [runner args...]

Before running ``technical.runner`` as ``__main__`` it:

- disables ``dotenv.load_dotenv`` (``.env`` is never read);
- blocks every socket connection except loopback when
  ``MIAS_TEST_SHIM_ALLOW_LOOPBACK=true`` (the disposable PostgreSQL);
- replaces Telegram and OpenAI with counting stubs;
- makes ``market_data.providers.build_provider`` return the real Polygon/Massive
  adapter with a fake HTTP session serving SYNTHETIC bars and a fixed clock
  (``MIAS_TEST_TECHNICAL_NOW``). With ``MIAS_TEST_TECHNICAL_FAIL=auth``, every
  request returns HTTP 401.

A JSON report (exit code, network/Telegram/OpenAI attempts, HTTP requests) is written to
``$MIAS_TEST_SHIM_REPORT/technical.runner.json``.
"""
from datetime import date, datetime
import json
import os
import runpy
import socket
import sys
from unittest.mock import patch

report = dict(exit_code=None, network_attempts=0, telegram_attempts=0, openai_attempts=0, http_requests=0)
REAL_CONNECT = socket.socket.connect
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def blocked_connect(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else None
    if host in LOOPBACK and os.environ.get("MIAS_TEST_SHIM_ALLOW_LOOPBACK") == "true":
        return REAL_CONNECT(self, address, *args, **kwargs)
    report["network_attempts"] += 1
    raise OSError("network disabled in technical runner test")


def fake_provider_factory():
    from market_data.calendar import default_calendar
    from market_data.providers.polygon import PolygonProvider
    from tests.market_data_fakes import FakeResponse, FakeSession, aggregates_route, calendar_bars
    calendar = default_calendar()
    now = datetime.fromisoformat(os.environ["MIAS_TEST_TECHNICAL_NOW"])
    days = calendar.trading_days(date(2026, 1, 1), now.date())
    data = {symbol: {5: calendar_bars(symbol, days[-12:], 5, base=base, calendar=calendar),
                     30: calendar_bars(symbol, days[-130:], 30, base=base, calendar=calendar)}
            for symbol, base in (("META", 740.0), ("NVDA", 182.0))}

    def route(url, params):
        report["http_requests"] += 1
        if os.environ.get("MIAS_TEST_TECHNICAL_FAIL") == "auth":
            return FakeResponse(401)
        symbol = url.split("/ticker/")[1].split("/")[0]
        return aggregates_route(data[symbol])(url, params)

    def build(settings, **kwargs):
        return PolygonProvider(settings, calendar=calendar, session=FakeSession(route=route), sleep=lambda s: None,
                               clock=lambda: now)
    return build


def main():
    patch("dotenv.load_dotenv", lambda *a, **k: False).start()
    patch.object(socket.socket, "connect", blocked_connect).start()
    import alert_engine.telegram_notifier as telegram

    def telegram_stub(message):
        report["telegram_attempts"] += 1
        raise RuntimeError("telegram disabled in technical runner test")
    patch.object(telegram, "send_telegram_alert", telegram_stub).start()
    try:
        import openai

        def openai_stub(*a, **k):
            report["openai_attempts"] += 1
            raise RuntimeError("openai disabled in technical runner test")
        patch.object(openai, "OpenAI", openai_stub).start()
    except ImportError:
        pass
    import market_data.providers
    patch.object(market_data.providers, "build_provider", fake_provider_factory()).start()
    sys.argv = ["technical.runner", *sys.argv[1:]]
    try:
        runpy.run_module("technical.runner", run_name="__main__", alter_sys=True)
        report["exit_code"] = 0
    except SystemExit as exit_:
        report["exit_code"] = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    finally:
        directory = os.environ.get("MIAS_TEST_SHIM_REPORT")
        if directory:
            with open(os.path.join(directory, "technical.runner.json"), "w") as handle:
                json.dump(report, handle)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
