"""Massive Options adapter (Phase 9C): the options chain snapshot -> provider-neutral Phase 9B source records.

Endpoint (the only one used; entitled per the Phase 9A live checks)::

    GET {OPTIONS_DATA_BASE_URL}/v3/snapshot/options/{underlying}?limit=N
        [&contract_type=call|put] [&expiration_date.gte=YYYY-MM-DD] [&expiration_date.lte=YYYY-MM-DD]

**HTTP:** the existing secure ``market_data.http.JsonHttpClient``: Bearer header only, timeouts, bounded
retries, pacing, no redirects, a hard request budget, and JSON numbers parsed as ``Decimal``. The key never
appears in a URL, error, log or result.

**Pagination:** pages are followed through ``next_url`` until one of these happens:

- the source ends: the chain is complete;
- ``max_pages`` is reached with a ``next_url`` still pending: ``truncated=True``;
- the request budget runs out after at least one page: ``truncated=True``.

A ``next_url`` that is not HTTPS on the configured host fails the retrieval (``pagination``). Other provider errors
fail it too: authentication or entitlement, HTTP, transport, malformed payloads, and a budget exhausted before any
page. A partial chain is never presented as complete.

**Mapping** (only the fields the source actually returns; nothing is invented):

| Massive | Phase 9B |
|---|---|
| ``details.ticker``, ``contract_type``, ``expiration_date``, ``strike_price`` | identity cross-check fields |
| ``underlying_asset.ticker`` | ``underlying`` cross-check |
| ``details.exercise_style``, ``shares_per_contract``, ``additional_underlyings`` | ``terms`` |
| ``day.*`` (open, high, low, close, previous_close, change, change_percent, volume, vwap) | ``day``; ``observed_at`` from ``day.last_updated`` (epoch ns) when valid |
| ``open_interest`` | ``open_interest.value``; no date is invented, so ``provider_snapshot_unverified`` |
| ``implied_volatility`` | ``implied_volatility.value``; untimed, so ``provider_snapshot_unverified`` |
| ``greeks.{delta,gamma,theta,vega,rho}`` | ``greeks`` (rho only if present); untimed, so ``provider_snapshot_unverified`` |
| ``last_quote`` / ``last_trade`` | ``quote`` / ``trade`` only when the source returns them |

**Unavailable groups:** ``quote`` or ``trade`` is declared unavailable when no returned record carries
``last_quote`` or ``last_trade``, as on the current plan (dedicated quotes and trades return 403). No bid or ask is
derived from day fields, and no last trade from ``day.close``.

**Underlying price:** never derived from option contracts. The chain's ``underlying_asset`` returned only a ticker
in the Phase 9A checks, so the price is unavailable (``not_supplied``). If a price ever appears, it is still not
used in v1 (``not_used_in_v1``) until it is verified.

**Scope:** the requested contract types and expiration bounds are sent as query filters. Any returned contract
outside them fails the retrieval (``scope``): a snapshot never claims a scope it does not match.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from market_data.http import JsonHttpClient, ProviderError
from options_data.provider import ChainResult, OptionsDataProvider, OptionsProviderError

PROVIDER_ID = "massive"
ADAPTER_VERSION = "massive-options-9c-v1"
ENDPOINT_FAMILY = "options_chain_snapshot"
TIMEOUT_SECONDS, MAX_RETRIES, BACKOFF_SECONDS, MAX_RATE_LIMIT_WAIT = 20.0, 2, 1.0, 30.0
DAY_FIELDS = ("open", "high", "low", "close", "previous_close", "change", "change_percent", "volume", "vwap")
GREEK_FIELDS = ("delta", "gamma", "theta", "vega", "rho")
QUOTE_FIELDS = dict(bid="bid", ask="ask", bid_size="bid_size", ask_size="ask_size")
TRADE_FIELDS = dict(price="price", size="size")


def make_client(settings, *, session=None, sleep=None, max_requests=None):
    client = JsonHttpClient(headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
                            timeout_seconds=TIMEOUT_SECONDS, max_retries=MAX_RETRIES, backoff_seconds=BACKOFF_SECONDS,
                            max_rate_limit_wait_seconds=MAX_RATE_LIMIT_WAIT, session=session,
                            min_interval_seconds=settings.min_request_interval_seconds,
                            **({"sleep": sleep} if sleep else {}))
    client.max_requests = max_requests
    return client


def epoch_instant(value):
    """UTC datetime from an epoch-nanoseconds integer (the Massive ``last_updated`` unit); None if not valid."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value <= 10 ** 17:
        return None
    ns = int(value)
    return datetime.fromtimestamp(ns // 10 ** 9, tz=timezone.utc).replace(microsecond=(ns % 10 ** 9) // 1000)


def _present(source, fields):
    """{field: value} for the fields the source actually returned (non-null)."""
    if not isinstance(source, dict):
        return {}
    return {field: source[name] for field, name in fields.items() if source.get(name) is not None}


def map_contract(item):
    """One Massive chain result -> one provider-neutral source record. Unmappable input becomes a record the pure
    layer excludes as malformed (it never raises)."""
    if not isinstance(item, dict) or not isinstance(item.get("details"), dict):
        return dict(provider_symbol=None)
    details = item["details"]
    record = dict(provider_symbol=details.get("ticker"))
    for field, name in (("option_type", "contract_type"), ("expiration", "expiration_date"),
                         ("strike", "strike_price")):
        if details.get(name) is not None:
            record[field] = details[name]
    underlying = item.get("underlying_asset")
    if isinstance(underlying, dict) and underlying.get("ticker") is not None:
        record["underlying"] = underlying["ticker"]
    terms = {}
    if details.get("exercise_style") is not None:
        terms["exercise_style"] = details["exercise_style"]
    if details.get("shares_per_contract") is not None:
        terms["shares_per_contract"] = details["shares_per_contract"]
    extra = details.get("additional_underlyings")
    if extra:
        terms["deliverables"] = [dict(kind=u.get("type"), symbol=u.get("underlying"), amount=u.get("amount"))
                                 if isinstance(u, dict) else u for u in extra]
    if terms:
        record["terms"] = terms
    day = _present(item.get("day"), {k: k for k in DAY_FIELDS})
    if day:
        observed = epoch_instant(item["day"].get("last_updated"))
        if observed:
            day["observed_at"] = observed
        record["day"] = day
    if item.get("open_interest") is not None:
        record["open_interest"] = dict(value=item["open_interest"])
    if item.get("implied_volatility") is not None:
        record["implied_volatility"] = dict(value=item["implied_volatility"])
    greeks = _present(item.get("greeks"), {k: k for k in GREEK_FIELDS})
    if greeks:
        record["greeks"] = greeks
    quote = _present(item.get("last_quote"), QUOTE_FIELDS)
    if quote:
        observed = epoch_instant(item["last_quote"].get("last_updated"))
        if observed:
            quote["observed_at"] = observed
        record["quote"] = quote
    trade = _present(item.get("last_trade"), TRADE_FIELDS)
    if trade:
        observed = epoch_instant(item["last_trade"].get("sip_timestamp"))
        if observed:
            trade["observed_at"] = observed
        record["trade"] = trade
    return record


def _in_scope(item, scope):
    details = item.get("details") if isinstance(item, dict) else None
    if not isinstance(details, dict):
        return True  # Malformed items are excluded by the pure layer, not treated as scope violations.
    kind, expiration = details.get("contract_type"), details.get("expiration_date")
    if isinstance(kind, str) and kind.lower() in ("call", "put") and kind.lower() not in scope.contract_types:
        return False
    try:
        day = date.fromisoformat(expiration) if isinstance(expiration, str) else None
    except ValueError:
        return True
    if day and scope.expiration_from and day < date.fromisoformat(scope.expiration_from):
        return False
    if day and scope.expiration_through and day > date.fromisoformat(scope.expiration_through):
        return False
    return True


class MassiveOptionsProvider(OptionsDataProvider):
    def __init__(self, settings, *, session=None, sleep=None, max_requests=None):
        self.settings = settings
        self.host = urlsplit(settings.base_url).hostname
        self.client = make_client(settings, session=session, sleep=sleep, max_requests=max_requests)

    def _get(self, url, params):
        try:
            return self.client.get_json(url, params=params)
        except ProviderError as error:
            raise OptionsProviderError(error.kind, str(error), error.status) from None

    def get_chain(self, underlying, scope):
        params = dict(limit=scope.page_size)
        if len(scope.contract_types) == 1:
            params["contract_type"] = scope.contract_types[0]
        if scope.expiration_from:
            params["expiration_date.gte"] = scope.expiration_from
        if scope.expiration_through:
            params["expiration_date.lte"] = scope.expiration_through
        url, items, pages, truncated = f"{self.settings.base_url}/v3/snapshot/options/{underlying}", [], 0, False
        while True:
            try:
                payload = self._get(url, params)
            except OptionsProviderError as error:
                if error.kind == "budget" and pages:
                    truncated = True  # Partial: recorded, never hidden.
                    break
                raise
            pages += 1
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise OptionsProviderError("payload", f"unexpected chain payload from {self.host}")
            for item in results:
                if not _in_scope(item, scope):
                    raise OptionsProviderError("scope", "provider returned a contract outside the requested scope")
            items.extend(results)
            next_url = payload.get("next_url")
            if not next_url:
                break
            parts = urlsplit(next_url) if isinstance(next_url, str) else None
            if not parts or parts.scheme != "https" or parts.hostname != self.host:
                raise OptionsProviderError("pagination", "refusing a pagination link that is not HTTPS on the "
                                                         "configured host")
            if pages >= scope.max_pages:
                truncated = True
                break
            url, params = next_url, None
        present_underlying_price = any(isinstance(i, dict) and isinstance(i.get("underlying_asset"), dict)
                                       and i["underlying_asset"].get("price") is not None for i in items)
        unavailable = tuple(group for group, key in (("quote", "last_quote"), ("trade", "last_trade"))
                            if not any(isinstance(i, dict) and i.get(key) for i in items))
        return ChainResult(
            provider=PROVIDER_ID, adapter_version=ADAPTER_VERSION, endpoint_families=(ENDPOINT_FAMILY,),
            records=tuple(map_contract(item) for item in items), unavailable_groups=unavailable,
            underlying_price=dict(reason="not_used_in_v1" if present_underlying_price else "not_supplied"),
            pages_fetched=pages, requests_made=self.client.requests_made, truncated=truncated)
