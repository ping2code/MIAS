"""Polygon.io (rebranded Massive) stock aggregates adapter (Phase 4B).

The documented REST endpoint is:

    GET {base}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}
        ?adjusted=true|false&sort=asc&limit=50000

- Authentication is ``Authorization: Bearer <key>`` (never a query parameter, so the
  key cannot leak through URLs).
- Pagination follows ``next_url``, only to the configured host.
- Each result is ``{t, o, h, l, c, v, ...}``: ``t`` is the bar **start** in Unix
  milliseconds (UTC), and prices are parsed as exact ``Decimal``.

Intervals:

| MIAS | Fetched as | Notes |
|---|---|---|
| 1m, 5m, 15m, 30m | native minute aggregates | clock-aligned; the 09:30 open is on every grid |
| 1h | 30m, aggregated | the vendor's hourly bars are clock-aligned (09:00-10:00 would mix pre-market and regular trading), so MIAS builds session-anchored hours (09:30, 10:30, …) |
| 1d | 30m, aggregated | regular session only, so daily OHLCV follows the MIAS session model |

Validation never repairs data. It raises ``MarketDataError`` for:

- a missing field or an unknown ``status``;
- a non-integral or negative volume;
- invalid OHLC;
- duplicate or out-of-order timestamps;
- a bar outside every session or off its session grid.

Only **completed** bars are returned: bars ending by ``now - MARKET_DATA_DELAY_SECONDS``
and, for aggregated intervals, ending within the fetched data's coverage.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from urllib.parse import urlsplit

from market_data.aggregation import derive_completed
from market_data.calendar import default_calendar
from market_data.completion import completed_bars
from market_data.http import JsonHttpClient, ProviderError, safe_target
from market_data.models import EXCHANGE_TZ, Interval, MarketBar, MarketDataError, Session
from market_data.provider import MarketDataProvider
from market_data.validation import validate_calendar_series

logger = logging.getLogger("market_data.polygon")
NATIVE = {Interval.M1: (1, "minute"), Interval.M5: (5, "minute"), Interval.M15: (15, "minute"),
          Interval.M30: (30, "minute")}
DERIVED = {Interval.H1: Interval.M30, Interval.D1: Interval.M30}
OK_STATUS = ("OK", "DELAYED")
MAX_PAGES = 50
FIELDS = ("t", "o", "h", "l", "c", "v")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class PolygonProvider(MarketDataProvider):
    def __init__(self, settings, *, calendar=None, session=None, sleep=None, clock=None):
        if not settings.api_key:
            raise MarketDataError("the polygon provider needs MARKET_DATA_API_KEY")
        self.settings = settings
        self.calendar = calendar or default_calendar()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        extra = {} if sleep is None else dict(sleep=sleep)
        self.http = JsonHttpClient(headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
                                   timeout_seconds=settings.timeout_seconds, max_retries=settings.max_retries,
                                   backoff_seconds=settings.backoff_seconds,
                                   max_rate_limit_wait_seconds=settings.max_rate_limit_wait_seconds, session=session,
                                   **extra)
        self._host = urlsplit(settings.base_url).hostname

    def as_of(self):
        return self._clock() - timedelta(seconds=self.settings.delay_seconds)

    def get_bars(self, symbol, interval, start, end):
        """Validated, completed bars with ``start <= timestamp < end`` (oldest first)."""
        interval = Interval.parse(interval)
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise MarketDataError("start/end must be timezone-aware with start < end")
        as_of = self.as_of()
        if interval in NATIVE:
            bars = self._fetch(symbol, interval, start, end)
            return [b for b in completed_bars(bars, as_of, self.calendar) if start <= b.timestamp < end]
        source = DERIVED[interval]
        fetch_start = start if interval.intraday else datetime.combine(start.astimezone(EXCHANGE_TZ).date(),
                                                                       datetime.min.time(), tzinfo=EXCHANGE_TZ)
        fetch_end = end if interval.intraday else end + timedelta(days=1)
        derived = derive_completed(self._fetch(symbol, source, fetch_start, fetch_end), interval, self.calendar, as_of)
        return [b for b in derived if start <= b.timestamp < end]

    def get_latest_bars(self, symbol, interval, limit, *, lookback):
        """The last ``limit`` completed bars within ``lookback`` (a timedelta) of the data cut-off."""
        if limit < 1:
            raise ValueError("limit must be positive")
        end = self.as_of()
        return self.get_bars(symbol, interval, end - lookback, end + timedelta(seconds=1))[-limit:]

    def _fetch(self, symbol, interval, start, end):
        multiplier, timespan = NATIVE[interval]
        from_ms = int(start.timestamp() * 1000)
        to_ms = int(end.timestamp() * 1000) - 1  # The vendor's range end is inclusive.
        url = f"{self.settings.base_url}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_ms}/{to_ms}"
        params = dict(adjusted="true" if self.settings.adjusted else "false", sort="asc", limit="50000")
        results = []
        for page in range(MAX_PAGES):
            payload = self.http.get_json(url, params=params)
            results.extend(self._results(payload, url))
            next_url = payload.get("next_url")
            if not next_url:
                break
            if not isinstance(next_url, str) or urlsplit(next_url).hostname != self._host or \
                    urlsplit(next_url).scheme != "https":
                raise ProviderError("payload", f"refusing pagination link to another host from {safe_target(url)}")
            url, params = next_url, None
        else:
            raise ProviderError("payload", f"more than {MAX_PAGES} pages from {safe_target(url)}")
        bars = [self._bar(symbol, interval, item, i) for i, item in enumerate(results)]
        bars = list(validate_calendar_series(bars, self.calendar))
        if not self.settings.include_extended_hours:
            bars = [b for b in bars if b.session is Session.REGULAR]
        logger.info("event=market_data_fetch provider=polygon symbol=%s interval=%s bars=%d", symbol, interval.label,
                    len(bars))
        return bars

    def _results(self, payload, url):
        if not isinstance(payload, dict):
            raise ProviderError("payload", f"unexpected payload type from {safe_target(url)}")
        status = payload.get("status")
        if status not in OK_STATUS:
            raise ProviderError("payload", f"unexpected status {str(status)[:32]!r} from {safe_target(url)}")
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ProviderError("payload", f"results is not a list from {safe_target(url)}")
        return results

    def _bar(self, symbol, interval, item, index):
        if not isinstance(item, dict) or any(key not in item for key in FIELDS):
            raise MarketDataError(f"result {index}: missing one of {', '.join(FIELDS)}")
        stamp, volume = item["t"], item["v"]
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise MarketDataError(f"result {index}: timestamp must be integer milliseconds")
        if isinstance(volume, bool) or not isinstance(volume, (int, Decimal)) or volume != int(volume):
            raise MarketDataError(f"result {index}: volume must be a whole number")
        prices = []
        for key in ("o", "h", "l", "c"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise MarketDataError(f"result {index}: {key} must be numeric")
            prices.append(Decimal(value))
        timestamp = (EPOCH + timedelta(milliseconds=stamp)).astimezone(EXCHANGE_TZ)
        try:
            return MarketBar(symbol, timestamp, interval, *prices, int(volume),
                             session=self.calendar.classify(timestamp))
        except MarketDataError as error:
            raise MarketDataError(f"result {index}: {error}") from None
