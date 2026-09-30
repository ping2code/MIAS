"""User-run, read-only live contract check of the Massive Options API (Phase 9A).

    OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.massive_check --symbol META \
        [--page-limit 50] [--max-requests 14]

Purpose: discover what the configured Massive Options subscription actually provides, before any Phase 9 schema is
frozen. This is **not** the production adapter, and its report format (``phase9a-check-v1``) is not a contract.

**Read-only and bounded:**

- only GET requests to the configured host (``OPTIONS_DATA_BASE_URL``); pagination links to any other host, or
  over plain HTTP, are refused;
- the existing secure client (``market_data.http.JsonHttpClient``): Bearer header, timeouts, bounded retries,
  pacing (``OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS``), no redirects, and a hard request budget
  (``--max-requests``, retries included, at most 25);
- chain pages are capped by ``OPTIONS_DATA_MAX_PAGES`` (at most 5) and ``--page-limit`` (at most 250 results);
- one symbol per run; no database, ledger, Redis, Telegram or OpenAI access; nothing is written.

**Secrets:** the key comes from ``OPTIONS_DATA_API_KEY`` in the process environment only (never ``.env``; never
``MARKET_DATA_*``) and is sent only as the ``Authorization: Bearer`` header. It never appears in the report,
errors or logs. Pagination links are reported as host plus query parameter *names* only.

**The report contains metadata only:** endpoint statuses, observed field paths with presence counts, timestamp
paths with their unit and newest/oldest instants, ages, pagination, rate-limit headers, and contract tickers of
non-standard contracts. It contains no prices, sizes, Greeks or IV values, and no raw payloads.

Endpoint families probed (Polygon-family v3 paths; every result is observed, never assumed):

| Family | Request |
|---|---|
| ``contracts_reference`` | ``/v3/reference/options/contracts?underlying_ticker=SYM&limit=N`` |
| ``contracts_reference_as_of`` | the same with ``as_of=<date 30 days before the check>`` |
| ``chain_snapshot`` | ``/v3/snapshot/options/SYM?limit=N`` (paginated, bounded) |
| ``contract_snapshot`` | ``/v3/snapshot/options/SYM/<contract>`` |
| ``quotes`` / ``trades`` | ``/v3/quotes/<contract>?limit=5``, ``/v3/trades/<contract>?limit=5`` |
| ``quotes_historical`` | ``/v3/quotes/<contract>?timestamp.lte=<date 7 days before>&limit=1`` |
| ``contract_aggregates_historical`` | ``/v2/aggs/ticker/<contract>/range/1/day/<30 days before>/<7 days before>`` |

``<contract>`` is the first contract ticker returned by the chain snapshot (else by the reference list).

Exit codes: 0 when the chain snapshot endpoint answered; 1 on a provider failure or no chain access; 2 on a
configuration or usage error (NOT EXECUTED).
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import re
import sys
from urllib.parse import parse_qsl, urlsplit

from market_data.http import JsonHttpClient, ProviderError
from market_data.models import EXCHANGE_TZ, SYMBOL
from options_data.config import OptionsDataConfigError, load_options_data_settings

CHECK_VERSION = "phase9a-check-v1"
MAX_REQUESTS, MAX_PAGE_LIMIT = 25, 250
TIMEOUT_SECONDS, MAX_RETRIES, BACKOFF_SECONDS, MAX_RATE_LIMIT_WAIT = 20.0, 1, 1.0, 30.0
HISTORY_DAYS, RECENT_HISTORY_DAYS = 30, 7
OCC = re.compile(r"O:([A-Z0-9.]+?)(\d{6})([CP])(\d{8})")
TIME_KEY = re.compile(r"(timestamp|updated|_time|^time$|_date$|^date$|_at$)")
ADJUSTMENT_KEYS = ("additional_underlyings", "deliverable", "correction", "adjust")
# Report-only heuristics for the delay classification (not production thresholds).
REALTIME_MAX_AGE, DELAYED_MAX_AGE = 120, 3600


def _family_status(status):
    if status is None:
        return "no_response"
    if status == 200:
        return "entitled"
    if status in (401, 403):
        return "not_entitled"
    if status == 404:
        return "not_found"
    if status in (400, 422):
        return "request_rejected"
    if status == 429:
        return "rate_limited"
    return "error"


def _leaves(value, path=""):
    """(path, value) for every leaf; lists become ``path[]`` (values are only used for types and timestamps)."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        if not value:
            yield f"{path}[]", None
        for child in value:
            yield from _leaves(child, f"{path}[]")
    else:
        yield path, value


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, Decimal)):
        return "number"
    return "string" if isinstance(value, str) else type(value).__name__


