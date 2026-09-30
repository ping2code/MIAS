# Phase 9A — Massive Options contract check

**Status:** implemented on `claude/phase9a-massive-options-check`; live result pending (operator-run).

> Phase 9A discovers what the configured Massive Options subscription actually provides. **The Phase 9B
> OptionsSnapshot schema will not be frozen until these findings have been reviewed.** This check is not the
> production adapter, and its report format (`phase9a-check-v1`) is not a contract.

## 1. Purpose

Phase 9 has no repository evidence about the options API. This check measures it empirically, with no
assumptions:

- which endpoint families the plan can use;
- which fields appear, and how often;
- how quotes, trades, IV and Greeks are timed;
- whether open interest has a date;
- how pagination works;
- which rate-limit headers are sent;
- whether historical data is reachable;
- whether adjusted contracts occur.

## 2. Operator command

Supply the key explicitly for this run. It is read only from `OPTIONS_DATA_API_KEY` in the process environment.
It is never read from `.env` or from `MARKET_DATA_*`, and never persisted. Use a leading space, or an interactive
prompt, so the key stays out of shell history:

```bash
read -rs OPTIONS_DATA_API_KEY && export OPTIONS_DATA_API_KEY
```

```bash
OPTIONS_DATA_PROVIDER=massive python -m options_data.massive_check --symbol META
```

The same for NVDA: `--symbol NVDA`. Optional flags:
- `--page-limit N`: chain results per page, 1–250, default 50;
- `--max-requests N`: the total request budget including retries, 1–25, default 14.

Run it during the regular session (09:30–16:00 ET) for a meaningful delay measurement. Outside the session, the
delay is reported as indeterminate.

## 3. Environment variables (options only)

| Variable | Default | Use in 9A |
|---|---|---|
| `OPTIONS_DATA_PROVIDER` | `none` | Must be `massive`, or nothing runs (exit 2, NOT EXECUTED) |
| `OPTIONS_DATA_API_KEY` | unset | Required; sent only as `Authorization: Bearer` |
| `OPTIONS_DATA_BASE_URL` | `https://api.massive.com` | HTTPS origin only |
| `OPTIONS_DATA_DELAY_SECONDS` | unset | Reported only. The check measures raw data and applies no delay |
| `OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS` | `1.0` | Pacing between requests |
| `OPTIONS_DATA_MAX_PAGES` | `2` | Chain pages followed, 1–5 |

`MARKET_DATA_*` is never read (a test records every environment read). Frozen Phase 6 settings are unaffected.

## 4. Security, and bounded read-only behaviour

- **Requests:** GET only, to the configured host only. Pagination links to another host, or over plain HTTP, are
  refused and reported.
- **Client:** the existing `market_data.http.JsonHttpClient`:
  - timeout 20 s, one retry for transient failures;
  - pacing;
  - redirects are never followed;
  - a hard request budget that includes retries.
- **Size limits:** at most 5 chain pages of at most 250 results. The chain is never traversed without a bound.
- **Isolation:** one symbol per run. No database, ledger, Redis, Telegram or OpenAI access, and no files are
  written.
- **The key:** it appears nowhere in the report, errors or logs. Pagination links are reported only as host and
  query-parameter *names*, and `next_url_embeds_api_key` says whether the provider embeds a key there. A final
  check refuses to print a report that contains the key.
- **The report:** metadata only. It has no prices, sizes, IV or Greek values, and no raw payloads.

## 5. What is measured

| Report section | Content |
|---|---|
| `endpoint_results` | Per family: HTTP status, and one of `entitled`, `not_entitled` (401/403), `not_found`, `request_rejected` (400/422), `rate_limited`, `transport`, `payload`, `not_attempted_budget`, `truncated_page_budget` or `pagination_refused_foreign_host`; page and result counts; provider `status` |
| `field_presence_counts` | Per endpoint: every observed field path (e.g. `last_quote.bid`, `greeks.delta`, `details.shares_per_contract`) with present and null counts and value types. No values |
| `timing` | Check time (UTC and ET), session state, newest quote and trade age, `delay_classification`, and every timestamp-like field with its unit (ns/us/ms/s/iso/date), newest and oldest instant, and age |
| `timestamp_capabilities` | For Greeks and IV: `own_timestamp`, `parent_timestamp` (item level) or **`time_basis_unverified`**; timestamp paths by group |
| `open_interest_capability` | Presence, and any date or time beside it. Otherwise `uncertain_no_date_provided` |
| `pagination` | Pages and results fetched; whether the chain ended within the budget; `next_url` host, scheme and parameter names; whether following pages worked with header authorization |
| `rate_limit_metadata` | Rate-limit headers seen, HTTP status counts, requests made against the budget, pacing time |
| `historical_capability` / `historical_results` | As-of contract reference, historical quotes and historical contract aggregates: status and result counts |
| `adjusted_contract_observation` | Contract tickers seen; non-standard roots (examples); `shares_per_contract` values; adjustment-related field paths. `not_observed` means none appeared in the sample, **not** that they are unsupported |
| `warnings` | Every family that did not answer, an unverified IV/Greeks time basis, and an outside-session delay |

The endpoint families probed are the Polygon-family v3 paths listed in the module docstring. Results are
observed; entitlement is never assumed.

The `delay_classification` is a report heuristic, not a production threshold:
- in the regular session, a newest quote or trade age ≤ 120 s gives `appears_realtime`;
- ≤ 3,600 s gives `appears_delayed`;
- anything else gives `indeterminate_*`.

The stocks 900 s delay is **not** assumed for options.

## 6. What remains unverified after this check

- **IV and Greeks cutoff safety** when the result is `time_basis_unverified`. A later phase must treat them as
  not provably cutoff-safe.
- **Open-interest date semantics** when no date is provided.
- **As-of chain snapshots.** No documented as-of parameter is exercised. Reconstruction is at best "possible"
  through as-of contracts plus historical quotes, if both return data.
- **Chain size** beyond the page budget. A truncated chain gives only a lower bound.
- **Adjusted contracts** if none appear in the sample.
- **Delay**, if the check ran outside the regular session.

## 7. Exit codes

| Code | Meaning |
|---|---|
| 0 | The chain snapshot answered (including when truncated by the page or request budget) |
| 1 | A provider failure, or no chain access (`live_validation: FAILED`) |
| 2 | Configuration or usage error. No request is made (`NOT EXECUTED`) |

## 8. Out of scope

- OptionsSnapshot or OptionsIntelligence;
- the production adapter or runner;
- persistence or migrations;
- alerts;
- Phase 9B–9E and Phase 10.

No Phase 6 setting changes on the basis of this check.
