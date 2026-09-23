"""Opt-in ingestion of six official US macroeconomic releases."""

import argparse
import copy
import json
from datetime import datetime, timezone
from uuid import uuid4
from time import monotonic

import requests

from analyzer import deduplicator
from analyzer.macro_scoring import score_macro_event
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from collector.macro_normalizer import (
    MACRO_SOURCES, discover_bea_release, normalize_macro_release, official_url,
)
from collector.bls_source import BLSDataError, fetch_bls_feed, normalize_bls_feed, enrich_bls_event
from shared.config import MACRO_MAX_AGE_HOURS, DEDUP_TTL_SECONDS, MACRO_PERSISTENCE_SHADOW_ENABLED
from shared.logger import get_logger


logger = get_logger("macro_collector")
LEASE_SECONDS = 900
BLS_RETRY_SECONDS = 4 * 3600  # v1 allows only 25 queries/day; avoid polling API lag.
MAX_DOCUMENT_BYTES = 2_000_000
_shadow_last_failure = float("-inf")
RELEASE_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
CACHE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


def analyze_macro_event(event):
    from analyzer.openai_analyzer import analyze_market_event
    return analyze_market_event(event)


def _shadow(event, *, make_current=True):
    global _shadow_last_failure
    if not MACRO_PERSISTENCE_SHADOW_ENABLED:
        return
    try:
        from persistence.macro_shadow import submit_macro
        submit_macro(event, make_current=make_current)
    except Exception:
        # Even import/initialization/enqueue failures cannot change collector outcomes.
        now = monotonic()
        if now - _shadow_last_failure >= 60:
            _shadow_last_failure = now
            logger.warning("Macro shadow submission failed")


def deliver_macro_alert(message):
    from alert_engine.telegram_notifier import send_telegram_alert
    return send_telegram_alert(message)


def fetch_document(url, agency):
    """No cross-host redirects, API credentials, or linked third-party content."""
    if agency == "bls":
        raise ValueError("BLS ingestion requires official RSS and Public Data API")
    url = official_url(url, agency)
    with requests.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                      headers={"User-Agent": "MIAS official macro release collector"}) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected source status")
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("Expected official HTML release")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise ValueError("Official release exceeds size limit")
        return content.decode("utf-8-sig")


def state_key(event, kind):
    return f"mias:macro:{kind}:{event['event_id']}"


def _ttl():
    return max(DEDUP_TTL_SECONDS, MACRO_MAX_AGE_HOURS * 3600) + 1


def _acquire(key):
    token = uuid4().hex
    if deduplicator.redis_client.set(key, token, nx=True, ex=LEASE_SECONDS):
        return token
    return None


def _release(key, token):
    # A timed-out worker cannot delete a newer worker's lease.
    deduplicator.redis_client.eval(RELEASE_LEASE, 1, key, token)


def _freshness(event, now):
    published = event.get("published_at")
    if not published:
        return "missing_date"
    age = (now - datetime.fromisoformat(published)).total_seconds()
    if age < 0:
        return "future"
    if age > MACRO_MAX_AGE_HOURS * 3600:
        return "stale"
    return None


def _analyze_candidate(event, enable_ai, stats):
    event = evaluate_alert(score_macro_event(event))
    if enable_ai and event["alert_decision"] == "ALERT":
        try:
            analyzed = analyze_macro_event(copy.deepcopy(event))
            # Only validated enrichment fields are copied. Official statistics
            # never receive the news opinion/prediction quality penalties.
            sentiment = analyzed["ai_sentiment"]
            confidence = analyzed["ai_confidence"]
            if sentiment not in {"STRONGLY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONGLY_BEARISH"}:
                raise ValueError("Invalid AI sentiment")
            if type(confidence) is not int or not 0 <= confidence <= 100:
                raise ValueError("Invalid AI confidence")
            fields = ("ai_summary", "ai_sentiment", "ai_why_it_matters", "ai_event_type")
            if any(not isinstance(analyzed[k], str) or not analyzed[k].strip() for k in fields):
                raise ValueError("Incomplete AI enrichment")
            event.update({k: analyzed[k] for k in (*fields, "ai_confidence")})
        except Exception as error:
            stats["analysis_errors"] += 1
            logger.error("Macro analysis failed (%s)", type(error).__name__)
    return evaluate_alert(event)


