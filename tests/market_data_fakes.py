"""Test doubles for Phase 4B: a fake HTTP session for the Polygon/Massive adapter and calendar-aware synthetic bars.

All prices are SYNTHETIC (deterministic formulas), never vendor data.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from urllib.parse import urlsplit

import requests

from market_data.calendar import default_calendar
from market_data.models import MarketBar, Session

TEST_KEY = "mias-canary-market-data-key-TEST-ONLY-0000"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body if body is not None else {})


class FakeSession:
    """Returns queued responses (or a routing function's result); records every call. Exceptions are raised."""

    def __init__(self, responses=None, route=None):
        self.responses, self.route, self.calls = list(responses or []), route, []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(dict(url=url, params=params, headers=headers, timeout=timeout, allow_redirects=allow_redirects))
        item = self.route(url, params) if self.route else self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def cents(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_at(base, k):
    """Deterministic synthetic path: slow drift plus two waves."""
    return base * (1 + 0.0004 * k / 10 + 0.004 * math.sin(k / 7.0) + 0.002 * math.sin(k / 2.3))


def calendar_bars(symbol, days, minutes, *, base=500.0, sessions=(Session.REGULAR,), calendar=None, start_k=0):
    """MarketBars on real XNYS trading days (holidays skipped, early closes honoured), session-labelled."""
    calendar = calendar or default_calendar()
    bars, k, previous = [], start_k, None
    step = timedelta(minutes=minutes)
    for day in days:
        times = calendar.session_times(day)
        if times is None:
            continue
        for session in (Session.PRE, Session.REGULAR, Session.POST):
            if session not in sessions:
                continue
            start, end = times.segment(session)
            stamp = start
            while stamp < end:
                close = price_at(base, k)
                open_ = previous if previous is not None else close
                high, low = max(open_, close) * 1.0006, min(open_, close) * 0.9994
                volume = 5_000 + (k * 7919) % 20_000
                bars.append(MarketBar(symbol, stamp, f"{minutes}m", cents(open_), cents(high), cents(low), cents(close),
                                      volume, session=session))
                previous, k, stamp = close, k + 1, stamp + step
    return bars


def to_results(bars):
    """Vendor-style aggregate results (UTC milliseconds, JSON numbers) for MarketBars."""
    return [dict(t=int(b.timestamp.astimezone(timezone.utc).timestamp() * 1000), o=float(b.open), h=float(b.high),
                 l=float(b.low), c=float(b.close), v=_json_volume(b.volume), vw=float(b.close), n=10) for b in bars]


def _json_volume(volume):
    """Whole volumes as JSON integers, fractional ones as JSON numbers (the vendor sends both)."""
    return int(volume) if volume == int(volume) else float(volume)


def payload(results, *, status="OK", next_url=None):
    body = dict(ticker="X", status=status, resultsCount=len(results), results=results, request_id="test")
    if next_url:
        body["next_url"] = next_url
    return body


def aggregates_route(bars_by_minutes):
    """Route function: serves ``bars_by_minutes[multiplier]`` filtered to the requested [from, to] millisecond range."""
    def route(url, params):
        parts = urlsplit(url).path.split("/")
        multiplier, from_ms, to_ms = int(parts[-4]), int(parts[-2]), int(parts[-1])
        results = [r for r in to_results(bars_by_minutes[multiplier]) if from_ms <= r["t"] <= to_ms]
        return FakeResponse(200, payload(results))
    return route


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)
