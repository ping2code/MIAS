"""Test-only launcher: run a real registry collector command with all network blocked.

    python -m tests.scheduler_collector_shim -m <module> [args...]

It runs the module exactly as ``python -m <module> [args...]`` would (``runpy`` with
``__main__``), after it has done all of the following:

- disabled ``dotenv.load_dotenv`` (``.env`` is never read);
- made every socket connection fail (no live feeds, Redis, PostgreSQL, Telegram or OpenAI), except
  loopback connections when ``MIAS_TEST_SHIM_ALLOW_LOOPBACK=true`` (disposable services only);
- replaced the Telegram notifier and the OpenAI client with counting stubs.

A profiler hook records whether the collector's own collection function ran. The
report is written as JSON to ``$MIAS_TEST_SHIM_REPORT/<module>.json`` before exit.
"""
import json
import os
import runpy
import socket
import sys
from unittest.mock import patch

ENTRY_FUNCTIONS = {"collect_fed_events", "collect_macro_events", "collect_treasury_events", "collect_sec_filings",
                   "process_sec_filings", "collect_all_sources", "read_feed", "collect_geopolitical_events"}
report = dict(argv=sys.argv[1:], entry_functions=[], network_attempts=0, telegram_attempts=0, openai_attempts=0,
              exit_code=None)


REAL_CONNECT = socket.socket.connect
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def blocked_connect(self, address, *args, **kwargs):
    """Every connection fails, except loopback when MIAS_TEST_SHIM_ALLOW_LOOPBACK=true (disposable Redis/PostgreSQL)."""
    host = address[0] if isinstance(address, tuple) else None
    if host in LOOPBACK and os.environ.get("MIAS_TEST_SHIM_ALLOW_LOOPBACK") == "true":
        return REAL_CONNECT(self, address, *args, **kwargs)
    report["network_attempts"] += 1
    raise OSError("network disabled in scheduler integration test")


def profile(frame, event, arg):
    if event == "call" and frame.f_code.co_name in ENTRY_FUNCTIONS:
        name = frame.f_code.co_name
        if name not in report["entry_functions"]:
            report["entry_functions"].append(name)


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "-m":
        print("usage: python -m tests.scheduler_collector_shim -m <module> [args...]", file=sys.stderr)
        return 2
    module, args = sys.argv[2], sys.argv[3:]
    patch("dotenv.load_dotenv", lambda *a, **k: False).start()
    patch.object(socket.socket, "connect", blocked_connect).start()
    import alert_engine.telegram_notifier as telegram
    def telegram_stub(message):
        report["telegram_attempts"] += 1
        raise RuntimeError("telegram disabled in scheduler integration test")
    patch.object(telegram, "send_telegram_alert", telegram_stub).start()
    try:
        import openai
        def openai_stub(*a, **k):
            report["openai_attempts"] += 1
            raise RuntimeError("openai disabled in scheduler integration test")
        patch.object(openai, "OpenAI", openai_stub).start()
    except ImportError:
        pass
    sys.argv = [module, *args]
    sys.setprofile(profile)
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        report["exit_code"] = 0
    except SystemExit as exit_:
        report["exit_code"] = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    finally:
        sys.setprofile(None)
        directory = os.environ.get("MIAS_TEST_SHIM_REPORT")
        if directory:
            with open(os.path.join(directory, f"{module}.json"), "w") as handle:
                json.dump(report, handle)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
