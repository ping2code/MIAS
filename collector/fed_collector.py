"""Opt-in Federal Reserve monetary-policy collector."""

import argparse
import json
from datetime import datetime, timezone
from time import monotonic

import feedparser
import requests

from analyzer.ai_alert_quality import adjust_alert_quality
from analyzer import deduplicator
from analyzer.deduplicator import is_duplicate
from analyzer.fed_scoring import score_fed_event
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from collector.fed_normalizer import normalize_fed_entry
from shared.config import RSS_ENTRY_LIMIT, FED_MAX_AGE_HOURS, DEDUP_TTL_SECONDS, FED_PERSISTENCE_SHADOW_ENABLED
from shared.logger import get_logger


FED_FEED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
logger = get_logger("fed_collector")
_shadow_last_failure = float("-inf")


def analyze_fed_event(event):
    # Import/client construction failures are handled by the candidate pipeline.
    from analyzer.openai_analyzer import analyze_market_event
    return analyze_market_event(event)


def _shadow(event, *, make_current=True):
    """Opt-in historical copy of an already-computed result; never touches Redis or delivery."""
    global _shadow_last_failure
    if not FED_PERSISTENCE_SHADOW_ENABLED:
        return
    try:
        if isinstance(event, (str, bytes)):  # Cached processed JSON: parse only for the shadow copy.
            event = json.loads(event)
        from persistence.fed_shadow import submit_fed
        # The collector's own Redis fingerprint is the authoritative identity.
        submit_fed(dict(event, fed_fingerprint=deduplicator.create_fingerprint(event), fed_source_feed=FED_FEED_URL),
                   make_current=make_current)
    except Exception:
        # Even import/initialization/enqueue failures cannot change collector outcomes.
        now = monotonic()
        if now - _shadow_last_failure >= 60:
            _shadow_last_failure = now
            logger.warning("Fed shadow submission failed")


def deliver_fed_alert(message):
    from alert_engine.telegram_notifier import send_telegram_alert
    return send_telegram_alert(message)


def _state_key(event, kind):
    return f"mias:fed:{kind}:{deduplicator.create_fingerprint(event)}"


def _read_state(event, kind):
    try:
        return deduplicator.redis_client.get(_state_key(event, kind))
    except deduplicator.redis.RedisError as error:
        logger.error("Fed state unavailable (%s)", type(error).__name__)
        return None


def _write_state(event, kind, value):
    # Retain delivery/cached results beyond the entire eligibility window,
    # independently of the shorter processing deduplication TTL.
    ttl = max(DEDUP_TTL_SECONDS, FED_MAX_AGE_HOURS * 3600) + 1
    try:
        deduplicator.redis_client.set(_state_key(event, kind), value, ex=ttl)
    except deduplicator.redis.RedisError as error:
        logger.error("Fed state write failed (%s)", type(error).__name__)


def _deliver_if_pending(event):
    if event["alert_decision"] != "ALERT" or _read_state(event, "delivered"):
        return
    try:
        result = deliver_fed_alert(format_alert(event))
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram did not confirm delivery")
    except Exception as error:
        logger.error("Fed alert delivery failed (%s)", type(error).__name__)
        return
    # Failed delivery never writes a success marker.
    _write_state(event, "delivered", "1")


def collect_fed_events(*, enable_ai=True, send_alerts=False):
    stats = dict(fetched=0, relevant=0, duplicates=0, processed=0)
    events = []
    try:
        response = requests.get(FED_FEED_URL, timeout=15, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected Fed feed response")
        feed = feedparser.parse(response.content)
    except Exception as error:
        logger.error("Fed feed unavailable (%s)", type(error).__name__)
        return events, stats

    if feed.get("bozo"):
        logger.warning("Fed feed has a parsing warning")

    for entry in feed.get("entries", [])[:RSS_ENTRY_LIMIT]:
        stats["fetched"] += 1
        try:
            event = normalize_fed_entry(entry)
        except (ValueError, TypeError):
            logger.warning("Skipping malformed Fed entry")
            continue
        stats["relevant"] += 1
        published_at = event.get("published_at")
        if not published_at:
            logger.info("Skipping Fed event with unknown publication time: %s", event["url"])
            _shadow(event, make_current=False)
            continue
        age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(published_at)).total_seconds()
        if age_seconds > FED_MAX_AGE_HOURS * 3600:
            logger.info("Skipping stale Fed event older than %s hours: %s", FED_MAX_AGE_HOURS, event["url"])
            _shadow(event, make_current=False)
            continue

        cached = _read_state(event, "processed")
        if cached:
            stats["duplicates"] += 1
            if send_alerts:
                try:
                    _deliver_if_pending(json.loads(cached))
                except (ValueError, TypeError, KeyError):
                    logger.error("Invalid cached Fed event")
            _shadow(cached)
            continue
        if is_duplicate(event, namespace="fed:event"):
            stats["duplicates"] += 1
            continue

        event = evaluate_alert(score_fed_event(event))
        stats["processed"] += 1
        if enable_ai and event["alert_decision"] == "ALERT":
            try:
                # Keep the deterministic result intact if enrichment fails.
                candidate = dict(event, score_reasons=list(event["score_reasons"]))
                event = evaluate_alert(adjust_alert_quality(analyze_fed_event(candidate)))
            except Exception as error:
                logger.error("Fed analysis unavailable (%s)", type(error).__name__)

        _write_state(event, "processed", json.dumps(event))
        if send_alerts:
            _deliver_if_pending(event)
        _shadow(event)
        events.append(event)

    return events, stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--send-alerts", action="store_true")
    args = parser.parse_args()
    events, stats = collect_fed_events(
        enable_ai=not args.no_ai, send_alerts=args.send_alerts,
    )
    print(stats)
    for event in events:
        print(format_alert(event))
