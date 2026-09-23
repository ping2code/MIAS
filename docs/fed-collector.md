# Federal Reserve collector

The collector reads only the official Board monetary-policy RSS feed:
https://www.federalreserve.gov/feeds/press_monetary.xml

Requests have a 15-second timeout and do not follow redirects. Entries must
have a headline and an HTTPS link on `www.federalreserve.gov`. It does not
scrape linked articles. AI analysis receives only the normalized headline,
summary, and actual company mentions; sparse RSS summaries limit the analysis.

Run from the repository root:

```sh
python -m collector.fed_collector --no-ai
python -m collector.fed_collector
python -m collector.fed_collector --send-alerts
```

The first command disables AI. The second analyzes alert candidates but does
not send Telegram messages. The third enables Telegram delivery. Non-delivery
runs still cache processing results, but do not consume delivery eligibility.
A subsequent delivery-enabled poll can send the cached result without repeating
scoring or AI analysis (including when the original run disabled AI).

`FED_MAX_AGE_HOURS` defaults to `48` in `shared/config.py` and can be supplied
through the environment or existing dotenv configuration. It must be a positive
integer. No `.env` file changes are needed to use the default. Entries older
than the cutoff are logged and skipped before Redis, scoring, or delivery;
entries exactly at the cutoff are eligible. Missing or invalid publication
times are skipped because freshness cannot be established.

For integration, call
`collect_all_sources(include_fed=True, fed_enable_ai=True, fed_send_alerts=False)`.
Existing calls without these options retain the news-only behavior. The Fed
delivery option controls only Fed alerts; existing RSS behavior is unchanged.

Events preserve the MIAS dictionary fields and add `event_type="fed_policy"`,
`fed_category`, and `market_scope="US macro"`. Monetary-policy entries are
relevant without ticker mentions. Company lists contain only actual matches.
Scores are 85 for statements/actions, 80 for economic projections, 70 for
minutes, and 40 for other policy communications. These are deterministic
category heuristics, not estimates of market direction or surprise.

The existing decision thresholds, AI analysis, quality adjustment, formatting,
and Telegram delivery are reused. Empty ticker lists display `N/A`, and events
with `market_scope` display a Market Scope line. OpenAI client construction is deferred until
analysis is requested. AI failures retain the original scored event; Telegram
failures are logged by exception type without credential-bearing details.
The SDK is declared in `requirements.txt`; existing configuration is reused.
No `.env` changes are required by this implementation.

Redis separates processing and delivery, using the same exact headline/URL
fingerprint for all three keys:

- `mias:fed:event:<fingerprint>` retains the existing atomic `SET NX`
  processing claim and configured deduplication TTL.
- `mias:fed:processed:<fingerprint>` caches the final scored/analyzed event.
  Repeated polls count it as a processing duplicate but may retry delivery.
- `mias:fed:delivered:<fingerprint>` is written only after Telegram returns
  `ok=true`. Non-delivery runs and failed sends do not create this marker.

Cached results and delivery markers live for the greater of the processing
deduplication TTL and freshness window, plus one second. This prevents the
shorter processing TTL from causing repeat delivery while an event is fresh.
Retries occur when a delivery-enabled poll sees the entry again and stop once
it becomes stale. News fuzzy matching is not applied.

Redis outages still allow processing to continue. Delivery is not exactly-once:
concurrent senders, an ambiguous Telegram timeout, or a successful send followed
by a Redis write failure can cause duplicate delivery. A missing cached result
with an existing processing claim is skipped until that claim expires.

Run the isolated tests (no live services or `.env` reads):

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_fed_pipeline -v
```

Existing smoke scripts can invoke live OpenAI and Telegram APIs at import time;
do not use unrestricted test discovery for offline verification. The isolated
suite exercises those scripts with mocked dependencies.

Optional shadow persistence: `FED_PERSISTENCE_SHADOW_ENABLED` defaults to
`false`. When `true`, already-computed results (and unscored stale/undated skips)
are copied to a bounded background PostgreSQL writer after the Redis and
delivery steps. Redis processed/delivered/claim state, delivery retry,
Telegram and OpenAI behavior are unchanged. See
[Persistence Phase 2L](persistence-phase2l.md).
