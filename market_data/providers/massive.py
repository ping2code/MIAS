"""Massive Stocks aggregates adapter (Phase 7A): the explicitly named ``massive_stocks`` provider.

It is selected only by ``MARKET_DATA_PROVIDER=massive_stocks``. The historical
``massive`` value stays an alias of ``polygon``, the Phase 6 frozen provider
identity, and its behaviour is unchanged.

Endpoint (same aggregates contract family, own host):

    GET https://api.massive.com/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}
        ?adjusted=true|false&sort=asc&limit=50000

**Reuse, not refactor.** ``PolygonProvider`` is frozen for Phase 6
reproducibility, so this class inherits it unchanged:

- the secure HTTP client: Bearer header, timeout, bounded retry, pacing, no redirects;
- ``get_bars`` and ``get_latest_bars``: completed bars only; 1h and 1d derived from
  30m with MIAS session semantics; 1d regular session only; no vendor daily bars;
- ``_results`` status and envelope checks, and ``_bar`` field, type and OHLC
  validation;
- the supported-session filter (bars starting before 04:00 or from 20:00 ET are
  excluded and counted) and the calendar contract.

Only the pagination loop (``_fetch``) is re-implemented here, using the same
validators. The reasons: its logs and diagnostics name this provider, not
``polygon``, and it counts, without keeping, the optional ``vw`` (VWAP) and ``n``
(trade count) fields for contract diagnostics.

``vw`` and ``n`` are **not** exposed in Phase 7A:

- ``MarketBar`` is frozen, and no parallel data model is introduced;
- derived VWAP and trade-count values are never computed.

**Keys:** the API key is only ever the ``Authorization: Bearer`` header value. It
never appears in URLs, query strings, logs, exceptions or diagnostics.
"""
import logging
from urllib.parse import urlsplit

from market_data.http import ProviderError, safe_target
from market_data.models import MarketDataError, Session
from market_data.providers.polygon import MAX_PAGES, NATIVE, PolygonProvider, exclude_unsupported_sessions
from market_data.validation import validate_calendar_series, validate_series

logger = logging.getLogger("market_data.massive")
PROVIDER_ID = "massive_stocks"
DEFAULT_BASE_URL = "https://api.massive.com"
OPTIONAL_FIELDS = ("vw", "n")


class MassiveStocksProvider(PolygonProvider):
    provider_id = PROVIDER_ID
    page_limit = 50_000  # Vendor maximum; the live contract check may lower it to observe pagination.

    def __init__(self, settings, *, calendar=None, session=None, sleep=None, clock=None):
        if settings.provider != PROVIDER_ID:
            raise ValueError(f"MassiveStocksProvider needs provider={PROVIDER_ID}")
        if not settings.api_key:
            raise MarketDataError("the massive_stocks provider needs MARKET_DATA_API_KEY")
        super().__init__(settings, calendar=calendar, session=session, sleep=sleep, clock=clock)

    def _fetch(self, symbol, interval, start, end):
        multiplier, timespan = NATIVE[interval]
        from_ms = int(start.timestamp() * 1000)
        to_ms = int(end.timestamp() * 1000) - 1  # The vendor's range end is inclusive.
        url = f"{self.settings.base_url}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_ms}/{to_ms}"
        params = dict(adjusted="true" if self.settings.adjusted else "false", sort="asc", limit=str(self.page_limit))
        results, statuses, adjusted_echo, next_hosts = [], set(), set(), set()
        for page in range(MAX_PAGES):
            payload = self.http.get_json(url, params=params)
            results.extend(self._results(payload, url))
            statuses.add(payload.get("status"))
            if "adjusted" in payload:
                adjusted_echo.add(payload.get("adjusted"))
            next_url = payload.get("next_url")
            if not next_url:
                break
            parts = urlsplit(next_url) if isinstance(next_url, str) else None
            if parts is not None and parts.hostname:
                next_hosts.add(parts.hostname)
            if parts is None or parts.hostname != self._host or parts.scheme != "https":
                raise ProviderError("payload", f"refusing pagination link to another host from {safe_target(url)}")
            url, params = next_url, None
        else:
            raise ProviderError("payload", f"more than {MAX_PAGES} pages from {safe_target(url)}")
        items = [item for item in results if isinstance(item, dict)]
        optional = {key: sum(1 for item in items if key in item) for key in OPTIONAL_FIELDS}
        bars = list(validate_series([self._bar(symbol, interval, item, i) for i, item in enumerate(results)]))
        bars, overnight = exclude_unsupported_sessions(bars)
        bars = list(validate_calendar_series(bars, self.calendar))
        raw_count = len(bars)
        if not self.settings.include_extended_hours:
            bars = [b for b in bars if b.session is Session.REGULAR]
        self.diagnostics.append(dict(provider=PROVIDER_ID, symbol=symbol, source_interval=interval.label,
                                     pages=page + 1, page_limit=self.page_limit,
                                     statuses=sorted(str(s) for s in statuses),
                                     adjusted_requested=self.settings.adjusted,
                                     adjusted_echo=sorted(str(a) for a in adjusted_echo),
                                     next_url_hosts=sorted(next_hosts), results=len(results), raw_bars=raw_count,
                                     kept_bars=len(bars), excluded_overnight_bars=overnight,
                                     results_with_vw=optional["vw"], results_with_n=optional["n"],
                                     first_bar=bars[0].timestamp.isoformat() if bars else None,
                                     last_bar=bars[-1].timestamp.isoformat() if bars else None))
        logger.info("event=market_data_fetch provider=%s symbol=%s interval=%s pages=%d bars=%d excluded_overnight=%d",
                    PROVIDER_ID, symbol, interval.label, page + 1, len(bars), overnight)
        return bars