def _deliver(event, stats):
    if event["alert_decision"] != "ALERT":
        return
    redis = deduplicator.redis_client
    delivered = state_key(event, "delivered")
    lease = state_key(event, "event") + ":delivery"
    token = _acquire(lease)
    if not token:
        return
    try:
        # Recheck eligibility after analysis and lease acquisition.
        if _freshness(event, datetime.now(timezone.utc)) or redis.get(delivered):
            return
        try:
            result = deliver_macro_alert(format_alert(event))
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Telegram did not confirm delivery")
        except Exception as error:
            stats["delivery_errors"] += 1
            logger.error("Macro delivery failed (%s)", type(error).__name__)
            return
        redis.set(delivered, "1", ex=_ttl())
        stats["delivered"] += 1
    finally:
        _release(lease, token)


def _process(event, enable_ai, send_alerts, stats):
    redis = deduplicator.redis_client
    processed_key = state_key(event, "processed")
    cached = redis.get(processed_key)
    if cached:
        cached_event = json.loads(cached)
        if not isinstance(cached_event, dict) or cached_event.get("event_id") != event["event_id"]:
            raise ValueError("Invalid cached macro identity")
        stats["duplicates"] += 1
        if send_alerts:
            _deliver(cached_event, stats)
        _shadow(cached_event)
        return None

    lease = state_key(event, "event")
    token = _acquire(lease)
    if not token:
        stats["duplicates"] += 1
        return None
    try:
        # Another worker may have populated the cache before this lease.
        if redis.get(processed_key):
            stats["duplicates"] += 1
            return None
        if event["agency"] == "bls":
            # Fetch only after freshness/state checks, once per processing lease.
            # An API lag/failure releases the lease without caching or delivery.
            retry_key = state_key(event, "event") + ":api-retry"
            if redis.get(retry_key):
                raise BLSDataError("BLS API retry cooling down")
            try:
                event = enrich_bls_event(event)
            except BLSDataError:
                redis.set(retry_key, "1", ex=BLS_RETRY_SECONDS)
                raise
        event = _analyze_candidate(event, enable_ai, stats)
        if not redis.eval(CACHE_IF_OWNER, 2, lease, processed_key,
                          token, json.dumps(event), _ttl()):
            raise RuntimeError("Macro processing lease expired")
        stats["processed"] += 1
    finally:
        _release(lease, token)
    if send_alerts:
        _deliver(event, stats)
    _shadow(event)
    return event


def collect_macro_events(*, enable_ai=True, send_alerts=False):
    stats = dict(fetched=0, relevant=0, duplicates=0, processed=0,
                 stale=0, future=0, missing_date=0, invalid=0, fetch_errors=0,
                 state_errors=0, analysis_errors=0, delivered=0, delivery_errors=0, data_errors=0)
    events = []
    for source in MACRO_SOURCES:
        try:
            url = source["url"]
            html = (fetch_bls_feed(source) if source["agency"] == "bls"
                    else fetch_document(url, source["agency"]))
            if source["agency"] == "bea":
                url = discover_bea_release(html, source)
                html = fetch_document(url, source["agency"])
            stats["fetched"] += 1
        except Exception as error:
            stats["fetch_errors"] += 1
            logger.error("Macro fetch failed category=%s (%s)", source["category"], type(error).__name__)
            continue
        try:
            event = (normalize_bls_feed(html, source) if source["agency"] == "bls"
                     else normalize_macro_release(html, source, url))
            stats["relevant"] += 1
        except (ValueError, KeyError, TypeError) as error:
            stats["invalid"] += 1
            logger.warning("Invalid macro release category=%s (%s)", source["category"], type(error).__name__)
            continue
        reason = _freshness(event, datetime.now(timezone.utc))
        if reason:
            stats[reason] += 1
            logger.info("Skipping %s macro event category=%s", reason, source["category"])
            _shadow(event, make_current=False)
            continue
        try:
            result = _process(event, enable_ai, send_alerts, stats)
            if result:
                events.append(result)
        except BLSDataError:
            stats["data_errors"] += 1
            logger.warning("BLS data unavailable category=%s; release remains retryable", source["category"])
        except (deduplicator.redis.RedisError, ValueError, KeyError, TypeError, RuntimeError) as error:
            # New macro path fails closed on unreliable state. Existing
            # collectors retain their existing Redis failure semantics.
            stats["state_errors"] += 1
            logger.error("Macro state unavailable category=%s (%s)", source["category"], type(error).__name__)
    return events, stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--send-alerts", action="store_true")
    args = parser.parse_args()
    events, stats = collect_macro_events(enable_ai=not args.no_ai, send_alerts=args.send_alerts)
    print(json.dumps(stats, indent=2))
    for event in events:
        print(format_alert(event))
    if stats["fetch_errors"] or stats["invalid"] or stats["state_errors"] or stats["delivery_errors"] or stats["data_errors"]:
        raise SystemExit(1)