def field_presence(items):
    """{path: {present, null, types}} over result items (no values)."""
    out = {}
    for item in items:
        seen = {}
        for path, value in _leaves(item):
            seen.setdefault(path, set()).add(_kind(value))
        for path, kinds in seen.items():
            entry = out.setdefault(path, dict(present=0, null=0, types=set()))
            if kinds == {"null"}:
                entry["null"] += 1
            else:
                entry["present"] += 1
            entry["types"] |= kinds - {"null"}
    return {path: dict(present=e["present"], null=e["null"], types=sorted(e["types"])) for path, e in sorted(out.items())}


def parse_instant(value):
    """(instant, unit) for an epoch number or ISO string; (None, None) if it is not a recognisable time."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, Decimal)):
        number = int(value)
        for unit, scale, low in (("ns", 10 ** 9, 10 ** 17), ("us", 10 ** 6, 10 ** 14), ("ms", 10 ** 3, 10 ** 11),
                                 ("s", 1, 10 ** 8)):
            if number >= low:
                return datetime.fromtimestamp(number / scale, tz=timezone.utc), unit
        return None, None
    if isinstance(value, str):
        text = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return datetime.combine(date.fromisoformat(text), datetime.min.time(), EXCHANGE_TZ), "date"
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=timezone.utc)), "iso"
        except ValueError:
            return None, None
    return None, None


def time_fields(items, now):
    """{path: {count, units, newest, oldest, newest_age_seconds}} for time-like leaves."""
    found = {}
    for item in items:
        for path, value in _leaves(item):
            leaf = path.split(".")[-1].replace("[]", "")
            if not TIME_KEY.search(leaf) or "expiration" in leaf:  # Expiration dates are terms, not observations.
                continue
            instant, unit = parse_instant(value)
            if instant is None:
                continue
            entry = found.setdefault(path, dict(count=0, units=set(), newest=instant, oldest=instant))
            entry["count"] += 1
            entry["units"].add(unit)
            entry["newest"], entry["oldest"] = max(entry["newest"], instant), min(entry["oldest"], instant)
    return {path: dict(count=e["count"], units=sorted(e["units"]),
                       newest=e["newest"].astimezone(timezone.utc).isoformat(timespec="seconds"),
                       oldest=e["oldest"].astimezone(timezone.utc).isoformat(timespec="seconds"),
                       newest_age_seconds=int((now - e["newest"]).total_seconds()))
            for path, e in sorted(found.items())}


def _groups(presence, token):
    """Top-level group names (object keys) containing ``token``."""
    return sorted({path.split(".")[0] for path in presence if "." in path and token in path.split(".")[0]})


def timestamp_capabilities(presence, times):
    """How IV and Greeks are timed: own timestamp, parent (item-level) timestamp, or no time basis."""
    top_level_times = sorted(p for p in times if "." not in p)

    def basis(group_paths):
        own = sorted(p for p in times if any(p.startswith(g + ".") for g in group_paths))
        if own:
            return dict(basis="own_timestamp", timestamp_paths=own)
        if top_level_times:
            return dict(basis="parent_timestamp", timestamp_paths=top_level_times)
        return dict(basis="time_basis_unverified", timestamp_paths=[])

    greek_groups = _groups(presence, "greek")
    iv_paths = sorted(p for p in presence if "implied_vol" in p)
    iv_groups = sorted({p.split(".")[0] for p in iv_paths if "." in p})
    groups = {name: sorted(p for p in times if p.startswith(name + ".")) for name in sorted(
        {p.split(".")[0] for p in presence if "." in p})}
    return dict(
        greeks=dict(observed=bool(greek_groups), groups=greek_groups, **basis(greek_groups)) if greek_groups
        else dict(observed=False, basis="not_observed"),
        implied_volatility=dict(observed=bool(iv_paths), paths=iv_paths, **basis(iv_groups)) if iv_paths
        else dict(observed=False, basis="not_observed"),
        item_level_timestamp_paths=top_level_times,
        timestamp_paths_by_group=groups)


def open_interest_capability(presence, times):
    paths = sorted(p for p in presence if "open_interest" in p)
    if not paths:
        return dict(observed=False, semantics="not_observed")
    parents = {p.rsplit(".", 1)[0] if "." in p else "" for p in paths}
    dated = sorted(p for p in times if (p.rsplit(".", 1)[0] if "." in p else "") in parents and p not in paths)
    present = sum(presence[p]["present"] for p in paths)
    return dict(observed=True, paths=paths, present_count=present, sibling_time_paths=dated,
                semantics="date_or_time_provided_alongside" if dated else "uncertain_no_date_provided")


def adjusted_contract_observation(items, symbol, presence):
    tickers = []
    shares = {}
    for item in items:
        for path, value in _leaves(item):
            leaf = path.split(".")[-1]
            if leaf == "ticker" and isinstance(value, str) and value.startswith("O:"):
                tickers.append(value)
            if leaf == "shares_per_contract" and isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                shares[str(value)] = shares.get(str(value), 0) + 1
    unparsed = sorted({t for t in tickers if not OCC.fullmatch(t)})
    nonstandard = sorted({t for t in tickers if OCC.fullmatch(t) and OCC.fullmatch(t)[1] != symbol})
    keys = sorted(p for p in presence if any(k in p for k in ADJUSTMENT_KEYS))
    observed = bool(nonstandard or keys or set(shares) - {"100"})
    return dict(status="observed" if observed else "not_observed", contract_tickers_seen=len(set(tickers)),
                nonstandard_root_count=len(nonstandard), nonstandard_root_examples=nonstandard[:5],
                unparsed_ticker_count=len(unparsed), shares_per_contract_values=dict(sorted(shares.items())),
                adjustment_related_paths=keys)


class Probe:
    """Bounded GETs through the secure client; statuses and pagination recorded without payloads or secrets."""

    def __init__(self, client, base_url, max_pages):
        self.client, self.base_url, self.max_pages = client, base_url, max_pages
        self.host = urlsplit(base_url).hostname
        self.endpoints, self.pagination = {}, {}

    def get(self, family, path, params=None, paginate=False):
        url, items, pages, first_link, ended = f"{self.base_url}{path}", [], 0, None, False
        result = dict(family=family, http_status=None, status="no_response", pages=0, results=0)
        while True:
            try:
                payload = self.client.get_json(url, params=params)
            except ProviderError as error:
                result.update(http_status=error.status, status=_family_status(error.status) if error.status
                              else error.kind, error_kind=error.kind)
                if error.kind == "budget":
                    result["status"] = "not_attempted_budget" if pages == 0 else "truncated_budget"
                break
            pages += 1
            result.update(http_status=200, status="entitled")
            batch = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(batch, dict):
                batch = [batch]
            if not isinstance(batch, list):
                result["status"] = "unexpected_payload"
                break
            items.extend(x for x in batch if isinstance(x, dict))
            if isinstance(payload, dict) and payload.get("status") is not None:
                result["provider_status"] = str(payload.get("status"))[:32]
            next_url = payload.get("next_url") if isinstance(payload, dict) else None
            if not paginate:
                break
            if not next_url:
                ended = True
                break
            parts = urlsplit(next_url) if isinstance(next_url, str) else None
            names = sorted({k for k, _ in parse_qsl(parts.query)}) if parts else []
            if first_link is None:  # Metadata of the first pagination link (host and parameter names only).
                first_link = dict(next_url_host=parts.hostname if parts else None,
                                  next_url_scheme=parts.scheme if parts else None,
                                  next_url_query_parameter_names=names,
                                  next_url_embeds_api_key=any(n.lower() in ("apikey", "api_key") for n in names))
            if not parts or parts.scheme != "https" or parts.hostname != self.host:
                result["status"] = "pagination_refused_foreign_host"
                break
            if pages >= self.max_pages:
                result["status"] = "truncated_page_budget"
                break
            url, params = next_url, None
        result.update(pages=pages, results=len(items))
        self.endpoints[family] = result
        if paginate:
            self.pagination[family] = dict(pages_fetched=pages, results_fetched=len(items), complete=ended,
                                           next_url_present=first_link is not None,
                                           followed_pages_used_header_auth=pages > 1, **(first_link or {}))
        return items


def _contract_ticker(*groups):
    for items in groups:
        for item in items:
            for path, value in _leaves(item):
                if path.split(".")[-1] == "ticker" and isinstance(value, str) and OCC.fullmatch(value):
                    return value
    return None


def classify_delay(age, regular_session):
    if age is None:
        return "indeterminate_no_timestamps"
    if not regular_session:
        return "indeterminate_outside_regular_session"
    if age <= REALTIME_MAX_AGE:
        return "appears_realtime"
    if age <= DELAYED_MAX_AGE:
        return "appears_delayed"
    return "indeterminate_old_data"


def run(client, settings, *, symbol, page_limit, now, calendar):
    probe = Probe(client, settings.base_url, settings.max_pages)
    history = (now - timedelta(days=HISTORY_DAYS)).astimezone(EXCHANGE_TZ).date()
    recent = (now - timedelta(days=RECENT_HISTORY_DAYS)).astimezone(EXCHANGE_TZ).date()
    reference = probe.get("contracts_reference", "/v3/reference/options/contracts",
                          dict(underlying_ticker=symbol, limit=page_limit))
    probe.get("contracts_reference_as_of", "/v3/reference/options/contracts",
              dict(underlying_ticker=symbol, as_of=history.isoformat(), limit=10))
    chain = probe.get("chain_snapshot", f"/v3/snapshot/options/{symbol}", dict(limit=page_limit), paginate=True)
    contract = _contract_ticker(chain, reference)
    single = quotes = trades = []
    if contract:
        single = probe.get("contract_snapshot", f"/v3/snapshot/options/{symbol}/{contract}")
        quotes = probe.get("quotes", f"/v3/quotes/{contract}", dict(limit=5))
        trades = probe.get("trades", f"/v3/trades/{contract}", dict(limit=5))
        probe.get("quotes_historical", f"/v3/quotes/{contract}", {"timestamp.lte": recent.isoformat(), "limit": 1})
        probe.get("contract_aggregates_historical",
                  f"/v2/aggs/ticker/{contract}/range/1/day/{history.isoformat()}/{recent.isoformat()}",
                  dict(adjusted="true", limit=10))
    else:
        for family in ("contract_snapshot", "quotes", "trades", "quotes_historical", "contract_aggregates_historical"):
            probe.endpoints[family] = dict(family=family, status="not_attempted_no_contract", pages=0, results=0)

    chain_presence, chain_times = field_presence(chain), time_fields(chain, now)
    regular = calendar.classify(now).value == "regular"

    def newest(times, token):
        ages = [t["newest_age_seconds"] for p, t in times.items() if token in p.split(".")[0]]
        return min(ages) if ages else None

    quote_age, trade_age = newest(chain_times, "quote"), newest(chain_times, "trade")
    quote_endpoint_times = time_fields(quotes, now)
    trade_endpoint_times = time_fields(trades, now)
    ages = [a for a in (quote_age, trade_age) if a is not None]
    warnings = []
    for family, result in probe.endpoints.items():
        if result["status"] not in ("entitled",):
            warnings.append(f"{family}: {result['status']}")
    caps = timestamp_capabilities(chain_presence, chain_times)
    for name in ("greeks", "implied_volatility"):
        if caps[name]["basis"] in ("time_basis_unverified", "parent_timestamp"):
            warnings.append(f"{name}: {caps[name]['basis']} (cutoff-safety not proven)")
    if not regular:
        warnings.append("checked outside the regular session: delay classification is indeterminate")
    report = dict(
        check_version=CHECK_VERSION, live_validation="COMPLETED", provider=settings.provider,
        settings=settings.public(), symbol=symbol,
        checked_at=now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        checked_at_exchange_time=now.astimezone(EXCHANGE_TZ).isoformat(timespec="seconds"),
        session_state=calendar.classify(now).value, page_limit=page_limit, sample_contract=contract,
        endpoint_results=probe.endpoints,
        timing=dict(newest_quote_age_seconds=quote_age, newest_trade_age_seconds=trade_age,
                    delay_classification=classify_delay(min(ages) if ages else None, regular),
                    classification_rule=f"regular session and newest age <= {REALTIME_MAX_AGE}s: appears_realtime; "
                                        f"<= {DELAYED_MAX_AGE}s: appears_delayed; otherwise indeterminate "
                                        "(report heuristic, not a production threshold)",
                    chain_timestamp_fields=chain_times,
                    quotes_endpoint_timestamp_fields=quote_endpoint_times,
                    trades_endpoint_timestamp_fields=trade_endpoint_times),
        field_presence_counts=dict(
            chain_snapshot=dict(items=len(chain), fields=chain_presence),
            contracts_reference=dict(items=len(reference), fields=field_presence(reference)),
            contract_snapshot=dict(items=len(single), fields=field_presence(single)),
            quotes=dict(items=len(quotes), fields=field_presence(quotes)),
            trades=dict(items=len(trades), fields=field_presence(trades))),
        timestamp_capabilities=caps,
        open_interest_capability=open_interest_capability(chain_presence, chain_times),
        pagination=probe.pagination,
        rate_limit_metadata=dict(headers_seen=dict(sorted(client.rate_limit_headers.items())),
                                 http_status_counts={str(k): v for k, v in sorted(client.status_counts.items())},
                                 requests_made=client.requests_made, request_budget=client.max_requests,
                                 paced_seconds=round(client.paced_seconds, 1)),
        historical_capability={
            family: probe.endpoints.get(family, {}).get("status")
            for family in ("contracts_reference_as_of", "quotes_historical", "contract_aggregates_historical")}
        | dict(chain_snapshot_as_of="unverified: no documented as-of parameter was exercised",
               note="entitled means the request succeeded; results show whether data came back"),
        historical_results={family: probe.endpoints.get(family, {}).get("results")
                            for family in ("contracts_reference_as_of", "quotes_historical",
                                           "contract_aggregates_historical")},
        adjusted_contract_observation=adjusted_contract_observation(chain + reference, symbol,
                                                                    {**chain_presence, **field_presence(reference)}),
        warnings=sorted(warnings))
    return report, probe.endpoints.get("chain_snapshot", {}).get("status") in (
        "entitled", "truncated_page_budget", "truncated_budget")


def main(argv=None, environ=None, *, session=None, clock=None, sleep=None, out=None, calendar=None):
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m options_data.massive_check")
    parser.add_argument("--symbol", default="META")
    parser.add_argument("--page-limit", type=int, default=50)
    parser.add_argument("--max-requests", type=int, default=14)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason="usage error")), file=out)
        return 2
    symbol = args.symbol.strip().upper()
    if not SYMBOL.fullmatch(symbol) or not 1 <= args.page_limit <= MAX_PAGE_LIMIT \
            or not 1 <= args.max_requests <= MAX_REQUESTS:
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason="usage error: symbol, --page-limit (1-250) or "
                                                                     "--max-requests (1-25)")), file=out)
        return 2
    try:
        settings = load_options_data_settings(environ)
    except OptionsDataConfigError as error:
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason=str(error))), file=out)
        return 2
    if settings.provider != "massive":
        print(json.dumps(dict(live_validation="NOT EXECUTED", reason="OPTIONS_DATA_PROVIDER must be massive")),
              file=out)
        return 2
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    client = JsonHttpClient(headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
                            timeout_seconds=TIMEOUT_SECONDS, max_retries=MAX_RETRIES, backoff_seconds=BACKOFF_SECONDS,
                            max_rate_limit_wait_seconds=MAX_RATE_LIMIT_WAIT, session=session,
                            min_interval_seconds=settings.min_request_interval_seconds,
                            **({"sleep": sleep} if sleep else {}))
    client.max_requests = args.max_requests
    try:
        report, passed = run(client, settings, symbol=symbol, page_limit=args.page_limit, now=now, calendar=calendar)
    except ProviderError as error:  # Defensive: per-endpoint errors are recorded inside run().
        print(json.dumps(dict(live_validation="FAILED", kind=error.kind, error=str(error))), file=out)
        return 1
    if not passed:
        report["live_validation"] = "FAILED"
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if settings.api_key and settings.api_key in text:  # Last-line defence; unreachable by construction.
        print(json.dumps(dict(live_validation="FAILED", kind="redaction", error="secret detected in report")), file=out)
        return 1
    print(text, file=out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
